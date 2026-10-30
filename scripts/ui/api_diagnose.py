"""진단 API: 대상 목록, 비교(Claude 없음), 설명(작업) → 미리보기, 저장, 의도한 차이, 기록 보기.

진단 작업(kind "diagnose")의 생애
- 항목은 대상 하나(id = 대상 경로). 스크립트가 비교하고(`compare`), Claude가 notes를 만든 뒤(`claude`),
  항목을 `state="awaiting", step="preview", ask="save"`로 두고 **작업은 `done`으로 끝낸다**.
- 저장은 작업과 별개인 `POST /api/diagnose/save`다. 저장 직전에 비교를 다시 해 notes를 검증하고(`validate_notes`)
  `similarity.save`로 `mine/<이름>.md`를 쓴다(`## 내 메모`는 save가 유지한다).
- 미리보기 단계에서는 mine/을 건드리지 않는다. 의도한 차이 표시는 사용자 행동이라 바로 저장한다.
"""
from __future__ import annotations

import json
import os
import posixpath
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts import claude_cli, conti, ctx, features, keys, llm, refstore, similarity
from scripts.common import ScriptError
from scripts.ui.api_refs import _body_parts
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "diagnose"
DIAG_TASK = "yt-diagnose 규칙대로 입력 비교 결과의 notes JSON(요약, 갭마다 설명, 다음 행동 1~3개)을 만드세요."
NOTES_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "gaps": {"type": "object", "additionalProperties": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
    },
    "required": ["summary", "gaps", "actions"],
}
AXIS_KO = features.AXIS_KO
UNSAFE_NAME_CHARS = frozenset('<>:"/|?*' + chr(92))      # Windows 파일 이름에 못 쓰는 문자(+ 제어 문자는 따로 본다)
MAX_NAME = 120
WRITE_LOCK = threading.Lock()                     # 진단 기록(mine/*.md)의 읽고-고쳐-쓰기를 한 번에 하나씩
MAX_KEY = 120
RECORD_FIELDS = ("target", "date", "nearest", "proximity", "gaps", "intentional", "preliminary")


def _rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-diagnose" / "SKILL.md"]


def _category(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ScriptError("bad_request", "category가 필요합니다")
    refstore.require_category(value)                  # 없는 카테고리는 category_not_found
    return value


def _bad_path() -> ScriptError:
    return ScriptError("bad_path", "진단할 수 있는 파일이 아닙니다(이 카테고리의 내 영상 또는 기획안만 가능)")


def _target_key(category: str, value: Any) -> str:
    """작업 폴더 안 refs/ref_<cat>/videos/*.md 또는 drafts/*.md(콘티 제외)만 허용하고 그 문서 키를 돌려준다.
    탈출 검사는 resolve 기준(`scratch.key_of`: `..`·심볼릭 링크·작업 폴더 밖 절대 경로·다른 드라이브). 풀린 키의
    폴더가 두 허용 폴더 중 하나와 같아야 한다(하위 폴더·다른 카테고리 안 됨. Windows는 대소문자 무시). 확장자는 `.md` 그대로(대소문자 구분)."""
    if not isinstance(value, str) or not value.strip():
        raise _bad_path()
    try:
        key = ctx.here().scratch.key_of(value)
    except ScriptError as e:
        raise _bad_path() from e
    folder = os.path.normcase(posixpath.dirname(key) + "/")      # Windows는 폴더 이름 대소문자 무시(예전 WindowsPath 비교)
    allowed = {os.path.normcase(keys.videos_dir(category)), os.path.normcase(keys.DRAFTS)}
    if (posixpath.splitext(key)[1] != ".md" or folder not in allowed
            or keys.stem(key).endswith(conti.CONTI_SUFFIX)):
        raise _bad_path()
    return key


def _name(value: Any) -> str:
    """파일 이름으로 안전한지만 본다(save가 만드는 이름 — 유니코드·공백·괄호 — 은 허용)."""
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME or any(ord(c) < 32 or c in UNSAFE_NAME_CHARS for c in value)
            or value.startswith(".") or value.endswith((".", " "))):
        raise ScriptError("bad_request", "진단 이름이 올바르지 않습니다")
    return value


