"""리포트·주간 회고 API.

- 리포트: 목록 조회, 만들기(Claude 없음, 동기). 파일은 `reports/<카테고리>-YYYY-MM-DD.html`만 목록에 올린다.
- 회고: 주 목록, 한 주 조회(숫자·저장된 회고·프로필), 회고 쓰기(작업, kind `review`) → 미리보기에서 확인,
  저장(`reviews/<주>.md`, `## 내 메모`는 옮겨 붙임), 프로필 반영(체크한 섹션만).
- 회고 쓰기 작업은 미리보기에서 사용자 확인을 기다린 뒤 `done`으로 끝난다. 저장·반영은 별도 요청이다.
- 기록 쓰기는 모듈 lock으로 직렬화한다.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import date as Date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts import claude_cli, mission, motif, refstore, report
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.api_mission import _mark_saved
from scripts.ui.api_refs import _body_parts
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "review"
REVIEW_TASK = ("yt-mission 규칙 6절대로 입력 주의 회고 JSON(summary, repeated_weakness, next_week, profile_proposal)을 "
               "만드세요. 근거가 약하면 profile_proposal은 비워 두세요.")
REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "repeated_weakness": {"type": "array", "items": {"type": "string"}},
        "next_week": {"type": "array", "items": {"type": "string"}},
        "profile_proposal": {
            "type": "object",
            "properties": {name: {"type": "string"} for name in mission.PROFILE_SECTIONS},
        },
    },
    "required": ["summary"],
}
WEEK_RE = re.compile(r"[0-9]{4}-W[0-9]{2}")
REPORT_DATE = r"([0-9]{4}-[0-9]{2}-[0-9]{2})"
MAX_SECTION_TEXT = 4000
WRITE_LOCK = threading.Lock()                     # reviews/*.md, profile.md의 읽고-고쳐-쓰기를 한 번에 하나씩


# ---- 입력 검증 --------------------------------------------------------------------------

def _category(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ScriptError("bad_request", "category가 필요합니다")
    refstore.existing_category_dir(value)             # 없는 카테고리는 category_not_found
    return value


def _week(value: Any, *, required: bool = True) -> str:
    """`YYYY-Www`이고 달력에 실제로 있는 주만 받는다. 없으면(required=False) 이번 주."""
    if value is None and not required:
        return mission._week_range(None)[0]
    if not isinstance(value, str) or not WEEK_RE.fullmatch(value):
        raise ScriptError("bad_request", "주는 YYYY-Www 형식이어야 합니다")
    try:
        Date.fromisocalendar(int(value[:4]), int(value[6:]), 1)
    except ValueError as e:
        raise ScriptError("bad_request", "없는 주입니다") from e
    return value


def _review_path(week: str) -> Path:
    return work_root() / "reviews" / f"{week}.md"


def _tmp_json(prefix: str, data: Any) -> Path:
    tmp_dir = workspace_dir("tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=tmp_dir)       # 요청마다 고유한 파일
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return Path(name)


# ---- 리포트 -----------------------------------------------------------------------------

def _file_url(rel: str) -> str:
    return "/files/" + quote(rel, safe="/")


@route("GET", "/api/report/list")
def _report_list(req: Request) -> dict:
    category = _category(req.query.get("category"))
    pattern = re.compile(re.escape(category) + "-" + REPORT_DATE + r"\.html")
    folder = work_root() / "reports"
    found = []
    for path in folder.glob("*.html") if folder.is_dir() else []:
        m = pattern.fullmatch(path.name)
        if m and path.is_file():
            found.append((m.group(1), f"reports/{path.name}"))
    reports = [{"path": rel, "date": day, "url": _file_url(rel)} for day, rel in sorted(found, reverse=True)]
    return {"category": category, "reports": reports, "has_typify": motif.typify_path(category).exists()}


@route("POST", "/api/report/build")
def _report_build(req: Request) -> dict:
    category = _category(req.json.get("category"))
    with WRITE_LOCK:
        built = report.build(category)
    return {**built, "url": _file_url(built["path"])}


# ---- 회고 읽기 --------------------------------------------------------------------------

def _saved_view(week: str) -> dict | None:
    path = _review_path(week)
    if not path.exists():
        return None
    fm, body = refstore.read_md(path)
    return {"fm": fm, "body": _body_parts(body)}


def _profile_view() -> dict:
    sections = motif._profile()
    if sections is None:
        return {"exists": False, "sections": {}}
    return {"exists": True, "sections": {k: v for k, v in sections.items() if k in mission.PROFILE_SECTIONS}}


@route("GET", "/api/review/weeks")
def _weeks(req: Request) -> dict:
    current = mission._week_range(None)[0]
    folder = work_root() / "reviews"
    saved = {p.stem for p in folder.glob("*.md")} if folder.is_dir() else set()
    names = {current}
    for stem in saved:
        try:
            names.add(_week(stem))
        except ScriptError:
            continue                                  # 주 이름이 아닌 파일은 목록에서 뺀다
    weeks = [{"week": w, "range": [d.isoformat() for d in mission._week_range(w)[1:]], "saved": w in saved}
             for w in sorted(names, reverse=True)]
    return {"current": current, "weeks": weeks}


@route("GET", "/api/review")
def _review(req: Request) -> dict:
    week = _week(req.query.get("week"), required=False)
    data = mission.week(week)
    gaps = data["diagnosis_gaps"]
    stats = {
        "missions": data["missions"], "difficulty": data["difficulty"], "skip_reasons": data["skip_reasons"],
        "kinds": data["kinds"], "cards_created": data["cards_created"],
        "diagnosis_gap_count": sum(len(g["gaps"]) for g in gaps), "diagnosis_gaps": gaps, "refs": data["refs"],
    }
    return {"week": week, "range": data["range"], "stats": stats, "saved": _saved_view(week), "profile": _profile_view()}


# ---- 회고 쓰기(작업) --------------------------------------------------------------------

def run_write(week: str) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="stats")
        job.check_cancelled()
        payload = mission.week(week)
        job.update(0, step="claude")
        job.check_cancelled()
        _, result = claude_cli.ask_validated(
            [claude_cli.SKILLS_DIR / "yt-mission" / "SKILL.md"], payload, REVIEW_TASK, mission.validate_review,
            schema=REVIEW_SCHEMA,
        )
        job.update(0, state="awaiting", step="preview", ask="save", preview={"review": result, "week": week})
        job.finish("done", {"week": week})

    return target


@route("POST", "/api/review/write")
def _write(req: Request) -> dict:
    week = _week(req.json.get("week"))
    claude_cli.find_claude()                          # claude가 없으면 작업을 만들지 않는다
    items = [new_item(week, title=week, step="stats")]
    job = req.app.jobs.start(KIND, run_write(week), items=items)
    return {"job": job.snapshot()}


# ---- 저장 · 프로필 반영 -----------------------------------------------------------------

@route("POST", "/api/review/save")
def _save(req: Request) -> dict:
    body = req.json
    week = _week(body.get("week"))
    review = mission.validate_review(body.get("review"))
    with WRITE_LOCK:
        tmp = _tmp_json("review_", review)
        try:
            saved = mission.review_save(week, tmp)
        finally:
            tmp.unlink(missing_ok=True)
        _mark_saved(req, body.get("job_id"), KIND, week)
        return {**saved, "saved": _saved_view(week)}


def _sections(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ScriptError("bad_request", "반영할 섹션을 골라 주세요")
    out: dict[str, str] = {}
    for name, text in value.items():
        if not isinstance(text, str):
            raise ScriptError("bad_request", f"{name} 값은 문자열이어야 합니다")
        if len(text) > MAX_SECTION_TEXT:
            raise ScriptError("bad_request", f"{name}은(는) {MAX_SECTION_TEXT}자까지 쓸 수 있습니다")
        out[name] = text
    return out


@route("POST", "/api/review/apply")
def _apply(req: Request) -> dict:
    body = req.json
    week = _week(body.get("week"))
    sections = _sections(body.get("sections"))
    with WRITE_LOCK:
        tmp = _tmp_json("profile_", sections)
        try:
            result = mission.profile_update(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        recorded = False
        path = _review_path(week)
        if path.exists():                             # 회고가 아직 저장 전이면 기록할 곳이 없다
            try:
                previous = refstore.read_md(path)[0].get("approved")
                done = list(previous) if isinstance(previous, list) else []
                approved = done + [name for name in result["sections"] if name not in done]
                refstore.update_frontmatter(path, {"approved": approved})
                recorded = True
            except ScriptError:
                pass                                  # 프로필은 이미 바뀌었다: 기록 실패는 recorded=false로 알린다
        return {"updated": result["updated"], "sections": result["sections"], "recorded": recorded,
                "profile": _profile_view()}
