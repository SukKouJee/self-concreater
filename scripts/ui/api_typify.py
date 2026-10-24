"""유형화 API: 상태 조회, 실행(작업) → 미리보기, 저장.

유형화 작업(kind "typify")의 생애
- 항목은 카테고리 하나(id = category). 스크립트가 군집을 돌리고(`cluster`), Claude가 근거 문장·아키타입 이름을
  만든 뒤(`claude`), 항목을 `state="awaiting", step="preview", ask="save"`로 두고 **작업은 `done`으로 끝낸다**.
- 저장은 작업과 별개인 `POST /api/typify/save`다. 화면이 고친 notes와 미리보기의 run_at을 보낸다.
  미리보기 뒤에 유형화가 다시 돌았다면(run_at이 다르면) 저장하지 않는다.
- 미리보기 단계에서는 index.md를 건드리지 않는다. 저장(apply)만이 index.md와 applied.json을 쓴다.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from scripts import claude_cli, cluster, llm, refstore
from scripts.common import ScriptError, workspace_dir
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "typify"
TYPIFY_TASK = "yt-typify 규칙대로 입력 유형화 결과의 notes JSON(축별 근거 문장, 아키타입 이름)을 만드세요."
NOTES_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "axes": {"type": "object", "additionalProperties": {"type": "string"}},
        "archetypes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                "required": ["id", "name"],
            },
        },
    },
    "required": ["axes", "archetypes"],
}


def _rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-typify" / "SKILL.md"]


def _category(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ScriptError("bad_request", "category가 필요합니다")
    refstore.existing_category_dir(value)             # 없는 카테고리는 category_not_found
    return value


def _stage(ref: int) -> tuple[str, int]:
    preliminary, formal = refstore._stage_limits()
    if ref < preliminary:
        return "too_few", preliminary - ref
    return ("formal" if ref >= formal else "preliminary"), 0


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None                                   # 깨진 스냅숏이 화면 전체를 막지 않게 한다(다시 저장하면 새로 쓴다)


def _overlay_names(applied: dict | None, fm: dict) -> dict | None:
    """index.md 프런트매터의 아키타입 이름이 기준이다(손으로 고친 이름이 스냅숏을 이긴다)."""
    if applied is None:
        return None
    names = {str(a.get("id")): a.get("name") for a in fm.get("archetypes") or [] if isinstance(a, dict)}
    archetypes = [{**a, "name": names.get(str(a.get("id")), a.get("name"))} for a in applied.get("archetypes") or []]
    return {**applied, "archetypes": archetypes}


# ---- 상태 -------------------------------------------------------------------------------

@route("GET", "/api/typify")
def _status(req: Request) -> dict:
    category = _category(req.query.get("category"))
    counts = refstore.count(category)
    stage, need = _stage(counts["ref"])
    fm, _ = refstore.read_md(refstore.existing_category_dir(category) / "index.md")
    return {
        "category": category, "ref": counts["ref"], "mine": counts["mine"], "stage": stage, "need": need,
        "typified_at": fm.get("typified_at"), "applied": _overlay_names(_read_json(cluster.applied_path(category)), fm),
    }


# ---- 실행(작업) -------------------------------------------------------------------------

def run_target(category: str) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="cluster")
        job.check_cancelled()
        result = cluster.run(category, out=cluster.preview_path(category))   # ref만 쓴다(mine은 cluster가 뺀다)
        job.update(0, step="claude")
        job.check_cancelled()
        notes, _ = llm.ask_validated(
            _rules(), result, TYPIFY_TASK, lambda d: cluster.validate_notes(result, d), schema=NOTES_SCHEMA,
        )
        job.update(0, state="awaiting", step="preview", ask="save", preview={
                "result": result, "notes": notes, "titles": cluster.video_titles(category, result),
            })
        job.finish("done", {"archetypes": len(result["archetypes"]), "ref": result["ref"]})

    return target


@route("POST", "/api/typify/run")
def _run(req: Request) -> dict:
    category = _category(req.json.get("category"))
    ref = refstore.count(category)["ref"]
    stage, need = _stage(ref)
    if stage == "too_few":
        raise ScriptError("too_few", f"ref가 {ref}개라 아직 유형을 볼 수 없습니다. {need}개 더 넣어 주세요")
    llm.check()                          # claude가 없으면 작업을 만들지 않는다
    items = [new_item(category, title=category, step="cluster")]
    job = req.app.jobs.start(KIND, run_target(category), items=items, category=category)
    return {"job": job.snapshot()}


# ---- 저장 -------------------------------------------------------------------------------

@route("POST", "/api/typify/save")
def _save(req: Request) -> dict:
    body = req.json
    category = _category(body.get("category"))
    run_at = body.get("run_at")
    if not isinstance(run_at, str) or not run_at:
        raise ScriptError("bad_request", "run_at이 필요합니다. 유형화를 다시 실행해 주세요")
    notes = body.get("notes")
    if not isinstance(notes, dict):
        raise ScriptError("bad_request", "notes는 JSON 객체여야 합니다")
    tmp_dir = workspace_dir("tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="typify_notes_", suffix=".json", dir=tmp_dir)   # 요청마다 고유한 파일
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False)
        result = cluster.apply(category, tmp, expected_run_at=run_at, source=cluster.preview_path(category))
    finally:
        tmp.unlink(missing_ok=True)
    return {**result, "applied": _read_json(cluster.applied_path(category))}
