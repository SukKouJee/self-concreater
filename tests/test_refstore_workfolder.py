"""refstore의 작업 폴더 동작(scaffold·init·count·update-stage·find_video·list_categories·문서 키 읽기/쓰기)을 바이트까지 고정한다.

S3 단계 3(키 기반 이전) 전후로 같은 테스트가 통과해야 한다: 픽스처 작업 폴더를 복사해 쓰고, 반환 JSON과 파일 트리 해시를 비교한다.
"""
import datetime
import hashlib
import os

import pytest

from scripts import refstore
from scripts.common import ScriptError
from scripts.stores import VersionConflict
from tests.helpers import tree_snapshot as snap


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path / "work"))
    return tmp_path / "work"


@pytest.fixture
def work(home):
    return snap.copy_workfolder(home)


@pytest.fixture
def frozen_today(monkeypatch):
    class FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 31)
    monkeypatch.setattr(refstore, "date", FrozenDate)


# --- init -> count -> update-stage ---------------------------------------------

def test_init_count_update_stage_snapshot(work, frozen_today):
    before = snap.tree(work)

    init = snap.capture(work, refstore.init_category, "newcat")
    count = refstore.count("newcat")
    stage = refstore.update_stage("newcat")
    after = snap.tree(work)

    assert init.result == {
        "category": "newcat", "path": "refs/ref_newcat",
        "created": ["refs/ref_newcat/index.md", "refs/ref_newcat/techniques.md"], "skipped": [],
    }
    assert count == {"ref": 0, "mine": 0, "stage": 1}
    assert stage["ref"] == 0 and stage["stage"] == 1 and stage["text"].startswith("- ref: 0개 / mine: 0개")
    ch = snap.changes(before, after)
    assert ch["removed"] == [] and ch["modified"] == []
    assert ch["added"] == ["refs/ref_newcat/index.md", "refs/ref_newcat/techniques.md"]
    assert (work / "refs/ref_newcat/videos").is_dir()
    index = (work / "refs/ref_newcat/index.md").read_bytes()
    assert b"\r" not in index and b"category: newcat\n" in index and b"created: 2026-10-31\n" in index
    # init 직후의 단계 문구와 update-stage 결과가 같아서 바이트가 그대로다
    assert init.tree["refs/ref_newcat/index.md"] == after["refs/ref_newcat/index.md"]


def test_init_existing_category_skips_and_changes_nothing(work):
    before = snap.tree(work)
    result = refstore.init_category("travel")
    assert result == {
        "category": "travel", "path": "refs/ref_travel", "created": [],
        "skipped": ["refs/ref_travel/index.md", "refs/ref_travel/techniques.md"],
    }
    assert snap.tree(work) == before


def test_init_creates_missing_file_only(work):
    (work / "refs/ref_travel/techniques.md").unlink()
    result = refstore.init_category("travel")
    assert result["created"] == ["refs/ref_travel/techniques.md"]
    assert result["skipped"] == ["refs/ref_travel/index.md"]


def test_init_invalid_category_creates_nothing(work):
    before = snap.tree(work)
    with pytest.raises(ScriptError) as exc:
        refstore.init_category("Bad Name")
    assert exc.value.code == "invalid_category"
    assert "영문 소문자·숫자·-·_만" in exc.value.message
    assert snap.tree(work) == before and not (work / "refs/ref_Bad Name").exists()


def test_count_missing_category_message(work):
    with pytest.raises(ScriptError) as exc:
        refstore.count("nope")
    assert (exc.value.code, exc.value.message) == ("category_not_found", "카테고리가 없습니다: nope (먼저 init)")


def test_count_invalid_category_checked_before_existence(work):
    with pytest.raises(ScriptError) as exc:
        refstore.count("Nope!")
    assert exc.value.code == "invalid_category"


def test_count_fixture_roles(work):
    result = refstore.count("travel")
    assert result["stage"] == 1 and result["ref"] + result["mine"] == 3


def test_count_reports_unparsable_video_with_path_label(work):
    bad = work / "refs/ref_travel/videos/broken.md"
    bad.write_bytes(b"no frontmatter here\n")
    with pytest.raises(ScriptError) as exc:
        refstore.count("travel")
    assert exc.value.code == "no_frontmatter"
    assert exc.value.message.startswith(f"{bad}: ")


def test_update_stage_keeps_crlf_drops_bom_and_memo(home):
    cat = home / "refs/ref_a"
    (cat / "videos").mkdir(parents=True)
    (cat / "videos/v1.md").write_bytes(b"---\nid: v1\nrole: ref\n---\nbody\n")
    (cat / "videos/V2.MD").write_bytes(b"---\nid: V2\nrole: mine\n---\nbody\n")
    index = "---\r\ncategory: a\r\n---\r\n# a\r\n\r\n## 단계\r\n- 옛 값\r\n\r\n## 내 메모\r\n나의 메모\r\n"
    (cat / "index.md").write_bytes(b"\xef\xbb\xbf" + index.encode("utf-8"))

    result = refstore.update_stage("a")

    assert (result["ref"], result["mine"], result["stage"]) == (1, 1, 1)
    out = (cat / "index.md").read_bytes()
    assert not out.startswith(b"\xef\xbb\xbf")
    assert out.startswith(b"---\r\ncategory: a\r\n---\r\n")
    assert "## 내 메모\r\n나의 메모\r\n".encode() in out
    assert "- ref: 1개 / mine: 1개".encode() in out


