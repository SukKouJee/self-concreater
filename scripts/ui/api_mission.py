"""미션 API: 오늘 상태 조회, 미션 뽑기(작업) → 미리보기, 저장, 제출 피드백(작업) → 미리보기, 제출, 건너뛰기.

작업은 둘이고 둘 다 **미리보기에서 사용자 확인을 기다린 뒤 작업은 `done`으로 끝낸다**(저장은 별도 요청).
- `mission`(뽑기·바꾸기): 항목 id = 날짜. `context` → `claude` → `awaiting/preview/ask="save"`.
  저장은 `POST /api/mission/save`. 이미 그날 미션이 있으면 `replace`가 있어야만 덮어쓴다(미리보기 사이 다른 곳이
  먼저 저장했으면 `mission_exists`).
- `mission_feedback`(제출 피드백): 항목 id = 날짜. 파일이 있으면 `copy`(작업 폴더 임시 복사) → `claude`(Read 허용) →
  `awaiting/preview`. 임시 복사본은 작업이 끝나면(성공·실패·취소 모두) finally로 지운다. 사용자 원본은 읽기만 한다.
  기록은 `POST /api/mission/submit`이 쓴다.
- 날짜는 화면이 본 날짜를 요청마다 보낸다(자정을 넘겨도 다른 날 미션에 적용되지 않게). 기록 쓰기는 모듈 lock으로 직렬화한다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import threading
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts import claude_cli, llm, mission, refstore
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.api_refs import _body_parts
from scripts.ui.jobs import Job, mark_saved, new_item
from scripts.ui.server import Request, route

KIND = "mission"
FEEDBACK_KIND = "mission_feedback"
MISSION_TASK = "yt-mission 규칙 1~4절대로 입력에 맞는 오늘의 미션 JSON 하나를 만드세요."
SWAP_TASK_SUFFIX = " 오늘 미션과 다른 재료·종류로 만드세요."
FEEDBACK_TASK = ("yt-mission 규칙 5절대로 제출물에 2~4문장 피드백(잘된 점 1개 → 다음에 바꿀 점 1개)을 쓰세요. "
                 "파일이 있으면 Read로 보세요. 난이도는 입력 difficulty로 이미 받았으니 묻거나 다시 적지 마세요.")
MISSION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "kind": {"type": "string", "enum": mission.KINDS},
        "minutes": {"type": "integer"},
        "source": {"type": "string"},
        "why": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "deliverable": {"type": "string"},
    },
    "required": ["title", "kind", "minutes", "steps"],
}
FEEDBACK_SCHEMA: dict = {"type": "object", "properties": {"feedback": {"type": "string"}}, "required": ["feedback"]}
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HISTORY_DAYS = 14
MAX_NOTE = 300
STALE_COPY_SECONDS = 3600
MAX_FEEDBACK = 4000
MAX_REASON = 100
MAX_FILE_NAME = 255
MAX_FILE_BYTES = 10 * 1024 * 1024
FILE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".md"})
HISTORY_FIELDS = ("title", "kind", "status", "difficulty", "skip_reason", "minutes")
WRITE_LOCK = threading.Lock()                     # missions/*.md의 읽고-고쳐-쓰기를 한 번에 하나씩


def _rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-mission" / "SKILL.md"]


# ---- 입력 검증 --------------------------------------------------------------------------

def _server_today() -> Date:
    return Date.fromisoformat(mission._today(None))


def _day(value: Any, *, required: bool = True) -> str:
    """서버 로컬 오늘 ±1일만 받는다(자정을 넘긴 화면은 허용, 임의의 날짜에 파일을 만들지 못하게)."""
    if value is None and not required:
        return _server_today().isoformat()
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ScriptError("bad_request", "날짜는 YYYY-MM-DD 형식이어야 합니다")
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as e:
        raise ScriptError("bad_request", "없는 날짜입니다") from e
    if abs((parsed - _server_today()).days) > 1:
        raise ScriptError("bad_request", "오늘 날짜의 미션만 다룰 수 있습니다. 화면을 새로고침해 주세요")
    return value


def _refuse_replacing_completed(day: str) -> None:
    path = mission._mission_path(day)
    if path.exists() and refstore.read_md(path)[0].get("status") == "submitted":
        raise ScriptError("already_submitted", "이미 완료한 미션은 바꿀 수 없습니다")


def _flag(value: Any, name: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ScriptError("bad_request", f"{name}는 true/false여야 합니다")
    return value


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScriptError("bad_request", f"{name}을(를) 입력해 주세요")
    text = value.strip()
    if len(text) > limit:
        raise ScriptError("bad_request", f"{name}은(는) {limit}자까지 쓸 수 있습니다")
    return text


def _difficulty(value: Any) -> str:
    if value not in mission.DIFFICULTIES:
        raise ScriptError("bad_request", f"난이도는 {', '.join(mission.DIFFICULTIES)} 중 하나입니다")
    return value


def _bad_file(message: str) -> ScriptError:
    return ScriptError("bad_file", message)


def _user_file(value: Any) -> tuple[Path, str] | None:
    """제출 파일 경로를 검사해 (실제 경로, 사용자가 적은 파일 이름)을 돌려준다.

    없음/폴더/허용 밖 확장자/10MB 초과는 bad_file. 빈 값이면 파일 없음. 확장자·크기는 심볼릭 링크를 푼 실제 대상으로 본다."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad_file("파일 경로가 올바르지 않습니다")
    text = value.strip().strip("\"'").strip()
    if not text:
        return None
    try:
        given = Path(text).expanduser()
        if not given.is_absolute():
            given = work_root() / given
        path = given.resolve(strict=True)
        info = path.stat()
        is_file = path.is_file()
    except (OSError, ValueError) as e:
        raise _bad_file(f"파일을 찾을 수 없습니다: {text}") from e
    if not is_file:
        raise _bad_file("폴더가 아니라 파일 경로를 적어 주세요")
    if path.suffix.lower() not in FILE_EXTENSIONS:
        raise _bad_file("이미지(png·jpg·jpeg·webp·gif)나 텍스트(txt·md) 파일만 낼 수 있습니다. 영상은 설명으로 적어 주세요")
    if info.st_size > MAX_FILE_BYTES:
        raise _bad_file("파일이 너무 큽니다(10MB까지)")
    return path, given.name


