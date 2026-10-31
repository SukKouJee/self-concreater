"""ServerScratch(서버 설계 §3.2·§3.4): 사용자별 프로세스 임시 폴더, 서버 전역 상태 폴더, 로컬 전용 기능은 분명한 오류.

PG·MinIO가 필요 없다(디스크만).
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from scripts import keys
from scripts.common import ScriptError
from server.scratch import ServerScratch, clear_scratch_root

UID = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER = uuid.UUID("99999999-2222-3333-4444-555555555555")


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / "ytc"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def scratch(base: Path, state: Path) -> ServerScratch:
    return ServerScratch(base, state, UID)


def test_tmp_path_is_under_the_users_own_folder_and_creates_parents(scratch: ServerScratch, base: Path) -> None:
    path = scratch.tmp_path("sub/draft.json")
    assert path == base / str(UID) / "sub" / "draft.json"
    assert path.parent.is_dir()
    assert not path.exists()


def test_two_users_never_share_a_tmp_folder(base: Path, state: Path) -> None:
    a = ServerScratch(base, state, UID).tmp_path("x.json")
    b = ServerScratch(base, state, OTHER).tmp_path("x.json")
    assert a != b
    assert a.parent.name == str(UID) and b.parent.name == str(OTHER)


@pytest.mark.parametrize("name", ["", "a/", "../x.json", "a/../../x", "C:x", "a\\b"])
def test_tmp_path_rejects_bad_names(scratch: ServerScratch, name: str) -> None:
    with pytest.raises(ScriptError) as e:
        scratch.tmp_path(name)
    assert e.value.code == "bad_path"


def test_tmp_file_yields_tmp_path_for_a_scratch_key(scratch: ServerScratch) -> None:
    with scratch.tmp_file(keys.tmp("a.json")) as path:
        assert path == scratch.tmp_path("a.json")


def test_tmp_file_rejects_a_non_scratch_key(scratch: ServerScratch) -> None:
    with pytest.raises(ValueError):
        with scratch.tmp_file(keys.draft_md("x")):
            pass


def test_local_path_tmp_is_the_users_tmp_folder(scratch: ServerScratch, base: Path) -> None:
    assert scratch.local_path("tmp") == base / str(UID)


@pytest.mark.parametrize("name", ["timings.json", ".last_fetch"])
def test_local_path_machine_state_lives_in_the_server_wide_state_folder(
        scratch: ServerScratch, state: Path, base: Path, name: str) -> None:
    path = scratch.local_path(name)
    assert path == state / name
    assert state.is_dir()
    other = ServerScratch(base, state, OTHER).local_path(name)
    assert other == path                       # one fetch interval for the whole server (§3.2)


@pytest.mark.parametrize("parts", [("library",), ("library.lock",), ("library", "x"), ("raw",), ("tmp", "x")])
def test_local_path_for_local_only_features_raises_not_implemented(scratch: ServerScratch,
                                                                   parts: tuple[str, ...]) -> None:
    with pytest.raises(NotImplementedError) as e:
        scratch.local_path(*parts)
    assert "서버판" in str(e.value)


def test_workdir_is_local_only(scratch: ServerScratch) -> None:
    with pytest.raises(NotImplementedError) as e:
        scratch.workdir()
    assert "claude" in str(e.value).lower() and "서버판" in str(e.value)


def test_arg_path_relative_is_tmp_based(scratch: ServerScratch, base: Path) -> None:
    assert scratch.arg_path("draft.json") == base / str(UID) / "draft.json"
    assert scratch.arg_path(Path("a") / "b.json") == base / str(UID) / "a" / "b.json"


def test_arg_path_absolute_inside_tmp_is_returned(scratch: ServerScratch) -> None:
    inside = scratch.tmp_path("x.json")
    assert scratch.arg_path(inside) == inside
    assert scratch.arg_path(str(inside)) == inside


@pytest.mark.parametrize("make", [
    lambda base, tmp: tmp / "elsewhere.json",                              # outside the scratch root
    lambda base, tmp: base / str(OTHER) / "x.json",                        # another user's tmp
    lambda base, tmp: base / str(UID) / ".." / str(OTHER) / "x.json",      # escape with ..
])
def test_arg_path_absolute_outside_own_tmp_is_bad_path(scratch: ServerScratch, base: Path, tmp_path: Path,
                                                       make: object) -> None:
    value = make(base, tmp_path)  # type: ignore[operator]
    with pytest.raises(ScriptError) as e:
        scratch.arg_path(value)
    assert e.value.code == "bad_path"


def test_arg_path_relative_escape_is_bad_path(scratch: ServerScratch) -> None:
    with pytest.raises(ScriptError) as e:
        scratch.arg_path("../" + str(OTHER) + "/x.json")
    assert e.value.code == "bad_path"


@pytest.mark.parametrize("value, key", [
    ("drafts/a.md", "drafts/a.md"),
    ("./drafts/a.md", "drafts/a.md"),
    ("drafts/x/../a.md", "drafts/a.md"),
    (Path("refs") / "ref_a" / "index.md", "refs/ref_a/index.md"),
    ("workspace/tmp/x.json", "workspace/tmp/x.json"),
])
def test_key_of_relative_values_are_keys(scratch: ServerScratch, value: str | Path, key: str) -> None:
    assert scratch.key_of(value) == key


def test_key_of_a_path_inside_tmp_is_a_scratch_key(scratch: ServerScratch) -> None:
    path = scratch.tmp_path("sub/x.json")
    assert scratch.key_of(path) == keys.tmp("sub/x.json")
    assert scratch.key_of(str(path)) == keys.tmp("sub/x.json")


@pytest.mark.parametrize("value", ["../x.md", "drafts/../../x.md", "a\\..\\..\\b.md", "C:/x.md", "drafts/CON.md",
                                   ".", ""])
def test_key_of_bad_relative_values_are_bad_path(scratch: ServerScratch, value: str) -> None:
    with pytest.raises(ScriptError) as e:
        scratch.key_of(value)
    assert e.value.code == "bad_path"


def test_key_of_absolute_path_outside_own_tmp_is_bad_path(scratch: ServerScratch, base: Path,
                                                          tmp_path: Path) -> None:
    for value in (tmp_path / "x.md", base / str(OTHER) / "x.json", base / str(UID)):
        with pytest.raises(ScriptError) as e:
            scratch.key_of(value)
        assert e.value.code == "bad_path"


def test_clear_scratch_root_removes_only_user_folders(base: Path, state: Path) -> None:
    ServerScratch(base, state, UID).tmp_path("a/b.json").write_text("x", encoding="utf-8")
    ServerScratch(base, state, OTHER).tmp_path("c.json").write_text("y", encoding="utf-8")
    keep = base / "not-a-user"
    keep.mkdir()
    (base / "note.txt").write_text("z", encoding="utf-8")
    clear_scratch_root(base)
    assert sorted(p.name for p in base.iterdir()) == ["not-a-user", "note.txt"]


def test_clear_scratch_root_creates_a_missing_root(base: Path) -> None:
    clear_scratch_root(base)
    assert base.is_dir()
