"""PgDocStore만의 규칙(서버 설계 §3.3): 표시 문자열, 임시 local_path, mtime_ns 단조, 사용자 격리, 원문 바이트 저장.

공통 계약(File과 같은 단언)은 `tests/contract/test_stores.py`. 여기는 FileStore와 값이 원래 다른 부분만 둔다.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest

from tests.helpers.pg import pg_doc_store

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def store(request: pytest.FixtureRequest) -> Any:
    return pg_doc_store(request, "a@example.com")


@pytest.fixture
def other(request: pytest.FixtureRequest, store: Any) -> Any:
    return pg_doc_store(request, "b@example.com")


def test_label_is_edition_and_email(store: Any) -> None:
    assert store.label() == "ytc:a@example.com"


def test_doc_label_is_the_key(store: Any) -> None:
    store.write("drafts/a.md", b"x")

    assert store.read("drafts/a.md").label == "drafts/a.md"


def test_parse_error_message_names_the_key(store: Any) -> None:
    from scripts.common import ScriptError

    store.write("drafts/broken.md", b"---\nid: [unclosed\n---\n")

    with pytest.raises(ScriptError) as e:
        _ = store.read("drafts/broken.md").parsed
    assert "drafts/broken.md" in e.value.message


def test_local_path_is_a_temp_copy_removed_afterwards(store: Any) -> None:
    store.write("drafts/a.md", b"abc")

    with store.local_path("drafts/a.md") as path:
        assert path.read_bytes() == b"abc"
        path.write_bytes(b"changed locally")

    assert not path.exists()
    assert not path.parent.exists()
    assert store.read("drafts/a.md").raw == b"abc"


def test_mtime_ns_strictly_increases_on_quick_rewrites(store: Any) -> None:
    stamps = []
    for _ in range(20):
        store.write("drafts/a.md", b"same size")
        stamps.append(store.stat("drafts/a.md").mtime_ns)

    assert all(a < b for a, b in zip(stamps, stamps[1:], strict=False))


def test_mtime_is_mtime_ns_over_1e9_exactly(store: Any) -> None:
    store.write("drafts/a.md", b"x")

    meta = store.stat("drafts/a.md")

    assert meta.mtime == meta.mtime_ns / 1e9


def test_same_key_is_separate_per_user(store: Any, other: Any) -> None:
    store.write("drafts/a.md", b"from a")
    store.ensure_folder("refs/ref_a/")

    assert other.exists("drafts/a.md") is False
    with pytest.raises(FileNotFoundError):
        other.read("drafts/a.md")
    assert other.list("drafts/") == []
    assert other.folders("refs/", "*") == []
    assert other.folder_exists("refs/") is False

    other.create("drafts/a.md", b"from b")
    assert store.read("drafts/a.md").raw == b"from a"
    assert other.read("drafts/a.md").raw == b"from b"


def test_row_holds_raw_bytes_version_size(store: Any, request: pytest.FixtureRequest) -> None:
    data = b"\xef\xbb\xbf---\r\nid: x\r\n---\r\n"
    store.write("drafts/a.md", data)

    owner = request.getfixturevalue("migrated_db").owner_dsn
    with psycopg.connect(owner) as conn:
        row = conn.execute("SELECT raw, version, size FROM docs WHERE key = 'drafts/a.md'").fetchone()
        folders = [r[0] for r in conn.execute("SELECT prefix FROM folders ORDER BY prefix").fetchall()]
    assert (bytes(row[0]), row[1], row[2]) == (data, hashlib.sha256(data).hexdigest(), len(data))
    assert folders == ["drafts/"]


def test_store_accepts_uuid_string(request: pytest.FixtureRequest) -> None:
    from server.pgstore import PgDocStore

    db = request.getfixturevalue("pg_db")
    store = PgDocStore(db, str(uuid.uuid4()), "nobody@example.com")

    assert store.exists("drafts/a.md") is False


def test_folders_include_object_keys(store: Any, request: pytest.FixtureRequest) -> None:
    """폴더는 문서·객체 키 모두에서 유도한다(같은 작업 폴더 트리, §3.3). 객체 행은 T7 전까지 DB에 직접 넣는다."""
    owner = request.getfixturevalue("migrated_db").owner_dsn
    with psycopg.connect(owner) as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = 'a@example.com'").fetchone()[0]
        conn.execute("INSERT INTO objects (user_id, key, size, sha256, mtime_ns, s3_key) "
                     "VALUES (%s, 'workspace/conti_history/x/0001.json', 1, repeat('0', 64), 1, "
                     "'users/' || %s::text || '/workspace/conti_history/x/0001.json')", (uid, uid))

    assert store.folders("workspace/", "*") == ["workspace/conti_history/"]
    assert store.folder_exists("workspace/conti_history/x/")
    assert store.list("workspace/conti_history/x/", "*") == []


def test_mtime_ns_moves_forward_even_if_the_clock_does_not(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """conti_history 캐시 스탬프(mtime_ns, size)는 다시 쓰면 바뀌어야 한다: 시계가 멈추거나 뒤로 가도(NTP)."""
    import server.pgstore as pgstore

    monkeypatch.setattr(pgstore, "_now_ns", lambda: 1_000)
    store.write("drafts/a.md", b"same size")
    first = store.stat("drafts/a.md").mtime_ns
    monkeypatch.setattr(pgstore, "_now_ns", lambda: 500)

    store.write("drafts/a.md", b"same size")

    assert store.stat("drafts/a.md").mtime_ns == first + 1
