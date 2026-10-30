"""파일 저장소(S3 §4)와 그 기준이 되는 지금 동작(휴지통·픽스처 바이트)."""
from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts import common
from scripts.common import ScriptError
from tests.helpers.tree_snapshot import FIXTURE, changes, copy_workfolder, tree

STEM = "2026-10-30-제주-한달살기"
VID = "Q8PSsikq8PA"
CONTI = f"drafts/{STEM}-conti.md"

Mover = Callable[[Path, list[str], str], dict]


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    root = copy_workfolder(tmp_path / "work")
    monkeypatch.setenv("YT_COACH_HOME", str(root))
    return root


def _common_move(root: Path, rels: list[str], last: str) -> dict:
    return common.move_to_trash([root / r for r in rels], root / last)


MOVERS: dict[str, Mover] = {"common": _common_move}


@pytest.fixture(params=sorted(MOVERS))
def mover(request) -> Mover:
    return MOVERS[request.param]


# --- 픽스처 ------------------------------------------------------------------------

def test_fixture_keeps_line_endings_and_bom() -> None:
    videos = FIXTURE / "refs" / "ref_travel" / "videos"
    assert (videos / f"{VID}.md").read_bytes().startswith(b"---\nid: ")
    assert b"\r\n" not in (videos / f"{VID}.md").read_bytes()
    assert (videos / "-C8U1fizcms.md").read_bytes().startswith(b"---\r\nid: ")
    assert (videos / "aBcD_123xyz.md").read_bytes().startswith(b"\xef\xbb\xbf---\nid: ")
    assert b"\r\n" in (FIXTURE / "drafts" / f"{STEM}-conti.html").read_bytes()
    assert "🏆".encode() in (videos / f"{VID}.md").read_bytes()
    assert "## 내 메모".encode() in (videos / f"{VID}.md").read_bytes()
    assert (FIXTURE / "workspace" / "frames" / VID / "0001.jpg").is_file()


# --- 휴지통 (common.move_to_trash = FileTrash.move) ---------------------------------

_STAMP = r"workspace/trash/\d{8}-\d{6}"


def _conti_targets() -> tuple[list[str], str]:
    rels = [CONTI, f"drafts/{STEM}-conti.html", f"workspace/conti_history/{STEM}-conti", f"drafts/{STEM}-cuts",
            f"workspace/footage_match/{STEM}-conti.json"]
    return rels, CONTI


def test_trash_moves_files_and_folders_keeping_relative_paths(home: Path, mover: Mover) -> None:
    before = tree(home)
    rels, last = _conti_targets()

    out = mover(home, rels, last)

    assert re.fullmatch(_STAMP, out["trash"])
    assert out["moved"] == [r for r in rels if r != last] + [last]       # 지울 md는 마지막
    after = tree(home)
    diff = changes(before, after)
    assert diff["modified"] == []
    moved = {k: v for k, v in before.items() if any(k == r or k.startswith(r + "/") for r in rels)}
    assert sorted(diff["removed"]) == sorted(moved)
    assert {k: after[k] for k in diff["added"]} == {f"{out['trash']}/{k}": v for k, v in moved.items()}


def test_trash_same_second_gets_numbered_folder(home: Path, mover: Mover) -> None:
    now = datetime.now()
    for s in range(3):                                   # 이번 초부터 2초 뒤까지 이미 쓰인 것으로
        (home / "workspace" / "trash" / (now + timedelta(seconds=s)).strftime("%Y%m%d-%H%M%S")).mkdir(parents=True)

    out = mover(home, [f"drafts/{STEM}-conti.html"], f"drafts/{STEM}-conti.html")

    assert re.fullmatch(_STAMP + "-2", out["trash"])
    assert out["moved"] == [f"drafts/{STEM}-conti.html"]


def test_trash_rejects_paths_outside_work_folder(home: Path, tmp_path: Path, mover: Mover) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    before = tree(home)

    with pytest.raises(ScriptError) as e:
        mover(home, ["../outside.md"], CONTI)

    assert e.value.code == "bad_path"
    assert tree(home) == before
    assert outside.is_file()


def test_trash_rolls_back_when_a_move_fails(home: Path, monkeypatch, mover: Mover) -> None:
    before = tree(home)
    real = shutil.move
    calls: list[str] = []

    def flaky(src: str, dst: str):
        calls.append(src)
        if len(calls) == 2:
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr(shutil, "move", flaky)
    rels, last = _conti_targets()

    with pytest.raises(ScriptError) as e:
        mover(home, rels, last)

    assert e.value.code == "trash_failed"
    assert "locked" in e.value.message and "되돌리지 못한" not in e.value.message
    assert tree(home) == before
    assert not any((home / "workspace" / "trash").iterdir())


def test_trash_reports_files_it_could_not_restore(home: Path, monkeypatch, mover: Mover) -> None:
    real = shutil.move
    calls: list[str] = []

    def flaky(src: str, dst: str):
        calls.append(src)
        if len(calls) >= 2:                              # 두 번째 이동과 되돌리기 모두 실패
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr(shutil, "move", flaky)
    rels, last = _conti_targets()

    with pytest.raises(ScriptError) as e:
        mover(home, rels, last)

    assert e.value.code == "trash_failed"
    stuck = re.search(r"되돌리지 못한 파일은 휴지통에 남아 있습니다: (\S+)$", e.value.message)
    first = [r for r in rels if r != last][0]
    assert stuck and re.fullmatch(_STAMP + "/" + re.escape(first), stuck.group(1))
    assert (home / stuck.group(1)).is_file()
