"""기획안 API: 출처 목록, 저장된 기획안 목록·상세(읽기 전용), 만들기(작업) → 미리보기, 저장.

기획안 만들기 작업(kind "draft")의 생애
- 항목은 카테고리 하나(id = category, title = 출처 제목). 스크립트가 입력을 모으고(`context`), Claude가 기획안을
  만든 뒤(`claude`), 항목을 `state="awaiting", step="preview", ask="save"`로 두고 **작업은 `done`으로 끝낸다**.
- 저장은 작업과 별개인 `POST /api/draft/save`다. 화면이 고친 draft JSON을 그대로 보내고 `draft.save`가 쓴다.
- 미리보기 단계에서는 drafts/를 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import date
from pathlib import Path
from typing import Any

from scripts import claude_cli, conti, draft, library, llm, refstore, status
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.api_conti import CONTI_SUFFIX, FILE_JOB_KINDS, _rel, _safe_path, job_running_on
from scripts.ui.api_refs import _body_parts
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "draft"
MAX_TOPIC = 100
MIN_TARGET_SEC, MAX_TARGET_SEC = 5, 1800
DRAFT_TASK = "yt-draft 규칙대로 입력 출처로 기획안 draft JSON을 만드세요. target_sec가 있으면 그 길이에 맞추세요."
DRAFT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "topic": {"type": "string"},
        "source": {"type": "string"},
        "archetype": {"type": "string"},
        "hook_type": {"type": "string"},
        "narrative_type": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "sub": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "script": {"type": "string"},
                    "shots": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "size": {"type": "string"},
                                "angle": {"type": "string"},
                                "sec": {"type": "number"},
                                "action": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "prep": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "hook_type", "narrative_type", "sections"],
}


def _rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-draft" / "SKILL.md"]


def _category(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ScriptError("bad_request", "category가 필요합니다")
    refstore.existing_category_dir(value)             # 없는 카테고리는 category_not_found
    return value


def _topic(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScriptError("bad_request", "topic은 문자열이어야 합니다")
    topic = " ".join(value.split())
    if len(topic) > MAX_TOPIC:
        raise ScriptError("bad_request", f"주제는 {MAX_TOPIC}자까지 쓸 수 있습니다")
    return topic or None


def _target_sec(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ScriptError("bad_request", "target_sec는 숫자여야 합니다")
    if not MIN_TARGET_SEC <= value <= MAX_TARGET_SEC:
        raise ScriptError("bad_request", f"목표 길이는 {MIN_TARGET_SEC}~{MAX_TARGET_SEC}초여야 합니다")
    return float(value)


def _conti_links(path: Path) -> tuple[str | None, str | None]:
    made = conti.conti_path(path)
    html_path = made.with_suffix(".html")
    return (_rel(made) if made.exists() else None), (_rel(html_path) if html_path.exists() else None)


# ---- 출처 -------------------------------------------------------------------------------

def _library_templates() -> list[dict] | None:
    """연결된 라이브러리의 지인 틀 묶음. 연결 전이거나 라이브러리 조회가 실패하면 None(묶음을 뺀다)."""
    try:
        if not library.status().get("connected"):
            return None
        found = library.search(kind="template")
    except Exception as e:  # noqa: BLE001 - 지인 파일이 무엇이든 출처 목록은 라이브러리 때문에 실패하지 않는다
        print(f"[draft] 라이브러리 틀 목록을 읽지 못했습니다: {type(e).__name__}", file=sys.stderr)
        return None
    keys = ("id", "title", "author", "category", "target_sec", "hook_type", "narrative_type", "worked")
    return [{**{k: t.get(k) for k in keys}, "source": f"{draft.LIBRARY_PREFIX}{t['id']}"} for t in found["items"]]


@route("GET", "/api/draft/sources")
def _sources(req: Request) -> dict:
    category = _category(req.query.get("category"))
    out = draft.sources(category)
    templates = _library_templates()
    if templates is not None:
        out["library_templates"] = {"label": "지인 틀", "items": templates}
    return out


# ---- 목록 · 상세(읽기 전용) --------------------------------------------------------------

def list_drafts(category: str) -> dict:
    """카테고리의 저장된 기획안 목록(경로는 작업 폴더 기준 상대 경로)."""
    out = []
    folder = work_root() / "drafts"
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        if path.stem.endswith(CONTI_SUFFIX):
            continue
        try:
            fm, _ = refstore.read_md(path)
        except (ScriptError, OSError, UnicodeDecodeError):
            continue                      # 프런트매터가 깨진 파일은 목록에서만 뺀다(사용자가 고치는 중일 수 있다)
        if fm.get("category") != category:
            continue
        made, html_path = _conti_links(path)
        out.append({
            "path": _rel(path), "title": fm.get("title") or path.stem, "date": str(fm.get("date") or path.stem[:10]),
            "source": fm.get("source"), "archetype": fm.get("archetype"), "target_sec": fm.get("target_sec"),
            "sections": fm.get("sections") or [], "status": fm.get("status"), "conti": made, "html": html_path,
        })
    out.sort(key=lambda d: (d["date"], d["path"]), reverse=True)
    return {"drafts": out}


@route("GET", "/api/draft/list")
def _list(req: Request) -> dict:
    return list_drafts(_category(req.query.get("category")))


def _hook_of(path: Path) -> dict | None:
    """Hook section (same shape as polish.context) so the polish screen can refresh its comparison box."""
    try:
        hook = next((x for x in conti.parse_draft(path)["sections"] if x["label"] == "hook"), None)
    except ScriptError:
        return None
    return {"start": hook["start"], "end": hook["end"], "script": hook["script"]} if hook else None


@route("GET", "/api/draft")
def _detail(req: Request) -> dict:
    path = _safe_path(req.query.get("path"), conti_file=False)
    if not path.is_file():
        raise ScriptError("draft_not_found", f"기획안을 찾을 수 없습니다: {req.query.get('path')}")
    fm, body = refstore.read_md(path)
    made, html_path = _conti_links(path)
    return {
        "hash": conti.content_hash(path), "hook": _hook_of(path),
        "path": _rel(path), "title": fm.get("title") or path.stem, "category": fm.get("category"),
        "date": str(fm.get("date") or path.stem[:10]), "source": fm.get("source"), "archetype": fm.get("archetype"),
        "target_sec": fm.get("target_sec"), "sections": fm.get("sections") or [], "pacing": fm.get("pacing"),
        "hook_type": fm.get("hook_type"), "narrative_type": fm.get("narrative_type"),
        "body": _body_parts(body), "conti": made, "html": html_path,
    }


# ---- 만들기(작업) -----------------------------------------------------------------------

def _validate_with_shots(data: Any) -> dict:
    """Every section needs shots in the UI flow, whatever script_only says."""
    return draft.validate({**data, "script_only": False} if isinstance(data, dict) else data)


def run_target(category: str, source: str, topic: str | None, target_sec: float | None) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="context")
        job.check_cancelled()
        payload = draft.context(category, source, topic)
        if target_sec is not None:
            payload["target_sec"] = target_sec
        job.update(0, step="claude")
        job.check_cancelled()
        server_template = (payload.get("source") or {}).get("template")      # 틀 표시값은 서버 소유: Claude에게 보이지도 맡기지도 않는다
        shown = {**payload, "source": {k: v for k, v in payload["source"].items() if k != "template"}}

        def validate(data: Any) -> dict:
            if isinstance(data, dict):
                data = {k: v for k, v in data.items() if k != "template"}      # Claude가 낸 template은 버린다
                if server_template:
                    data["template"] = server_template
            return _validate_with_shots(data)

        _, valid = llm.ask_validated(
            _rules(), payload=shown, task=DRAFT_TASK, schema=DRAFT_SCHEMA, validate=validate,
        )
        valid = {k: v for k, v in valid.items() if k != "script_only"}   # the UI never saves script-only drafts
        pacing = draft.planned_pacing(valid["sections"])
        comp = draft.compare(valid["sections"], draft.find_archetype(category, valid.get("archetype")))
        job.update(0, state="awaiting", step="preview", ask="save", preview={
            "draft": valid, "pacing": pacing, "compare": comp, "source": payload["source"],
        })
        job.finish("done", {"sections": len(valid["sections"]), "duration": valid["sections"][-1]["end"]})

    return target


@route("POST", "/api/draft/run")
def _run(req: Request) -> dict:
    body = req.json
    category = _category(body.get("category"))
    source = body.get("source")
    if not isinstance(source, str) or not source:
        raise ScriptError("bad_request", "source가 필요합니다")
    topic = _topic(body.get("topic"))
    target_sec = _target_sec(body.get("target_sec"))
    context = draft.context(category, source, topic)      # 작업 전에 출처를 확인한다(없으면 source_not_found)
    llm.check()                              # claude가 없으면 작업을 만들지 않는다
    items = [new_item(category, title=context["source"]["title"], step="context")]
    job = req.app.jobs.start(KIND, run_target(category, source, topic, target_sec), items=items, category=category)
    return {"job": job.snapshot()}


# ---- 저장 -------------------------------------------------------------------------------

def _job_template(req: Request, job_id: Any, category: str, source: Any) -> dict | None:
    """미리보기 작업이 들고 있는 지인 틀 표시값. 저장이 라이브러리에 기대지 않게 서버가 작업에서 복사한다."""
    if not isinstance(job_id, str) or not isinstance(source, str) or not source.startswith(draft.LIBRARY_PREFIX):
        return None
    try:
        job = req.app.jobs.get(job_id)
        preview = job.item(0).get("preview") or {}
    except ScriptError:
        return None
    src = preview.get("source") or {}
    if job.kind != KIND or job.category != category or src.get("id") != source:
        return None
    return src.get("template")


def _mark_saved(req: Request, job_id: Any, category: str) -> None:
    """미리보기 항목이 아직 저장 대기면 저장됨으로 바꾼다. 없는 작업·다른 종류는 무시."""
    if not isinstance(job_id, str):
        return
    try:
        job = req.app.jobs.get(job_id)
    except ScriptError:
        return
    if job.kind == KIND and job.category == category and job.item(0)["state"] == "awaiting":
        job.update(0, state="saved", ask=None)


@route("POST", "/api/draft/save")
def _save(req: Request) -> dict:
    body = req.json
    category = _category(body.get("category"))
    raw = body.get("draft")
    if isinstance(raw, dict):
        raw = {k: v for k, v in raw.items() if k != "template"}      # 클라이언트가 보낸 template은 믿지 않는다
        template = _job_template(req, body.get("job_id"), category, raw.get("source"))
        if isinstance(template, dict):
            raw["template"] = template                # 작업에 저장된 값만 되돌려 넣는다
    data = draft.validate(raw)
    tmp_dir = workspace_dir("tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="draft_ui_", suffix=".json", dir=tmp_dir)   # 요청마다 고유한 파일
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        saved = draft.save(category, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    _mark_saved(req, body.get("job_id"), category)
    return {"path": saved["path"], "pacing": saved["pacing"], "compare": saved["compare"]}


# ---- 촬영함 체크 ------------------------------------------------------------------------

_FILMED_LOCK = threading.Lock()      # read hash -> compare -> write -> new hash must not interleave


@route("POST", "/api/drafts/filmed")
def _filmed(req: Request) -> dict:
    body = req.json
    category = _category(body.get("category"))
    filmed = body.get("filmed")
    if not isinstance(filmed, bool):
        raise ScriptError("bad_request", "filmed는 true/false여야 합니다")
    expected = body.get("hash")
    if not isinstance(expected, str) or not expected:
        raise ScriptError("bad_request", "hash가 필요합니다")
    raw = body.get("path")
    if isinstance(raw, str) and (Path(raw).is_absolute() or "\\" in raw):
        raise ScriptError("bad_path", f"작업 폴더 기준 상대 경로만 쓸 수 있습니다: {raw}")
    path = _safe_path(raw, conti_file=False)
    if path.parent != (work_root() / "drafts").resolve():
        raise ScriptError("bad_path", "drafts 폴더 바로 아래의 기획안만 체크할 수 있습니다")
    with _FILMED_LOCK:
        if not path.is_file():
            raise ScriptError("draft_not_found", f"기획안을 찾을 수 없습니다: {body.get('path')}")
        fm, _ = refstore.read_md(path)
        if fm.get("category") != category:
            raise ScriptError("draft_not_found", f"기획안을 찾을 수 없습니다: {body.get('path')}")
        if conti.content_hash(path) != expected:
            raise ScriptError("draft_changed", "화면을 연 뒤 기획안이 바뀌었습니다. 다시 불러와 주세요")
        if filmed:
            fm = refstore.update_frontmatter(path, {"filmed_at": date.today().isoformat()})
        else:
            fm = refstore.update_frontmatter(path, {}, remove=["filmed_at"])
        return {"path": _rel(path), "filmed_at": status.filmed_date(fm.get("filmed_at")),
                "hash": conti.content_hash(path)}


# ---- 삭제(휴지통) ------------------------------------------------------------------------

@route("POST", "/api/draft/trash")
def _trash(req: Request) -> dict:
    path = _safe_path(req.json.get("path"), conti_file=False)
    rel = _rel(path)
    # 다듬기·콘티 만들기는 기획안을, 자막 작업은 그 콘티를 읽는다.
    conti_rel = _rel(conti.conti_path(path))
    if job_running_on(req.app, ("polish", "conti"), rel) or job_running_on(req.app, FILE_JOB_KINDS, conti_rel):
        raise ScriptError("draft_busy", "이 기획안으로 작업하는 중입니다(다듬기·콘티·자막). 끝나거나 멈춘 뒤에 삭제하세요")
    with _FILMED_LOCK:                   # 촬영함 체크가 같은 파일을 쓰는 중이면 기다린다
        return draft.trash(rel)