def test_update_stage_without_index_raises_file_not_found(home):
    (home / "refs/ref_a/videos").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        refstore.update_stage("a")


def test_update_stage_is_idempotent_on_fixture(work):
    refstore.update_stage("travel")
    first = snap.tree(work)
    refstore.update_stage("travel")
    assert snap.tree(work) == first


# --- find_video / list_categories ------------------------------------------------

def test_find_video_fixture(work):
    assert refstore.find_video("Q8PSsikq8PA") == [
        {"category": "travel", "role": "ref", "md": "refs/ref_travel/videos/Q8PSsikq8PA.md"},
    ]
    assert refstore.find_video("nothere") == []


def test_find_video_across_categories_in_sorted_order(work):
    shared = (work / "refs/ref_travel/videos/Q8PSsikq8PA.md").read_bytes()
    for cat in ("alpha", "zeta"):
        folder = work / f"refs/ref_{cat}/videos"
        folder.mkdir(parents=True)
        (folder / "Q8PSsikq8PA.md").write_bytes(shared)
    found = refstore.find_video("Q8PSsikq8PA")
    assert [f["category"] for f in found] == ["alpha", "travel", "zeta"]


def test_find_video_missing_refs_folder(home):
    home.mkdir()
    assert refstore.find_video("x") == []


def test_list_categories_fixture(work):
    cats = refstore.list_categories()
    assert [c["category"] for c in cats] == ["food", "travel"]
    assert set(cats[0]) == {"category", "ref", "mine", "stage"}


def test_list_categories_ignores_files_and_missing_refs(home):
    home.mkdir()
    assert refstore.list_categories() == []
    (home / "refs").mkdir()
    (home / "refs/ref_file").write_text("x")
    (home / "refs/other").mkdir()
    assert refstore.list_categories() == []


# --- scaffold --------------------------------------------------------------------

def test_scaffold_empty_folder(home):
    home.mkdir()
    result = refstore.scaffold()
    assert result == {
        "root": str(home),
        "created": ["refs/", "mine/", "drafts/", "missions/", "reviews/", "workspace/", ".gitignore"],
        "profile_exists": False, "git": False, "categories": [],
    }
    assert (home / ".gitignore").read_bytes() == b"workspace/\n"
    assert refstore.scaffold()["created"] == []


def test_scaffold_appends_to_existing_gitignore_keeping_other_lines(home):
    home.mkdir()
    (home / ".gitignore").write_bytes(b"\xef\xbb\xbf*.log\r\nnode_modules")
    refstore.scaffold()
    assert (home / ".gitignore").read_bytes() == b"*.log\nnode_modules\nworkspace/\n"


def test_scaffold_leaves_gitignore_alone_when_listed(home):
    home.mkdir()
    (home / ".gitignore").write_bytes(b"workspace/\r\n")
    assert ".gitignore" not in refstore.scaffold()["created"]
    assert (home / ".gitignore").read_bytes() == b"workspace/\r\n"


def test_scaffold_reports_profile_git_and_categories(work):
    (work / ".git").mkdir()
    result = refstore.scaffold()
    assert result["profile_exists"] is True and result["git"] is True
    assert result["categories"] == ["food", "travel"]


def test_scaffold_counts_git_file_as_git(home):
    home.mkdir()
    (home / ".git").write_text("gitdir: elsewhere\n")      # worktree·submodule은 .git이 파일이다
    assert refstore.scaffold()["git"] is True


# --- read / update_frontmatter (문서 키만) -----------------------------------------

VIDEO_KEY = "refs/ref_travel/videos/Q8PSsikq8PA.md"


def test_read_by_key(work):
    fm, body = refstore.read(VIDEO_KEY)
    assert fm["role"] == "ref" and body
    assert (fm, body) == refstore.parse_md((work / VIDEO_KEY).read_bytes(), work / VIDEO_KEY)


def test_read_by_key_missing_and_bad(work):
    with pytest.raises(FileNotFoundError):
        refstore.read("refs/ref_travel/videos/none.md")
    with pytest.raises(ScriptError) as exc:
        refstore.read("refs/../profile.md")
    assert exc.value.code == "bad_path"


def test_read_bad_frontmatter_message_uses_path(work):
    bad = work / "mine/bad.md"
    bad.write_bytes(b"---\n: [\n---\nx\n")
    with pytest.raises(ScriptError) as exc:
        refstore.read("mine/bad.md")
    assert exc.value.code == "bad_frontmatter" and exc.value.message.startswith(f"{bad}: ")


