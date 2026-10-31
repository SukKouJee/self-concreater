"""S3ObjectStore·S3SharedCache만의 규칙(서버 설계 §3.2~§3.5): 객체 배치, 쓰기 순서·실패 보상, 임시 경로, mtime_ns 단조.

공통 계약(File과 같은 단언)은 `tests/contract/test_stores.py`. 여기는 FileStore와 값이 원래 다르거나
(표시 경로, 폴더 write_path가 예전 내용을 비움) File에서 재현할 수 없는 것(실패 주입, 행·객체 대조)만 둔다.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from scripts.common import ScriptError
from server import s3store
from server.s3store import S3Error, shared_s3_key
from tests.helpers.pg import pg_object_store, pg_shared_cache

VID = "-C8U1fizcms"


# --- shared_s3_key (Docker 없이) ------------------------------------------------------------

@pytest.mark.parametrize(("key", "expected"), [
    (f"workspace/raw/{VID}.json", f"shared/videos/{VID}/raw.json"),
    (f"workspace/cuts/{VID}.json", f"shared/videos/{VID}/cuts.json"),
    (f"workspace/shots/{VID}.json", f"shared/videos/{VID}/shots.json"),
    (f"workspace/frames/{VID}/0003.jpg", f"shared/videos/{VID}/frames/0003.jpg"),
    ("workspace/frames/abc_DEF-1/0001.jpg", "shared/videos/abc_DEF-1/frames/0001.jpg"),
])
def test_shared_s3_key_maps_each_cache_kind(key: str, expected: str) -> None:
    assert shared_s3_key(key) == expected


@pytest.mark.parametrize("key", [
    "workspace/raw/a/b.json", "workspace/raw/x.txt", "workspace/raw/.json", "workspace/cuts/a.b.json",
    "workspace/frames/x/", "workspace/frames/" + "v" * 65 + "/0001.jpg", "workspace/frames/a.b/0001.jpg",
    "workspace/typify/x.json", "drafts/a-conti.html",
])
def test_shared_s3_key_rejects_other_shapes(key: str) -> None:
    with pytest.raises(ValueError):
        shared_s3_key(key)


def test_shared_s3_key_checks_the_key_first() -> None:
    with pytest.raises(ScriptError) as e:
        shared_s3_key("workspace/raw/../x.json")
    assert e.value.code == "bad_path"


# --- fixtures ---------------------------------------------------------------------------------

@pytest.fixture
def store(request: pytest.FixtureRequest) -> Any:
    return pg_object_store(request, "a@example.com")


@pytest.fixture
def shared(request: pytest.FixtureRequest) -> Any:
    return pg_shared_cache(request, "s@example.com")


@pytest.fixture
def bucket(request: pytest.FixtureRequest) -> Any:
    return request.getfixturevalue("s3_bucket")


def _rows(request: pytest.FixtureRequest, table: str) -> list[tuple[Any, ...]]:
    """owner 연결로 본 인덱스 행 전부(RLS 무시)."""
    import psycopg

    owner = request.getfixturevalue("migrated_db").owner_dsn
    query = ("SELECT key, size, sha256, s3_key FROM objects ORDER BY key" if table == "objects"
             else "SELECT key, size, sha256, s3_key FROM shared_objects ORDER BY key")
    with psycopg.connect(owner) as conn:
        return [tuple(r) for r in conn.execute(query).fetchall()]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- 배치 -------------------------------------------------------------------------------------

def test_user_object_lives_under_users_uid_key(store: Any, bucket: Any, request: pytest.FixtureRequest) -> None:
    store.put("drafts/a-conti.html", b"<html>")

    uid = store._uid
    assert bucket.keys("") == [f"users/{uid}/drafts/a-conti.html"]
    assert _rows(request, "objects") == [("drafts/a-conti.html", 6, _sha(b"<html>"),
                                          f"users/{uid}/drafts/a-conti.html")]


def test_shared_object_lives_under_shared_videos(shared: Any, bucket: Any, request: pytest.FixtureRequest) -> None:
    shared.put(f"workspace/raw/{VID}.json", b"{}")
    shared.put(f"workspace/frames/{VID}/0001.jpg", b"j")

    assert bucket.keys("") == [f"shared/videos/{VID}/frames/0001.jpg", f"shared/videos/{VID}/raw.json"]
    assert [(r[0], r[3]) for r in _rows(request, "shared_objects")] == [
        (f"workspace/frames/{VID}/0001.jpg", f"shared/videos/{VID}/frames/0001.jpg"),
        (f"workspace/raw/{VID}.json", f"shared/videos/{VID}/raw.json")]


def test_shared_row_remembers_first_writer(request: pytest.FixtureRequest) -> None:
    import psycopg

    a = pg_shared_cache(request, "a@example.com")
    b = pg_shared_cache(request, "b@example.com")
    a.put(f"workspace/raw/{VID}.json", b"1")
    b.put(f"workspace/raw/{VID}.json", b"22")

    with psycopg.connect(request.getfixturevalue("migrated_db").owner_dsn) as conn:
        row = conn.execute("SELECT created_by, size FROM shared_objects").fetchone()
    assert row is not None
    assert (row[0], row[1]) == (a._uid, 2)


def test_display_path_is_the_key(store: Any, shared: Any) -> None:
    assert store.display_path("drafts/a-conti.html") == "drafts/a-conti.html"
    assert shared.display_path(f"workspace/raw/{VID}.json") == f"workspace/raw/{VID}.json"


def test_put_rejects_objects_over_the_size_cap(store: Any, bucket: Any, monkeypatch: pytest.MonkeyPatch,
                                               request: pytest.FixtureRequest) -> None:
    monkeypatch.setattr(s3store, "MAX_OBJECT_BYTES", 4)

    with pytest.raises(ScriptError) as e:
        store.put("drafts/a-conti.html", b"12345")

    assert e.value.code == "object_too_large"
    assert bucket.keys("") == []
    assert _rows(request, "objects") == []


# --- 행이 원본: 고아 객체는 안 보인다 ----------------------------------------------------------

def test_object_without_index_row_is_invisible(store: Any, bucket: Any) -> None:
    bucket.put(f"users/{store._uid}/drafts/orphan.html", b"no row")

    assert store.exists("drafts/orphan.html") is False
    with pytest.raises(FileNotFoundError):
        store.get("drafts/orphan.html")
    assert store.list("drafts/") == []


def test_delete_of_rowless_key_clears_the_orphan_object(store: Any, bucket: Any) -> None:
    bucket.put(f"users/{store._uid}/drafts/orphan.html", b"no row")

    store.delete("drafts/orphan.html")

    assert bucket.keys("") == []


def test_get_after_object_vanished_raises_file_not_found(store: Any, bucket: Any) -> None:
    store.put("drafts/a-conti.html", b"x")
    bucket.delete(f"users/{store._uid}/drafts/a-conti.html")

    with pytest.raises(FileNotFoundError):
        store.get("drafts/a-conti.html")


# --- 실패 주입: 행·객체가 어긋나지 않는다 ---------------------------------------------------------

class Boom(Exception):
    pass


def _fail(*_: Any, **__: Any) -> None:
    raise Boom("injected")


def test_failed_s3_put_leaves_no_row_and_keeps_old_bytes(store: Any, bucket: Any, monkeypatch: pytest.MonkeyPatch,
                                                         request: pytest.FixtureRequest) -> None:
    store.put("drafts/a-conti.html", b"old")
    monkeypatch.setattr(bucket, "put", _fail)

    with pytest.raises(Boom):
        store.put("drafts/a-conti.html", b"new")
    with pytest.raises(Boom):
        store.put("drafts/b-conti.html", b"new")

    monkeypatch.undo()
    assert store.get("drafts/a-conti.html") == b"old"
    assert not store.exists("drafts/b-conti.html")
    assert [r[0] for r in _rows(request, "objects")] == ["drafts/a-conti.html"]
    assert bucket.keys("") == [f"users/{store._uid}/drafts/a-conti.html"]


def test_failed_index_upsert_on_new_key_removes_the_uploaded_object(
        store: Any, bucket: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    monkeypatch.setattr(store, "_upsert", _fail)

    with pytest.raises(Boom):
        store.put("drafts/a-conti.html", b"new")

    assert bucket.keys("") == []
    assert _rows(request, "objects") == []
    assert not store.folder_exists("drafts/")


def test_failed_index_upsert_on_existing_key_keeps_row_pointing_at_an_object(
        store: Any, bucket: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    store.put("drafts/a-conti.html", b"old")
    monkeypatch.setattr(store, "_upsert", _fail)

    with pytest.raises(Boom):
        store.put("drafts/a-conti.html", b"new")

    monkeypatch.undo()
    assert [r[0] for r in _rows(request, "objects")] == ["drafts/a-conti.html"]
    assert bucket.keys("") == [f"users/{store._uid}/drafts/a-conti.html"]
    store.get("drafts/a-conti.html")                       # the row still has its object


def test_failed_upsert_and_failed_cleanup_names_the_orphan(store: Any, bucket: Any,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "_upsert", _fail)

    def broken_delete(s3_key: str) -> None:
        raise S3Error(5, "down", s3_key)

    monkeypatch.setattr(bucket, "delete", broken_delete)

    with pytest.raises(Boom) as e:
        store.put("drafts/a-conti.html", b"new")

    assert any("drafts/a-conti.html" in note for note in getattr(e.value, "__notes__", []))
    assert not store.exists("drafts/a-conti.html")


def test_failed_s3_delete_keeps_row_and_object(store: Any, bucket: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store.put("drafts/a-conti.html", b"keep")
    monkeypatch.setattr(bucket, "delete", _fail)

    with pytest.raises(Boom):
        store.delete("drafts/a-conti.html")

    monkeypatch.undo()
    assert store.get("drafts/a-conti.html") == b"keep"


def test_failed_s3_delete_prefix_keeps_rows_and_objects(store: Any, bucket: Any,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    store.put("workspace/footage/f1/scenes.json", b"s")
    store.put("workspace/footage/f1/frames/0001.jpg", b"j")
    monkeypatch.setattr(bucket, "delete_many", _fail)

    with pytest.raises(Boom):
        store.delete_prefix("workspace/footage/f1/")

    monkeypatch.undo()
    assert store.get("workspace/footage/f1/scenes.json") == b"s"
    assert store.get("workspace/footage/f1/frames/0001.jpg") == b"j"
    assert store.folder_exists("workspace/footage/f1/frames/")


def test_shared_failed_index_upsert_removes_the_uploaded_object(
        shared: Any, bucket: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    monkeypatch.setattr(shared, "_upsert", _fail)

    with pytest.raises(Boom):
        shared.put(f"workspace/raw/{VID}.json", b"{}")

    assert bucket.keys("") == []
    assert _rows(request, "shared_objects") == []


def test_s3_client_errors_surface_as_os_error(bucket: Any) -> None:
    with pytest.raises(FileNotFoundError):
        bucket.get("users/none/x.json")
    broken = s3store.S3Bucket(bucket.client, "no-such-bucket-" + uuid.uuid4().hex[:8])
    with pytest.raises(OSError):
        broken.put("x", b"x")
    with pytest.raises(OSError):
        broken.get("x")


# --- 동시성: 행과 바이트가 같은 쓰기를 가리킨다 -------------------------------------------------

def test_concurrent_puts_leave_row_matching_the_bytes(store: Any, bucket: Any, monkeypatch: pytest.MonkeyPatch,
                                                      request: pytest.FixtureRequest) -> None:
    """PUT과 행 upsert 사이를 벌려 둔다: 잠금이 없으면 PUT A·PUT B·행 B·행 A 순서로 행이 B의 바이트를 A로 적는다."""
    key = "workspace/footage_match/race.json"
    real_put = bucket.put

    def slow_put(s3_key: str, data: bytes) -> None:
        real_put(s3_key, data)
        time.sleep(random.uniform(0, 0.05))

    monkeypatch.setattr(bucket, "put", slow_put)
    for _ in range(3):
        datas = [f"writer {i} ".encode() * (i + 1) for i in range(6)]
        barrier = threading.Barrier(len(datas))
        errors: list[BaseException] = []

        def put(i: int, d: list[bytes] = datas, b: threading.Barrier = barrier,
                out: list[BaseException] = errors) -> None:
            b.wait()
            try:
                store.put(key, d[i])
            except Exception as e:  # noqa: BLE001 - collected and asserted below
                out.append(e)

        threads = [threading.Thread(target=put, args=(i,)) for i in range(len(datas))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        assert errors == []
        final = store.get(key)
        [(_, size, sha, _)] = _rows(request, "objects")
        assert (size, sha) == (len(final), _sha(final))


def test_concurrent_put_and_delete_never_leave_a_dangling_row(store: Any, bucket: Any,
                                                              monkeypatch: pytest.MonkeyPatch,
                                                              request: pytest.FixtureRequest) -> None:
    """delete가 PUT과 행 upsert 사이에 끼면(잠금이 없으면) 객체 없는 행이 남는다."""
    key = "drafts/a-conti.html"
    real_put = bucket.put

    def slow_put(s3_key: str, data: bytes) -> None:
        real_put(s3_key, data)
        time.sleep(random.uniform(0, 0.03))

    monkeypatch.setattr(bucket, "put", slow_put)
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        barrier.wait()
        try:
            for n in range(5):
                if (i + n) % 2:
                    store.delete(key)
                else:
                    store.put(key, f"{i}-{n}".encode())
        except Exception as e:  # noqa: BLE001 - collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert errors == []
    rows = _rows(request, "objects")
    objects = bucket.keys("")
    assert [r[3] for r in rows] == objects                 # a row iff its object
    if rows:
        assert _sha(store.get(key)) == rows[0][2]


# --- mtime_ns ----------------------------------------------------------------------------------

def test_mtime_ns_strictly_increases_on_quick_rewrites(store: Any, shared: Any) -> None:
    for s, key in ((store, "workspace/conti_history/a/0001.json"), (shared, f"workspace/cuts/{VID}.json")):
        stamps = []
        for _ in range(10):
            s.put(key, b"same size")
            stamps.append(s.stat(key).mtime_ns)
        assert all(a < b for a, b in zip(stamps, stamps[1:], strict=False))


def test_mtime_ns_moves_forward_even_if_the_clock_does_not(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3store, "_now_ns", lambda: 1_000)
    store.put("reports/a.html", b"1")
    store.put("reports/a.html", b"2")

    assert store.stat("reports/a.html").mtime_ns == 1_001
    assert store.stat("reports/a.html").mtime == 1_001 / 1e9


# --- 임시 경로 ---------------------------------------------------------------------------------

def test_local_path_is_a_temp_copy_removed_afterwards(store: Any) -> None:
    store.put("drafts/a-conti.html", b"abc")

    with store.local_path("drafts/a-conti.html") as path:
        assert path.read_bytes() == b"abc"
        path.write_bytes(b"changed locally")

    assert not path.exists()
    assert not path.parent.exists()
    assert store.get("drafts/a-conti.html") == b"abc"


def test_local_path_of_folder_key_downloads_the_tree(shared: Any) -> None:
    shared.put(f"workspace/frames/{VID}/0001.jpg", b"1")
    shared.put(f"workspace/frames/{VID}/0002.jpg", b"2")

    with shared.local_path(f"workspace/frames/{VID}/") as folder:
        assert sorted(p.name for p in folder.iterdir()) == ["0001.jpg", "0002.jpg"]
        assert (folder / "0002.jpg").read_bytes() == b"2"

    assert not folder.exists()


def test_write_path_file_on_exception_stores_nothing_and_keeps_old(store: Any, bucket: Any) -> None:
    store.put("drafts/a-captions.mp4", b"old")

    with pytest.raises(Boom), store.write_path("drafts/a-captions.mp4") as path:
        path.write_bytes(b"half")
        raise Boom("render failed")

    assert not path.exists()
    assert store.get("drafts/a-captions.mp4") == b"old"
    assert not store.exists("drafts/b.mp4")


def test_write_path_folder_on_exception_uploads_nothing(shared: Any, bucket: Any) -> None:
    frames = f"workspace/frames/{VID}/"
    shared.put(f"{frames}0001.jpg", b"old")

    with pytest.raises(Boom), shared.write_path(frames) as folder:
        (folder / "0001.jpg").write_bytes(b"new")
        (folder / "0002.jpg").write_bytes(b"new")
        raise Boom("cut detection failed")

    assert not folder.exists()
    assert [m.key for m in shared.list(frames)] == [f"{frames}0001.jpg"]
    assert shared.get(f"{frames}0001.jpg") == b"old"
    assert bucket.keys("") == [f"shared/videos/{VID}/frames/0001.jpg"]


def test_write_path_folder_replaces_the_old_contents_in_name_order(shared: Any, bucket: Any,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    frames = f"workspace/frames/{VID}/"
    shared.put(f"{frames}0009.jpg", b"stale")
    order: list[str] = []
    real_put = shared.put

    def spy(key: str, data: bytes) -> None:
        order.append(key)
        real_put(key, data)

    monkeypatch.setattr(shared, "put", spy)

    with shared.write_path(frames) as folder:
        for n in (3, 1, 2):
            (folder / f"{n:04d}.jpg").write_bytes(b"j")

    assert order == [f"{frames}{n:04d}.jpg" for n in (1, 2, 3)]
    assert [m.key for m in shared.list(frames)] == order
    assert bucket.keys("") == [f"shared/videos/{VID}/frames/{n:04d}.jpg" for n in (1, 2, 3)]
    assert not folder.exists()


def test_write_path_folder_with_unstorable_file_uploads_nothing(store: Any) -> None:
    store.put("workspace/footage/f1/scenes.json", b"keep")

    with pytest.raises(ValueError), store.write_path("workspace/footage/f1/") as base:
        (base / "ok.jpg").write_bytes(b"j")
        (base / "notes.md").write_bytes(b"md belongs to the DocStore")

    assert store.get("workspace/footage/f1/scenes.json") == b"keep"
    assert not store.exists("workspace/footage/f1/ok.jpg")


def test_shared_write_path_empty_folder_leaves_no_folder(shared: Any) -> None:
    """공유 캐시는 폴더 행이 없다: 빈 프레임 폴더는 남지 않는다(File은 빈 폴더가 남는다. cuts가 실패 때 지운다)."""
    with shared.write_path(f"workspace/frames/{VID}/"):
        pass

    assert not shared.folder_exists(f"workspace/frames/{VID}/")


def test_write_path_temp_dir_is_removed_after_upload(store: Any) -> None:
    with store.write_path("reports/a.html") as path:
        path.write_bytes(b"r")
        temp: Path = path.parent

    assert not temp.exists()
    assert store.get("reports/a.html") == b"r"


# --- 사용자 격리(행·객체 위치) ---------------------------------------------------------------------

def test_two_users_same_key_get_separate_objects(request: pytest.FixtureRequest, bucket: Any) -> None:
    a = pg_object_store(request, "a@example.com")
    b = pg_object_store(request, "b@example.com")
    a.put("drafts/x-conti.html", b"A")
    b.put("drafts/x-conti.html", b"B")

    assert (a.get("drafts/x-conti.html"), b.get("drafts/x-conti.html")) == (b"A", b"B")
    assert bucket.keys("") == sorted([f"users/{a._uid}/drafts/x-conti.html", f"users/{b._uid}/drafts/x-conti.html"])

    b.delete_prefix("drafts/")

    assert bucket.keys("") == [f"users/{a._uid}/drafts/x-conti.html"]
    assert a.get("drafts/x-conti.html") == b"A"


def test_folder_exists_sees_docs_of_the_same_user_only(request: pytest.FixtureRequest) -> None:
    from tests.helpers.pg import pg_doc_store

    docs = pg_doc_store(request, "a@example.com")
    objects = s3store.S3ObjectStore(request.getfixturevalue("pg_db"), request.getfixturevalue("s3_bucket"),
                                    docs._uid)
    other = pg_object_store(request, "b@example.com")
    docs.write("drafts/a.md", b"x")

    assert objects.folder_exists("drafts/")
    assert not other.folder_exists("drafts/")
