"""읽기 전용 레퍼런스 API: 목록과 상세."""
import json
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


@pytest.mark.parametrize("evil", ["workspace/frames/..\\..\\x.jpg", "workspace/frames/../../x.jpg"])
def test_frames_outside_frames_dir_are_ignored(client, home, evil):  # noqa: F811
    (home / "x.jpg").write_bytes(b"x")
    (home / "workspace" / "cuts").mkdir(parents=True, exist_ok=True)
    (home / "workspace" / "cuts" / "a00.json").write_text(
        json.dumps({"scenes": [{"start": 0, "end": 30, "frame": evil}]}), encoding="utf-8")
    v = next(v for v in get(client, "/api/refs?category=mukbang")["videos"] if v["id"] == "a00")
    assert v["thumb"] is None
    assert get(client, "/api/refs/a00?category=mukbang")["frames"] == []


def test_truncated_cuts_json_means_no_frames(client, home):  # noqa: F811
    make_frames(home, "a00", n=3)
    (home / "workspace" / "cuts" / "a00.json").write_text('{"scenes": [', encoding="utf-8")
    v = next(v for v in get(client, "/api/refs?category=mukbang")["videos"] if v["id"] == "a00")
    assert v["thumb"] is None
    assert get(client, "/api/refs/a00?category=mukbang")["frames"] == []


def test_non_dict_scenes_means_no_frames(client, home):  # noqa: F811
    (home / "workspace" / "cuts").mkdir(parents=True, exist_ok=True)
    (home / "workspace" / "cuts" / "a00.json").write_text(json.dumps({"scenes": ["x", 3]}), encoding="utf-8")
    assert get(client, "/api/refs/a00?category=mukbang")["frames"] == []


def test_bad_section_value_skips_in_list_and_400_in_detail(client, home):  # noqa: F811
    path = video_path(home, "a00")
    fm, body = refstore.read_md(path)
    fm["sections"][0]["start"] = "abc"
    path.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))
    data = get(client, "/api/refs?category=mukbang")
    assert data["skipped"] == 1 and "a00" not in {v["id"] for v in data["videos"]}
    assert get(client, "/api/refs/a00?category=mukbang", status=400)["error"] == "bad_md"


def test_non_dict_pacing_and_camera_become_none(client, home):  # noqa: F811
    refstore.update_frontmatter(video_path(home, "a00"), {"pacing": 3, "camera": "x"})
    v = get(client, "/api/refs/a00?category=mukbang")["video"]
    assert v["pacing"] is None and v["camera"] is None


def _strict_get(client, path, status=200):
    """Fetch raw and parse as strict JSON (NaN/Infinity tokens are rejected like a browser would)."""
    def reject(token):
        raise AssertionError(f"non-strict JSON token: {token}")

    code, data = client.json("GET", path)
    assert code == status, data
    json.loads(json.dumps(data, allow_nan=True), parse_constant=reject)
    return data


def test_nan_section_start_skips_video_and_response_is_strict_json(client, home):  # noqa: F811
    path = video_path(home, "a00")
    path.write_text(path.read_text(encoding="utf-8").replace("start: 3.0", "start: .nan", 1), encoding="utf-8")
    assert ".nan" in path.read_text(encoding="utf-8")
    data = _strict_get(client, "/api/refs?category=mukbang")
    assert data["skipped"] == 1 and "a00" not in {v["id"] for v in data["videos"]}
    assert get(client, "/api/refs/a00?category=mukbang", status=400)["error"] == "bad_md"


def test_non_finite_duration_pacing_camera_are_strict_json(client, home):  # noqa: F811
    refstore.update_frontmatter(video_path(home, "a00"), {
        "duration_sec": float("inf"), "pacing": {"avg_cut_sec": float("nan"), "by_section": {"hook": 1.0}},
        "camera": {"static": float("inf"), "pan": 0.1}})
    data = _strict_get(client, "/api/refs?category=mukbang")
    assert next(v for v in data["videos"] if v["id"] == "a00")["duration_sec"] is None
    d = _strict_get(client, "/api/refs/a00?category=mukbang")
    assert d["video"]["pacing"] == {"by_section": {"hook": 1.0}}
    assert d["video"]["camera"] == {"pan": 0.1}


def test_huge_int_values_do_not_break_list(client, home):  # noqa: F811
    path = video_path(home, "a00")
    path.write_text(path.read_text(encoding="utf-8").replace("start: 3.0", "start: " + "9" * 400, 1), encoding="utf-8")
    refstore.update_frontmatter(video_path(home, "a01"), {"duration_sec": 10 ** 400})
    data = _strict_get(client, "/api/refs?category=mukbang")
    vids = {v["id"]: v for v in data["videos"]}
    assert "a00" not in vids and data["skipped"] == 1
    assert vids["a01"]["duration_sec"] is None
    assert get(client, "/api/refs/a00?category=mukbang", status=400)["error"] == "bad_md"


