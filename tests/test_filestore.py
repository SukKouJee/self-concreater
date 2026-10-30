"""파일 저장소(S3 §4)와 그 기준이 되는 지금 동작(휴지통·픽스처 바이트)."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts import conti_history, keys, refstore
from scripts.common import ScriptError
from scripts.filestore import FileDocStore, FileObjectStore, FileScratch, FileSharedCache, FileTrash
from scripts.stores import Meta, VersionConflict
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


def _file_trash_move(root: Path, rels: list[str], last: str) -> dict:
    return FileTrash(root).move(rels, last)


@pytest.fixture
def mover() -> Mover:
    return _file_trash_move


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


# --- 휴지통 (FileTrash.move) ---------------------------------

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


# --- FileTrash ---------------------------------------------------------------------

def test_file_trash_accepts_folder_keys_with_trailing_slash(home: Path) -> None:
    out = FileTrash(home).move([keys.frames_dir(VID), keys.raw(VID)], keys.raw(VID))

    assert out["moved"] == [f"workspace/frames/{VID}", keys.raw(VID)]
    assert (home / out["trash"] / "workspace" / "frames" / VID / "0001.jpg").is_file()


def test_file_trash_rejects_bad_keys(home: Path) -> None:
    with pytest.raises(ScriptError) as e:
        FileTrash(home).move(["C:/x.md"], CONTI)
    assert e.value.code == "bad_path"


# --- FileDocStore --------------------------------------------------------------------

MD_KEYS = [f"refs/ref_travel/videos/{VID}.md", "refs/ref_travel/videos/-C8U1fizcms.md",
           "refs/ref_travel/videos/aBcD_123xyz.md", f"drafts/{STEM}.md", "profile.md"]


@pytest.mark.parametrize("key", MD_KEYS)
def test_doc_round_trip_keeps_bytes(home: Path, key: str) -> None:
    store = FileDocStore(home)
    raw = (home / key).read_bytes()

    doc = store.read(key)
    version = store.write(key, doc.raw)

    assert doc.raw == raw and (home / key).read_bytes() == raw
    assert doc.key == key
    assert doc.version == version == hashlib.sha256(raw).hexdigest()
    assert doc.label == str(home / key)
    assert doc.parsed == refstore.parse_md(raw, home / key) == refstore.read(key)


def test_doc_nl_follows_frontmatter_line_ending(home: Path) -> None:
    store = FileDocStore(home)
    assert store.read(f"refs/ref_travel/videos/{VID}.md").nl == "\n"
    assert store.read("refs/ref_travel/videos/-C8U1fizcms.md").nl == "\r\n"
    assert store.read("refs/ref_travel/videos/aBcD_123xyz.md").nl == "\n"          # BOM은 떼고 본다
    (home / "drafts" / "bom-crlf.md").write_bytes(b"\xef\xbb\xbf---\r\na: 1\r\n---\r\nx")
    assert store.read("drafts/bom-crlf.md").nl == "\r\n"


def test_doc_parse_error_message_matches_parse_md(home: Path) -> None:
    (home / "drafts" / "broken.md").write_bytes("제목만 있음\n".encode())
    with pytest.raises(ScriptError) as want:
        refstore.parse_md((home / "drafts" / "broken.md").read_bytes(), home / "drafts" / "broken.md")
    doc = FileDocStore(home).read("drafts/broken.md")          # 읽기는 되고, 파싱할 때 같은 오류
    with pytest.raises(ScriptError) as got:
        doc.parsed
    assert (got.value.code, got.value.message) == (want.value.code, want.value.message)


def test_missing_keys_raise_file_not_found(home: Path) -> None:
    docs, objs = FileDocStore(home), FileObjectStore(home)
    with pytest.raises(FileNotFoundError):
        docs.read("drafts/none.md")
    with pytest.raises(FileNotFoundError):
        docs.stat("drafts/none.md")
    with pytest.raises(FileNotFoundError):
        objs.get("drafts/none.html")
    with pytest.raises(FileNotFoundError):
        objs.stat("drafts/none.html")
    assert not docs.exists("drafts/none.md") and not objs.exists("drafts/none.html")
    assert docs.exists(CONTI) and objs.exists(keys.conti_html(CONTI))


def test_stat_matches_os_stat(home: Path) -> None:
    key = f"refs/ref_travel/videos/{VID}.md"
    st = (home / key).stat()
    meta = FileDocStore(home).stat(key)
    assert meta == Meta(key=key, size=st.st_size, mtime=st.st_mtime, mtime_ns=st.st_mtime_ns)
    assert FileObjectStore(home).stat(keys.conti_html(CONTI)).mtime == (home / keys.conti_html(CONTI)).stat().st_mtime


def test_list_order_matches_sorted_glob(home: Path) -> None:
    videos = home / "refs" / "ref_travel" / "videos"
    for name in ["b.md", "A.md", "a2.md", "C.md", "_z.md", "Z9.md", "Up.MD", "note.txt"]:
        (videos / name).write_bytes(b"---\na: 1\n---\n")
    (videos / "dir.md").mkdir()

    metas = FileDocStore(home).list("refs/ref_travel/videos/")

    want = [p.relative_to(home).as_posix() for p in sorted(videos.glob("*.md")) if p.is_file()]
    assert [m.key for m in metas] == want
    assert "refs/ref_travel/videos/dir.md" not in want
    assert all(m.mtime == (home / m.key).stat().st_mtime for m in metas)
    store = FileDocStore(home)
    for m in metas:                                            # 목록에 나온 키는 모두 문서로 다시 읽힌다(대문자 .MD 포함)
        assert store.exists(m.key) and store.read(m.key).raw == (home / m.key).read_bytes()
        assert store.stat(m.key) == m
    objs = FileObjectStore(home).list("drafts/", "*.srt")
    assert [m.key for m in objs] == [p.relative_to(home).as_posix() for p in sorted((home / "drafts").glob("*.srt"))]
    assert FileDocStore(home).list("refs/ref_none/videos/") == []


def test_folders_and_folder_checks(home: Path) -> None:
    store = FileDocStore(home)
    (home / "refs" / "ref_Zeta").mkdir()
    (home / "refs" / "ref_file.md").write_bytes(b"x")

    got = store.folders("refs/", "ref_*")

    want = [p.relative_to(home).as_posix() + "/" for p in sorted((home / "refs").glob("ref_*")) if p.is_dir()]
    assert got == want and "refs/ref_food/" in got and "refs/ref_file.md/" not in got
    assert store.folder_exists("refs/ref_travel/") and not store.folder_exists("refs/ref_none/")
    store.ensure_folder("refs/ref_new/videos/")
    assert (home / "refs" / "ref_new" / "videos").is_dir()
    store.ensure_folder("refs/ref_new/videos/")                # 이미 있어도 된다
    assert store.label() == str(home)
    with pytest.raises(ValueError):
        store.list("refs/ref_travel/videos")                   # 접두사는 '/'로 끝난다


def test_write_creates_parent_and_returns_version(home: Path) -> None:
    store = FileDocStore(home)
    version = store.write("missions/2026-11-01.md", "---\na: 1\n---\n본문\r\n".encode())
    assert (home / "missions" / "2026-11-01.md").read_bytes() == "---\na: 1\n---\n본문\r\n".encode()
    assert version == store.read("missions/2026-11-01.md").version
    store.write("reviews/new/2026-W45.md", b"x")
    assert (home / "reviews" / "new" / "2026-W45.md").read_bytes() == b"x"


def test_expect_version_conflict_keeps_file(home: Path) -> None:
    store = FileDocStore(home)
    key = f"drafts/{STEM}.md"
    doc = store.read(key)
    (home / key).write_bytes(doc.raw + b"\nedited elsewhere")
    edited = (home / key).read_bytes()

    with pytest.raises(VersionConflict):
        store.write(key, b"mine", expect_version=doc.version)
    assert (home / key).read_bytes() == edited

    new = store.write(key, b"mine", expect_version=hashlib.sha256(edited).hexdigest())
    assert (home / key).read_bytes() == b"mine" and new == hashlib.sha256(b"mine").hexdigest()
    with pytest.raises(VersionConflict):
        store.write("drafts/none.md", b"x", expect_version=new)
    assert not (home / "drafts" / "none.md").exists()


def test_create_refuses_existing_key_and_returns_version(home: Path) -> None:
    store = FileDocStore(home)
    key = f"drafts/{STEM}.md"
    before = (home / key).read_bytes()
    with pytest.raises(FileExistsError):
        store.create(key, b"mine")
    assert (home / key).read_bytes() == before
    fresh = "drafts/brand-new/2026-11-01-new.md"
    version = store.create(fresh, "새 기획안".encode())
    assert (home / fresh).read_bytes() == "새 기획안".encode()
    assert version == hashlib.sha256("새 기획안".encode()).hexdigest()
    with pytest.raises(ValueError):
        store.create("drafts/x.txt", b"x")


def test_create_lets_only_one_concurrent_creator_win(home: Path) -> None:
    key = "drafts/2026-11-01-race.md"
    barrier = threading.Barrier(8, timeout=5)
    results: list[str] = []

    def work(i: int) -> None:
        store = FileDocStore(home)
        barrier.wait()
        try:
            store.create(key, f"creator {i}".encode())
            results.append("ok")
        except FileExistsError:
            results.append("exists")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["exists"] * 7 + ["ok"]
    assert (home / key).read_bytes().startswith(b"creator ")


def test_expect_version_lets_only_one_concurrent_writer_win(home: Path) -> None:
    key = f"drafts/{STEM}.md"
    base = FileDocStore(home).read(key).version
    barrier = threading.Barrier(8, timeout=5)
    results: list[str] = []

    def work(i: int) -> None:
        store = FileDocStore(home)                             # 저장소는 호출마다 새로 만든다(ADR-7)
        barrier.wait()
        try:
            store.write(key, f"writer {i}".encode(), expect_version=base)
            results.append("ok")
        except VersionConflict:
            results.append("conflict")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["conflict"] * 7 + ["ok"]


@pytest.mark.parametrize("key", ["drafts/a.html", "refs/ref_travel/", "yt-coach.json"])
def test_doc_store_rejects_non_md_keys(home: Path, key: str) -> None:
    with pytest.raises(ValueError):
        FileDocStore(home).read(key)
    with pytest.raises(ValueError):
        FileDocStore(home).write(key, b"x")


@pytest.mark.parametrize("key", ["../outside.md", "/abs.md", "a\\b.md", "C:/x.md"])
def test_stores_reject_bad_keys(home: Path, key: str) -> None:
    with pytest.raises(ScriptError) as e:
        FileDocStore(home).read(key)
    assert e.value.code == "bad_path"
    with pytest.raises(ScriptError) as e:
        FileObjectStore(home).put(key.replace(".md", ".json"), b"x")
    assert e.value.code == "bad_path"


# --- FileObjectStore · FileSharedCache --------------------------------------------------

@pytest.mark.parametrize("key", [keys.conti_html(CONTI), keys.srt(CONTI, "captions"), keys.footage_csv(CONTI),
                                 "workspace/typify/travel.json", "yt-coach.json", keys.captions_mp4(CONTI)])
def test_object_round_trip_keeps_bytes(home: Path, key: str) -> None:
    store = FileObjectStore(home)
    raw = (home / key).read_bytes()
    store.put(key, store.get(key))
    assert store.get(key) == raw == (home / key).read_bytes()


def test_object_put_creates_parent_and_delete(home: Path) -> None:
    store = FileObjectStore(home)
    key = f"workspace/conti-art/{STEM}-conti/c2-89abcdef-000000.svg"
    store.put(key, b"<svg/>")
    assert (home / key).read_bytes() == b"<svg/>" and store.exists(key)
    store.delete(key)
    store.delete(key)                                          # 없어도 된다
    assert not (home / key).exists()
    assert store.folder_exists(keys.history_dir(CONTI))
    store.delete_prefix(keys.history_dir(CONTI))
    store.delete_prefix(keys.history_dir(CONTI))
    assert not store.folder_exists(keys.history_dir(CONTI))
    assert store.list(keys.history_dir(CONTI)) == []


def test_object_list_returns_files_only(home: Path) -> None:
    metas = FileObjectStore(home).list(f"workspace/conti-art/{STEM}-conti/")
    assert [m.key for m in metas] == [f"workspace/conti-art/{STEM}-conti/c1-0123abcd-a1b2c3.svg"]
    assert FileObjectStore(home).list("drafts/", "*-cuts") == []


def test_local_and_write_path_are_real_paths(home: Path) -> None:
    store = FileObjectStore(home)
    with store.local_path(keys.conti_html(CONTI)) as p:
        assert p == home / keys.conti_html(CONTI) and p.is_file()
    key = "workspace/footage/0123456789ab-10-20/frames/0001.jpg"
    with store.write_path(key) as p:
        assert p == home / key and p.parent.is_dir()
        p.write_bytes(b"jpg")
    assert store.get(key) == b"jpg"
    with store.write_path("workspace/footage/0123456789ab-10-20/montage/") as p:
        assert p == home / "workspace" / "footage" / "0123456789ab-10-20" / "montage" and p.is_dir()
    assert store.display_path(keys.conti_html(CONTI)) == str(home / keys.conti_html(CONTI))
    with FileDocStore(home).local_path(CONTI) as p:
        assert p == home / CONTI


def test_object_store_rejects_md_keys(home: Path) -> None:
    with pytest.raises(ValueError):
        FileObjectStore(home).get(CONTI)
    with pytest.raises(ValueError):
        FileObjectStore(home).get("drafts/Idea.MD")
    with pytest.raises(ValueError):
        FileObjectStore(home).put(CONTI, b"x")


def test_shared_cache_only_takes_shared_prefixes(home: Path) -> None:
    shared = FileSharedCache(home)
    assert shared.get(keys.raw(VID)) == (home / keys.raw(VID)).read_bytes()
    assert [m.key for m in shared.list(keys.frames_dir(VID), "*.jpg")] == [
        f"workspace/frames/{VID}/0001.jpg", f"workspace/frames/{VID}/0002.jpg"]
    for key in ["workspace/typify/travel.json", keys.conti_html(CONTI), "workspace/rawx/a.json"]:
        with pytest.raises(ValueError):
            shared.get(key)
    with pytest.raises(ValueError):
        shared.list("workspace/")


# --- FileScratch -----------------------------------------------------------------------

def test_scratch_paths(home: Path, tmp_path: Path) -> None:
    s = FileScratch(home)
    tmp = s.tmp_path("cuts_abc.json")
    assert tmp == home / "workspace" / "tmp" / "cuts_abc.json" and tmp.parent.is_dir()
    assert s.local_path("timings.json") == home / "workspace" / "timings.json"
    assert s.local_path("library") == home / "workspace" / "library"
    assert s.arg_path("drafts/a.json") == home / "drafts" / "a.json"
    assert s.arg_path(tmp_path / "x.json") == tmp_path / "x.json"
    assert s.arg_path(str(tmp_path / "x.json")) == tmp_path / "x.json"
    assert s.workdir() == home
    with pytest.raises(ScriptError):
        s.tmp_path("../x.json")
    with pytest.raises(ScriptError):
        s.local_path("..", "x")


def test_scratch_key_of(home: Path, tmp_path: Path) -> None:
    s = FileScratch(home)
    assert s.key_of(home / CONTI) == CONTI
    assert s.key_of(CONTI) == CONTI
    assert s.key_of(str(home / "refs" / "ref_travel" / "videos" / f"{VID}.md")) == keys.video_md("travel", VID)
    assert s.key_of("drafts/../profile.md") == "profile.md"
    for bad in [tmp_path / "outside.md", "../outside.md", "drafts/../../x.md", home]:
        with pytest.raises(ScriptError) as e:
            s.key_of(bad)
        assert e.value.code == "bad_path"


# --- refstore 도우미 -----------------------------------------------------------------

@pytest.mark.parametrize("newline", [None, "", "\n", "\r\n", "\r"])
@pytest.mark.parametrize("text", ["", "a\nb\n", "a\r\nb", "한글 🏆\n\n끝", "{\n \"a\": 1\n}"])
def test_text_bytes_equals_write_text(tmp_path: Path, text: str, newline: str | None) -> None:
    p = tmp_path / "t.txt"
    p.write_text(text, encoding="utf-8", newline=newline)
    assert refstore.text_bytes(text, newline) == p.read_bytes()
    if newline is None:
        assert refstore.text_bytes(text) == p.read_bytes()


def test_write_atomic_temp_name_has_pid_and_thread(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []
    real = os.replace
    monkeypatch.setattr(refstore.os, "replace", lambda a, b: (seen.append(Path(a).name), real(a, b))[1])

    refstore.write_atomic(tmp_path / "0001.json", b"{}")
    refstore.write_atomic(tmp_path / "a.md", b"x")

    assert seen == [f".0001.json.{os.getpid()}.{threading.get_ident()}.tmp",
                    f".a.md.{os.getpid()}.{threading.get_ident()}.tmp"]
    assert not any(fnmatch.fnmatch(n, "*.md") or conti_history._VERSION_RE.match(n) for n in seen)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["0001.json", "a.md"]


def test_write_atomic_survives_concurrent_writers(tmp_path: Path) -> None:
    target = tmp_path / "same.md"
    barrier = threading.Barrier(6, timeout=5)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        barrier.wait()
        try:
            for _ in range(20):
                refstore.write_atomic(target, f"writer {i}".encode() * 100)
        except OSError as e:                                # 교체 재시도가 끝까지 막힌 경우만 남긴다
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert [p.name for p in tmp_path.iterdir()] == ["same.md"]
    assert target.read_bytes() in {f"writer {i}".encode() * 100 for i in range(6)}
    assert all(isinstance(e, PermissionError) for e in errors)


def test_write_locks_are_bounded_and_stable(tmp_path: Path) -> None:
    from scripts import filestore
    locks = {id(filestore._write_lock(tmp_path / f"f{i}.md")) for i in range(2000)}
    assert len(locks) <= filestore.LOCK_STRIPES
    assert filestore._write_lock(tmp_path / "a.md") is filestore._write_lock(tmp_path / "x" / ".." / "a.md")
