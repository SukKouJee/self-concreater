"""서버판 `/files/<key>`(서버 설계 §4.3, ADR-S7): 로그인한 사용자의 저장소에서 바이트를 꺼내 보낸다.

로컬 서버(`scripts/ui/server.py::_files`)와 같은 규칙:
- 경로(URL 디코딩한 것)는 키여야 하고(`keys.check`, 실패면 404), `FILE_DIRS`(`workspace/frames`, `workspace/conti-art`,
  `drafts`, `reports`) 바로 아래 이상만 내준다. 폴더 키(끝 `/`)·그 밖은 404.
- 영상(`VIDEO_SUFFIXES`)만 Range(206·416, `Accept-Ranges`, 끝 없는 `bytes=N-`는 `MAX_OPEN_RANGE`까지)로 나눠 보낸다.
  나머지는 통째로(객체 64MB 상한). SVG는 `SVG_CSP`.
- 저장소는 `keys.kind`로 고른다: md = 문서(RLS), `workspace/frames` = 공유 캐시(로그인 사용자 누구나 — 공개 YouTube 영상 프레임),
  그 밖 = 자기 객체(RLS). 남의 키는 RLS 때문에 "없음"과 똑같이 404.
- 호출자(어댑터)가 그 사용자의 `ctx.use(Ctx(user_id, backend))` 안에서 부른다. 스트리밍 반복자는 S3만 읽으므로 문맥이 필요 없다.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from scripts import ctx, keys
from scripts.common import ScriptError
from scripts.stores import ObjectStore
from scripts.ui import server as ui
from server.s3store import S3ObjectStore, S3SharedCache


@dataclass
class StreamReply:
    """본문을 나눠 보내는 응답(Range). length = 보낼 바이트 수(Content-Length)."""

    status: int
    content_type: str
    length: int
    chunks: Iterator[bytes]
    headers: list[tuple[str, str]] = field(default_factory=list)


Reply = ui.Response | StreamReply | tuple[int, dict]


def file_key(rel: str) -> str | None:
    """`/files/` 뒤(URL 디코딩한 것) → 내줄 수 있는 키, 아니면 None(404)."""
    try:
        key = keys.check(rel)
    except ScriptError:
        return None
    if key.endswith("/"):
        return None
    parts = tuple(key.split("/"))
    if not any(parts[: len(prefix)] == prefix and len(parts) > len(prefix) for prefix in ui.FILE_DIRS):
        return None
    return key


def _content_type(key: str) -> str:
    return ui.content_type(Path(PurePosixPath(key).name))


def _chunks(store: ObjectStore, key: str, start: int, length: int) -> Iterator[bytes]:
    if isinstance(store, (S3ObjectStore, S3SharedCache)):
        return store.open_range(key, start, length)
    return iter([store.get(key)[start: start + length]])     # other ObjectStore (tests): slice in memory


def _ranged(store: ObjectStore, key: str, ctype: str, header: str | None) -> Reply:
    size = store.stat(key).size
    ranges = [("Accept-Ranges", "bytes")]
    try:
        span = ui.byte_range(header, size, ui.MAX_OPEN_RANGE)
    except ui.RangeNotSatisfiable:
        return ui.Response(416, b"", "text/plain; charset=utf-8", [*ranges, ("Content-Range", f"bytes */{size}")])
    if span is None:
        return StreamReply(200, ctype, size, _chunks(store, key, 0, size), ranges)
    start, end = span
    length = end - start + 1
    return StreamReply(206, ctype, length, _chunks(store, key, start, length),
                       [*ranges, ("Content-Range", f"bytes {start}-{end}/{size}")])


def serve(rel: str, range_header: str | None) -> Reply:
    """지금 문맥(`ctx.here()`: 로그인 사용자)의 저장소에서 `/files/<rel>` 응답을 만든다."""
    key = file_key(rel)
    if key is None:
        return ui.NOT_FOUND
    kind = keys.kind(key)
    if kind == "scratch":
        return ui.NOT_FOUND
    c = ctx.here()
    ctype = _content_type(key)
    try:
        return _read(c, key, kind, ctype, range_header)
    except FileNotFoundError:
        return ui.NOT_FOUND
    except (ScriptError, OSError) as e:      # S3/PG trouble or a store refusal: same JSON error shape (500 is logged)
        return ui.error_result(e)


def _read(c: ctx.Ctx, key: str, kind: str, ctype: str, range_header: str | None) -> Reply:
    """없는 키는 FileNotFoundError(호출자가 404로 바꾼다)."""
    if kind == "doc":
        data = c.store.read(key).raw
    else:
        store = c.shared if kind == "shared" else c.objects
        if PurePosixPath(key).suffix.lower() in ui.VIDEO_SUFFIXES:
            return _ranged(store, key, ctype, range_header)
        data = store.get(key)
    resp = ui.Response(200, data, ctype)
    if key.lower().endswith(".svg"):
        resp.headers.append(("Content-Security-Policy", ui.SVG_CSP))
    return resp
