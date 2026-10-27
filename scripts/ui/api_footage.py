"""촬영 영상 API: 폴더 연결, 처리 작업(kind "footage"), 판정 조작, 새 컷 넣기, 원본·사진 서빙.

- 콘티는 `path`(작업 폴더 기준 drafts/…-conti.md)로 가리킨다. 판정은 `footage_match`의 상태 파일에 있고,
  연결 폴더 목록(`folders`)도 거기에 둔다(정규화한 절대 경로, 더하기·빼기는 STATE_LOCK 안에서).
- 처리 작업: 연결 폴더의 영상을 파일 하나씩 `footage.process_file`로 처리한 뒤(끝난 파일은 캐시로 건너뜀)
  마지막 '맞추기' 항목에서 `footage_match.match`에 **연결된 모든 파일의 결과**를 넘긴다(앞서 처리한 파일도 —
  빠지면 그 파일로 정한 자동 판정이 지워진다). 취소는 파일 사이와 맞추기 직전에 확인한다(맞추기 호출 도중은 못 멈춤).
  Claude가 준비됐는지는 작업 전에 확인한다(llm.check → 400). collect·footage_clips와는 동시에 돌지 않는다
  (jobs.EXCLUSIVE → `busy`).
- 화면 응답(`footage_view`): 지금 콘티의 컷만. 후보·선택·새 컷 후보가 가리키는 파일이 지금 연결 폴더에 없으면
  `missing_source: true`(원본 없음, F7). 같은 파일이 같은 자리에 돌아오면(같은 지문) 자동으로 되살아난다.
- 원본 서빙(`/api/footage/media/<fid>?path=<콘티>`): 그 콘티의 연결 폴더 바로 아래에서 지금 찾은 파일만 내준다.
  실제 경로를 다시 풀어 폴더 밖(링크 탈출·하위 폴더)이면 거절하고, 지문을 다시 계산해 맞을 때만 내준다.
  폴더를 빼면 곧바로 404. Range(206, Content-Range, Accept-Ranges)를 지원하고 나눠 읽어 보낸다(수 GB 파일).
- 사진 서빙(`/api/footage/frame/<fid>/<rel>`): footage_dir(fid) 안의 .jpg만.
"""
from __future__ import annotations

import os
import re
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts import conti, footage, llm
from scripts import footage_match as fmatch
from scripts.common import ScriptError
from scripts.ui.api_conti import rel_path, view_of
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import NOT_FOUND, STREAM_CHUNK, FileSlice, Request, Response, _contained, route

KIND = "footage"
MATCH_ITEM = "match"
CHUNK = STREAM_CHUNK
MSG_CACHED = "이미 처리함"
MSG_NO_FOLDERS = "먼저 촬영 영상 폴더를 더해 주세요"
MSG_NO_FILES = "연결된 폴더에 영상 파일(.mp4 .mov .m4v .mkv)이 없습니다"
MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska"}
FILMED = frozenset({"auto", "pinned"})
REMAINING = frozenset({"missing", "not_filmed"})


# ---- 입력 ----------------------------------------------------------------------------

def _conti(value: Any) -> Path:
    path = conti.safe_draft_path(value, conti_file=True)
    if not path.is_file():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {value}")
    return path


def _text_arg(body: dict, key: str, message: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ScriptError("bad_request", message)
    return value


def _folder_arg(value: Any) -> Path:
    """사용자가 붙여 넣은 폴더 경로(앞뒤 공백·따옴표 허용). 드라이브부터 시작하는 전체 경로만."""
    raw = value.strip().strip("\"'").strip() if isinstance(value, str) else ""
    if not raw or "\x00" in raw:
        raise ScriptError("bad_request", "폴더 경로를 넣어 주세요")
    path = Path(raw)
    if not path.is_absolute():
        raise ScriptError("bad_request", f"드라이브부터 시작하는 전체 경로를 넣어 주세요 (예: D:\\촬영\\1일차): {raw}")
    return path


def _key(folder: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(folder)))


# ---- 폴더 스캔 --------------------------------------------------------------------------

@dataclass
class _Linked:
    folder: str
    missing: bool = False
    files: list[dict] = field(default_factory=list)