def _compare(category: str, key: str) -> dict:
    return {**similarity.compare(category, key), "axis_ko": dict(AXIS_KO)}


def _date(fm: dict, key: str) -> str:
    if fm.get("date"):
        return str(fm["date"])
    return datetime.fromtimestamp(ctx.here().store.stat(key).mtime).date().isoformat()


def _diagnosed(category: str, name: str) -> dict | None:
    key = similarity._diag_key(name)
    if not ctx.here().store.exists(key):
        return None
    try:
        fm, _ = refstore.read(key)
    except (ScriptError, OSError, UnicodeDecodeError):
        return None
    if fm.get("category") != category:
        return None
    return {"date": str(fm.get("date") or ""), "nearest": fm.get("nearest")}


def _entry(category: str, key: str, fm: dict, name: str) -> dict:
    return {"path": key, "name": name, "title": fm.get("title") or name,
            "date": _date(fm, key), "diagnosed": _diagnosed(category, name)}


def _read(key: str) -> dict | None:
    try:
        return refstore.read(key)[0]
    except (ScriptError, OSError, UnicodeDecodeError):
        return None                       # 프런트매터가 깨진 파일은 목록에서만 뺀다(사용자가 고치는 중일 수 있다)


# ---- 대상 -------------------------------------------------------------------------------

@route("GET", "/api/diagnose/targets")
def _targets(req: Request) -> dict:
    category = _category(req.query.get("category"))
    gate = similarity.typify_gate(category)
    has_typify, preliminary = gate["available"], gate["preliminary"]
    store = ctx.here().store
    videos, drafts = [], []
    for meta in store.list(keys.videos_dir(category)):
        fm = _read(meta.key)
        if fm is not None and fm.get("role") == "mine":
            videos.append(_entry(category, meta.key, fm, str(fm.get("id") or keys.stem(meta.key))))
    for meta in store.list(keys.DRAFTS):
        stem = keys.stem(meta.key)
        fm = _read(meta.key)
        if stem.endswith(conti.CONTI_SUFFIX) or fm is None or fm.get("category") != category:
            continue
        drafts.append(_entry(category, meta.key, fm, stem))
    videos.sort(key=lambda v: (v["date"], v["path"]), reverse=True)
    drafts.sort(key=lambda d: (d["date"], d["path"]), reverse=True)
    return {"has_typify": has_typify, "preliminary": preliminary, "videos": videos, "drafts": drafts}


# ---- 비교(Claude 없음) ------------------------------------------------------------------

@route("POST", "/api/diagnose/compare")
def _compare_route(req: Request) -> dict:
    category = _category(req.json.get("category"))
    return _compare(category, _target_key(category, req.json.get("target")))


# ---- 설명(작업) -------------------------------------------------------------------------

def run_target(category: str, key: str) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="compare")
        job.check_cancelled()
        result = similarity.compare(category, key)
        job.update(0, step="claude")
        job.check_cancelled()
        _, notes = llm.ask_validated(
            _rules(), result, DIAG_TASK, lambda d: similarity.validate_notes(result, d), schema=NOTES_SCHEMA,
        )
        job.update(0, state="awaiting", step="preview", ask="save", preview={"result": {**result, "axis_ko": dict(AXIS_KO)}, "notes": notes})
        job.finish("done", {"gaps": len(result["gaps"])})

    return target


