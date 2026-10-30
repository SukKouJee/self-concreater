"""콘티 API: draft 목록, 콘티 만들기(작업) · 저장, 불러오기, 장면 후보, 컷 대사 대안, 컷 고치기, 삭제(휴지통).

콘티 만들기 작업(kind "conti")의 생애
- 항목은 draft 하나(id = draft 상대 경로). Claude 호출이 끝나면 항목을 `state="awaiting", step="preview",
  ask="save"`로 두고 preview를 채운 채 **작업은 `done`으로 끝낸다**(스레드를 결정 대기로 묶어 두지 않는다).
- 저장은 작업과 별개인 `POST /api/conti/save`다. 화면이 preview.conti를 고친 JSON을 그대로 보낸다.
  그래서 브라우저를 다시 열어도 `GET /api/jobs/current?kind=conti`로 미리보기를 되살릴 수 있다.
- "collect"와 달리 exclusive가 아니라 수집 작업과 동시에 돌 수 있다.

장면으로 합치기 작업(kind "conti_merge")도 같은 생애다. 항목은 콘티 하나(id = 콘티 상대 경로). 반복이 없으면 작업을
만들지 않고 바로 `nothing_to_merge`. Claude가 대사 나눌 자리를 정하면 preview `{before, after, cuts, sections, hash, warnings}`로
`awaiting`. 저장은 `POST /api/conti/merge/apply {path, cuts, hash}`(미리보기 뒤 콘티가 바뀌면 `conti_changed`).

장면에 사진 고르기
- 만들기 작업: 콘티 검증 뒤 설명(desc)이 있는 레퍼런스 사진이 있으면 Claude가 장면마다 사진을 골라 preview 컷(`cuts`와
  `conti.cuts`)에 `ref_auto`를 넣는다. 실패하면 사진 없이 계속하고 경고 `refs_pick_failed`(뒤 묶음만 실패하면 앞 묶음은
  남기고 `refs_pick_partial`). 반복 컷이 남아 있으면 고르지 않고 경고 `refs_merge_first`.
- 사진 다시 고르기(kind "conti_refs"): `POST /api/conti/refs/pick {path, hash}`. 반복 컷이 남아 있으면 작업 없이
  `merge_first`. 미리보기 없이 컷의 `ref_auto`만 저장하고(콘티 내용이 아니라 표시용) 이력 auto를 남긴다. 고르는 사이
  콘티가 바뀌어도 저장하되, 지워졌거나 action·guide·size가 바뀐 장면은 건너뛴다(`skipped`, 경고 `refs_pick_skipped`).
  끝나면 summary `{path, hash, picked, scenes, skipped, warnings}`.

장면 그림 그리기(kind "conti_art"): `POST /api/conti/art {path, ids?, stale_only?, hash?}`. hash가 있고 다르면 `conti_changed`
(저장 직후 자동으로 다시 그릴 때 최신 hash를 넘긴다). 같은 콘티에 그리는 작업이 돌면 `conti_busy`(다른 콘티끼리는 동시에 가능),
반복 컷이 남아 있으면 작업 없이 `merge_first`. 묶음(2장면) 3개를 동시에 그리고 묶음마다 저장하므로 멈춰도 그린 그림은 남는다.
끝나면 summary `{path, hash, drawn, skipped, failed, warnings, remaining}`(remaining: 고정 안 된 장면 중 아직 그림이 없거나
옛 그림인 수, 화면의 "N장면을 못 그렸어요"). 콘티 보기 응답의 `art_counts {total, missing, stale, fresh}`는 고정 안 된
장면의 그림 상태 수다(도구 줄 버튼 문구).
"""
from __future__ import annotations

import json
from urllib.parse import quote
from pathlib import Path
from typing import Any

from scripts import captions, claude_cli, conti, conti_art, conti_history, conti_refs, llm, refstore, scene_merge
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.jobs import Job, JobCancelled, new_item
from scripts.ui.server import Request, route