def _scan_linked(folders: list[str]) -> list[_Linked]:
    """연결 폴더마다 지금 있는 영상 파일. 폴더가 없거나 읽을 수 없으면(드라이브 분리 등) missing."""
    out = []
    for folder in folders:
        try:
            out.append(_Linked(folder, files=footage.scan_folder(folder)))
        except ScriptError as e:
            if e.code != "folder_not_found":
                raise
            out.append(_Linked(folder, missing=True))
        except OSError:
            out.append(_Linked(folder, missing=True))   # 접근 거부·드라이브 분리: 화면에 '폴더 없음'으로 보인다
    return out


def _complete(fid: str) -> dict | None:
    """끝난 처리 캐시(읽을 수 없음, 또는 장면과 받아쓰기까지). 받아쓰기가 남았으면 None(다시 처리)."""
    cached = footage.cached_result(fid)
    if cached is None:
        return None
    if cached["status"] == "done" and not (footage.footage_dir(fid) / "transcript.json").is_file():
        return None
    return cached


def _folder_view(linked: _Linked) -> dict:
    processed = unreadable = 0
    duration = 0.0
    for f in linked.files:
        cached = footage.cached_result(f["fid"])
        if cached is None:
            continue
        if cached["status"] == "done":
            processed += 1
            duration += float(cached.get("duration_sec") or 0.0)
        elif cached["status"] == "unreadable":
            unreadable += 1
    return {"path": linked.folder, "files": len(linked.files), "duration_sec": round(duration, 2),
            "processed": processed, "unreadable": unreadable,
            "pending": len(linked.files) - processed - unreadable, "missing": linked.missing}


# ---- 화면 응답 ---------------------------------------------------------------------------

class _Sources:
    """fid → 파일 이름과 원본이 지금 연결 폴더에 있는지."""

    def __init__(self, linked: list[_Linked]) -> None:
        self.present = {f["fid"]: f["name"] for item in linked for f in item.files}
        self._names: dict[str, str | None] = {}

    def name(self, fid: str) -> str | None:
        if fid in self.present:
            return self.present[fid]
        if fid not in self._names:
            cached = footage.cached_result(fid) if footage.valid_fid(fid) else None
            self._names[fid] = cached.get("name") if cached else None
        return self._names[fid]

    def mark(self, item: dict) -> dict:
        fid = item.get("fid")
        ok = isinstance(fid, str)
        return {**item, "name": self.name(fid) if ok else None, "missing_source": not (ok and fid in self.present)}


def _counts(cuts: list[dict]) -> dict:
    kinds = [c["kind"] for c in cuts]
    return {"filmed": sum(k in FILMED for k in kinds), "review": kinds.count("review"),
            "missing": sum(k in REMAINING for k in kinds), "total": len(kinds)}


def _outputs(path: Path) -> dict:
    base = path.stem[: -len(conti.CONTI_SUFFIX)]
    html = path.with_name(f"{base}-remaining.html")
    csv = path.with_name(f"{base}-footage.csv")
    clips = path.with_name(f"{base}-cuts")
    return {"remaining_html": rel_path(html) if html.is_file() else None,
            "csv": rel_path(csv) if csv.is_file() else None,
            "clips_dir": rel_path(clips) if clips.is_dir() else None}


def _job_for(app: Any, rel: str) -> dict | None:
    job = app.jobs.latest_where(KIND, lambda j: (j.summary or {}).get("path") == rel)
    return job.snapshot() if job is not None else None


def footage_view(app: Any, path: Path) -> dict:
    """GET /api/footage 응답(지금 콘티의 컷만)."""
    state = fmatch.load_state(path)
    linked = _scan_linked(state["folders"])
    sources = _Sources(linked)
    cuts = []
    for c in fmatch.visible_cuts(path, state):
        verdict = c["match"]
        choice = verdict.get("choice")
        cuts.append({"id": c["id"], "n": c["n"], "kind": verdict["kind"],
                     "choice": sources.mark(choice) if choice else None,
                     "candidates": [sources.mark(x) for x in verdict.get("candidates") or []],
                     "reason": verdict.get("reason"), "user": bool(verdict.get("user"))})
    suggestions = [sources.mark({**s, "key": s.get("key") or fmatch.suggestion_key(s)})
                   for s in state["suggestions"] if not s.get("hidden")]
    rel = rel_path(path)
    return {"path": rel, "folders": [_folder_view(item) for item in linked], "summary": _counts(cuts),
            "cuts": cuts, "suggestions": suggestions, "stt_missing": bool(state.get("stt_missing")),
            "images_used": state.get("images_used", 0), "matched_at": state.get("matched_at"),
            "outputs": _outputs(path), "job": _job_for(app, rel)}


