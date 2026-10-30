"""모티브 API: 상태 조회, 카드 뽑기(작업) → 미리보기, 선택 저장, ★ 표시.

모티브 작업(kind "motif")의 생애
- 항목은 카테고리 하나(id = category). 스크립트가 입력을 모으고(`context`), Claude가 카드를 만든 뒤(`claude`),
  항목을 `state="awaiting", step="preview", ask="save"`로 두고 **작업은 `done`으로 끝낸다**.
- 저장은 작업과 별개인 `POST /api/motif/save`다. 화면이 고른 카드만 `motif.add`로 index.md에 쓴다.
- 미리보기 단계에서는 index.md를 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from scripts import claude_cli, llm, motif, refstore
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "motif"
MAX_TOPIC = 100
CARD_ID_RE = re.compile(r"^M-[a-z0-9_-]+-\d{3}$")
MOTIF_TASK = "yt-inspire 규칙대로 입력의 모티브 카드를 만들어 {\"cards\": [...]} 형태로 답하세요. 기법 카탈로그 병합은 하지 마세요."
CARDS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": motif.TYPES},
                    "title": {"type": "string"},
                    "idea": {"type": "string"},
                    "how": {"type": "array", "items": {"type": "string"}},
                    "basis": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["type", "title", "idea", "evidence"],
            },
        },
    },
    "required": ["cards"],
}


def _rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-inspire" / "SKILL.md"]


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


def _library_ids(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ScriptError("bad_request", "library_ids는 지인 기법 id 문자열의 목록이어야 합니다")
    return list(dict.fromkeys(value)) or None


def _allowed(category: str) -> list[str]:
    return motif.allowed_types(category)


# ---- 상태 -------------------------------------------------------------------------------

@route("GET", "/api/motif")
def _status(req: Request) -> dict:
    category = _category(req.query.get("category"))
    counts = refstore.count(category)
    allowed = _allowed(category)
    return {
        "category": category, "ref": counts["ref"], "stage": counts["stage"],
        "allowed_types": allowed, "allowed_ko": [motif.TYPE_KO[t] for t in allowed],
        "has_profile": (work_root() / "profile.md").exists(),
        "has_typify": motif.has_typify(category),      # context()의 typify가 None이 아닌 조건과 같다
        "cards": motif.card_details(category),
    }


# ---- 실행(작업) -------------------------------------------------------------------------

def run_target(category: str, topic: str | None, shared: list[dict] | None = None) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="context")
        job.check_cancelled()
        payload = motif.context(category, topic, shared=shared or [])
        if payload["stage"] == 3 and payload["typify"] is None:
            payload["allowed_types"] = ["technique", "angle"]
        allowed = payload["allowed_types"]
        job.update(0, step="claude")
        job.check_cancelled()
        _, cards = llm.ask_validated(
            _rules(), payload, MOTIF_TASK,
            lambda d: motif.validate_cards(category, d.get("cards") if isinstance(d, dict) else None, allowed),
            schema=CARDS_SCHEMA,
        )
        job.update(0, state="awaiting", step="preview", ask="save", preview={
            "cards": cards, "topic": topic, "stage": payload["stage"], "allowed_types": payload["allowed_types"],
            "titles": motif.ref_titles(category),
        })
        job.finish("done", {"cards": len(cards), "ref": payload["ref"]})

    return target


@route("POST", "/api/motif/run")
def _run(req: Request) -> dict:
    category = _category(req.json.get("category"))
    topic = _topic(req.json.get("topic"))
    library_ids = _library_ids(req.json.get("library_ids"))
    if refstore.count(category)["ref"] == 0:
        raise ScriptError("no_refs", "먼저 레퍼런스를 넣어 주세요")
    shared = motif.library_techniques(library_ids)      # 없는·틀인 지인 id는 작업을 만들기 전에 library_item_missing
    llm.check()                          # claude가 없으면 작업을 만들지 않는다
    items = [new_item(category, title=category, step="context")]
    job = req.app.jobs.start(KIND, run_target(category, topic, shared), items=items, category=category)
    return {"job": job.snapshot()}


# ---- 저장 -------------------------------------------------------------------------------

def _star_indexes(value: Any, count: int) -> list[int]:
    if not isinstance(value, list):
        raise ScriptError("bad_request", "star는 목록이어야 합니다")
    for i in value:
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < count:
            raise ScriptError("bad_request", f"star에 저장 목록 밖의 번호가 있습니다: {i!r}")
    return sorted(set(value))


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


@route("POST", "/api/motif/save")
def _save(req: Request) -> dict:
    body = req.json
    category = _category(body.get("category"))
    cards = body.get("cards")
    if not isinstance(cards, list) or not cards:
        raise ScriptError("bad_request", "저장할 카드를 하나 이상 골라 주세요")
    star_at = _star_indexes(body.get("star", []), len(cards))
    job_id = body.get("job_id")
    allowed = _allowed(category)
    tmp_dir = workspace_dir("tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with refstore.INDEX_LOCK:
        fd, name = tempfile.mkstemp(prefix="motif_cards_", suffix=".json", dir=tmp_dir)   # 요청마다 고유한 파일
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cards, f, ensure_ascii=False)
            ids = motif.add(category, tmp, allowed)["ids"]
        finally:
            tmp.unlink(missing_ok=True)
        starred: list[str] = []
        warning: str | None = None
        for i in star_at:
            try:
                motif.star(category, ids[i])
            except ScriptError as e:
                warning = f"카드는 저장했지만 ★ 표시는 하지 못했습니다: {e.message}"
                break
            starred.append(ids[i])
        details = motif.card_details(category)
    _mark_saved(req, job_id, category)
    result: dict = {"ids": ids, "starred": starred, "cards": details}
    if warning:
        result["warning"] = warning
    return result


@route("POST", "/api/motif/star")
def _star(req: Request) -> dict:
    body = req.json
    category = _category(body.get("category"))
    card_id, on = body.get("id"), body.get("on")
    if not isinstance(card_id, str) or not CARD_ID_RE.match(card_id):
        raise ScriptError("bad_request", "카드 id 형식이 맞지 않습니다")
    if not isinstance(on, bool):
        raise ScriptError("bad_request", "on은 true/false여야 합니다")
    with refstore.INDEX_LOCK:
        motif.star(category, card_id, off=not on)
        return {"cards": motif.card_details(category)}
