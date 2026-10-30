"""영상 md·기법 카탈로그·유형화 JSON·진단 md의 바이트와 부수 효과를 고정한다(S3 Task 4 특성 테스트).

저장소로 옮기기 전 코드에서 통과해야 하고, 옮긴 뒤에도 같은 바이트여야 한다.
`write_text` 기본 줄바꿈(Windows CRLF)은 기대값에서 `os.linesep`으로 직접 계산한다.
"""
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from scripts import cluster, features, refstore, similarity, videomd
from scripts.common import ScriptError
from tests.helpers.tree_snapshot import changes, tree
from tests.test_videomd import ANALYSIS, CAT as VCAT, CUTS, RAW, VID
from tests.typify_fixtures import CAT, standard_set, write_video

MD_KEY = f"refs/ref_{VCAT}/videos/{VID}.md"
TECH_KEY = f"refs/ref_{VCAT}/techniques.md"
INDEX_KEY = f"refs/ref_{VCAT}/index.md"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def native(text: str) -> bytes:
    """`Path.write_text(text, encoding="utf-8")`가 쓰는 바이트."""
    return text.replace("\n", os.linesep).encode("utf-8")


@pytest.fixture
def vhome(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from scripts import fetch
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    refstore.init_category(VCAT)
    fetch.save_raw(RAW)
    cuts_path = tmp_path / "workspace" / "cuts" / f"{VID}.json"
    cuts_path.parent.mkdir(parents=True, exist_ok=True)
    cuts_path.write_text(json.dumps(CUTS), encoding="utf-8")
    return tmp_path


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


def analysis(home: Path, data: dict | None = None, name: str = VID) -> Path:
    path = home / "workspace" / "tmp" / f"analysis_{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data or ANALYSIS, ensure_ascii=False), encoding="utf-8")
    return path


# --- videomd.write ------------------------------------------------------------------

def test_write_fresh_md_is_lf_and_touches_only_md_catalog_index(vhome: Path) -> None:
    src = analysis(vhome)
    before = tree(vhome)
    out = videomd.write(VID, VCAT, "ref", src)
    diff = changes(before, tree(vhome))
    assert out["md"] == MD_KEY and out["status"] == "written"
    assert diff["added"] == [MD_KEY]
    assert diff["removed"] == ["workspace/tmp/analysis_dQw4w9WgXcQ.json"]      # 분석 JSON은 소비된다
    assert set(diff["modified"]) <= {TECH_KEY, INDEX_KEY}
    raw = (vhome / MD_KEY).read_bytes()
    assert raw.startswith(b"---\n") and b"\r" not in raw
    tech = (vhome / TECH_KEY).read_bytes().decode("utf-8")
    assert f"- **결과 선공개**: 완성 장면을 첫 1초에 보여 준다 — [{VID}](videos/{VID}.md)" in tech
    assert out["techniques_added"] == 2 and out["techniques_merged"] == 0


def test_write_existing_md_is_untouched_without_force(vhome: Path) -> None:
    videomd.write(VID, VCAT, "ref", analysis(vhome))
    before = tree(vhome)
    out = videomd.write(VID, VCAT, "ref", analysis(vhome))
    assert out == {"id": VID, "status": "exists", "md": MD_KEY}
    assert changes(before, tree(vhome))["modified"] == []


def test_force_rewrite_keeps_memo_bytes_and_regenerates_the_rest(vhome: Path) -> None:
    videomd.write(VID, VCAT, "ref", analysis(vhome))
    path = vhome / MD_KEY
    memo = "훅이 좋아서 따라 해 볼 것 🎬\n- 다음엔 자막 색도 볼 것\n"
    path.write_bytes(path.read_bytes() + memo.encode("utf-8"))
    before = tree(vhome)
    videomd.write(VID, VCAT, "ref", analysis(vhome, {**ANALYSIS, "summary": "새 요약"}), force=True)
    raw = path.read_bytes()
    assert raw.endswith(("## 내 메모\n" + memo).encode("utf-8"))
    assert "새 요약".encode() in raw and b"\r" not in raw
    diff = changes(before, tree(vhome))
    assert diff["added"] == [] and MD_KEY in diff["modified"]
    # 기법 출처는 한 번만 남는다
    assert (vhome / TECH_KEY).read_bytes().decode("utf-8").count(f"[{VID}](videos/{VID}.md)") == 2


def test_force_rewrite_of_crlf_md_stays_crlf_with_memo(vhome: Path) -> None:
    videomd.write(VID, VCAT, "ref", analysis(vhome))
    path = vhome / MD_KEY
    path.write_bytes((path.read_bytes().decode("utf-8") + "메모장 메모\n").replace("\n", "\r\n").encode("utf-8"))
    videomd.write(VID, VCAT, "ref", analysis(vhome), force=True)
    raw = path.read_bytes().decode("utf-8")
    assert raw.startswith("---\r\n") and raw.endswith("## 내 메모\r\n메모장 메모\r\n")
    assert "\n" not in raw.replace("\r\n", "")


def test_force_rewrite_of_bom_crlf_md_becomes_lf_without_bom(vhome: Path) -> None:
    """현재 규칙: 줄바꿈은 디스크 바이트가 `---\\r\\n`으로 시작하는지로 정한다(BOM이 있으면 LF)."""
    videomd.write(VID, VCAT, "ref", analysis(vhome))
    path = vhome / MD_KEY
    text = (path.read_bytes().decode("utf-8") + "BOM 메모\n").replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    videomd.write(VID, VCAT, "ref", analysis(vhome), force=True)
    raw = path.read_bytes()
    assert raw.startswith(b"---\n") and b"\r" not in raw
    assert raw.endswith("## 내 메모\nBOM 메모\n".encode("utf-8"))


def test_write_cli_resolves_relative_analysis_against_work_folder(vhome: Path) -> None:
    analysis(vhome)
    out = videomd.main(["write", VID, "--category", VCAT, "--analysis", f"workspace/tmp/analysis_{VID}.json"])
    assert out["md"] == MD_KEY and not (vhome / "workspace/tmp" / f"analysis_{VID}.json").exists()


# --- videomd.context / examples ------------------------------------------------------

def test_examples_are_reviewed_refs_newest_first_max_three(home: Path) -> None:
    refstore.init_category(CAT)
    now = time.time()
    for i, vid in enumerate(["a1", "b1", "c1", "d1", "e1"]):
        path = write_video(home, vid)
        fm, body = refstore.read_md(path)
        refstore.update_frontmatter(path, {"reviewed": vid != "e1", "role": "mine" if vid == "d1" else "ref"})
        os.utime(path, (now + i * 10, now + i * 10))
    assert videomd.examples(CAT) == [f"refs/ref_{CAT}/videos/{v}.md" for v in ("c1", "b1", "a1")]


def test_context_json_shape_and_md_exists(vhome: Path) -> None:
    out = videomd.main(["context", VID, "--category", VCAT])
    assert out["md_exists"] is False and out["examples"] == [] and out["avg_cut_sec"] == 7.5
    videomd.write(VID, VCAT, "ref", analysis(vhome))
    assert videomd.main(["context", VID, "--category", VCAT])["md_exists"] is True
    assert videomd.load_cuts("nope0000000") is None
    assert videomd.load_cuts(VID)["scene_count"] == 4


# --- relabel ------------------------------------------------------------------------

def _written(vhome: Path) -> tuple[Path, str]:
    videomd.write(VID, VCAT, "ref", analysis(vhome))
    path = vhome / MD_KEY
    return path, sha(path.read_bytes())


def test_relabel_changes_only_that_md_and_returns_new_hash(vhome: Path) -> None:
    path, h0 = _written(vhome)
    before = tree(vhome)
    out = videomd.relabel(VID, VCAT, {"hook_type": "question"}, h0)
    diff = changes(before, tree(vhome))
    assert diff == {"added": [], "removed": [], "modified": [MD_KEY]}
    assert out["md"] == MD_KEY and out["changed"] == ["hook_type"] and out["reviewed"] is True
    assert out["hash"] == sha(path.read_bytes()) != h0


def test_relabel_stale_hash_wins_over_bad_labels_and_leaves_tree(vhome: Path) -> None:
    path, h0 = _written(vhome)
    path.write_bytes(path.read_bytes() + "사용자가 고침\n".encode("utf-8"))
    before = tree(vhome)
    with pytest.raises(ScriptError) as exc:
        videomd.relabel(VID, VCAT, {"hook_type": "nonsense"}, h0)
    assert exc.value.code == "ref_changed"
    assert exc.value.message == "화면을 연 뒤 md가 바뀌었습니다. 다시 불러온 뒤 고쳐 주세요"
    assert tree(vhome) == before


def test_relabel_noop_returns_expected_hash_without_writing(vhome: Path) -> None:
    path, h0 = _written(vhome)
    out = videomd.relabel(VID, VCAT, {"hook_type": "result_first"}, h0)
    assert out["changed"] == [] and out["hash"] == h0 and sha(path.read_bytes()) == h0


def test_relabel_hash_matches_bom_file_bytes(vhome: Path) -> None:
    """해시는 BOM을 포함한 디스크 바이트의 sha256이다."""
    path, _ = _written(vhome)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    h = sha(path.read_bytes())
    out = videomd.relabel(VID, VCAT, {"narrative_type": "list"}, h)
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert out["hash"] == sha(path.read_bytes())


# --- trash_video --------------------------------------------------------------------

def _data(home: Path, vid: str) -> None:
    for sub, name, data in (("raw", f"{vid}.json", "{}"), ("cuts", f"{vid}.json", "{}"), ("shots", f"{vid}.json", "{}")):
        p = home / "workspace" / sub / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")
    frames = home / "workspace" / "frames" / vid
    frames.mkdir(parents=True, exist_ok=True)
    (frames / "0001.jpg").write_bytes(b"jpg")


def test_trash_video_tree_and_result(home: Path) -> None:
    refstore.init_category(CAT)
    write_video(home, "a00")
    write_video(home, "a01")
    _data(home, "a00")
    _data(home, "a01")
    (home / "workspace" / "typify").mkdir(parents=True)
    (home / "workspace" / "typify" / f"{CAT}.json").write_text(
        json.dumps({"archetypes": [{"members": ["a00"]}], "outliers": [], "axes": {}}), encoding="utf-8")
    before = tree(home)
    out = videomd.trash_video("a00", CAT)
    diff = changes(before, tree(home))
    stamp = out["trash"]
    assert stamp.startswith("workspace/trash/")
    assert out["moved"][-1] == f"refs/ref_{CAT}/videos/a00.md" and len(out["moved"]) == 5
    assert sorted(diff["removed"]) == sorted([
        f"refs/ref_{CAT}/videos/a00.md", "workspace/raw/a00.json", "workspace/cuts/a00.json",
        "workspace/shots/a00.json", "workspace/frames/a00/0001.jpg"])
    assert sorted(diff["added"]) == sorted(f"{stamp}/{k}" for k in (
        f"refs/ref_{CAT}/videos/a00.md", "workspace/raw/a00.json", "workspace/cuts/a00.json",
        "workspace/shots/a00.json", "workspace/frames/a00/0001.jpg"))
    assert (out["kept_data"], out["typified"], out["techniques_stale"]) == (False, True, False)
    assert set(out) == {"trash", "moved", "kept_data", "typified", "techniques_stale"}


def test_trash_video_keeps_data_when_another_category_has_the_id(home: Path) -> None:
    refstore.init_category(CAT)
    refstore.init_category("other")
    path = write_video(home, "a00")
    (home / "refs" / "ref_other" / "videos" / "a00.md").write_bytes(path.read_bytes())
    _data(home, "a00")
    out = videomd.trash_video("a00", CAT)
    assert out["kept_data"] is True and out["moved"] == [f"refs/ref_{CAT}/videos/a00.md"]
    assert (home / "workspace" / "raw" / "a00.json").is_file()


def test_trash_video_errors(home: Path) -> None:
    refstore.init_category(CAT)
    for vid, code in (("../x", "bad_request"), ("zz9", "video_not_found")):
        with pytest.raises(ScriptError) as exc:
            videomd.trash_video(vid, CAT)
        assert exc.value.code == code
    with pytest.raises(ScriptError) as exc:
        videomd.trash_video("a00", "nocat")
    assert exc.value.code == "category_not_found"


# --- features -----------------------------------------------------------------------

def test_load_videos_sorted_by_id_role_filtered_and_id_falls_back_to_stem(home: Path) -> None:
    refstore.init_category(CAT)
    write_video(home, "b2")
    write_video(home, "A1")
    write_video(home, "m1", role="mine")
    noid = write_video(home, "c3")
    refstore.update_frontmatter(noid, {}, remove=["id"])
    ids = [r["id"] for r in features.load_videos(CAT)]
    assert ids == sorted(["b2", "A1", "c3"])
    assert [r["id"] for r in features.load_videos(CAT, "mine")] == ["m1"]
    paths = {r["id"]: str(r["path"]).replace("\\", "/") for r in features.load_videos(CAT)}
    assert paths["c3"].endswith(f"refs/ref_{CAT}/videos/c3.md")


# --- cluster ------------------------------------------------------------------------

NOTES = {
    "axes": {a: "근거 문장" for a in ["structure", "hook", "narrative", "title", "pacing"]},
    "archetypes": [{"id": "A1", "name": "결과 먼저 몰아먹기"}, {"id": "A2", "name": "사이즈별 리스트 비교"}],
}


def preview_arg():
    """cluster.run(out=)·apply(source=)에 넘기는 미리보기 위치(키)."""
    return cluster.preview_key(CAT)


def notes_file(home: Path, data: dict = NOTES) -> Path:
    path = home / "workspace" / "tmp" / "typify_notes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_cluster_run_writes_indent2_json_with_platform_newlines(home: Path) -> None:
    standard_set(home)
    result = cluster.run(CAT)
    path = home / "workspace" / "typify" / f"{CAT}.json"
    assert path.read_bytes() == native(json.dumps(result, ensure_ascii=False, indent=2))
    assert [p.name for p in path.parent.iterdir()] == [f"{CAT}.json"]


def test_cluster_run_with_out_writes_only_that_file_and_too_few_writes_nothing(home: Path) -> None:
    standard_set(home, n_a=4, n_b=0)
    assert cluster.run(CAT)["status"] == "too_few"
    assert not (home / "workspace" / "typify").exists()
    standard_set(home)
    result = cluster.run(CAT, out=preview_arg())
    prev = home / "workspace" / "typify" / f"{CAT}.preview.json"
    assert prev.read_bytes() == native(json.dumps(result, ensure_ascii=False, indent=2))
    assert not (home / "workspace" / "typify" / f"{CAT}.json").exists()


@pytest.mark.parametrize("variant", ["lf", "crlf", "bom"])
def test_cluster_apply_index_bytes_follow_existing_newline(home: Path, variant: str) -> None:
    standard_set(home)
    cluster.run(CAT)
    index = home / "refs" / f"ref_{CAT}" / "index.md"
    raw = index.read_bytes().decode("utf-8")
    if variant != "lf":
        raw = raw.replace("\r\n", "\n").replace("\n", "\r\n")
    index.write_bytes((b"\xef\xbb\xbf" if variant == "bom" else b"") + raw.encode("utf-8"))
    crlf = variant != "lf"
    notes = notes_file(home)
    before = tree(home)
    out = cluster.apply(CAT, notes)
    diff = changes(before, tree(home))
    after = index.read_bytes().decode("utf-8")
    assert not index.read_bytes().startswith(b"\xef\xbb\xbf")
    assert after.startswith("---\r\n" if crlf else "---\n")
    assert ("\n" in after.replace("\r\n", "")) is False if crlf else "\r" not in after
    assert out["index"] == f"refs/ref_{CAT}/index.md" and out["status"] == "applied"
    assert set(diff["added"]) == {f"workspace/typify/{CAT}.applied.json"}
    assert diff["removed"] == ["workspace/tmp/typify_notes.json"]
    assert f"refs/ref_{CAT}/index.md" in diff["modified"]


def test_cluster_apply_snapshot_bytes_use_platform_newlines(home: Path) -> None:
    standard_set(home)
    cluster.run(CAT)
    cluster.apply(CAT, notes_file(home))
    path = home / "workspace" / "typify" / f"{CAT}.applied.json"
    snap = json.loads(path.read_bytes().decode("utf-8"))
    assert path.read_bytes() == native(json.dumps(snap, ensure_ascii=False, indent=2))
    assert [p.name for p in path.parent.iterdir() if p.suffix == ".tmp"] == []


def test_cluster_apply_promotes_preview_to_result(home: Path) -> None:
    standard_set(home)
    cluster.run(CAT, out=preview_arg())
    typify = home / "workspace" / "typify"
    preview = (typify / f"{CAT}.preview.json").read_bytes()
    cluster.apply(CAT, notes_file(home), source=preview_arg())
    assert (typify / f"{CAT}.json").read_bytes() == preview
    assert not (typify / f"{CAT}.preview.json").exists()


def test_cluster_apply_errors(home: Path) -> None:
    standard_set(home)
    with pytest.raises(ScriptError) as exc:
        cluster.apply(CAT, notes_file(home))
    assert exc.value.code == "no_typify_result"
    assert CAT in exc.value.message
    with pytest.raises(ScriptError) as exc:
        cluster.apply(CAT, notes_file(home), source=preview_arg())
    assert exc.value.code == "typify_changed"
    cluster.run(CAT)
    with pytest.raises(ScriptError) as exc:
        cluster.apply(CAT, notes_file(home), expected_run_at="2000-01-01")
    assert exc.value.code == "typify_changed"
    assert (home / "workspace" / "tmp" / "typify_notes.json").exists()      # 실패하면 notes는 남는다


# --- similarity ---------------------------------------------------------------------

@pytest.fixture
def shome(home: Path) -> Path:
    standard_set(home)
    cluster.run(CAT)
    cluster.apply(CAT, notes_file(home))
    return home


def sim_notes(home: Path, result: dict) -> Path:
    data = {"summary": "구조가  비슷하다", "gaps": {g["key"]: f"{g['key']} 설명" for g in result["gaps"]},
            "actions": ["첫째", "둘째"]}
    path = home / "workspace" / "tmp" / "sim_notes.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_similarity_save_keeps_memo_and_appends_history(shome: Path) -> None:
    target = f"refs/ref_{CAT}/videos/a00.md"
    refstore.update_frontmatter(target, {"role": "mine", "id": "my-1"})
    result = similarity.compare(CAT, Path(target))
    assert result["target"]["path"] == target and result["target"]["name"] == "my-1"
    out = similarity.save(CAT, Path(target), sim_notes(shome, result))
    mine = shome / "mine" / "my-1.md"
    assert out["path"] == "mine/my-1.md" and mine.is_file()
    raw = mine.read_bytes()
    assert raw.startswith(b"---\n") and b"\r" not in raw and raw.endswith("## 내 메모\n".encode())
    mine.write_bytes(raw + "나중에 쓴 메모\n".encode())
    similarity.save(CAT, Path(target), sim_notes(shome, result))
    fm, body = refstore.read_md("mine/my-1.md")
    assert body.endswith("## 내 메모\n나중에 쓴 메모\n") and len(fm["history"]) == 2
    assert fm["target"] == target and not (shome / "workspace" / "tmp" / "sim_notes.json").exists()


def test_similarity_intentional_edits_frontmatter_only(shome: Path) -> None:
    target = f"refs/ref_{CAT}/videos/a00.md"
    refstore.update_frontmatter(target, {"role": "mine", "id": "my-1"})
    result = similarity.compare(CAT, Path(target))
    similarity.save(CAT, Path(target), sim_notes(shome, result))
    mine = shome / "mine" / "my-1.md"
    body_before = mine.read_bytes().split(b"---", 2)[2]
    assert similarity.intentional(CAT, "my-1", "hook") == {"name": "my-1", "intentional": ["hook"]}
    assert similarity.intentional(CAT, "my-1", "hook", off=True)["intentional"] == []
    assert mine.read_bytes().split(b"---", 2)[2] == body_before
    with pytest.raises(ScriptError) as exc:
        similarity.intentional(CAT, "nobody", "hook")
    assert exc.value.code == "diagnosis_not_found" and exc.value.message == "진단 기록이 없습니다: mine/nobody.md (먼저 진단)"
    with pytest.raises(ScriptError) as exc:
        similarity.intentional("nocat", "my-1", "hook")
    assert exc.value.code == "category_not_found"


def test_similarity_target_resolution_and_errors(shome: Path) -> None:
    rel = f"refs/ref_{CAT}/videos/a00.md"
    refstore.update_frontmatter(rel, {"role": "mine", "id": "my-1"})
    by_rel = similarity.compare(CAT, Path(rel))
    assert similarity.compare(CAT, shome / rel) == by_rel                 # 절대 경로도 같다
    assert by_rel["target"]["path"] == rel
    with pytest.raises(ScriptError) as exc:
        similarity.compare(CAT, Path("mine/none.md"))
    assert exc.value.code == "target_not_found"
    assert exc.value.message == f"진단할 파일이 없습니다: {shome / 'mine' / 'none.md'}"
    with pytest.raises(ScriptError) as exc:
        similarity.compare(CAT, Path(f"refs/ref_{CAT}/videos/a01.md"))
    assert exc.value.code == "not_mine"


def test_similarity_load_typify_messages(home: Path) -> None:
    standard_set(home)
    target = f"refs/ref_{CAT}/videos/a00.md"
    refstore.update_frontmatter(target, {"role": "mine"})
    with pytest.raises(ScriptError) as exc:
        similarity.compare(CAT, Path(target))
    assert exc.value.message == f"유형화 결과가 없습니다. 먼저 /yt-typify {CAT}"
    typify = home / "workspace" / "typify"
    typify.mkdir(parents=True)
    (typify / f"{CAT}.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ScriptError) as exc:
        similarity.compare(CAT, Path(target))
    assert exc.value.code == "no_typify_result" and "읽을 수 없습니다" in exc.value.message
    assert similarity.typify_gate(CAT) == {"available": False, "preliminary": None}


# --- 저장소 이전 뒤에 새로 생기는 동작 (옮기기 전 코드에서는 통과하지 않는다) ------------------

def test_similarity_target_outside_work_folder_or_not_md_is_target_not_found(shome: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("outside") / "mine.md"
    outside.write_bytes((shome / "refs" / f"ref_{CAT}" / "videos" / "a00.md").read_bytes())
    (shome / "notes.txt").write_text("x", encoding="utf-8")
    for target in (outside, Path("notes.txt"), Path("../outside.md")):
        with pytest.raises(ScriptError) as exc:
            similarity.compare(CAT, target)
        assert exc.value.code == "target_not_found"
        assert exc.value.message.startswith("진단할 파일이 없습니다: ")


def test_relabel_write_conflict_after_the_early_check_is_ref_changed(vhome: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import filestore
    path, h0 = _written(vhome)
    original = filestore.FileDocStore.write

    def racing(self, key, data, **kwargs):          # 확인과 쓰기 사이에 사용자가 파일을 고친다
        path.write_bytes(path.read_bytes() + "사용자 메모\n".encode("utf-8"))
        return original(self, key, data, **kwargs)

    monkeypatch.setattr(filestore.FileDocStore, "write", racing)
    with pytest.raises(ScriptError) as exc:
        videomd.relabel(VID, VCAT, {"hook_type": "question"}, h0)
    assert exc.value.code == "ref_changed"
    assert path.read_bytes().endswith("사용자 메모\n".encode("utf-8")) and b"question" not in path.read_bytes()