@route("GET", "/api/footage")
def _get(req: Request) -> dict:
    return footage_view(req.app, _conti(req.query.get("path")))


# ---- 폴더 더하기·빼기 ------------------------------------------------------------------------

@route("POST", "/api/footage/folders")
def _add_folder(req: Request) -> dict:
    path = _conti(req.json.get("path"))
    given = _folder_arg(req.json.get("folder"))
    try:
        folder = given.resolve()
        found = folder.is_dir()
    except OSError:
        found = False
    if not found:
        raise ScriptError("folder_not_found", f"폴더를 찾을 수 없습니다: {given}")
    with fmatch.STATE_LOCK:
        state = fmatch.load_state(path)
        if any(_key(f) == _key(folder) for f in state["folders"]):
            raise ScriptError("folder_exists", f"이미 연결된 폴더입니다: {folder}")
        state["folders"].append(str(folder))
        fmatch.save_state(path, state)
    return footage_view(req.app, path)


@route("POST", "/api/footage/folders/remove")
def _remove_folder(req: Request) -> dict:
    """연결만 끊는다(처리 결과·판정은 남긴다). 그 폴더의 원본은 곧바로 내주지 않는다."""
    path = _conti(req.json.get("path"))
    given = _key(_folder_arg(req.json.get("folder")))
    with fmatch.STATE_LOCK:
        state = fmatch.load_state(path)
        kept = [f for f in state["folders"] if _key(f) != given]
        if len(kept) == len(state["folders"]):
            raise ScriptError("folder_not_linked", "연결된 폴더가 아닙니다")
        state["folders"] = kept
        fmatch.save_state(path, state)
    return footage_view(req.app, path)


# ---- 처리 작업 ------------------------------------------------------------------------------

def _set_stt_missing(path: Path, missing: bool) -> None:
    with fmatch.STATE_LOCK:
        state = fmatch.load_state(path)
        state["stt_missing"] = missing
        fmatch.save_state(path, state)


def _reporter(job: Job, index: int) -> Callable[[str, float | None, str], None]:
    def report(phase: str, fraction: float | None, text: str) -> None:
        job.update(index, step=phase, fraction=fraction, message=text)
    return report


def _process_one(job: Job, index: int, file: dict) -> dict | None:
    """파일 하나. 실패해도 다음 파일로 간다(그 항목만 failed). -> 처리 결과, 실패면 None."""
    job.update(index, state="running", step="scenes", fraction=0.0, message=None, started_at=time.time())
    try:
        result = footage.process_file(file["path"], progress=_reporter(job, index))
    except ScriptError as e:
        job.update(index, state="failed", message=e.message, finished_at=time.time())
        return None
    except Exception as e:  # noqa: BLE001 - 파일 하나의 예상치 못한 오류로 나머지 파일을 멈추지 않는다
        traceback.print_exc(file=sys.stderr)
        job.update(index, state="failed", message=f"예상치 못한 오류: {type(e).__name__}: {e}",
                   finished_at=time.time())
        return None
    if result["status"] == "done":
        job.update(index, state="done", step="done", fraction=1.0, message=None,
                   duration_sec=result.get("duration_sec"), finished_at=time.time())
    elif result["status"] == "unreadable":
        reason = result.get("reason")
        job.update(index, state="failed", message=f"읽을 수 없음: {reason}" if reason else "읽을 수 없음",
                   finished_at=time.time())
    else:
        job.update(index, state="failed", message=result.get("reason") or "다음 처리 때 다시 해 봅니다",
                   finished_at=time.time())
    return result