def _require(day: str) -> Path:
    path = mission._mission_path(day)
    if not path.exists():
        raise ScriptError("no_mission", f"{day} 미션이 없습니다. 먼저 오늘 미션을 받아 주세요")
    return path


# ---- 읽기 -------------------------------------------------------------------------------

def _today_view(day: str) -> dict | None:
    path = mission._mission_path(day)
    if not path.exists():
        return None
    record = mission._mission_record(path)
    return {**record, "body": _body_parts(refstore.read_md(path)[1])}


def _entry(day: str) -> dict:
    """그날의 한 줄 기록. 미션이 없거나 파일이 깨져 있으면 `{date, status: None}`."""
    path = mission._mission_path(day)
    if not path.exists():
        return {"date": day, "status": None}
    try:
        record = mission._mission_record(path)
    except (ScriptError, OSError, UnicodeDecodeError):
        return {"date": day, "status": None}               # 사용자가 고치는 중일 수 있다
    return {"date": day, **{k: record[k] for k in HISTORY_FIELDS}}


def _week(today: Date) -> dict:
    monday = today - timedelta(days=today.weekday())
    entries = [_entry((monday + timedelta(days=i)).isoformat()) for i in range((today - monday).days + 1)]
    statuses = [e["status"] for e in entries if e["status"] is not None]
    done, skipped, total = statuses.count("submitted"), statuses.count("skipped"), len(statuses)
    return {"done": done, "skipped": skipped, "total": total, "rate": round(done / total, 2) if total else 0.0}


