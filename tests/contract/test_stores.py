"""저장소 계약(서버 설계 §3.3·§10-1): DocStore Protocol의 규칙을 FileDocStore·PgDocStore에서 같은 단언으로 고정한다.

각 테스트는 `doc_store` fixture(`file`·`pg` 매개변수)를 받는다. 대소문자 섞인 이름의 일치·순서는
Windows FileStore가 기준이라(원본 작업 폴더가 Windows에서 만들어졌다), `file` 매개변수는 Windows에서만 돌고
`pg`는 어디서나 같은 고정 기대 목록을 낸다.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import PureWindowsPath
from typing import Any

import pytest

from scripts.common import ScriptError
from scripts.stores import DocStore, Meta, VersionConflict

THREADS = 6

CRLF_DOC = b"---\r\nid: x\r\n---\r\n# \xec\xa0\x9c\xeb\xaa\xa9\r\n"
BOM_DOC = b"\xef\xbb\xbf---\nid: x\n---\nbody\n"
EMOJI_DOC = "---\nid: x\ntitle: 제주 🏆 한달살기\n---\n본문 🏆\n".encode()
MEMO = "## 내 메모".encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _with_memo(body: str, memo: str | None) -> bytes:
    text = f"---\nid: x\n---\n# {body}\n"
    if memo is not None:
        text += f"\n## 내 메모\n{memo}\n"
    return text.encode()


def _case_sensitive_fs(doc_store: DocStore, backend: str) -> None:
    """대소문자 섞인 이름 테스트: FileStore는 Windows(대소문자 무시 glob·정렬)에서만 기준이 된다."""
    if backend == "file" and os.name != "nt":
        pytest.skip("FileStore의 대소문자 무시 목록은 Windows 기준(리눅스 FileStore는 대소문자 구분, 서버 설계 §12)")


def _run_together(n: int, work: Callable[[int], Any]) -> list[tuple[str, Any]]:
    """n개 스레드가 Barrier에서 함께 출발해 work(i)를 한다. -> [("ok", 결과) | ("err", 예외)] (스레드 순서)."""
    barrier = threading.Barrier(n)
    results: list[tuple[str, Any]] = [("err", RuntimeError("not run"))] * n

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = ("ok", work(i))
        except Exception as e:  # noqa: BLE001 - the test inspects every outcome
            results[i] = ("err", e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    return results


# --- 바이트 왕복·version ----------------------------------------------------------------

ROUND_TRIP = [
    pytest.param("drafts/lf.md", b"---\nid: x\n---\nbody\n", id="lf"),
    pytest.param("drafts/crlf.md", CRLF_DOC, id="crlf"),
    pytest.param("drafts/bom.md", BOM_DOC, id="bom"),
    pytest.param("drafts/2026-10-30-제주-한달살기.md", EMOJI_DOC, id="korean-name"),
    pytest.param("drafts/🏆-기록.md", EMOJI_DOC, id="emoji-name"),
    pytest.param("refs/ref_travel/videos/-C8U1fizcms.md", b"---\r\nid: -C8U1fizcms\r\n---\r\n", id="nested"),
    pytest.param("profile.md", b"", id="top-level-empty"),
    pytest.param("drafts/raw.md", bytes(range(256)), id="non-utf8-bytes"),
]


@pytest.mark.parametrize(("key", "data"), ROUND_TRIP)
def test_write_then_read_returns_same_bytes_and_version(doc_store: DocStore, key: str, data: bytes) -> None:
    version = doc_store.write(key, data)

    doc = doc_store.read(key)
    assert (doc.key, doc.raw, doc.version, version) == (key, data, _sha(data), _sha(data))
    assert doc_store.exists(key)
    assert doc_store.stat(key).size == len(data)
    assert doc_store.stat(key).key == key


@pytest.mark.parametrize(("key", "data"), ROUND_TRIP)
def test_create_then_read_returns_same_bytes_and_version(doc_store: DocStore, key: str, data: bytes) -> None:
    version = doc_store.create(key, data)

    assert version == _sha(data)
    assert doc_store.read(key).raw == data


def test_write_overwrites_and_returns_new_version(doc_store: DocStore) -> None:
    doc_store.write("drafts/a.md", b"one\n")

    version = doc_store.write("drafts/a.md", b"two\r\n")

    assert version == _sha(b"two\r\n")
    assert doc_store.read("drafts/a.md").raw == b"two\r\n"


def test_doc_crlf_and_bom_helpers_see_stored_bytes(doc_store: DocStore) -> None:
    doc_store.write("drafts/crlf.md", CRLF_DOC)
    doc_store.write("drafts/bom.md", BOM_DOC)

    assert doc_store.read("drafts/crlf.md").nl == "\r\n"
    assert doc_store.read("drafts/bom.md").nl == "\n"
    assert doc_store.read("drafts/bom.md").parsed[0] == {"id": "x"}


# --- 없는 키 -----------------------------------------------------------------------------

def test_missing_key_read_and_stat_raise_file_not_found(doc_store: DocStore) -> None:
    with pytest.raises(FileNotFoundError):
        doc_store.read("drafts/none.md")
    with pytest.raises(FileNotFoundError):
        doc_store.stat("drafts/none.md")
    assert doc_store.exists("drafts/none.md") is False


def test_missing_key_local_path_yields_a_path_that_does_not_exist(doc_store: DocStore) -> None:
    with doc_store.local_path("drafts/none.md") as path:
        assert path.name == "none.md"
        assert not path.exists()
        with pytest.raises(FileNotFoundError):
            path.read_bytes()


def test_missing_prefix_lists_nothing(doc_store: DocStore) -> None:
    assert doc_store.list("drafts/") == []
    assert doc_store.folders("refs/", "ref_*") == []
    assert doc_store.folder_exists("drafts/") is False


# --- 키 검사 -----------------------------------------------------------------------------

NOT_DOC = ["drafts/a-conti.html", "workspace/raw/x.json", "yt-coach.json", "drafts/a.md.bak"]
BAD_DOC = ["../x.md", "drafts/../x.md", "drafts\\x.md", "c:/x.md", "drafts//x.md", "drafts/con.md",
           "drafts/x./a.md", "drafts/x /a.md", "/abs.md", "drafts/\x00.md"]


@pytest.mark.parametrize("key", NOT_DOC)
def test_non_md_key_raises_value_error_everywhere(doc_store: DocStore, key: str) -> None:
    for call in (lambda: doc_store.read(key), lambda: doc_store.exists(key), lambda: doc_store.stat(key),
                 lambda: doc_store.write(key, b"x"), lambda: doc_store.create(key, b"x")):
        with pytest.raises(ValueError):
            call()
    with pytest.raises(ValueError), doc_store.local_path(key):
        pass


def test_md_extension_is_case_insensitive(doc_store: DocStore) -> None:
    doc_store.write("drafts/Idea.MD", b"x\n")

    assert doc_store.read("drafts/Idea.MD").raw == b"x\n"


@pytest.mark.parametrize("key", BAD_DOC)
def test_bad_doc_key_raises_bad_path(doc_store: DocStore, key: str) -> None:
    for call in (lambda: doc_store.read(key), lambda: doc_store.exists(key), lambda: doc_store.stat(key),
                 lambda: doc_store.write(key, b"x"), lambda: doc_store.create(key, b"x")):
        with pytest.raises(ScriptError) as e:
            call()
        assert e.value.code == "bad_path"
    with pytest.raises(ScriptError) as e, doc_store.local_path(key):
        pass
    assert e.value.code == "bad_path"


@pytest.mark.parametrize("prefix", ["drafts", "refs/ref_a"])
def test_prefix_without_slash_raises_value_error(doc_store: DocStore, prefix: str) -> None:
    for call in (lambda: doc_store.list(prefix), lambda: doc_store.folders(prefix, "*"),
                 lambda: doc_store.folder_exists(prefix), lambda: doc_store.ensure_folder(prefix)):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("prefix", ["../", "drafts/../", "c:/", "con/", "a\\b/"])
def test_bad_prefix_raises_bad_path(doc_store: DocStore, prefix: str) -> None:
    for call in (lambda: doc_store.list(prefix), lambda: doc_store.folders(prefix, "*"),
                 lambda: doc_store.folder_exists(prefix), lambda: doc_store.ensure_folder(prefix)):
        with pytest.raises(ScriptError) as e:
            call()
        assert e.value.code == "bad_path"


# --- create (O_EXCL) ---------------------------------------------------------------------

def test_create_existing_raises_file_exists_and_keeps_bytes(doc_store: DocStore) -> None:
    doc_store.create("drafts/a.md", b"first\n")

    with pytest.raises(FileExistsError):
        doc_store.create("drafts/a.md", b"second\n")

    assert doc_store.read("drafts/a.md").raw == b"first\n"


def test_create_over_written_doc_raises_file_exists(doc_store: DocStore) -> None:
    doc_store.write("drafts/a.md", _with_memo("t", "메모"))

    with pytest.raises(FileExistsError):
        doc_store.create("drafts/a.md", b"no memo\n")


def test_concurrent_create_has_exactly_one_winner(doc_store: DocStore) -> None:
    results = _run_together(THREADS, lambda i: doc_store.create("drafts/race.md", f"writer {i}\n".encode()))

    winners = [i for i, (state, _) in enumerate(results) if state == "ok"]
    assert len(winners) == 1
    assert all(isinstance(err, FileExistsError) for state, err in results if state == "err")
    assert doc_store.read("drafts/race.md").raw == f"writer {winners[0]}\n".encode()


# --- write(expect_version) ---------------------------------------------------------------

def test_write_with_matching_version_succeeds(doc_store: DocStore) -> None:
    v1 = doc_store.write("drafts/a.md", b"one\n")

    v2 = doc_store.write("drafts/a.md", b"two\n", expect_version=v1)

    assert v2 == _sha(b"two\n")
    assert doc_store.read("drafts/a.md").raw == b"two\n"


def test_write_with_stale_version_raises_conflict_and_keeps_bytes(doc_store: DocStore) -> None:
    v1 = doc_store.write("drafts/a.md", b"one\n")
    doc_store.write("drafts/a.md", b"two\n")

    with pytest.raises(VersionConflict):
        doc_store.write("drafts/a.md", b"three\n", expect_version=v1)

    assert doc_store.read("drafts/a.md").raw == b"two\n"


def test_write_with_version_to_missing_key_raises_conflict_and_creates_nothing(doc_store: DocStore) -> None:
    with pytest.raises(VersionConflict):
        doc_store.write("drafts/none.md", b"x\n", expect_version=_sha(b"x\n"))

    assert doc_store.exists("drafts/none.md") is False
    assert doc_store.folder_exists("drafts/") is False


def test_write_without_version_creates_missing_doc(doc_store: DocStore) -> None:
    version = doc_store.write("drafts/new.md", b"x\n")

    assert version == _sha(b"x\n")
    assert doc_store.exists("drafts/new.md")


def test_concurrent_cas_writes_have_exactly_one_winner(doc_store: DocStore) -> None:
    v0 = doc_store.write("drafts/cas.md", b"base\n")

    results = _run_together(THREADS, lambda i: doc_store.write("drafts/cas.md", f"writer {i}\n".encode(),
                                                               expect_version=v0))

    winners = [i for i, (state, _) in enumerate(results) if state == "ok"]
    assert len(winners) == 1
    assert all(isinstance(err, VersionConflict) for state, err in results if state == "err")
    assert doc_store.read("drafts/cas.md").raw == f"writer {winners[0]}\n".encode()


# --- 내 메모 가드 ------------------------------------------------------------------------

def test_write_dropping_memo_raises_memo_lost_and_keeps_bytes(doc_store: DocStore) -> None:
    old = _with_memo("t", "내 생각")
    doc_store.write("drafts/m.md", old)

    with pytest.raises(ScriptError) as e:
        doc_store.write("drafts/m.md", _with_memo("t2", None))

    assert e.value.code == "memo_lost"
    assert doc_store.read("drafts/m.md").raw == old


def test_write_changing_memo_raises_memo_lost_even_with_matching_version(doc_store: DocStore) -> None:
    v1 = doc_store.write("drafts/m.md", _with_memo("t", "내 생각"))

    with pytest.raises(ScriptError) as e:
        doc_store.write("drafts/m.md", _with_memo("t", "다른 생각"), expect_version=v1)

    assert e.value.code == "memo_lost"


def test_write_keeping_memo_succeeds(doc_store: DocStore) -> None:
    doc_store.write("drafts/m.md", _with_memo("t", "내 생각"))

    doc_store.write("drafts/m.md", _with_memo("새 제목", "내 생각").replace(b"\n", b"\r\n"))

    assert MEMO in doc_store.read("drafts/m.md").raw


def test_write_over_doc_without_memo_is_not_guarded(doc_store: DocStore) -> None:
    doc_store.write("drafts/m.md", _with_memo("t", None))

    doc_store.write("drafts/m.md", b"anything\n")

    assert doc_store.read("drafts/m.md").raw == b"anything\n"


def test_concurrent_unversioned_writes_never_drop_a_memo(doc_store: DocStore) -> None:
    """메모 있는 쓰기와 메모 없는 쓰기가 없는 키에 동시에: 어느 순서든 마지막 바이트에는 메모가 남는다."""
    for round_no in range(5):
        key = f"drafts/memo-race-{round_no}.md"
        datas = [_with_memo("with", "지킬 메모") if i == 0 else _with_memo(f"plain {i}", None) for i in range(THREADS)]

        results = _run_together(THREADS, lambda i, k=key, d=datas: doc_store.write(k, d[i]))

        assert results[0][0] == "ok"
        assert all(state == "ok" or (isinstance(err, ScriptError) and err.code == "memo_lost")
                   for state, err in results)
        assert MEMO in doc_store.read(key).raw


# --- list: 바로 아래·대소문자 무시 일치·Windows 순서 ---------------------------------------

# "🏆.md": a character above U+FFFF right after the prefix (a `prefix || U&'\FFFF'` range bound would miss it)
LIST_NAMES = ["b.md", "A.md", "c.MD", "_x.md", "2026-🏆.md", "가.md", "a-conti.md", "B2.md", "🏆.md"]
LIST_ORDER = ["2026-🏆.md", "_x.md", "a-conti.md", "A.md", "b.md", "B2.md", "c.MD", "가.md", "🏆.md"]


def _seed_list(doc_store: DocStore) -> None:
    for name in LIST_NAMES:
        doc_store.write(f"drafts/{name}", name.encode())
    doc_store.write("drafts/sub/d.md", b"d")
    doc_store.write("drafts/sub/deeper/e.md", b"e")
    doc_store.write("mine/z.md", b"z")


def test_list_order_constant_is_windows_path_order() -> None:
    assert sorted(LIST_NAMES, key=PureWindowsPath) == LIST_ORDER
    assert sorted(LIST_NAMES) != LIST_ORDER  # a plain (C) string sort would differ


def test_list_returns_direct_children_in_windows_order(doc_store: DocStore, backend: str) -> None:
    _case_sensitive_fs(doc_store, backend)
    _seed_list(doc_store)

    metas = doc_store.list("drafts/")

    assert [m.key for m in metas] == [f"drafts/{n}" for n in LIST_ORDER]
    assert [m.size for m in metas] == [len(n.encode()) for n in LIST_ORDER]


@pytest.mark.parametrize(("pattern", "expected"), [
    ("a*", ["a-conti.md", "A.md"]),
    ("*-CONTI.md", ["a-conti.md"]),
    ("C.md", ["c.MD"]),
    ("[AB]*", ["a-conti.md", "A.md", "b.md", "B2.md"]),
    ("?.md", ["A.md", "b.md", "c.MD", "가.md", "🏆.md"]),
    ("*", LIST_ORDER),
    ("*.txt", []),
])
def test_list_pattern_matches_case_insensitively(doc_store: DocStore, backend: str, pattern: str,
                                                 expected: list[str]) -> None:
    _case_sensitive_fs(doc_store, backend)
    _seed_list(doc_store)

    assert [m.key for m in doc_store.list("drafts/", pattern)] == [f"drafts/{n}" for n in expected]


def test_list_does_not_descend_or_return_folders(doc_store: DocStore) -> None:
    doc_store.write("drafts/sub/d.md", b"d")
    doc_store.write("drafts/sub/deeper/e.md", b"e")
    doc_store.write("drafts/top.md", b"t")

    assert [m.key for m in doc_store.list("drafts/")] == ["drafts/top.md"]
    assert [m.key for m in doc_store.list("drafts/sub/")] == ["drafts/sub/d.md"]
    assert [m.key for m in doc_store.list("drafts/sub/deeper/")] == ["drafts/sub/deeper/e.md"]


def test_list_metas_equal_stat(doc_store: DocStore) -> None:
    doc_store.write("mine/a.md", b"a")
    doc_store.write("mine/b.md", b"bb")

    assert doc_store.list("mine/") == [doc_store.stat("mine/a.md"), doc_store.stat("mine/b.md")]


def test_list_exact_video_name_finds_only_that_video(doc_store: DocStore) -> None:
    doc_store.write("refs/ref_a/videos/-C8U1fizcms.md", b"1")
    doc_store.write("refs/ref_a/videos/Q8PSsikq8PA.md", b"2")

    assert [m.key for m in doc_store.list("refs/ref_a/videos/", "-C8U1fizcms.md")] == \
        ["refs/ref_a/videos/-C8U1fizcms.md"]


# --- folders·folder_exists·ensure_folder ---------------------------------------------------

def test_folders_lists_direct_child_folders_matching_pattern(doc_store: DocStore, backend: str) -> None:
    _case_sensitive_fs(doc_store, backend)
    doc_store.write("refs/ref_a/videos/x.md", b"x")
    doc_store.ensure_folder("refs/ref_b/")
    doc_store.ensure_folder("refs/Ref_C/")
    doc_store.write("refs/other/index.md", b"i")
    doc_store.write("refs/ref_file.md", b"not a folder")

    assert doc_store.folders("refs/", "ref_*") == ["refs/ref_a/", "refs/ref_b/", "refs/Ref_C/"]
    assert doc_store.folders("refs/", "*") == ["refs/other/", "refs/ref_a/", "refs/ref_b/", "refs/Ref_C/"]
    assert doc_store.folders("refs/ref_a/", "*") == ["refs/ref_a/videos/"]
    assert doc_store.folders("refs/ref_b/", "*") == []


def test_folders_include_ensured_empty_folders_and_doc_parents(doc_store: DocStore) -> None:
    doc_store.ensure_folder("refs/ref_empty/")
    doc_store.write("refs/ref_full/index.md", b"i")

    assert doc_store.folders("refs/", "ref_*") == ["refs/ref_empty/", "refs/ref_full/"]


def test_folder_exists_for_written_doc_ancestors(doc_store: DocStore) -> None:
    doc_store.write("refs/ref_a/videos/x.md", b"x")

    assert doc_store.folder_exists("refs/")
    assert doc_store.folder_exists("refs/ref_a/")
    assert doc_store.folder_exists("refs/ref_a/videos/")
    assert not doc_store.folder_exists("refs/ref_z/")
    assert not doc_store.folder_exists("refs/ref_a/videos/x.md/")


def test_folder_exists_for_created_doc_ancestors(doc_store: DocStore) -> None:
    doc_store.create("missions/2026-11-01.md", b"m")

    assert doc_store.folder_exists("missions/")


def test_ensure_folder_creates_folder_and_ancestors_and_is_idempotent(doc_store: DocStore) -> None:
    doc_store.ensure_folder("refs/ref_a/videos/")
    doc_store.ensure_folder("refs/ref_a/videos/")

    assert doc_store.folder_exists("refs/")
    assert doc_store.folder_exists("refs/ref_a/")
    assert doc_store.folder_exists("refs/ref_a/videos/")
    assert doc_store.list("refs/ref_a/videos/") == []
    assert doc_store.folders("refs/", "*") == ["refs/ref_a/"]


def test_failed_writes_do_not_create_folders(doc_store: DocStore) -> None:
    doc_store.write("drafts/m.md", _with_memo("t", "메모"))
    with pytest.raises(ScriptError):
        doc_store.write("drafts/m.md", b"x")
    with pytest.raises(VersionConflict):
        doc_store.write("mine/none.md", b"x", expect_version=_sha(b"y"))

    assert not doc_store.folder_exists("mine/")


# --- Meta ---------------------------------------------------------------------------------

def test_meta_mtime_is_write_time_and_matches_mtime_ns(doc_store: DocStore) -> None:
    before = time.time_ns()
    doc_store.write("drafts/a.md", b"abc")
    after = time.time_ns()

    meta = doc_store.stat("drafts/a.md")

    assert isinstance(meta, Meta)
    assert (meta.key, meta.size) == ("drafts/a.md", 3)
    assert before - 2_000_000_000 <= meta.mtime_ns <= after + 2_000_000_000
    assert meta.mtime == pytest.approx(meta.mtime_ns / 1e9, abs=1e-6)


def test_meta_mtime_ns_does_not_go_back_on_rewrite(doc_store: DocStore) -> None:
    doc_store.write("drafts/a.md", b"one")
    first = doc_store.stat("drafts/a.md")
    time.sleep(0.02)

    doc_store.write("drafts/a.md", b"two")                # same size: only mtime_ns can move the stamp
    second = doc_store.stat("drafts/a.md")

    assert second.size == first.size
    assert second.mtime_ns > first.mtime_ns


# --- local_path ---------------------------------------------------------------------------

def test_local_path_holds_the_doc_bytes_under_its_file_name(doc_store: DocStore) -> None:
    doc_store.write("drafts/2026-제주.md", CRLF_DOC)

    with doc_store.local_path("drafts/2026-제주.md") as path:
        assert path.name == "2026-제주.md"
        assert path.read_bytes() == CRLF_DOC


def test_label_is_a_non_empty_string(doc_store: DocStore) -> None:
    assert isinstance(doc_store.label(), str)
    assert doc_store.label()