def process_target(path: Path, rel: str, files: list[dict]) -> Callable[[Job], None]:
    def target(job: Job) -> None:
        results: list[dict] = []
        tally = {"processed": 0, "cached": 0, "failed": 0}
        stt_missing = False
        for index, file in enumerate(files):
            cached = _complete(file["fid"]) if job.item(index)["state"] == "done" else None
            if cached is not None:
                results.append(cached)
                tally["cached"] += 1
                continue
            job.check_cancelled()
            result = _process_one(job, index, file)
            if result is None or result["status"] != "done":
                tally["failed"] += 1
            else:
                tally["processed"] += 1
                stt_missing = stt_missing or bool(result.get("stt_missing"))
            if result is not None:
                results.append(result)
        job.check_cancelled()
        last = len(files)
        job.update(last, state="running", started_at=time.time())
        _set_stt_missing(path, stt_missing)
        summary: dict[str, Any] = {"path": rel, "files": len(files), **tally, "stt_missing": stt_missing}
        try:
            state = fmatch.match(path, results)
        except ScriptError as e:
            job.update(last, state="failed", message=e.message, finished_at=time.time())
            job.finish("failed", summary, e.message)
            return
        counts = _counts([{"kind": c["match"]["kind"]} for c in fmatch.visible_cuts(path, state)])
        job.update(last, state="done", message=None, finished_at=time.time())
        job.finish("done", {**summary, "counts": counts})

    return target


@route("POST", "/api/footage/process")
def _process(req: Request) -> dict:
    """새 파일을 처리하고 맞춘다(작업). 진행 항목은 파일마다 하나 + 마지막 '맞추기'."""
    path = _conti(req.json.get("path"))
    rel = rel_path(path)
    state = fmatch.load_state(path)
    if not state["folders"]:
        raise ScriptError("no_folders", MSG_NO_FOLDERS)
    files = [f for item in _scan_linked(state["folders"]) for f in item.files]
    if not files:
        raise ScriptError("no_files", MSG_NO_FILES)
    llm.check()              # claude가 없으면 처리하기 전에 400으로 알린다
    items = []
    for f in files:
        item = new_item(f["fid"], title=f["name"], step="scenes")
        if _complete(f["fid"]) is not None:
            item.update(state="done", step="done", message=MSG_CACHED)
        items.append(item)
    items.append(new_item(MATCH_ITEM, title="맞추기", step="match"))
    job = req.app.jobs.start(KIND, process_target(path, rel, files), items=items, summary={"path": rel})
    return {"job": job.snapshot()}


# ---- 판정 조작 ------------------------------------------------------------------------------

def _cut_arg(body: dict) -> str:
    return _text_arg(body, "cut", "cut(컷 id)이 필요합니다")


@route("POST", "/api/footage/choose")
def _choose(req: Request) -> dict:
    body = req.json
    path = _conti(body.get("path"))
    fmatch.choose(path, _cut_arg(body), body.get("fid"), body.get("start"), body.get("end"))
    return footage_view(req.app, path)


@route("POST", "/api/footage/not-filmed")
def _not_filmed(req: Request) -> dict:
    path = _conti(req.json.get("path"))
    fmatch.mark_not_filmed(path, _cut_arg(req.json))
    return footage_view(req.app, path)


@route("POST", "/api/footage/reset")
def _reset(req: Request) -> dict:
    path = _conti(req.json.get("path"))
    fmatch.reset(path, _cut_arg(req.json))
    return footage_view(req.app, path)


@route("POST", "/api/footage/suggestions/hide")
def _hide(req: Request) -> dict:
    path = _conti(req.json.get("path"))
    fmatch.hide_suggestion(path, req.json.get("key"))
    return footage_view(req.app, path)


def suggested_sec(start: float, end: float) -> float:
    """구간 길이를 컷 길이 규칙(0.5초 단위, 0.5~120초)에 맞춘다."""
    sec = round((end - start) / conti.SEC_STEP) * conti.SEC_STEP
    return min(conti.MAX_CUT_SEC, max(conti.MIN_CUT_SEC, sec))


