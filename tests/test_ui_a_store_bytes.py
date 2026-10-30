"""UI API A(refs·add·typify·motif·help)의 응답 JSON·핸드오프 파일 바이트·프레임 탈출 검사를 저장소 이전 전후로 고정한다."""
import json
import os
from pathlib import Path

import pytest

from scripts import cluster, motif, refstore, shots, videomd, llm
from scripts.ui import api_add, api_help
from tests.test_conti import home, make_frames  # noqa: F401 - fixtures
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture
from tests.typify_fixtures import CAT

FIXED_MTIME = 1_700_000_000          # 2023-11-14T22:13:20Z


@pytest.fixture
def client(home, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def _video(home, vid):  # noqa: F811
    return home / "refs" / f"ref_{CAT}" / "videos" / f"{vid}.md"


# ---- refs: 목록·상세·labels 응답 -----------------------------------------------------------

def test_refs_list_added_is_file_mtime_utc(client, home):  # noqa: F811
    os.utime(_video(home, "a00"), (FIXED_MTIME, FIXED_MTIME))
    code, data = client.json("GET", f"/api/refs?category={CAT}")
    assert code == 200
    by_id = {v["id"]: v for v in data["videos"]}
    assert by_id["a00"]["added"] == "2023-11-14T22:13:20Z"
    assert [v["id"] for v in data["videos"]] == sorted(by_id)            # 파일 이름 순서


def test_refs_detail_md_and_hash_are_key_and_sha256(client, home):  # noqa: F811
    import hashlib
    make_frames(home, "a00", n=3)
    code, d = client.json("GET", f"/api/refs/a00?category={CAT}")
    assert code == 200
    assert d["md"] == f"refs/ref_{CAT}/videos/a00.md"
    assert d["hash"] == hashlib.sha256(_video(home, "a00").read_bytes()).hexdigest()
    assert [f["frame"] for f in d["frames"]] == [f"workspace/frames/a00/scene{i:03d}.jpg" for i in (1, 2, 3)]
    assert d["video"]["thumb"] == "workspace/frames/a00/scene001.jpg"


def test_refs_detail_unreadable_md_is_bad_md_and_missing_is_not_found(client, home):  # noqa: F811
    code, d = client.json("GET", f"/api/refs/zzz?category={CAT}")
    assert code == 400 and d["error"] == "video_not_found" and d["message"] == "영상 md가 없습니다: zzz"


def test_refs_detail_parse_error_message_has_full_path_prefix(client, home):  # noqa: F811
    _video(home, "a00").write_text("---\nid: [\n---\n", encoding="utf-8")
    code, d = client.json("GET", f"/api/refs/a00?category={CAT}")
    assert code == 400 and d["message"].startswith(str(_video(home, "a00")))


def _write_cuts(home, vid, frames):  # noqa: F811
    scenes = [{"start": i * 10.0, "end": (i + 1) * 10.0, "frame": f} for i, f in enumerate(frames)]
    path = home / "workspace" / "cuts" / f"{vid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": vid, "scenes": scenes}), encoding="utf-8")


def test_refs_frames_with_escape_or_missing_files_are_dropped(client, home):  # noqa: F811
    make_frames(home, "a00", n=1)
    (home / "workspace" / "secret.jpg").write_bytes(b"x")
    ok = "workspace/frames/a00/scene001.jpg"
    detour = "workspace/frames/a00/../a00/scene001.jpg"          # resolve하면 안쪽: 지금은 통과
    _write_cuts(home, "a00", [
        ok, detour, "workspace/frames/a00/../../secret.jpg", "workspace/frames/a00/none.jpg",
        "workspace/frames/a00", "refs/x.jpg", "workspace/frames/a00\\scene001.jpg", 5,
    ])
    code, d = client.json("GET", f"/api/refs/a00?category={CAT}")
    assert code == 200
    assert [f["frame"] for f in d["frames"]] == [ok, detour]


def test_refs_symlinked_frame_outside_frames_is_dropped(client, home):  # noqa: F811
    make_frames(home, "a00", n=1)
    secret = home / "workspace" / "secret.jpg"
    secret.write_bytes(b"x")
    link = home / "workspace" / "frames" / "a00" / "link.jpg"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("심볼릭 링크를 만들 수 없는 환경")
    _write_cuts(home, "a00", ["workspace/frames/a00/scene001.jpg", "workspace/frames/a00/link.jpg"])
    code, d = client.json("GET", f"/api/refs/a00?category={CAT}")
    assert [f["frame"] for f in d["frames"]] == ["workspace/frames/a00/scene001.jpg"]


def test_refs_labels_response_video_summary(client, home):  # noqa: F811
    os.utime(_video(home, "a00"), (FIXED_MTIME, FIXED_MTIME))
    _, d = client.json("GET", f"/api/refs/a00?category={CAT}")
    code, r = client.json("POST", f"/api/refs/a00/labels?category={CAT}", {"hash": d["hash"], "hook_type": "question"})
    assert code == 200
    assert set(r) >= {"video", "hash", "changed"}
    assert r["video"]["id"] == "a00" and r["video"]["hook_type"] == "question"
    assert r["video"]["added"] != "2023-11-14T22:13:20Z"            # 다시 썼으니 mtime이 바뀐다


# ---- 핸드오프 tmp 파일 ---------------------------------------------------------------------

NOTES = {"axes": {"hook": "한글 근거\n두 줄"}, "archetypes": [{"id": "A1", "name": "이름 😀"}]}