def test_non_finite_scene_times_are_skipped(client, home):  # noqa: F811
    make_frames(home, "a00", n=2)
    cuts = home / "workspace" / "cuts" / "a00.json"
    data = json.loads(cuts.read_text(encoding="utf-8"))
    data["scenes"][0]["start"] = float("nan")
    cuts.write_text(json.dumps(data), encoding="utf-8")   # json.dumps writes NaN, which python json reads back
    d = _strict_get(client, "/api/refs/a00?category=mukbang")
    assert len(d["frames"]) == 1


def test_yaml_date_and_int_titles_become_strings(client, home):  # noqa: F811
    import datetime
    refstore.update_frontmatter(video_path(home, "a00"), {"title": datetime.date(2024, 5, 1)})
    refstore.update_frontmatter(video_path(home, "a01"), {"title": 1984, "channel": 7})
    vids = {v["id"]: v for v in get(client, "/api/refs?category=mukbang")["videos"]}
    assert vids["a00"]["title"] == "2024-05-01"
    assert vids["a01"]["title"] == "1984" and vids["a01"]["channel"] == "7"
    assert get(client, "/api/refs/a01?category=mukbang")["video"]["title"] == "1984"


def test_detail_non_utf8_md_is_bad_md(client, home):  # noqa: F811
    video_path(home, "a00").write_bytes(b"---\nid: a00\n---\n\xff\xfe\x80bad")
    assert get(client, "/api/refs/a00?category=mukbang", status=400)["error"] == "bad_md"


def test_bad_md_message_has_no_raw_exception_text(client, home):  # noqa: F811
    path = video_path(home, "a00")
    fm, body = refstore.read_md(path)
    fm["sections"][0]["start"] = "abc"
    path.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))
    d = get(client, "/api/refs/a00?category=mukbang", status=400)
    assert "abc" not in d["message"] and "could not convert" not in d["message"]
    assert "숫자" in d["message"]


def test_list_skips_stems_failing_id_re(client, home):  # noqa: F811
    good = video_path(home, "a00")
    (good.parent / "bad name.md").write_bytes(good.read_bytes())
    data = get(client, "/api/refs?category=mukbang")
    assert "bad name" not in {v["id"] for v in data["videos"]} and data["skipped"] == 1


def test_api_is_read_only(client, home):  # noqa: F811
    make_frames(home, "a00", n=3)
    files = sorted((home / "refs").rglob("*.md"))
    assert files
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    get(client, "/api/refs?category=mukbang")
    get(client, "/api/refs/a00?category=mukbang")
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    assert after == before


def test_send_backstop_returns_500_json_for_unserializable_payload(client, home, monkeypatch):  # noqa: F811
    from scripts.ui import server

    monkeypatch.setattr(server, "ROUTES", list(server.ROUTES))
    server.route("GET", r"/api/_nan_probe")(lambda req: {"x": float("nan")})
    code, data = client.json("GET", "/api/_nan_probe")
    assert code == 500 and data["error"] == "internal"


def _sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def post(client, path, body, status=200):
    code, data = client.json("POST", path, body)
    assert code == status, data
    return data


def test_detail_has_hash_and_vocab(client, home):  # noqa: F811
    from scripts import videomd
    d = get(client, "/api/refs/a00?category=mukbang")
    assert d["hash"] == _sha(video_path(home, "a00"))
    assert d["vocab"] == {"label": videomd.LABELS, "body_sub": videomd.BODY_SUBS,
                          "hook_type": videomd.HOOK_TYPES, "narrative_type": videomd.NARRATIVE_TYPES}


def test_post_labels_saves_and_returns_summary(client, home):  # noqa: F811
    d = get(client, "/api/refs/a00?category=mukbang")
    r = post(client, "/api/refs/a00/labels?category=mukbang", {"hash": d["hash"], "hook_type": "question"})
    assert r["video"]["hook_type"] == "question" and r["video"]["reviewed"] is True
    assert r["video"]["id"] == "a00" and r["changed"] == ["hook_type"]
    assert r["hash"] != d["hash"] and r["hash"] == _sha(video_path(home, "a00"))
    again = post(client, "/api/refs/a00/labels?category=mukbang", {"hash": d["hash"], "hook_type": "situation"}, 400)
    assert again["error"] == "ref_changed"


def test_post_labels_requires_hash(client, home):  # noqa: F811
    for body in ({"hook_type": "question"}, {"hash": "", "hook_type": "question"}, {"hash": 5}):
        assert post(client, "/api/refs/a00/labels?category=mukbang", body, 400)["error"] == "bad_request"


def test_post_labels_bad_id_and_category(client, home):  # noqa: F811
    assert post(client, "/api/refs/a.b/labels?category=mukbang", {"hash": "x"}, 400)["error"] == "bad_request"
    assert post(client, "/api/refs/a00/labels?category=nope", {"hash": "x"}, 400)["error"] == "category_not_found"


def test_post_labels_returns_null_video_when_summary_fails(client, home, monkeypatch):  # noqa: F811
    from scripts.ui import api_refs
    d = get(client, "/api/refs/a00?category=mukbang")

    def boom(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(api_refs, "_summary", boom)
    r = post(client, "/api/refs/a00/labels?category=mukbang", {"hash": d["hash"], "hook_type": "question"})
    assert r["video"] is None and r["changed"] == ["hook_type"]
