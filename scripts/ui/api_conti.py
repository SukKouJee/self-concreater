"""콘티 API: draft 목록, 콘티 만들기(작업) · 저장, 불러오기, 장면 후보, 컷 대사 대안, 컷 고치기.

콘티 만들기 작업(kind "conti")의 생애
- 항목은 draft 하나(id = draft 상대 경로). Claude 호출이 끝나면 항목을 `state="awaiting", step="preview",
  ask="save"`로 두고 preview를 채운 채 **작업은 `done`으로 끝낸다**(스레드를 결정 대기로 묶어 두지 않는다).
- 저장은 작업과 별개인 `POST /api/conti/save`다. 화면이 preview.conti를 고친 JSON을 그대로 보낸다.
  그래서 브라우저를 다시 열어도 `GET /api/jobs/current?kind=conti`로 미리보기를 되살릴 수 있다.
- "collect"와 달리 exclusive가 아니라 수집 작업과 동시에 돌 수 있다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import claude_cli, conti, refstore
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "conti"
CONTI_SUFFIX = "-conti"
MAX_ALTERNATIVES = 10
CREATE_TASK = "yt-conti 규칙대로 입력 draft의 콘티 JSON을 만드세요."
ALT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"line": {"type": "string"}, "intent": {"type": "string"}},
                "required": ["line", "intent"],
            },
        },
    },
    "required": ["alternatives"],
}


def _rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-conti" / "SKILL.md"]


def _safe_path(value: Any, *, conti_file: bool | None = None) -> Path:
    """작업 폴더의 drafts/ 안 .md 파일만 허용한다(상대 경로 → 절대 경로)."""
    if not isinstance(value, str) or not value:
        raise ScriptError("bad_request", "path가 필요합니다")
    drafts = (work_root() / "drafts").resolve()
    try:
        target = (work_root() / value).resolve()
    except (OSError, ValueError) as e:
        raise ScriptError("bad_path", f"잘못된 경로입니다: {value}") from e
    if not target.is_relative_to(drafts) or target.suffix.lower() != ".md" or "\x00" in value:
        raise ScriptError("bad_path", f"drafts 폴더 안의 .md 파일만 쓸 수 있습니다: {value}")
    if conti_file is not None and target.stem.endswith(CONTI_SUFFIX) != conti_file:
        raise ScriptError("bad_path", f"{'콘티' if conti_file else '기획안'} 파일이 아닙니다: {value}")
    return target


def _rel(path: Path) -> str:
    return path.resolve().relative_to(work_root().resolve()).as_posix()


def _flag(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ScriptError("bad_request", f"{key}는 true/false여야 합니다")
    return value


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ScriptError("bad_request", f"{name}는 정수여야 합니다")
    try:
        return int(value)
    except ValueError as e:
        raise ScriptError("bad_request", f"{name}는 정수여야 합니다") from e


# ---- draft 목록 -----------------------------------------------------------------------

@route("GET", "/api/drafts")
def _drafts(req: Request) -> dict:
    out = []
    folder = work_root() / "drafts"
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        if path.stem.endswith(CONTI_SUFFIX):
            continue
        try:
            fm, _ = refstore.read_md(path)
        except ScriptError:
            continue                      # 프런트매터가 깨진 파일은 목록에서만 뺀다(사용자가 고치는 중일 수 있다)
        made = conti.conti_path(path)
        html_path = made.with_suffix(".html")
        out.append({
            "path": _rel(path), "title": fm.get("title") or path.stem, "category": fm.get("category"),
            "date": str(fm.get("date") or path.stem[:10]),
            "conti": _rel(made) if made.exists() else None,
            "html": _rel(html_path) if html_path.exists() else None,
        })
    out.sort(key=lambda d: (d["date"], d["path"]), reverse=True)
    return {"drafts": out}


# ---- 만들기(작업) · 저장 ----------------------------------------------------------------

def create_target(ctx: dict) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="claude")
        job.check_cancelled()
        data, (script, cuts) = claude_cli.ask_validated(
            _rules(), ctx, CREATE_TASK, lambda d: conti.validate(d, ctx["sections"]),
        )
        preview = {"conti": data, "script": script, "cuts": cuts,
                   "warnings": conti.warnings(script, cuts, ctx["speech_cps"])}
        job.update(0, state="awaiting", step="preview", ask="save", preview=preview)
        job.finish("done", {"cuts": len(cuts), "warnings": len(preview["warnings"])})

    return target


@route("POST", "/api/conti/create")
def _create(req: Request) -> dict:
    body = req.json
    draft = _safe_path(body.get("draft"), conti_file=False)
    force = _flag(body, "force")
    ctx = conti.context(_rel(draft))
    if ctx["conti_exists"] and not force:
        raise ScriptError("conti_exists", f"이미 콘티가 있습니다: {ctx['conti']} (다시 만들려면 덮어쓰기를 고르세요)")
    items = [new_item(_rel(draft), title=ctx["title"], step="claude")]
    job = req.app.jobs.start(KIND, create_target(ctx), items=items, category=ctx["category"])
    return {"job": job.snapshot()}


@route("POST", "/api/conti/save")
def _save(req: Request) -> dict:
    body = req.json
    draft = _safe_path(body.get("draft"), conti_file=False)
    data = body.get("conti")
    if not isinstance(data, dict):
        raise ScriptError("bad_request", "conti는 JSON 객체여야 합니다")
    force = _flag(body, "force")
    tmp = workspace_dir("tmp", "conti_ui.json")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        result = conti.save(_rel(draft), tmp, force)
    finally:
        tmp.unlink(missing_ok=True)
    return {**result, "hash": conti.content_hash(result["path"])}


# ---- 불러오기 -------------------------------------------------------------------------

@route("GET", "/api/conti")
def _get(req: Request) -> dict:
    path = _safe_path(req.query.get("path"), conti_file=True)
    fm = conti._load_conti(path)
    sections = [{"label": s["label"]} for s in fm["script"]]
    pool = conti.frame_pool(fm.get("category"))
    matched = conti.match_frames(fm["cuts"], sections, pool)
    vertical = fm.get("format") == "shorts"
    cuts = []
    for c in fm["cuts"]:
        pinned = c.get("ref_frame")
        ref: dict | None = None
        if pinned and (work_root() / pinned).exists():
            ref = {"frame": pinned, "match": "pinned", "title": Path(pinned).name}
        elif c["n"] in matched:
            m = matched[c["n"]]
            ref = {"frame": m["frame"], "match": m["match"], "title": m.get("title")}
        cuts.append({**c, "label": fm["script"][c["section"]]["label"], "sketch": conti.sketch(c["size"], vertical),
                     "ref": ref, "max_chars": int(fm["speech_cps"] * c["sec"])})
    return {"path": _rel(path), "hash": conti.content_hash(path), "fm": fm, "html": fm.get("html"),
            "cuts": cuts, "warnings": conti.warnings(fm["script"], fm["cuts"], fm["speech_cps"])}


@route("GET", "/api/conti/scenes")
def _scenes(req: Request) -> dict:
    path = _safe_path(req.query.get("path"), conti_file=True)
    return {"scenes": conti.scenes(path, _int(req.query.get("n"), "n"))}


# ---- 대안 · 컷 고치기 -------------------------------------------------------------------

@route("POST", "/api/conti/alternatives")
def _alternatives(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    n = _int(body.get("n"), "n")
    count = _int(body.get("count", 3), "count")
    if not 1 <= count <= MAX_ALTERNATIVES:
        raise ScriptError("bad_request", f"count는 1~{MAX_ALTERNATIVES}이어야 합니다")
    exclude = body.get("exclude") or []
    if not isinstance(exclude, list) or not all(isinstance(x, str) for x in exclude):
        raise ScriptError("bad_request", "exclude는 문자열 목록이어야 합니다")
    ctx = conti.cut_context(path, n)
    payload = {**ctx, "count": count, "exclude": exclude}
    answer = claude_cli.ask(_rules(), payload, f"7절 규칙으로 컷 대사 대안을 {count}개 만드세요", schema=ALT_SCHEMA)
    raw = answer.get("alternatives")
    if not isinstance(raw, list) or not all(isinstance(a, dict) and isinstance(a.get("line"), str) for a in raw):
        raise ScriptError("claude_failed", "Claude의 대안 응답이 형식에 맞지 않습니다. 다시 시도해 주세요")
    max_chars = ctx["max_chars"]
    alternatives = []
    for a in raw[:count]:
        chars = len(conti._norm(a["line"]))
        alternatives.append({"line": a["line"], "intent": str(a.get("intent") or ""), "chars": chars,
                             "over": chars > max_chars})
    return {"alternatives": alternatives, "max_chars": max_chars}


@route("POST", "/api/conti/cut")
def _cut(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    fields = body.get("fields")
    if not isinstance(fields, dict):
        raise ScriptError("bad_request", "fields는 JSON 객체여야 합니다")
    expected = body.get("hash")
    if expected is not None and not isinstance(expected, str):
        raise ScriptError("bad_request", "hash는 문자열이어야 합니다")
    return conti.update_cut(path, _int(body.get("n"), "n"), fields, expected_hash=expected)