KIND = "conti"
MERGE_KIND = "conti_merge"
REFS_KIND = "conti_refs"
ART_KIND = "conti_art"
CONTI_SUFFIX = conti.CONTI_SUFFIX
MAX_ALTERNATIVES = 10
CREATE_TASK = "yt-conti 규칙대로 입력 draft의 콘티 JSON을 만드세요."
MERGE_TASK = "merge 규칙대로 섹션마다 장면 순서와 대사 나눌 자리를 정한 JSON을 만드세요. 대사 글자는 바꾸지 마세요."
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
        warnings = conti.warnings(script, cuts, ctx["speech_cps"])
        # Claude가 콘티에 적어 낸 ref_auto는 믿지 않는다: 같은 카테고리에서 검증해 고른 것만 남긴다
        cuts = conti_refs.apply_picks(cuts, {})
        data = {**data, "cuts": [{k: v for k, v in raw.items() if k != "ref_auto"} if isinstance(raw, dict) else raw
                                 for raw in data["cuts"]]}
        if conti_refs.candidates(ctx.get("category")):
            job.update(0, step="refs")
            try:
                conti_refs.check_merged(cuts)
                picked = conti_refs.pick(cuts, ctx["category"], lambda done, total: job.check_cancelled())
            except ScriptError as e:                 # 사진은 표시용: 고르지 못해도 콘티 미리보기는 낸다
                warnings.append(_create_pick_warning(e))
            else:
                if picked.error is not None:
                    warnings.append(_partial_warning(picked, len(cuts)))
                cuts = conti_refs.apply_picks(cuts, picked.picks)
                data = {**data, "cuts": _with_picked(data["cuts"], cuts)}
        preview = {"conti": data, "script": script, "cuts": cuts, "warnings": warnings}
        job.update(0, state="awaiting", step="preview", ask="save", preview=preview)
        job.finish("done", {"cuts": len(cuts), "warnings": len(preview["warnings"])})

    return target


def _create_pick_warning(e: ScriptError) -> dict:
    if e.code == "merge_first":
        return {"kind": "refs_merge_first",
                "message": "반복 컷이 있어 사진을 고르지 않았어요. 저장한 뒤 장면으로 합치고 사진 다시 고르기를 눌러 주세요"}
    return {"kind": "refs_pick_failed",
            "message": f"장면에 맞는 사진을 고르지 못했어요({e.message}). 저장한 뒤 사진 다시 고르기를 눌러 주세요"}


def _partial_warning(picked: conti_refs.PickResult, total: int) -> dict:
    message = picked.error.message if picked.error is not None else ""
    return {"kind": "refs_pick_partial",
            "message": f"장면 {total}개 중 {len(picked.basis)}개만 사진을 골랐어요({message}). "
                       "나머지는 사진 다시 고르기를 눌러 주세요"}


def _with_picked(raw_cuts: list[dict], cuts: list[dict]) -> list[dict]:
    """Claude가 낸 컷(저장할 때 다시 검증된다)에 검증한 컷의 id와 ref_auto를 옮긴다. id가 같아야 저장 뒤에도 사진이 남는다."""
    out = []
    for raw, cut in zip(raw_cuts, cuts, strict=True):
        merged = {k: v for k, v in raw.items() if k != "ref_auto"}
        merged["id"] = cut["id"]
        if cut.get("ref_auto"):
            merged["ref_auto"] = cut["ref_auto"]
        out.append(merged)
    return out


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

# 콘티 파일을 읽어 산출물을 쓰는 작업(촬영 영상 처리·컷 파일은 summary의 path로 콘티를 가리킨다)
FILE_JOB_KINDS = ("srtfit", "caption_video", "footage", "footage_clips", MERGE_KIND, REFS_KIND, ART_KIND)


def job_running_on(app: Any, kinds: tuple[str, ...], item_id: str) -> bool:
    """그 kind들의 최근 작업 중 item_id(작업 폴더 기준 상대 경로)를 다루며 아직 도는 것이 있는지.
    검사와 이동 사이에 잠금은 없다(로컬 한 사람용이라 그 사이에 작업을 시작할 일은 드물다)."""
    return any((job.summary or {}).get("path") == item_id or any(it.get("id") == item_id for it in job.items)
               for kind in kinds for job in app.jobs.active(kind))


