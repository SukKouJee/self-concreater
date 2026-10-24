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
import tempfile
from pathlib import Path
from typing import Any

from scripts import claude_cli, conti, draft, motif, refstore
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.api_conti import CONTI_SUFFIX, _rel, _safe_path
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

@route("GET", "/api/draft/sources")
def _sources(req: Request) -> dict:
    category = _category(req.query.get("category"))
    details = motif.card_details(category)

    def brief(c: dict) -> dict:
        return {k: c[k] for k in ("id", "title", "type", "type_ko", "idea")}

    index_fm, _ = refstore.read_md(refstore.existing_category_dir(category) / "index.md")
    typify = motif._typify(category, index_fm)
    archetypes = [{"id": a["id"], "name": a.get("name"), "size": a.get("size"), "skeleton": a.get("skeleton"),
                   "mean_duration": a.get("mean_duration")} for a in (typify or {}).get("archetypes") or []]
    return {
        "stars": [brief(c) for c in details if c["starred"]],
        "cards": [brief(c) for c in details if not c["starred"]],
        "archetypes": archetypes,
        "has_typify": motif.typify_path(category).exists(),
    }


# ---- 목록 · 상세(읽기 전용) --------------------------------------------------------------

@route("GET", "/api/draft/list")
def _list(req: Request) -> dict:
    category = _category(req.query.get("category"))
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


@route("GET", "/api/draft")
def _detail(req: Request) -> dict:
    path = _safe_path(req.query.get("path"), conti_file=False)
    if not path.is_file():
        raise ScriptError("draft_not_found", f"기획안을 찾을 수 없습니다: {req.query.get('path')}")
    fm, body = refstore.read_md(path)
    made, html_path = _conti_links(path)
    return {
        "path": _rel(path), "title": fm.get("title") or path.stem, "category": fm.get("category"),
        "date": str(fm.get("date") or path.stem[:10]), "source": fm.get("source"), "archetype": fm.get("archetype"),
        "target_sec": fm.get("target_sec"), "sections": fm.get("sections") or [], "pacing": fm.get("pacing"),
        "hook_type": fm.get("hook_type"), "narrative_type": fm.get("narrative_type"),
        "body": _body_parts(body), "conti": made, "html": html_path,
    }


# ---- 만들기(작업) -----------------------------------------------------------------------

def run_target(category: str, source: str, topic: str | None, target_sec: float | None) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="context")
        job.check_cancelled()
        payload = draft.context(category, source, topic)
        if target_sec is not None:
            payload["target_sec"] = target_sec
        job.update(0, step="claude")
        job.check_cancelled()
        _, valid = claude_cli.ask_validated(
            _rules(), payload=payload, task=DRAFT_TASK, validate=draft.validate, schema=DRAFT_SCHEMA,
        )
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
    context = draft.context(category, source, topic)      # 없는 출처는 작업 전에 source_not_found
    claude_cli.find_claude()                              # claude가 없으면 작업을 만들지 않는다
    items = [new_item(category, title=context["source"]["title"], step="context")]
    job = req.app.jobs.start(KIND, run_target(category, source, topic, target_sec), items=items, category=category)
    return {"job": job.snapshot()}


# ---- 저장 -------------------------------------------------------------------------------

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
    data = draft.validate(body.get("draft"))
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
