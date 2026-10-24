"""읽기 전용 레퍼런스 API: 목록과 상세."""
import re

import pytest

from scripts import refstore
from tests.test_conti import home, make_frames  # noqa: F401 - fixtures
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture
from tests.typify_fixtures import CAT, write_video


@pytest.fixture
def client(home, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def get(client, path, status=200):
    code, data = client.json("GET", path)
    assert code == status, data
    return data


def video_path(home, vid):  # noqa: F811
    return home / "refs" / f"ref_{CAT}" / "videos" / f"{vid}.md"


def test_list_returns_all_roles_with_summary(client, home):  # noqa: F811
    write_video(home, "m00", role="mine", title="내 라면")
    make_frames(home, "a00", n=3)
    data = get(client, "/api/refs?category=mukbang")
    ids = {v["id"]: v for v in data["videos"]}
    expected = {f"a{i:02d}" for i in range(7)} | {f"b{i:02d}" for i in range(3)} | {"m00"}
    assert set(ids) == expected
    assert ids["m00"]["role"] == "mine"
    a = ids["a00"]
    assert a["thumb"] == "workspace/frames/a00/scene001.jpg"
    assert a["sections"][0] == {"label": "hook", "start": 0.0, "end": 3.0}
    assert a["sections"][1]["sub"] == "story"
    assert a["hook_type"] == "result_first" and a["duration_sec"] == 60
    assert ids["a01"]["thumb"] is None
    assert data["skipped"] == 0
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", a["added"])


def test_list_skips_broken_frontmatter(client, home):  # noqa: F811
    video_path(home, "a00").write_text("---\nid: [\n---\n", encoding="utf-8")
    data = get(client, "/api/refs?category=mukbang")
    assert "a00" not in {v["id"] for v in data["videos"]} and data["skipped"] == 1


def test_list_missing_or_unknown_category(client, home):  # noqa: F811
    assert get(client, "/api/refs", status=400)["error"] == "bad_request"
    assert get(client, "/api/refs?category=", status=400)["error"] == "bad_request"
    assert get(client, "/api/refs?category=nope", status=400)["error"] == "category_not_found"


def test_thumb_skips_deleted_frame_files(client, home):  # noqa: F811
    frames = make_frames(home, "a00", n=3)
    (home / frames[0]).unlink()
    v = next(v for v in get(client, "/api/refs?category=mukbang")["videos"] if v["id"] == "a00")
    assert v["thumb"] == frames[1]


def test_detail_body_frames_and_md_path(client, home):  # noqa: F811
    make_frames(home, "a00", n=4, length=60)
    d = get(client, "/api/refs/a00?category=mukbang")
    assert d["md"] == "refs/ref_mukbang/videos/a00.md"
    assert d["video"]["url"] == "https://youtu.be/a00"
    assert d["video"]["pacing"]["avg_cut_sec"] == 1.2
    assert d["video"]["camera"]["static"] == 0.9
    assert [f["section"] for f in d["frames"]] == ["body", "body", "body", "cta"]
    assert d["body"][0] == {"heading": "한 줄 요약", "text": "요약"}
    assert d["body"][-1]["heading"] == "내 메모"


def test_detail_body_keeps_html_as_text(client, home):  # noqa: F811
    path = video_path(home, "a00")
    fm, _ = refstore.read_md(path)
    path.write_bytes(refstore.join_frontmatter(fm, "intro\n\n## 내 메모\n<script>x</script>\n").encode("utf-8"))
    d = get(client, "/api/refs/a00?category=mukbang")
    assert d["body"][0] == {"heading": "", "text": "intro"}
    assert d["body"][-1] == {"heading": "내 메모", "text": "<script>x</script>"}


@pytest.mark.parametrize("vid", ["..%2Fx", "a%20b", "a00.md", "%00"])
def test_detail_rejects_bad_id(client, home, vid):  # noqa: F811
    assert get(client, f"/api/refs/{vid}?category=mukbang", status=400)["error"] == "bad_request"


def test_detail_not_found(client, home):  # noqa: F811
    assert get(client, "/api/refs/zzz?category=mukbang", status=400)["error"] == "video_not_found"


def test_detail_requires_category(client, home):  # noqa: F811
    assert get(client, "/api/refs/a00", status=400)["error"] == "bad_request"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://evil.example/", None, 5])
def test_detail_url_only_youtube_https(client, home, url):  # noqa: F811
    refstore.update_frontmatter(video_path(home, "a00"), {"url": url})
    assert get(client, "/api/refs/a00?category=mukbang")["video"]["url"] is None