@route("POST", "/api/conti/trash")
def _trash(req: Request) -> dict:
    path = _safe_path(req.json.get("path"), conti_file=True)
    rel = _rel(path)
    if job_running_on(req.app, FILE_JOB_KINDS, rel):
        raise ScriptError("conti_busy", "이 콘티로 작업하는 중입니다(자막·촬영 영상·컷 파일·합치기·사진 고르기·장면 그림). 끝나거나 멈춘 뒤에 삭제하세요")
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
    refs = conti.cut_refs(fm)                            # 컷마다 {frame, match(pinned/content/composition), title, desc}
    vertical = fm.get("format") == "shorts"
    cuts = []
    ratio = conti_art.aspect(fm)
    for c in fm["cuts"]:
        shown = conti_art.display_art(c, ratio)
        art = {"url": _file_url(shown["file"]), "state": shown["state"]} if shown else None
        cuts.append({**c, "art": art, "label": fm["script"][c["section"]]["label"], "sketch": conti.sketch(c["size"], vertical),
                     "ref": refs.get(c["n"]), "max_chars": int(fm["speech_cps"] * c["sec"])})
    base = path.stem[: -len(CONTI_SUFFIX)]
    srt_files = [path.with_name(f"{base}-{suffix}.srt") for _, suffix in conti.SRT_TRACKS.values()]
    return {"path": _rel(path), "hash": digest, "fm": fm, "html": fm.get("html"),
            "cuts": cuts, "art_counts": conti_art.counts(fm),
            "warnings": conti.warnings(fm["script"], fm["cuts"], fm["speech_cps"]),
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
    before = _art_states(conti.validated(conti.read_conti(path).stored))
    result = conti.update_cut(path, _int(body.get("n"), "n"), fields, expected_hash=expected)
    return {**result, "art_stale": _art_stale(before, [result["cut"]], before["aspect"])}


def _art_states(fm: dict) -> dict:
    """편집 전 컷별 그림 근거 {"aspect", "bases": {id: basis}}. 편집 응답의 art_stale 계산용."""
    ratio = conti_art.aspect(fm)
    return {"aspect": ratio, "bases": {c["id"]: conti_art.basis(c, ratio) for c in fm["cuts"] if c.get("id")}}


def _art_stale(before: dict, after_cuts: list[dict], ratio: str) -> list[str]:
    """그림이 있고 이번 편집으로 근거(basis)가 바뀌어 옛 그림인 컷 id. 이미 옛 그림이던 장면도 다시 고치면 넣는다
    (기다리는 중·그리는 중에 또 고쳐도 다시 그려지게, 설계 결정 4). 같은 값으로 저장하면 넣지 않는다."""
    return [c["id"] for c in after_cuts
            if c.get("id") in before["bases"] and conti_art.basis(c, ratio) != before["bases"][c["id"]]
            and conti_art.art_state(c, ratio) == "stale"]


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
    before = _art_states(conti.validated(conti.read_conti(path).stored))
    result, fm = conti.edit_cuts_fm(path, op, expected)
    return {**view_of(path, fm, result["hash"]), "cut_id": result["cut_id"],
            "art_stale": _art_stale(before, fm["cuts"], conti_art.aspect(fm))}


# ---- 장면으로 합치기(작업) · 저장 -----------------------------------------------------------

def _merge_rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-conti" / "merge.md"]


def merge_target(plan: dict) -> Any:
    def target(job: Job) -> None:
        job.update(0, state="running", step="claude")
        job.check_cancelled()
        _, cuts = llm.ask_validated(_merge_rules(), scene_merge.split_request(plan), MERGE_TASK,
                                    lambda d: scene_merge.validate_split(plan, d))
        preview = {"before": plan["before"], "after": plan["after"], "cuts": cuts, "sections": plan["sections"],
                   "hash": plan["hash"], "warnings": plan["warnings"]}
        job.update(0, state="awaiting", step="preview", ask="save", preview=preview)
        job.finish("done", {"before": plan["before"], "after": plan["after"]})

    return target


@route("POST", "/api/conti/merge/preview")
def _merge_preview(req: Request) -> dict:
    path = _safe_path(req.json.get("path"), conti_file=True)
    rel = _rel(path)
    if job_running_on(req.app, (MERGE_KIND,), rel):
        raise ScriptError("conti_busy", "이 콘티를 장면으로 합치는 중입니다. 끝난 뒤에 다시 눌러 주세요")
    plan = scene_merge.plan(path)          # 반복이 없으면 작업을 만들지 않고 바로 nothing_to_merge
    llm.check()
    items = [new_item(rel, title=path.name, step="claude")]
    job = req.app.jobs.start(MERGE_KIND, merge_target(plan), items=items)
    return {"job": job.snapshot()}


@route("POST", "/api/conti/merge/apply")
def _merge_apply(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    expected = _hash(body)
    cuts = body.get("cuts")
    if not isinstance(cuts, list):
        raise ScriptError("bad_request", "cuts는 컷 목록이어야 합니다")
    rel = _rel(path)
    if job_running_on(req.app, ("footage", "footage_clips"), rel):
        raise ScriptError("conti_busy", "촬영 영상을 맞추는 중입니다. 끝나거나 멈춘 뒤에 합치세요")
    result, fm = scene_merge.apply_fm(path, cuts, expected)
    # 합치기 자체의 경고(촬영 영상 판정을 못 옮김)는 merged에. 응답 warnings는 다른 응답처럼 컷 경고만(머리말 경고 상자).
    merge_warnings = [w for w in result["warnings"] if w["kind"] in scene_merge.MERGE_WARNING_KINDS]
    return {**view_of(path, fm, result["hash"]),
            "merged": {"before": result["before"], "after": result["after"], "warnings": merge_warnings}}


# ---- 장면에 사진 다시 고르기(작업) ------------------------------------------------------------

def refs_pick_target(path: Path, category: str | None, cuts: list[dict]) -> Any:
    rel = _rel(path)

    def target(job: Job) -> None:
        job.update(0, state="running", step="claude")

        def on_batch(done: int, total: int) -> None:
            job.check_cancelled()
            if total > 1:
                job.update(0, message=f"{done}/{total} 묶음")

        picked = conti_refs.pick(cuts, category, on_batch)
        job.check_cancelled()
        job.update(0, step="save", message=None)
        result = conti_refs.save_picks(path, picked.picks, picked.basis)
        warnings = []
        if picked.error is not None:
            warnings.append(_partial_warning(picked, len(cuts)))
        if result["skipped"]:
            warnings.append({"kind": "refs_pick_skipped",
                             "message": f"고르는 사이 바뀌거나 지워진 장면 {result['skipped']}개는 사진을 바꾸지 않았어요"})
        job.update(0, state="done", step="save", ask=None)
        job.finish("done", {"path": rel, "hash": result["hash"], "picked": result["picked"], "scenes": result["scenes"],
                            "skipped": result["skipped"], "warnings": warnings})

    return target


@route("POST", "/api/conti/refs/pick")
def _refs_pick(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    expected = _hash(body)
    rel = _rel(path)
    if job_running_on(req.app, (REFS_KIND,), rel):
        raise ScriptError("conti_busy", "이 콘티의 사진을 고르는 중입니다. 끝난 뒤에 다시 눌러 주세요")
    with conti.HISTORY_LOCK:
        cf = conti.read_conti(path)
        if conti.bytes_hash(cf.raw) != expected:
            raise ScriptError("conti_changed", "파일이 밖에서 바뀌었습니다. 다시 불러오세요")
        _, fm, _ = conti.ensure_ids_file(cf)              # 옛 파일은 id를 먼저 붙여야 고른 사진을 같은 컷에 저장한다
    if not conti_refs.candidates(fm.get("category")):
        raise ScriptError("no_ref_descs", "설명이 있는 레퍼런스 사진이 없어요. 레퍼런스 사진 설명을 먼저 만들어 주세요")
    conti_refs.check_merged(fm["cuts"])
    llm.check()
    items = [new_item(rel, title=path.name, step="claude")]
    target = refs_pick_target(path, fm.get("category"), fm["cuts"])
    job = req.app.jobs.start(REFS_KIND, target, items=items, summary={"path": rel})
    return {"job": job.snapshot()}


# ---- 장면 그림 그리기(작업) ------------------------------------------------------------------

def art_target(path: Path, ids: list[str] | None, stale_only: bool) -> Any:
    rel = _rel(path)

    def target(job: Job) -> None:
        job.update(0, state="running", step="claude")
        result = conti_art.draw(path, ids, stale_only, job)
        job.update(0, state="done", step="save", ask=None, message=None)
        job.finish("done", {"path": rel, "hash": conti.content_hash(path), **result})

    return target


@route("POST", "/api/conti/art")
def _art(req: Request) -> dict:
    body = req.json
    path = _safe_path(body.get("path"), conti_file=True)
    ids = body.get("ids")
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
        raise ScriptError("bad_request", "ids는 장면 id 문자열 목록이어야 합니다")
    stale_only = body.get("stale_only", False)
    if not isinstance(stale_only, bool):
        raise ScriptError("bad_request", "stale_only는 true 또는 false여야 합니다")
    rel = _rel(path)
    expected = body.get("hash")
    if expected is not None:
        if not isinstance(expected, str) or not expected:
            raise ScriptError("bad_request", "hash는 문자열이어야 합니다")
        if conti.content_hash(path) != expected:
            raise ScriptError("conti_changed", "파일이 밖에서 바뀌었습니다. 다시 불러오세요")
    if job_running_on(req.app, (ART_KIND,), rel):
        raise ScriptError("conti_busy", "이 콘티의 장면 그림을 그리는 중입니다. 끝난 뒤에 다시 눌러 주세요")
    with conti.HISTORY_LOCK:
        _, fm, _ = conti.ensure_ids_file(conti.read_conti(path))
    if scene_merge.has_repeats(fm["cuts"]):
        raise ScriptError("merge_first", conti_art.MERGE_FIRST_MESSAGE)
    llm.check()
    items = [new_item(rel, title=path.name, step="claude")]
    job = req.app.jobs.start(ART_KIND, art_target(path, ids, stale_only), items=items, summary={"path": rel})
    return {"job": job.snapshot()}


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
