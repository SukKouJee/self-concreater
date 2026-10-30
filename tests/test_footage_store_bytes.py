"""S3 Task 9 특성(characterization) 테스트: 촬영 영상 캐시·판정·결과물·자막 영상·장면 합치기가 디스크에 남기는 바이트와
돌려주는 문자열. 저장소 이전 전후에 똑같이 통과해야 한다(CRLF/LF/BOM, 경로 문자열, 오류 코드)."""
from __future__ import annotations

import csv
import io
import json

import pytest

from scripts import conti, footage, scene_merge, stt
from scripts import footage_match as fm
from scripts import footage_out
from scripts.common import ScriptError
from tests.helpers import tree_snapshot
from tests.test_conti import conti_file, draft_md, home  # noqa: F401 - fixtures
from tests.test_cuts import sample_video  # noqa: F401 - fixture
from tests.test_footage_out import chosen, saved, seed, shoot, two_cuts, av_video  # noqa: F401 - fixtures
from tests.test_scene_merge import REPEAT, _applied, ids_of, repeat_conti, split  # noqa: F401 - fixtures


def jdump(data: object) -> bytes:
    """footage.py·footage_match.py가 쓰는 모양(LF, indent=2, 한글 그대로, 끝 줄바꿈 없음)."""
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


# --- footage: 처리 캐시 ---------------------------------------------------------------------

def test_process_file_cache_layout_and_bytes(home, sample_video):
    out = footage.process_file(sample_video, run_stt=False)
    fid = out["fid"]
    base = home / "workspace" / "footage" / fid
    files = tree_snapshot.tree(home)
    names = sorted(k for k in files if k.startswith(f"workspace/footage/{fid}/"))
    assert f"workspace/footage/{fid}/scenes.json" in names
    assert not [n for n in names if n.endswith(".tmp")]
    for scene in out["scenes"]:
        assert all(f"workspace/footage/{fid}/{rel}" in names for rel in [*scene["frames"], scene["montage"]])
        assert all(rel.startswith("frames/s") and rel.endswith(".jpg") for rel in scene["frames"])
        assert (base / scene["montage"]).read_bytes().startswith(b"\xff\xd8")
    raw = (base / "scenes.json").read_bytes()
    record = json.loads(raw)
    assert raw == jdump(record) and b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    assert record["schema"] == 1 and record["fid"] == fid and record["status"] == "done"
    assert {k: v for k, v in record.items() if k != "schema"} == {k: v for k, v in out.items() if k != "transcript"}
    footage.process_file(sample_video, run_stt=False)                     # 캐시: 바이트가 그대로
    assert tree_snapshot.tree(home) == files


def test_transcript_cache_bytes(home, sample_video, monkeypatch):
    segs = [{"start": 0.5, "end": 1.25, "text": "안녕하세요 🙂"}]
    monkeypatch.setattr(stt, "transcribe_file", lambda path, lang, progress=None: segs)
    out = footage.process_file(sample_video)
    base = home / "workspace" / "footage" / out["fid"]
    assert (base / "transcript.json").read_bytes() == jdump({"segments": segs})
    assert out["transcript"] == segs