@route("GET", "/api/mission")
def _status(req: Request) -> dict:
    day = _day(req.query.get("date"), required=False)
    today = Date.fromisoformat(day)
    ctx = mission.context(day)
    history = [_entry((today - timedelta(days=n)).isoformat()) for n in range(HISTORY_DAYS, 0, -1)]
    return {
        "date": day, "today": _today_view(day), "recommended": ctx["recommended"], "stage": ctx["stage"],
        "has_profile": (work_root() / "profile.md").exists(), "history": history, "week": _week(today),
    }


# ---- 미션 뽑기(작업) --------------------------------------------------------------------

def run_draft(day: str, replace: bool) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="context")
        job.check_cancelled()
        payload = mission.context(day)
        current = payload["today"] if replace else None
        task = MISSION_TASK
        if current:
            payload["swap_from"] = {"title": current["title"], "source": current["source"], "kind": current["kind"]}
            task += SWAP_TASK_SUFFIX
        job.update(0, step="claude")
        job.check_cancelled()
        _, result = llm.ask_validated(
            _rules(), payload, task, mission.validate_mission, schema=MISSION_SCHEMA,
        )
        job.update(0, state="awaiting", step="preview", ask="save", preview={
            "mission": result, "recommended": payload["recommended"], "date": day, "replace": replace,
        })
        job.finish("done", {"title": result["title"]})

    return target


@route("POST", "/api/mission/draft")
def _draft(req: Request) -> dict:
    day = _day(req.json.get("date"), required=False)
    replace = _flag(req.json.get("replace"), "replace")
    llm.check()                          # claude가 없으면 작업을 만들지 않는다
    if replace:
        _refuse_replacing_completed(day)
    elif mission._mission_path(day).exists():
        raise ScriptError("mission_exists", f"{day} 미션이 이미 있습니다. 저장된 미션을 다시 불러와 주세요")
    items = [new_item(day, title=day, step="context")]
    job = req.app.jobs.start(KIND, run_draft(day, replace), items=items)
    return {"job": job.snapshot()}


# ---- 저장 -------------------------------------------------------------------------------