def test_typify_save_hands_notes_json_over_a_tmp_file(client, home, monkeypatch):  # noqa: F811
    seen = {}

    def fake_apply(category, notes_path, **kw):
        p = Path(notes_path)
        seen.update(path=p, bytes=p.read_bytes(), kw=kw)
        return {"status": "applied"}

    monkeypatch.setattr(cluster, "apply", fake_apply)
    code, body = client.json("POST", "/api/typify/save", {"category": CAT, "run_at": "r", "notes": NOTES})
    assert code == 200 and body == {"status": "applied", "applied": None}
    assert seen["path"].parent == home / "workspace" / "tmp" and seen["path"].name.startswith("typify_notes_")
    assert seen["path"].suffix == ".json"
    assert seen["bytes"] == refstore.text_bytes(json.dumps(NOTES, ensure_ascii=False))
    assert seen["kw"] == {"expected_run_at": "r", "source": cluster.preview_key(CAT)}
    assert not seen["path"].exists()


def test_motif_save_hands_cards_json_over_a_tmp_file(client, home, monkeypatch):  # noqa: F811
    seen = {}
    cards = [{"type": "technique", "title": "카드\n제목", "idea": "아", "evidence": ["a00"]}]

    def fake_add(category, cards_path, allowed):
        p = Path(cards_path)
        seen.update(path=p, bytes=p.read_bytes())
        return {"ids": ["M-mukbang-001"]}

    monkeypatch.setattr(motif, "add", fake_add)
    monkeypatch.setattr(motif, "card_details", lambda category: [])
    code, body = client.json("POST", "/api/motif/save", {"category": CAT, "cards": cards})
    assert code == 200 and body == {"ids": ["M-mukbang-001"], "starred": [], "cards": []}
    assert seen["path"].parent == home / "workspace" / "tmp" and seen["path"].name.startswith("motif_cards_")
    assert seen["bytes"] == refstore.text_bytes(json.dumps(cards, ensure_ascii=False))
    assert not seen["path"].exists()


def test_add_write_hands_analysis_json_over_a_tmp_file(home, monkeypatch):  # noqa: F811
    seen = {}
    analysis = {"summary": "요약\n줄", "sections": [{"label": "hook", "start": 0, "end": 3}], "t": "😀"}

    def fake_write(video_id, category, role, path, force):
        seen.update(path=path, bytes=Path(path).read_bytes(), args=(video_id, category, role, force))
        return {"status": "written"}

    monkeypatch.setattr(videomd, "write", fake_write)
    opts = api_add.AddOptions(category=CAT, role="ref")
    assert api_add._write("AAAAAAAAAAA", opts, analysis) == {"status": "written"}
    assert str(seen["path"]) == str(home / "workspace" / "tmp" / "analysis_AAAAAAAAAAA.json")
    assert seen["bytes"] == refstore.text_bytes(json.dumps(analysis, ensure_ascii=False, indent=1))
    assert seen["args"] == ("AAAAAAAAAAA", CAT, "ref", False)
    assert not Path(seen["path"]).exists()


def test_add_shots_and_describe_hand_labels_json_over_a_tmp_file(home, monkeypatch):  # noqa: F811
    seen = {}
    labels = [{"frame": "workspace/frames/AAAAAAAAAAA/s1.jpg", "size": "바스트"}]
    frame = "workspace/frames/AAAAAAAAAAA/s1.jpg"

    def fake_ask(rules, ctx, task, apply, files=None):
        seen["files"] = files
        return {}, apply({"labels": labels})

    def fake_apply(video_id, category, path):
        seen["bytes"] = Path(path).read_bytes()
        seen["name"] = Path(path).name
        return {"status": "applied"}

    monkeypatch.setattr(llm, "ask_validated", fake_ask)
    monkeypatch.setattr(shots, "context", lambda v, c: {"frames": [{"frame": frame}]})
    monkeypatch.setattr(shots, "apply", fake_apply)
    assert api_add._classify_shots("AAAAAAAAAAA", CAT) == {"status": "applied"}
    assert seen["name"] == "shots_AAAAAAAAAAA.json"
    assert seen["bytes"] == refstore.text_bytes(json.dumps(labels, ensure_ascii=False))
    assert [_rel(f, home) for f in seen["files"]] == [frame]
    assert not (home / "workspace" / "tmp" / "shots_AAAAAAAAAAA.json").exists()

    monkeypatch.setattr(shots, "describe_context", lambda v, c, limit: {"frames": [{"frame": frame}]})
    monkeypatch.setattr(shots, "apply_desc", fake_apply)
    assert api_add._describe_batch("AAAAAAAAAAA", CAT) == {"status": "applied"}
    assert seen["name"] == "desc_AAAAAAAAAAA.json"
    assert seen["bytes"] == refstore.text_bytes(json.dumps(labels, ensure_ascii=False))
    assert [_rel(f, home) for f in seen["files"]] == [frame]
    assert not (home / "workspace" / "tmp" / "desc_AAAAAAAAAAA.json").exists()


def _rel(f, home):  # noqa: F811
    return f.relative_to(home).as_posix() if isinstance(f, Path) else f


# ---- help ---------------------------------------------------------------------------------

def test_help_page_title_and_markdown_come_from_the_plugin_file(client):
    page = api_help.SCREEN_PAGES[0]
    code, d = client.json("GET", f"/api/help/{page}")
    assert code == 200 and set(d) == {"page", "title", "markdown"} and d["page"] == page
    fm, body = refstore.parse_md((api_help.HELP_DIR / f"{page}.md").read_bytes(), "x")
    assert d["markdown"] == body and d["title"] == str(fm.get("title", page))
