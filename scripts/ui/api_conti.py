"""콘티 API: draft 목록, 콘티 만들기(작업) · 저장, 불러오기, 장면 후보, 컷 대사 대안, 컷 고치기, 삭제(휴지통).

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

from scripts import captions, claude_cli, conti, conti_history, llm, refstore
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


# ---- 삭제(휴지통) ------------------------------------------------------------------------

FILE_JOB_KINDS = ("srtfit", "caption_video")      # 콘티 파일을 읽어 산출물을 쓰는 작업


def job_running_on(app: Any, kinds: tuple[str, ...], item_id: str) -> bool:
    """그 kind들의 최근 작업 중 item_id(작업 폴더 기준 상대 경로)를 다루며 아직 도는 것이 있는지.
    검사와 이동 사이에 잠금은 없다(로컬 한 사람용이라 그 사이에 작업을 시작할 일은 드물다)."""
    return any(any(it.get("id") == item_id for it in job.items) for kind in kinds for job in app.jobs.active(kind))


@route("POST", "/api/conti/trash")
def _trash(req: Request) -> dict:
    path = _safe_path(req.json.get("path"), conti_file=True)
    rel = _rel(path)
    if job_running_on(req.app, FILE_JOB_KINDS, rel):
        raise ScriptError("conti_busy", "이 콘티로 자막을 만드는 중입니다. 끝나거나 멈춘 뒤에 삭제하세요")
    return conti.trash(rel)


# ---- 불러오기 -------------------------------------------------------------------------

def _stale(path: Path, outputs: list[Path]) -> bool:
    """콘티가 산출물보다 나중에 바뀌었는지(산출물이 있을 때만)."""
    mtime = path.stat().st_mtime
    return any(out.is_file() and mtime > out.stat().st_mtime for out in outputs)


def conti_view(path: Path) -> dict:
    """GET /api/conti 응답. 불러올 때 id를 붙이고 밖에서 고친 내용을 이력에 남긴다(편집과 겹치지 않게 잠금 안에서)."""
    _, fm, digest = conti_history.baseline_state(path)
    return view_of(path, fm, digest)


def view_of(path: Path, fm: dict, digest: str) -> dict:
    """이미 읽은(또는 막 쓴) fm과 그 파일의 content hash로 화면 응답을 만든다. 콘티 md를 다시 읽지 않는다."""
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
    base = path.stem[: -len(CONTI_SUFFIX)]
    srt_files = [path.with_name(f"{base}-{suffix}.srt") for _, suffix in conti.SRT_TRACKS.values()]
    return {"path": _rel(path), "hash": digest, "fm": fm, "html": fm.get("html"),
            "cuts": cuts, "warnings": conti.warnings(fm["script"], fm["cuts"], fm["speech_cps"]),
            "total_sec": round(sum(float(c["sec"]) for c in fm["cuts"]), 2), "target_sec": fm.get("target_sec"),
            "outputs_stale": {"srt": _stale(path, srt_files), "video": _stale(path, [path.with_name(f"{base}-captions.mp4")])}}


@route("GET", "/api/conti")
def _get(req: Request) -> dict:
    return conti_view(_safe_path(req.query.get("path"), conti_file=True))


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


def _hash(body: dict) -> str:
    expected = body.get("hash")
    if not isinstance(expected, str) or not expected:
        raise ScriptError("bad_request", "hash가 필요합니다. 콘티를 다시 불러오세요")
    return expected


OP_KEYS = {"insert": {"op", "at", "section", "cut"}, "delete": {"op", "id", "n"},
           "move": {"op", "id", "n", "to", "section"}, "duplicate": {"op", "id", "n"},
           "update": {"op", "id", "n", "fields"}}


@route("POST", "/api/conti/cuts")
def _cuts(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    expected = _hash(body)
    op = {k: v for k, v in body.items() if k not in ("path", "hash")}
    allowed = OP_KEYS.get(op["op"]) if isinstance(op.get("op"), str) else None
    if allowed is None:
        raise ScriptError("invalid_edit", f"알 수 없는 편집입니다: {op.get('op')!r} ({', '.join(OP_KEYS)})")
    unknown = sorted(set(op) - allowed)
    if unknown:
        raise ScriptError("invalid_edit", f"{op['op']}에서 쓸 수 없는 항목입니다: {', '.join(unknown)}")
    result, fm = conti.edit_cuts_fm(path, op, expected)
    return {**view_of(path, fm, result["hash"]), "cut_id": result["cut_id"]}


# ---- 이력 ------------------------------------------------------------------------------

@route("GET", "/api/conti/history")
def _history(req: Request) -> dict:
    path = _safe_path(req.query.get("path"), conti_file=True)
    with conti.HISTORY_LOCK:
        conti_history.baseline(path)
        return {"versions": conti_history.list_versions(path)}


@route("GET", "/api/conti/history/version")
def _history_version(req: Request) -> dict:
    path = _safe_path(req.query.get("path"), conti_file=True)
    n = _int(req.query.get("n"), "n")
    with conti.HISTORY_LOCK:
        return {"version": conti_history.get_version(path, n)}


@route("GET", "/api/conti/history/diff")
def _history_diff(req: Request) -> dict:
    path = _safe_path(req.query.get("path"), conti_file=True)
    b = req.query.get("b")
    a_n, b_n = _int(req.query.get("a"), "a"), None if b in (None, "") else _int(b, "b")
    with conti.HISTORY_LOCK:                             # 편집이 파일을 쓰는 도중에 읽지 않도록
        return conti_history.diff_versions(path, a_n, b_n)


@route("POST", "/api/conti/history/name")
def _history_name(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    label = body.get("name")
    if not isinstance(label, str):
        raise ScriptError("bad_request", "name은 문자열이어야 합니다")
    version = conti_history.name(path, label, _hash(body))
    return {"version": version, "hash": conti.content_hash(path)}


@route("POST", "/api/conti/history/restore")
def _history_restore(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    return conti_history.restore(path, _int(body.get("n"), "n"), _hash(body))


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