@route("POST", "/api/diagnose/explain")
def _explain(req: Request) -> dict:
    category = _category(req.json.get("category"))
    key = _target_key(category, req.json.get("target"))
    llm.check()                          # claude가 없으면 작업을 만들지 않는다
    result = _compare(category, key)                  # no_typify_result·not_mine 등은 작업 전에 400으로 알린다
    items = [new_item(key, title=result["target"]["title"] or result["target"]["name"], step="compare")]
    job = req.app.jobs.start(KIND, run_target(category, key), items=items, category=category)
    return {"job": job.snapshot()}


# ---- 저장 -------------------------------------------------------------------------------

def _mark_saved(req: Request, job_id: Any, category: str, target: str) -> None:
    """미리보기 항목이 아직 저장 대기면 저장됨으로 바꾼다. 없는 작업·다른 종류·다른 대상은 무시."""
    if not isinstance(job_id, str):
        return
    try:
        job = req.app.jobs.get(job_id)
    except ScriptError:
        return
    if job.kind != KIND or job.category != category:
        return
    item = job.item(0)
    if item["id"] == target and item["state"] == "awaiting":
        job.update(0, state="saved", ask=None)


def _record(category: str, name: str) -> dict:
    key = similarity._diag_key(name)
    missing = ScriptError("diagnosis_not_found", f"진단 기록이 없습니다: mine/{name}.md")
    if not ctx.here().store.exists(key):
        raise missing
    fm, body = refstore.read(key)
    if fm.get("category") != category:
        raise missing
    return {"name": name, "path": key,
            "fm": {k: (str(fm.get(k)) if k == "date" else fm.get(k)) for k in RECORD_FIELDS},
            "body": _body_parts(body)}


@route("POST", "/api/diagnose/save")
def _save(req: Request) -> dict:
    body = req.json
    category = _category(body.get("category"))
    key = _target_key(category, body.get("target"))
    # similarity.save도 compare를 한 번 더 한다. 여기서 먼저 비교하는 것은 저장 직전의 갭으로 notes를 검증해
    # 모양·누락 오류를 400으로 알리고 tmp 파일을 만들지 않기 위해서다(의도한 차이가 그 사이 바뀔 수 있다).
    with WRITE_LOCK:
        return _save_locked(req, body, category, key)


def _save_locked(req: Request, body: dict, category: str, key: str) -> dict:
    result = _compare(category, key)
    notes = similarity.validate_notes(result, body.get("notes"))
    tmp = ctx.here().scratch.tmp_path(f"diag_notes_{secrets.token_hex(8)}.json")   # 요청마다 고유한 파일
    try:
        tmp.write_bytes(refstore.text_bytes(json.dumps(notes, ensure_ascii=False)))
        saved = similarity.save(category, key, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    _mark_saved(req, body.get("job_id"), category, key)
    return {**saved, "record": _record(category, result["target"]["name"])}


# ---- 의도한 차이 · 기록 -----------------------------------------------------------------

@route("POST", "/api/diagnose/intentional")
def _intentional(req: Request) -> dict:
    body = req.json
    category = _category(body.get("category"))
    name = _name(body.get("name"))
    key, on = body.get("key"), body.get("on")
    if not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY:
        raise ScriptError("bad_request", "갭 key가 올바르지 않습니다")
    if not isinstance(on, bool):
        raise ScriptError("bad_request", "on은 true/false여야 합니다")
    record = _record(category, name)                  # 없는 기록·다른 카테고리는 diagnosis_not_found
    target = record["fm"]["target"]
    with WRITE_LOCK:
        previous = list(refstore.read(similarity._diag_key(name))[0].get("intentional") or [])
        result = similarity.intentional(category, name, key, off=not on)
        try:
            compare = _compare(category, _target_key(category, target))
        except ScriptError:
            refstore.update_frontmatter(similarity._diag_key(name), {"intentional": previous})   # 되돌린다
            raise
    return {**result, "compare": compare}


@route("GET", "/api/diagnose/record")
def _record_route(req: Request) -> dict:
    category = _category(req.query.get("category"))
    return _record(category, _name(req.query.get("name")))
