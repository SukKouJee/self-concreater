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
from urllib.parse import quote
from pathlib import Path
from typing import Any

from scripts import captions, claude_cli, conti, llm, refstore
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.jobs import Job, JobCancelled, new_item
from scripts.ui.server import Request, route

KIND = "conti"
CONTI_SUFFIX = conti.CONTI_SUFFIX
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


_safe_path = conti.safe_draft_path          # 경로 규칙은 conti에 있다(기존 이름 유지)


def rel_path(path: Path) -> str:
    return path.resolve().relative_to(work_root().resolve()).as_posix()


_rel = rel_path                          # 기존 이름 유지


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
        data, (script, cuts) = llm.ask_validated(
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
    llm.check()              # claude가 없으면 작업을 만들지 않는다
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
    llm.check()
    payload = {**ctx, "count": count, "exclude": exclude}
    answer = llm.ask(_rules(), payload, f"7절 규칙으로 컷 대사 대안을 {count}개 만드세요", schema=ALT_SCHEMA)
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
    if not isinstance(expected, str) or not expected:
        raise ScriptError("bad_request", "hash가 필요합니다. 콘티를 다시 불러오세요")
    return conti.update_cut(path, _int(body.get("n"), "n"), fields, expected_hash=expected)


@route("POST", "/api/conti/srt")
def _srt(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    track = body.get("track", "caption")
    if not isinstance(track, str):
        raise ScriptError("bad_request", "track은 문자열이어야 합니다")
    result = conti.export_srt(path, track)
    if not result["path"]:
        return result
    return {**result, "url": "/files/" + "/".join(quote(part) for part in result["path"].split("/"))}


def _file_url(rel_path: str) -> str:
    return "/files/" + "/".join(quote(part) for part in rel_path.split("/"))


def srt_fit_target(path: Path, track: str, video: Path) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="detect")
        job.check_cancelled()
        result = conti.export_srt(path, track, video, progress=lambda step: job.update(0, step=step))
        preview = {**result, "url": _file_url(result["path"]) if result["path"] else None}
        job.update(0, state="done", step="write", ask=None, preview=preview)
        job.finish("done", {"count": result["count"], "snapped": result["retime"]["snapped"]})

    return target


@route("POST", "/api/conti/srt-fit")
def _srt_fit(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    track = body.get("track", "caption")
    if not isinstance(track, str) or track not in conti.SRT_TRACKS:
        raise ScriptError("bad_request", "track은 caption 또는 line이어야 합니다")
    if not conti._load_conti(path)["cuts"]:
        raise ScriptError("bad_request", "콘티에 컷이 없습니다")
    video = conti._clean_video_path(body.get("video"))       # 작업을 만들기 전에 영상 경로부터 확인
    items = [new_item(_rel(path), title=path.name, step="detect")]
    job = req.app.jobs.start("srtfit", srt_fit_target(path, track, video), items=items)
    return {"job": job.snapshot()}


# ---- 움직이는 자막 영상(Remotion, 선택 설치) -----------------------------------------------------

@route("GET", "/api/captions/check")
def _captions_check(req: Request) -> dict:
    try:
        captions.check_setup()
    except ScriptError as e:
        return {"ready": False, "message": e.message}
    return {"ready": True}


def caption_video_target(path: Path, track: str, video: Path | None) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="render")
        job.check_cancelled()

        def on_progress(done: int, total: int) -> None:
            job.update(0, step="render", message=f"{done}/{total} 프레임")

        try:
            result = captions.render(path, video, track, progress=on_progress, cancel=job.is_cancelled,
                                     step=lambda name: job.update(0, step=name))
        except captions.RenderCancelled as e:
            raise JobCancelled from e
        job.update(0, state="done", step="render", ask=None, message=None,
                   preview={**result, "url": _file_url(result["path"])})
        job.finish("done", {"count": result["count"]})

    return target


@route("POST", "/api/conti/caption-video")
def _caption_video(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    track = body.get("track", "caption")
    if not isinstance(track, str) or track not in conti.SRT_TRACKS:
        raise ScriptError("bad_request", "track은 caption 또는 line이어야 합니다")
    video = None if body.get("video") is None else conti._clean_video_path(body.get("video"))
    captions.check_setup()                                   # 작업을 만들기 전에 설치부터 확인
    if not conti.caption_blocks(path, track)["blocks"]:
        raise ScriptError("no_captions", "내보낼 자막이 없습니다")
    items = [new_item(_rel(path), title=path.name, step="render")]
    job = req.app.jobs.start("caption_video", caption_video_target(path, track, video), items=items)
    return {"job": job.snapshot()}