def test_unreadable_record_bytes(home, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all")
    out = footage.process_file(bad, run_stt=False)
    assert out["status"] == "unreadable"
    raw = (home / "workspace" / "footage" / out["fid"] / "scenes.json").read_bytes()
    assert raw == jdump({"schema": 1, "fid": out["fid"], "name": "bad.mp4", "status": "unreadable",
                         "reason": out["reason"], "duration_sec": None, "scenes": []})
    assert not list((home / "workspace" / "footage" / out["fid"]).glob("frames/*"))


def test_corrupt_cache_is_redone_and_cached_result_reads_it(home, sample_video):
    first = footage.process_file(sample_video, run_stt=False)
    path = home / "workspace" / "footage" / first["fid"] / "scenes.json"
    good = path.read_bytes()
    path.write_bytes(b"{half")
    assert footage.cached_result(first["fid"]) is None
    assert footage.process_file(sample_video, run_stt=False) == first
    assert path.read_bytes() == good


@pytest.mark.parametrize("fid", ["../x", "abc", "0123456789ab-1", "0123456789AB-1-2", 5, None])
def test_footage_dir_validation_error(home, fid):
    with pytest.raises(ScriptError) as e:
        footage.footage_dir(fid)
    assert e.value.code == "bad_fid" and e.value.message == "잘못된 파일 id입니다"


# --- footage_match: 판정 파일 ---------------------------------------------------------------

def test_state_file_location_and_bytes(saved, home):
    state = {"folders": ["D:/촬영"], "cuts": {"c1": {"kind": "pinned", "user": True}}, "suggestions": [],
             "images_used": 3}
    fm.save_state(saved, state)
    stem = saved.stem
    target = home / "workspace" / "footage_match" / f"{stem}.json"
    assert target.read_bytes() == jdump(state)
    assert fm.load_state(saved) == state
    assert fm.load_state(f"drafts/{saved.name}") == state                 # 키로 불러도 같은 파일(콘티 이름만 쓴다)
    assert not list(target.parent.glob("*.tmp"))


def test_state_missing_is_empty_and_bad_state_errors(saved, home):
    assert fm.load_state(saved) == {"folders": [], "cuts": {}, "suggestions": [], "images_used": 0}
    target = home / "workspace" / "footage_match" / f"{saved.stem}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"{half")
    with pytest.raises(ScriptError) as e:
        fm.load_state(saved)
    assert e.value.code == "bad_state" and e.value.message.startswith(f"촬영 영상 판정 파일을 읽지 못했습니다: {target.name}: ")
    target.write_bytes(b"[1]")
    with pytest.raises(ScriptError) as e:
        fm.load_state(saved)
    assert e.value.message == f"촬영 영상 판정 파일의 형식이 맞지 않습니다: {target.name}"
    target.write_bytes(jdump({"cuts": [], "folders": []}))
    with pytest.raises(ScriptError) as e:
        fm.load_state(saved)
    assert e.value.code == "bad_state"


# --- footage_out: CSV · HTML · 컷 파일 --------------------------------------------------------

def test_csv_bytes_bom_and_crlf(scene_setup_fixture, home):
    path, folder, f = scene_setup_fixture
    rel = footage_out.csv_export(path)
    raw = (home / rel).read_bytes()
    buf = io.StringIO()
    present = footage_out.present_sources(fm.load_state(path)["folders"])
    fmx, cuts, _ = footage_out._load(path)
    csv.writer(buf).writerows(footage_out.csv_rows(fmx, cuts, present))
    assert raw == b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
    assert raw.count(b"\r\n") == 5 and raw.count(b"\n") == 5            # 머리 + 컷 4개, 모두 CRLF
    assert not list((home / "drafts").glob("*.tmp"))


def test_remaining_html_bytes_are_lf_and_stable(scene_setup_fixture, home):
    path, _, _ = scene_setup_fixture
    rel = footage_out.remaining_html(path, now=1_700_000_000.0)
    assert rel == "drafts/" + path.name.replace("-conti.md", "-remaining.html")
    raw = (home / rel).read_bytes()
    assert b"\r" not in raw and raw.startswith(b"<!doctype html>") and not raw.startswith(b"\xef\xbb\xbf")
    footage_out.remaining_html(path, now=1_700_000_000.0)
    assert (home / rel).read_bytes() == raw
    assert not list((home / "drafts").glob("*.tmp"))


def test_remaining_html_embed_uses_html_folder_for_urls(scene_setup_fixture, home, monkeypatch):
    path, _, _ = scene_setup_fixture
    frame = home / "workspace" / "frames" / "x" / "0001.jpg"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"\xff\xd8x")
    ref = {"frame": "workspace/frames/x/0001.jpg", "title": "t", "match": "content"}
    monkeypatch.setattr(conti, "cut_refs", lambda fmx: {c["n"]: ref for c in fmx["cuts"]})
    text = (home / footage_out.remaining_html(path, now=1.0)).read_text("utf-8")
    assert "url(../workspace/frames/x/0001.jpg)" in text
    embedded = (home / footage_out.remaining_html(path, now=1.0, embed=True)).read_text("utf-8")
    assert "data:image/jpeg;base64," in embedded and "../workspace" not in embedded


def test_clips_result_keys_and_folder_contents(two_cuts, home):
    result = footage_out.export_clips(two_cuts)
    base = two_cuts.name.replace("-conti.md", "")
    assert result["dir"] == f"drafts/{base}-cuts"
    assert [c["file"] for c in result["clips"]] == [f"drafts/{base}-cuts/01_talk.mp4", f"drafts/{base}-cuts/02_talk.mp4"]
    folder = home / "drafts" / f"{base}-cuts"
    assert sorted(p.name for p in folder.iterdir()) == ["01_talk.mp4", "02_talk.mp4"]
    (folder / "notes.txt").write_bytes(b"keep")
    (folder / "03_old.mov").write_bytes(b"old")
    footage_out.export_clips(two_cuts)
    assert sorted(p.name for p in folder.iterdir()) == ["01_talk.mp4", "02_talk.mp4", "notes.txt"]


def test_conti_trash_moves_cut_folder_and_state(two_cuts, home):
    footage_out.export_clips(two_cuts)
    fm.save_state(two_cuts, {**fm.load_state(two_cuts), "images_used": 1})
    base = two_cuts.name.replace("-conti.md", "")
    out = conti.trash(f"drafts/{two_cuts.name}")
    moved = out["moved"]
    assert f"drafts/{base}-cuts" in moved and f"workspace/footage_match/{two_cuts.stem}.json" in moved
    assert not (home / "drafts" / f"{base}-cuts").exists()
    assert (home / out["trash"] / "drafts" / f"{base}-cuts" / "01_talk.mp4").is_file()


# --- scene_merge: 문자열·바이트 ---------------------------------------------------------------

def test_merge_result_strings_and_state_bytes(repeat_conti, home):
    ids = ids_of(repeat_conti)
    pinned = {"kind": "pinned", "user": True, "choice": {"fid": "f", "start": 1.0, "end": 2.0}}
    fm.save_state(repeat_conti, {"folders": ["x"], "suggestions": [], "cuts": {ids[3]: pinned}})
    before = tree_snapshot.tree(home)
    plan, out = _applied(repeat_conti)
    key = f"drafts/{repeat_conti.name}"
    assert plan["path"] == key and out["path"] == key
    assert out["html"] == key.replace("-conti.md", "-conti.html")
    assert out["abs_html"] == str(home / out["html"])
    state_key = f"workspace/footage_match/{repeat_conti.stem}.json"
    assert (home / state_key).read_bytes() == jdump(fm.load_state(repeat_conti))
    assert fm.load_state(repeat_conti)["cuts"] == {ids[1]: pinned, ids[3]: pinned}
    changed = tree_snapshot.changes(before, tree_snapshot.tree(home))
    assert key in changed["modified"] and state_key in changed["modified"]
    assert out["html"] in changed["added"] or out["html"] in changed["modified"]
    assert any(p.startswith(f"workspace/conti_history/{repeat_conti.stem}/") for p in changed["added"])


def test_merge_outside_and_missing_conti_errors(home, repeat_conti, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "x-conti.md"
    outside.write_bytes(repeat_conti.read_bytes())
    with pytest.raises(ScriptError) as e:
        scene_merge.plan(outside)
    assert e.value.code == "bad_path"
    with pytest.raises(ScriptError) as e:
        scene_merge.plan("drafts/none-conti.md")
    assert e.value.code == "conti_not_found"
    with pytest.raises(ScriptError) as e:
        scene_merge.apply("drafts/none-conti.md", [], "h")
    assert e.value.code == "conti_not_found"


def test_merge_cli_reads_relative_and_absolute_cuts_files(repeat_conti, home):
    plan = scene_merge.run_cli(f"drafts/{repeat_conti.name}", None, None)
    cuts = scene_merge.validate_split(plan, split(plan, [1, 0], ["하나 둘", "셋 넷"]))
    rel = "workspace/tmp/merge.json"
    (home / rel).parent.mkdir(parents=True, exist_ok=True)
    (home / rel).write_text(json.dumps({"cuts": cuts}, ensure_ascii=False), encoding="utf-8")
    out = scene_merge.run_cli(f"drafts/{repeat_conti.name}", rel, plan["hash"])
    assert out["status"] == "merged" and out["after"] == 4
    with pytest.raises(ScriptError) as e:
        scene_merge.run_cli(f"drafts/{repeat_conti.name}", "workspace/tmp/none.json", out["hash"])
    assert e.value.code == "invalid_merge" and e.value.message.startswith("합친 컷 JSON을 읽을 수 없습니다: ")
    with pytest.raises(ScriptError) as e:
        scene_merge.run_cli(f"drafts/{repeat_conti.name}", str(home / rel), None)
    assert e.value.code == "bad_request"


@pytest.fixture
def scene_setup_fixture(saved, tmp_path, sample_video):  # noqa: F811
    from tests.test_footage_out import cached, review
    folder, [f] = shoot(tmp_path, sample_video)
    cached(f["fid"], f["name"])
    seed(saved, folder, {0: chosen(f["fid"], 1.0, 3.0), 1: review(f["fid"], 2.0, 4.0),
                         3: {"kind": "not_filmed", "choice": None, "candidates": [], "user": True}})
    return saved, folder, f

