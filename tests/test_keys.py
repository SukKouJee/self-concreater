"""키 함수(S3 §2·ADR-1): 키 = 지금 모듈이 만드는 경로의 `relative_to(root).as_posix()`."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts import (
    cluster, common, conti, fetch, footage, footage_match, footage_out, keys, library, llm, mission,
    refstore, shots, similarity, videomd,
)
from scripts.common import ScriptError
from tests.helpers.tree_snapshot import copy_workfolder

STEM = "2026-10-30-제주-한달살기"
VID = "Q8PSsikq8PA"
DRAFT = f"drafts/{STEM}.md"
CONTI = f"drafts/{STEM}-conti.md"


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    root = copy_workfolder(tmp_path / "work")
    monkeypatch.setenv("YT_COACH_HOME", str(root))
    return root


def rel(path: Path) -> str:
    return path.relative_to(common.work_root()).as_posix()


# --- 모듈 함수와 대조 ------------------------------------------------------------------

def test_category_keys_match_module_paths(home: Path) -> None:
    assert keys.category("travel") == rel(common.category_dir("travel")) + "/"
    assert keys.video_md("travel", VID) == videomd.video_md_key(VID, "travel")
    assert keys.video_md("travel", "-C8U1fizcms") == videomd.video_md_key("-C8U1fizcms", "travel")
    assert keys.index_md("travel") == rel(refstore.existing_category_dir("travel") / "index.md")
    assert keys.techniques_md("travel") == rel(refstore.existing_category_dir("travel") / "techniques.md")
    assert keys.REFS == "refs/"
    assert keys.category_name(keys.category("travel")) == "travel"


@pytest.mark.parametrize("bad", ["Travel", "-x", "", "a/b", "../x", "여행"])
def test_invalid_category_raises_same_error_as_common(bad: str) -> None:
    with pytest.raises(ScriptError) as want:
        common.category_dir(bad)
    with pytest.raises(ScriptError) as got:
        keys.category(bad)
    assert (got.value.code, got.value.message) == (want.value.code, want.value.message) == (
        "invalid_category", want.value.message)
    with pytest.raises(ScriptError, match="카테고리 이름"):
        keys.video_md(bad, VID)
    with pytest.raises(ScriptError, match="카테고리 이름"):
        keys.typify(bad)


def test_conti_keys_match_module_paths(home: Path) -> None:
    root = common.work_root()
    draft_path, conti_path = root / DRAFT, root / CONTI
    assert keys.draft_md(STEM) == DRAFT
    assert keys.conti_md(DRAFT) == rel(conti.conti_path(draft_path)) == CONTI
    outputs = conti.conti_outputs(conti_path)                         # 픽스처에 산출물이 모두 있다(키)
    assert outputs == [CONTI, keys.conti_html(CONTI), keys.srt(CONTI, "captions"), keys.srt(CONTI, "lines"),
                       keys.captions_mp4(CONTI), keys.remaining_html(CONTI), keys.footage_csv(CONTI)]
    assert [s for _, s in conti.SRT_TRACKS.values()] == ["captions", "lines"]
    assert keys.cut_files_dir(CONTI) == CONTI.replace("-conti.md", "-cuts/")
    history = f"workspace/conti_history/{conti_path.stem}/"                           # 콘티 이름(stem)만 쓴다
    assert keys.history_dir(CONTI) == keys.history_dir(conti_path.as_posix()) == history
    assert keys.history_version(CONTI, 1) == f"{history}0001.json"
    assert keys.history_version(CONTI, 12345) == f"{history}12345.json"
    assert keys.footage_match(CONTI) == footage_match.state_key(conti_path)
    data = b"<svg/>"
    sha6 = hashlib.sha1(data).hexdigest()[:6]
    art = Path(*conti.ART_DIR, conti_path.stem, f"c1-0123abcd-{sha6}.svg")              # conti_art.py 그대로
    assert keys.art(CONTI, "c1", "0123abcd", sha6) == art.as_posix()
    assert conti.art_of({"file": keys.art(CONTI, "c1", "0123abcd", sha6), "basis": "0123abcd"}) is not None


def test_conti_output_keys_reject_non_conti_key() -> None:
    for fn in (keys.conti_html, keys.captions_mp4, keys.remaining_html, keys.footage_csv, keys.cut_files_dir):
        with pytest.raises(ValueError):
            fn(DRAFT)
    with pytest.raises(ValueError):
        keys.srt(DRAFT, "captions")
    with pytest.raises(ValueError):
        keys.conti_md("drafts/a.html")


def test_html_of_replaces_md_suffix_like_with_suffix() -> None:
    for key in (CONTI, "top-conti.md", "drafts/a.b-conti.MD", "drafts/sub/x.md"):
        assert keys.html_of(key) == Path(key).with_suffix(".html").as_posix()
    assert keys.html_of(CONTI) == keys.conti_html(CONTI)
    with pytest.raises(ValueError):
        keys.html_of("drafts/a.html")


def test_other_doc_keys_match_module_paths(home: Path) -> None:
    root = common.work_root()
    assert keys.mine_md("my-jeju-vlog") == similarity._diag_key("my-jeju-vlog")
    assert keys.mission_md("2026-10-30") == mission._mission_key("2026-10-30") == "missions/2026-10-30.md"
    assert keys.review_md("2026-W44") == rel(root / "reviews" / "2026-W44.md")             # mission.py review_save
    assert keys.report_html("travel", "2026-10-30") == rel(root / "reports" / "travel-2026-10-30.html")  # report.py
    assert keys.PROFILE == rel(root / "profile.md")                                       # motif·mission
    assert keys.CONFIG == rel(root / llm.CONFIG_FILE)
    assert keys.LIBRARY_CONFIG == library.CONFIG_FILE == "library.json"
    assert (keys.GITIGNORE, keys.GIT_DIR) == (".gitignore", ".git/")                      # refstore.scaffold
    assert (keys.DRAFTS, keys.MINE, keys.MISSIONS, keys.REVIEWS, keys.REPORTS) == (
        "drafts/", "mine/", "missions/", "reviews/", "reports/")
    for k in (keys.mine_md("x"), keys.mission_md("2026-10-30"), keys.review_md("2026-W44"), keys.report_html("a", "b"),
              keys.PROFILE, keys.CONFIG, keys.LIBRARY_CONFIG, keys.GITIGNORE, DRAFT):
        assert (root / k).exists() or k.endswith(("x.md", "a-b.html")), k


def test_workspace_keys_match_module_paths(home: Path) -> None:
    assert keys.raw(VID) == rel(common.workspace_dir("raw", f"{VID}.json"))     # fetch.py (옮긴 뒤엔 저장소가 같은 키를 씀)
    assert keys.cuts(VID) == rel(common.workspace_dir("cuts", f"{VID}.json"))           # cuts.py·videomd.load_cuts
    assert keys.frames_dir(VID) == rel(common.workspace_dir("frames", VID)) + "/"         # cuts.py
    assert keys.shots(VID) == rel(common.workspace_dir("shots", f"{VID}.json"))   # shots.py
    assert keys.typify("travel") == cluster.result_key("travel")
    assert keys.typify("travel", "applied") == cluster.applied_key("travel")
    assert keys.typify("travel", "preview") == cluster.preview_key("travel")
    with pytest.raises(ValueError):
        keys.typify("travel", "other")
    fid = "0123456789ab-10-20"
    assert footage.valid_fid(fid)
    assert keys.footage_dir(fid) == footage.footage_key(fid) == f"workspace/footage/{fid}/"
    assert keys.TRASH == "workspace/trash/"


# --- check · kind ------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "refs/ref_travel/videos/Q8PSsikq8PA.md", "refs/ref_travel/", f"workspace/frames/{VID}/0001.jpg", "profile.md",
    ".git/", f"drafts/{STEM}-conti.md", "workspace/footage/0123456789ab-10-20/frames/0001.jpg",
])
def test_check_accepts_relative_posix_keys(key: str) -> None:
    assert keys.check(key) == key


@pytest.mark.parametrize("key", [
    "", "/", "/abs.md", "C:/x.md", "c:x.md", "a\\b.md", "a//b.md", "./a.md", "a/./b.md", "a/../b.md", "../a.md",
    "a/..", "a\x00b.md", "a//", "drafts/a.", "drafts/a ", "drafts/a. /b.md", "CON", "drafts/con.md", "nul.txt",
    "workspace/frames/Aux/0001.jpg", "drafts/COM1.md", "lpt9.json", "prn", "drafts/Com3.tar.gz/",
])
def test_check_rejects_bad_keys(key: str) -> None:
    with pytest.raises(ScriptError) as e:
        keys.check(key)
    assert e.value.code == "bad_path"


@pytest.mark.parametrize(("key", "kind"), [
    ("refs/ref_travel/videos/x.md", "doc"), (DRAFT, "doc"), ("profile.md", "doc"), ("mine/a.md", "doc"),
    (f"workspace/raw/{VID}.json", "shared"), (f"workspace/cuts/{VID}.json", "shared"),
    (f"workspace/frames/{VID}/0001.jpg", "shared"), (f"workspace/shots/{VID}.json", "shared"),
    (f"drafts/{STEM}-conti.html", "object"), ("workspace/typify/travel.json", "object"), ("yt-coach.json", "object"),
    ("workspace/rawx/a.json", "object"), ("reports/a-b.html", "object"), ("drafts/Idea.MD", "doc"),
    ("refs/ref_travel/videos/X.Md", "doc"), ("drafts/a.mdx", "object"),
])
def test_kind_picks_store(key: str, kind: str) -> None:
    assert keys.kind(key) == kind


def test_is_doc_ignores_case() -> None:
    assert keys.is_doc("drafts/a.md") and keys.is_doc("drafts/A.MD") and keys.is_doc("x.mD")
    assert not keys.is_doc("drafts/a.mdx") and not keys.is_doc("drafts/md") and not keys.is_doc("refs/ref_a/")


@pytest.mark.parametrize("key", [
    "drafts/console.md", "drafts/auxiliary.md", "workspace/frames/COM10/0001.jpg", "drafts/a.b.md",
    "workspace/frames/-C8U1fizcms/0001.jpg", "drafts/nul-x.md", ".gitignore",
])
def test_check_keeps_lookalikes_of_reserved_names(key: str) -> None:
    assert keys.check(key) == key


def test_every_fixture_key_passes_check() -> None:
    from tests.helpers.tree_snapshot import FIXTURE
    rels = [p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*")]
    assert rels and all(keys.check(r) == r for r in rels)