@route("POST", "/api/mission/save")
def _save(req: Request) -> dict:
    body = req.json
    day = _day(body.get("date"))
    replace = _flag(body.get("replace"), "replace")
    data = mission.validate_mission(body.get("mission"))
    tmp_dir = workspace_dir("tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with WRITE_LOCK:
        if replace:
            _refuse_replacing_completed(day)
        fd, name = tempfile.mkstemp(prefix="mission_", suffix=".json", dir=tmp_dir)   # 요청마다 고유한 파일
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            saved = mission.save(tmp, day, replace)
        finally:
            tmp.unlink(missing_ok=True)
        mark_saved(req.app, body.get("job_id"), KIND, day)
        return {**saved, "today": _today_view(day)}


# ---- 제출 피드백(작업) ------------------------------------------------------------------

def _remove_stale_copies() -> None:
    """서버가 도중에 죽어 남은 임시 복사본(1시간 넘은 mission_submit_*)을 지운다."""
    tmp_dir = workspace_dir("tmp")
    if not tmp_dir.is_dir():
        return
    cutoff = time.time() - STALE_COPY_SECONDS
    for path in tmp_dir.glob("mission_submit_*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue                                 # 다른 프로그램이 잡고 있으면 다음 기회에


def _copy_temp(src: Path) -> Path:
    """사용자 파일을 작업 폴더 임시 위치로 복사한다(원본은 읽기만). 실패하면 복사본을 지우고 bad_file."""
    tmp_dir = workspace_dir("tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="mission_submit_", suffix=src.suffix.lower(), dir=tmp_dir)
    os.close(fd)
    tmp = Path(name)
    try:
        shutil.copyfile(src, tmp)
        too_big = tmp.stat().st_size > MAX_FILE_BYTES
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise _bad_file(f"파일을 읽을 수 없습니다: {e}") from e
    if too_big:
        tmp.unlink(missing_ok=True)
        raise _bad_file("파일이 너무 큽니다(10MB까지)")
    return tmp


def _validate_feedback(data: Any) -> str:
    text = data.get("feedback") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ScriptError("invalid_feedback", "feedback이 비어 있습니다")
    return text.strip()


def run_feedback(day: str, note: str, difficulty: str, picked: tuple[Path, str] | None) -> Any:
    def target(job: Job) -> None:
        tmp: Path | None = None
        try:
            job.update(0, state="running", step="copy" if picked else "claude")
            job.check_cancelled()
            current = _today_view(day)
            if current is None:
                raise ScriptError("no_mission", f"{day} 미션이 없습니다")
            payload: dict[str, Any] = {"mission": current, "note": note, "difficulty": difficulty}
            if picked is not None:
                tmp = _copy_temp(picked[0])
                payload["file"] = tmp.relative_to(work_root()).as_posix()
                job.update(0, step="claude")
                job.check_cancelled()
            _, feedback = llm.ask_validated(
                _rules(), payload, FEEDBACK_TASK, _validate_feedback,
                schema=FEEDBACK_SCHEMA, files=[tmp] if tmp else None,
            )
            job.update(0, state="awaiting", step="preview", ask="save", preview={
                "feedback": feedback, "note": note, "difficulty": difficulty, "file_name": picked[1] if picked else None,
            })
            job.finish("done", {"date": day})
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    return target


@route("POST", "/api/mission/feedback")
def _feedback(req: Request) -> dict:
    body = req.json
    day = _day(body.get("date"))
    note = _text(body.get("note"), "한 일", MAX_NOTE)
    difficulty = _difficulty(body.get("difficulty"))
    _require(day)
    picked = _user_file(body.get("file"))                # 작업을 만들기 전에 거른다(임시 복사도 아직 없다)
    llm.check()                          # claude가 없으면 작업을 만들지 않는다
    _remove_stale_copies()
    items = [new_item(day, title=day, step="copy" if picked else "claude")]
    job = req.app.jobs.start(FEEDBACK_KIND, run_feedback(day, note, difficulty, picked), items=items)
    return {"job": job.snapshot()}


# ---- 제출 · 건너뛰기 --------------------------------------------------------------------

@route("POST", "/api/mission/submit")
def _submit(req: Request) -> dict:
    body = req.json
    day = _day(body.get("date"))
    note = _text(body.get("note"), "한 일", MAX_NOTE)
    difficulty = _difficulty(body.get("difficulty"))
    feedback = _text(body.get("feedback"), "피드백", MAX_FEEDBACK)
    file_name = body.get("file_name")
    if file_name is not None and (not isinstance(file_name, str) or len(file_name) > MAX_FILE_NAME):
        raise ScriptError("bad_request", "file_name이 올바르지 않습니다")
    with WRITE_LOCK:
        _require(day)
        result = mission.submit(file_name or "", difficulty, feedback, day, note)   # 완료·건너뜀 뒤에도 다시 제출할 수 있다
        mark_saved(req.app, body.get("job_id"), FEEDBACK_KIND, day)
        return {**result, "today": _today_view(day)}


@route("POST", "/api/mission/skip")
def _skip(req: Request) -> dict:
    body = req.json
    day = _day(body.get("date"))
    reason = " ".join(_text(body.get("reason"), "사유", MAX_REASON).split())
    with WRITE_LOCK:
        path = _require(day)
        if refstore.read_md(path)[0].get("status") == "submitted":
            raise ScriptError("already_submitted", "이미 완료한 미션은 건너뛸 수 없습니다")
        result = mission.skip(reason, day)
        return {**result, "today": _today_view(day)}
