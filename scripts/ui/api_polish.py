"""다듬기 API: 기획안의 훅·제목·썸네일 후보를 Claude로 만들고(작업) 미리보기를 거쳐 고른 것을 저장한다.

다듬기 작업(kind "polish")의 생애
- 항목은 기획안 하나(id = 기획안 경로, title = 기획안 제목). 스크립트가 입력을 모으고(`context`), Claude가 후보를 만든 뒤
  (`claude`), 점수·지적·근거를 붙여 항목을 `state="awaiting", step="preview", ask="save"`로 두고 **작업은 `done`으로 끝낸다**.
- 저장은 작업과 별개인 `POST /api/polish/save`다. 화면이 고른 것(choice)과 미리보기 때의 hash를 보낸다.
- 미리보기 단계에서는 기획안을 건드리지 않는다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import claude_cli, polish
from scripts.common import ScriptError
from scripts.ui.api_conti import _rel, _safe_path
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "polish"
POLISH_TASK = "yt-polish 규칙대로 입력 기획안의 훅 5개, 제목 10개, 썸네일 지시서를 JSON으로 만드세요."
POLISH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "hooks": {
            "type": "array",
            "minItems": polish.HOOK_COUNT[0],
            "maxItems": polish.HOOK_COUNT[1],
            "items": {
                "type": "object",
                "properties": {
                    "script": {"type": "string"},
                    "formula": {"type": "string", "enum": [h["id"] for h in polish._hooks_file()["hooks"]]},
                    "hook_type": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["script", "formula", "hook_type", "why"],
            },
        },
        "titles": {
            "type": "array",
            "minItems": polish.TITLE_COUNT[0],
            "maxItems": polish.TITLE_COUNT[1],
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "why": {"type": "string"}},
                "required": ["title", "why"],
            },
        },
        "thumbnail": {
            "type": "object",
            "properties": {
                "words": {"type": "array", "items": {"type": "string"}},
                "expression": {"type": "string"},
                "framing": {"type": "string"},
                "background": {"type": "string"},
            },
            "required": ["words", "expression", "framing", "background"],
        },
    },
    "required": ["hooks", "titles", "thumbnail"],
}


def _rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-polish" / "SKILL.md"]


def _preview_context(ctx: dict) -> dict:
    refs = ctx["refs"]
    return {"draft": ctx["draft"], "title": ctx["title"], "hook": ctx["hook"], "hash": ctx["hash"],
            "conti_exists": ctx["conti_exists"], "refs": {"ref": refs["ref"], "examples": refs["examples"]}}


def run_target(draft_rel: str) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="context")
        job.check_cancelled()
        ctx = polish.context(draft_rel)
        job.update(0, step="claude")
        job.check_cancelled()
        _, result = claude_cli.ask_validated(
            _rules(), payload=ctx, task=POLISH_TASK, schema=POLISH_SCHEMA,
            validate=lambda data: polish.evaluate(ctx, data),
        )
        job.update(0, state="awaiting", step="preview", ask="save",
                   preview={"result": result, "context": _preview_context(ctx)})
        job.finish("done", {"hooks": len(result["hooks"]), "titles": len(result["titles"])})

    return target


@route("POST", "/api/polish/run")
def _run(req: Request) -> dict:
    path = _safe_path(req.json.get("draft"), conti_file=False)
    ctx = polish.context(_rel(path))                  # 없는 기획안은 작업 전에 draft_not_found
    claude_cli.find_claude()                          # claude가 없으면 작업을 만들지 않는다
    items = [new_item(ctx["draft"], title=ctx["title"], step="context")]
    job = req.app.jobs.start(KIND, run_target(ctx["draft"]), items=items, category=ctx["category"])
    return {"job": job.snapshot()}


def _mark_saved(req: Request, job_id: Any, draft_rel: str) -> None:
    """같은 기획안의 미리보기 항목이 아직 저장 대기면 저장됨으로 바꾼다. 없는 작업·다른 종류·다른 기획안은 무시."""
    if not isinstance(job_id, str):
        return
    try:
        job = req.app.jobs.get(job_id)
    except ScriptError:
        return
    if job.kind != KIND:
        return
    try:
        item = job.item(0)
    except IndexError:
        return                                        # 항목이 없는 작업: 저장은 이미 끝났으니 표시만 건너뛴다
    if item["id"] == draft_rel and item["state"] == "awaiting":
        job.update(0, state="saved", ask=None)


@route("POST", "/api/polish/save")
def _save(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("draft"), conti_file=False)
    expected = body.get("hash")
    if not isinstance(expected, str) or not expected:
        raise ScriptError("bad_request", "hash가 필요합니다")
    saved = polish.apply(_rel(path), body.get("choice"), expected)
    _mark_saved(req, body.get("job_id"), saved["path"])
    return saved