@route("POST", "/api/footage/suggestions/insert")
def _insert(req: Request) -> dict:
    """새 컷 후보를 after_cut 뒤에 컷으로 넣고(edit_cuts insert → 이력 auto) 그 컷을 이 구간으로 고정(pinned)한다."""
    body = req.json
    path = _conti(body.get("path"))
    expected = _text_arg(body, "hash", "hash가 필요합니다. 콘티를 다시 불러오세요")
    key = _text_arg(body, "key", "새 컷 후보 key가 필요합니다")
    after = _text_arg(body, "after_cut", "after_cut(이 컷 뒤에 넣을 컷 id)이 필요합니다")
    found = next((s for s in fmatch.load_state(path)["suggestions"]
                  if not s.get("hidden") and fmatch.suggestion_key(s) == key), None)
    if found is None:
        raise ScriptError("suggestion_not_found", "새 컷 후보가 없습니다. 화면을 다시 불러오세요")
    cut_ids = [c["id"] for c in conti._load_conti(path)["cuts"]]
    if after not in cut_ids:
        raise ScriptError("cut_not_found", f"컷 {after}이(가) 없습니다")
    start, end = float(found["start"]), float(found["end"])
    fields = {"line": str(found.get("line") or ""),
              "guide": str(found.get("guide") or "").strip() or conti.CUT_DEFAULTS["guide"],
              "size": found["size"], "sec": suggested_sec(start, end)}
    op = {"op": "insert", "at": cut_ids.index(after) + 1, "cut": fields}
    result, written = conti.edit_cuts_fm(path, op, expected)
    fmatch.choose(path, result["cut_id"], found["fid"], start, end)
    fmatch.hide_suggestion(path, key)          # 넣은 후보는 다시 나오지 않게
    return {"hash": result["hash"], "cut_id": result["cut_id"], "conti": view_of(path, written, result["hash"]),
            "footage": footage_view(req.app, path)}


# ---- 원본·사진 서빙 ---------------------------------------------------------------------------

class RangeNotSatisfiable(Exception):
    """Range가 파일 밖을 가리킨다(416)."""


_RANGE_RE = re.compile(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*")


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """HTTP Range 한 구간 → (시작, 끝 포함). 없거나 형식이 틀리거나 여러 구간이면 None(전체, RFC 9110은 무시를 허용).
    파일 밖이면 RangeNotSatisfiable."""
    if not header or "," in header:
        return None
    m = _RANGE_RE.fullmatch(header)
    if m is None or not (m.group(1) or m.group(2)):
        return None
    if not m.group(1):                         # bytes=-N: 끝에서 N바이트
        n = int(m.group(2))
        if n == 0 or size == 0:
            raise RangeNotSatisfiable
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size:
        raise RangeNotSatisfiable
    if end < start:
        return None                            # 끝이 시작보다 앞: 틀린 형식이라 무시한다
    return start, min(end, size - 1)


def media_file(path: Path, fid: str) -> Path | None:
    """fid를 그 콘티의 연결 폴더 바로 아래 실제 파일로 푼다. 못 찾거나 폴더 밖이면 None."""
    if not footage.valid_fid(fid):
        return None
    for item in _scan_linked(fmatch.load_state(path)["folders"]):
        entry = next((f for f in item.files if f["fid"] == fid), None)
        if entry is None:
            continue
        try:
            root = Path(item.folder).resolve(strict=True)
            real = Path(entry["path"]).resolve(strict=True)
            if real.parent != root or real.suffix.lower() not in footage.VIDEO_EXTS or not real.is_file():
                continue                       # 링크로 폴더 밖을 가리키거나 하위 폴더
            if footage.fingerprint(real) != fid:
                continue                       # 스캔한 뒤 바뀌었다
        except OSError:
            continue                           # 그 사이에 지워졌거나 드라이브가 빠졌다: 없는 파일과 같게
        return real
    return None


@route("GET", r"/api/footage/media/(?P<fid>[^/]+)")
def _media(req: Request, fid: str) -> Response | tuple[int, dict]:
    path = _conti(req.query.get("path"))
    real = media_file(path, fid)
    if real is None:
        return NOT_FOUND
    size = real.stat().st_size
    ctype = MEDIA_TYPES.get(real.suffix.lower(), "application/octet-stream")
    ranges = [("Accept-Ranges", "bytes")]
    try:
        span = byte_range(req.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(416, b"", "text/plain; charset=utf-8", [*ranges, ("Content-Range", f"bytes */{size}")])
    if span is None:
        return Response(200, b"", ctype, ranges, file=FileSlice(real, 0, size, CHUNK))
    start, end = span
    return Response(206, b"", ctype, [*ranges, ("Content-Range", f"bytes {start}-{end}/{size}")],
                    file=FileSlice(real, start, end - start + 1, CHUNK))


@route("GET", r"/api/footage/frame/(?P<fid>[^/]+)/(?P<rel>.+)")
def _frame(req: Request, fid: str, rel: str) -> Response | tuple[int, dict]:
    if not footage.valid_fid(fid) or not rel.lower().endswith(".jpg"):
        return NOT_FOUND
    found = _contained(footage.footage_dir(fid), rel)
    if found is None:
        return NOT_FOUND
    return Response(200, found.read_bytes(), "image/jpeg")