@pytest.mark.parametrize("key", ["refs/../profile.md", "../outside.md", "C:/x.md", "/abs.md", "mine\\m.md"])
def test_read_and_update_reject_keys_leaving_the_work_folder(work, key):
    before = snap.tree(work)
    with pytest.raises(ScriptError) as exc:
        refstore.read(key)
    assert exc.value.code == "bad_path"
    with pytest.raises(ScriptError) as exc:
        refstore.update_frontmatter(key, {"k": 1})
    assert exc.value.code == "bad_path"
    assert snap.tree(work) == before


def test_read_and_update_take_keys_not_paths(work):
    """과도기 Path 래퍼(read_md, Path를 받는 update_frontmatter, existing_category_dir)는 없어졌다."""
    before = snap.tree(work)
    with pytest.raises(TypeError):
        refstore.read(work / VIDEO_KEY)
    with pytest.raises(TypeError):
        refstore.update_frontmatter(work / VIDEO_KEY, {"k": 1})
    assert snap.tree(work) == before
    for name in ("read_md", "_key_of", "existing_category_dir"):
        assert not hasattr(refstore, name), name


def test_read_non_md_key_is_a_program_error(work):
    with pytest.raises(ValueError):
        refstore.read("workspace/typify/travel.json")


def test_update_frontmatter_keeps_body_bytes_and_crlf(home):
    path = home / "mine/m.md"
    path.parent.mkdir(parents=True)
    body = "## 내 메모\r\n그대로 🏆\r\n"
    path.write_bytes(b"\xef\xbb\xbf" + f"---\r\nid: m\r\nold: 1\r\n---\r\n{body}".encode())

    fm = refstore.update_frontmatter("mine/m.md", {"new": [1, 2]}, remove=["old", "absent"])

    assert fm == {"id": "m", "new": [1, 2]}
    assert path.read_bytes() == f"---\r\nid: m\r\nnew: [1, 2]\r\n---\r\n{body}".encode()


def test_update_frontmatter_missing_file_raises(work):
    with pytest.raises(FileNotFoundError):
        refstore.update_frontmatter("mine/none.md", {"a": 1})


def test_update_frontmatter_leaves_no_temp_files(work):
    before = set(os.listdir(work / "mine"))
    refstore.update_frontmatter("mine/my-jeju-vlog.md", {"zz": 1})
    assert set(os.listdir(work / "mine")) == before


def test_update_frontmatter_changes_only_that_file(work):
    before = snap.tree(work)
    refstore.update_frontmatter("mine/my-jeju-vlog.md", {"zz": 1})
    ch = snap.changes(before, snap.tree(work))
    assert ch == {"added": [], "removed": [], "modified": ["mine/my-jeju-vlog.md"]}
    assert "zz: 1" in (work / "mine/my-jeju-vlog.md").read_text("utf-8")


def test_update_frontmatter_expect_version(work):
    path = work / "mine/my-jeju-vlog.md"
    before = path.read_bytes()
    with pytest.raises(VersionConflict):
        refstore.update_frontmatter("mine/my-jeju-vlog.md", {"zz": 1}, expect_version="0" * 64)
    assert path.read_bytes() == before
    refstore.update_frontmatter("mine/my-jeju-vlog.md", {"zz": 1},
                                expect_version=hashlib.sha256(before).hexdigest())
    assert b"zz: 1" in path.read_bytes()


def test_main_categories_json(work):
    assert refstore.main(["categories"])["categories"][0]["category"] == "food"


def test_require_category(work):
    assert refstore.require_category("travel") == "refs/ref_travel/"
    with pytest.raises(ScriptError) as exc:
        refstore.require_category("nope")
    assert exc.value.code == "category_not_found"
    with pytest.raises(ScriptError) as exc:
        refstore.require_category("../x")
    assert exc.value.code == "invalid_category"


def test_follows_ctx_when_environment_changes(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root, cat in ((a, "one"), (b, "two")):
        monkeypatch.setenv("YT_COACH_HOME", str(root))
        root.mkdir()
        refstore.init_category(cat)
    monkeypatch.setenv("YT_COACH_HOME", str(a))
    assert [c["category"] for c in refstore.list_categories()] == ["one"]
    monkeypatch.setenv("YT_COACH_HOME", str(b))
    assert [c["category"] for c in refstore.list_categories()] == ["two"]


def test_scaffold_refs_as_file_raises_and_creates_nothing(home):
    home.mkdir()
    (home / "refs").write_bytes(b"not a folder")
    before = snap.tree(home)
    with pytest.raises(FileExistsError):
        refstore.scaffold()
    assert snap.tree(home) == before and sorted(p.name for p in home.iterdir()) == ["refs"]


def test_update_frontmatter_by_key_through_in_root_symlink_replaces_the_link(work):
    """키로 고칠 때 심볼릭 링크는 따라가지 않는다: 링크 자리에 새 파일을 쓰고 대상은 그대로 둔다."""
    target = work / "mine/my-jeju-vlog.md"
    link = work / "mine/link.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink를 만들 수 없는 환경")
    before = target.read_bytes()
    refstore.update_frontmatter("mine/link.md", {"zz": 1})
    assert target.read_bytes() == before
    assert not link.is_symlink() and b"zz: 1" in link.read_bytes()
