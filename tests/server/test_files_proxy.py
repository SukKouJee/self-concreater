"""서버판 `/files/<key>`(서버 설계 §4.3): 로컬 서버와 같은 FILE_DIRS 허용 규칙, 영상만 Range(206·416),
SVG CSP, 사용자 파일 CSP. 실제 PG 16 + MinIO. 환경이 없으면 skip."""
from __future__ import annotations

from typing import Any

import pytest

from scripts import ctx, keys
from scripts.ctx import Ctx
from scripts.ui import server as ui
from server import asgi

CONTI = keys.conti_md(keys.draft_md("trip"))


@pytest.fixture
def alice(asgi_stack: Any) -> tuple[Any, Ctx]:
    client = asgi_stack.login("a@example.com")
    return client, Ctx(user_id=str(asgi_stack.users["a@example.com"]), backend=asgi_stack.backend)


def _put(c: Ctx, key: str, data: bytes) -> None:
    with ctx.use(c):
        c.for_key(key).put(key, data)          # type: ignore[union-attr]


@pytest.mark.parametrize("key", [
    "profile.md", "yt-coach.json", "workspace/raw/vid12345678.json", "workspace/typify/travel.json",
    "workspace/conti_history/trip/0001.json", "refs/ref_travel/index.md", "drafts", "reports",
])
def test_outside_file_dirs_is_404_even_when_it_exists(alice, key):
    client, c = alice
    with ctx.use(c):
        if keys.is_doc(key):
            c.store.write(key, b"---\ntitle: x\n---\n")
        elif "/" in key or "." in key:
            c.for_key(key).put(key, b"{}")   # type: ignore[union-attr]
    r = client.get(f"/files/{key}")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


@pytest.mark.parametrize("rel", ["drafts/../profile.md", "drafts/%2e%2e/profile.md", "drafts/a%5Cb.html",
                                 "drafts/CON.html", "drafts/x/", "drafts/a%00.html", "workspace/tmp/a.json"])
def test_bad_keys_are_404(alice, rel):
    client, _ = alice
    assert client.get(f"/files/{rel}").status_code == 404


def test_missing_file_is_404(alice):
    client, _ = alice
    assert client.get("/files/drafts/nope-conti.html").status_code == 404


def test_html_doc_svg_and_frames_are_served_with_types(alice):
    client, c = alice
    html, svg = keys.html_of(CONTI), keys.art(CONTI, "c1", "draft", "abcdef")
    frame = keys.frames_dir("vid12345678") + "0001.jpg"
    _put(c, html, "<html>콘티</html>".encode())
    _put(c, svg, b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    _put(c, frame, b"\xff\xd8jpeg")
    with ctx.use(c):
        c.store.write(CONTI, "---\ntitle: 콘티\n---\n".encode())

    r = client.get(f"/files/{html}")
    assert r.status_code == 200 and r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["content-security-policy"] == asgi.FILES_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    r = client.get(f"/files/{svg}")
    assert r.headers["content-type"] == "image/svg+xml" and r.headers["content-security-policy"] == ui.SVG_CSP
    r = client.get(f"/files/{frame}")
    assert r.headers["content-type"] == "image/jpeg" and r.content == b"\xff\xd8jpeg"
    r = client.get(f"/files/{CONTI}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")


def test_percent_encoded_korean_key(alice):
    client, c = alice
    key = keys.report_html("travel", "2026-10-31")
    _put(c, key, b"<html>r</html>")
    from urllib.parse import quote
    assert client.get("/files/" + quote(key)).content == b"<html>r</html>"
    hangul = keys.html_of(keys.conti_md(keys.draft_md("제주")))
    _put(c, hangul, b"<html>j</html>")
    assert client.get("/files/" + quote(hangul)).content == b"<html>j</html>"


VIDEO = bytes(range(256)) * 40          # 10240 bytes


@pytest.fixture
def video(alice) -> tuple[Any, str]:
    client, c = alice
    key = keys.captions_mp4(CONTI)
    _put(c, key, VIDEO)
    return client, key


def test_video_without_range_streams_everything(video):
    client, key = video
    r = client.get(f"/files/{key}")
    assert r.status_code == 200 and r.content == VIDEO
    assert r.headers["accept-ranges"] == "bytes" and r.headers["content-length"] == str(len(VIDEO))
    assert r.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize("header,start,end", [("bytes=0-99", 0, 99), ("bytes=10000-", 10000, 10239),
                                              ("bytes=-40", 10200, 10239), ("bytes=10200-99999", 10200, 10239)])
def test_video_range_is_206(video, header, start, end):
    client, key = video
    r = client.get(f"/files/{key}", headers={"Range": header})
    assert r.status_code == 206 and r.content == VIDEO[start: end + 1]
    assert r.headers["content-range"] == f"bytes {start}-{end}/{len(VIDEO)}"
    assert r.headers["content-length"] == str(end - start + 1)


def test_open_range_is_capped_like_the_local_server(alice, monkeypatch):
    client, c = alice
    monkeypatch.setattr(ui, "MAX_OPEN_RANGE", 100)
    key = keys.captions_mp4(CONTI)
    _put(c, key, VIDEO)
    r = client.get(f"/files/{key}", headers={"Range": "bytes=5-"})
    assert r.status_code == 206 and r.content == VIDEO[5:105]


@pytest.mark.parametrize("header", ["bytes=20000-", "bytes=-0"])
def test_video_range_outside_is_416(video, header):
    client, key = video
    r = client.get(f"/files/{key}", headers={"Range": header})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(VIDEO)}"


@pytest.mark.parametrize("header", ["bytes=0-1,5-6", "items=0-1", "bytes=9-3"])
def test_ignored_range_forms_send_the_whole_file(video, header):
    client, key = video
    r = client.get(f"/files/{key}", headers={"Range": header})
    assert r.status_code == 200 and r.content == VIDEO


def test_non_video_ignores_range_like_the_local_server(alice):
    client, c = alice
    key = keys.html_of(CONTI)
    _put(c, key, b"0123456789")
    r = client.get(f"/files/{key}", headers={"Range": "bytes=0-1"})
    assert r.status_code == 200 and r.content == b"0123456789"


def test_empty_video(alice):
    client, c = alice
    key = keys.captions_mp4(CONTI)
    _put(c, key, b"")
    r = client.get(f"/files/{key}")
    assert r.status_code == 200 and r.content == b""
