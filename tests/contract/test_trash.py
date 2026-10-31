"""휴지통 계약(서버 설계 §3.2·§3.3·§10-1): `Trash.move`의 반환 형식·문자열·오류를
FileTrash와 PgTrash에서 같은 단언으로 고정한다.

`file` = 작업 폴더 Ctx(FileStore), `pg` = 서버 Ctx(ServerBackend, PG·MinIO 없으면 skip).
원래 다른 규칙(공유 캐시 키는 서버에서 옮기지 않음, trash_items·MinIO 배치)은 아래 `server` 절.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import ctx as ctx_mod
from scripts import filestore, keys
from scripts.common import ScriptError
from scripts.ctx import Ctx
from tests.helpers.pg import pg_ctx

CONTI = keys.conti_md(keys.draft_md("2026-11-01-제주"))
HTML = keys.conti_html(CONTI)
HIST = keys.history_dir(CONTI)
VID = "Q8PSsikq8PA"
STAMP_RE = re.compile(r"^workspace/trash/\d{8}-\d{6}(-\d+)?$")


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> _Frozen:
        return cls(2026, 11, 1, 9, 30, 15)


@pytest.fixture(params=["file", "pg"])
def c(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Ctx]:
    if request.param == "file":
        root = tmp_path / "work"
        root.mkdir()
        made = Ctx(root=root)
    else:
        made = pg_ctx(request)
    with ctx_mod.use(made):
        yield made


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filestore, "datetime", _Frozen)
    from server import pgstore
    monkeypatch.setattr(pgstore, "datetime", _Frozen)


def _seed(c: Ctx) -> None:
    c.store.write(CONTI, "---\ncuts: []\nscript: []\n---\n# 콘티 🏆\n\n## 내 메모\n메모\n".encode())
    c.objects.put(HTML, b"<html>\r\n</html>\r\n")
    c.objects.put(keys.history_version(CONTI, 1), b'{\n "n": 1\n}')
    c.objects.put(keys.history_version(CONTI, 2), b'{\n "n": 2\n}')


def test_move_returns_trash_folder_and_moved_keys_with_last_at_the_end(c: Ctx) -> None:
    _seed(c)
    out = c.trash.move([CONTI, HTML, HIST], last=CONTI)
    assert set(out) == {"trash", "moved"}
    assert STAMP_RE.match(out["trash"])
    assert out["moved"] == [HTML, HIST.removesuffix("/"), CONTI]
    assert not c.store.exists(CONTI)
    assert not c.objects.exists(HTML)
    assert not c.objects.folder_exists(HIST)
    assert c.objects.list(HIST) == []
    assert c.store.folder_exists(keys.DRAFTS)                  # the parent folder stays (like the disk)


def test_folder_key_without_trailing_slash_moves_the_folder(c: Ctx) -> None:
    _seed(c)
    out = c.trash.move([HIST.removesuffix("/")], last=HIST.removesuffix("/"))
    assert out["moved"] == [HIST.removesuffix("/")]
    assert not c.objects.folder_exists(HIST)
    assert c.store.exists(CONTI) and c.objects.exists(HTML)


def test_same_second_moves_get_numbered_folders(c: Ctx, frozen: None) -> None:
    _seed(c)
    first = c.trash.move([HTML], last=HTML)
    second = c.trash.move([CONTI], last=CONTI)
    assert first["trash"] == "workspace/trash/20261101-093015"
    assert second["trash"] == "workspace/trash/20261101-093015-2"


def test_missing_key_is_trash_failed_and_nothing_moves(c: Ctx) -> None:
    _seed(c)
    with pytest.raises(ScriptError) as e:
        c.trash.move([HTML, keys.srt(CONTI, "-lines"), CONTI], last=CONTI)
    assert e.value.code == "trash_failed"
    if isinstance(c.trash, filestore.FileTrash):   # local store: message unchanged
        assert e.value.message.startswith("파일을 옮기지 못했습니다(다른 프로그램이 열어 두었는지 확인하세요): ")
    else:                                       # server store: no local-program hint, no internal detail
        assert e.value.message == "파일을 옮기지 못했습니다. 잠시 뒤 다시 시도해 주세요."
    assert c.store.exists(CONTI) and c.objects.exists(HTML)
    assert c.store.read(CONTI).raw.endswith("메모\n".encode())


@pytest.mark.parametrize("bad", ["../x.md", "C:/x.md", "drafts\\x.md"])
def test_bad_key_is_bad_path(c: Ctx, bad: str) -> None:
    _seed(c)
    with pytest.raises(ScriptError) as e:
        c.trash.move([bad], last=bad)
    assert e.value.code == "bad_path"
    assert c.store.exists(CONTI)


def test_moved_doc_can_be_written_again_as_a_new_file(c: Ctx) -> None:
    _seed(c)
    c.trash.move([CONTI], last=CONTI)
    c.store.create(CONTI, b"---\ncuts: []\nscript: []\n---\n# new\n")
    assert c.store.read(CONTI).raw.endswith(b"# new\n")


# --- server: documented differences and layout ----------------------------------------------

@pytest.fixture
def server_c(request: pytest.FixtureRequest) -> Iterator[Ctx]:
    made = pg_ctx(request)
    with ctx_mod.use(made):
        yield made


def test_server_does_not_move_shared_cache_keys(server_c: Ctx) -> None:
    c = server_c
    _seed(c)
    c.shared.put(keys.raw(VID), b"{}")
    c.shared.put(keys.frames_dir(VID) + "0001.jpg", b"jpg")
    out = c.trash.move([keys.raw(VID), keys.frames_dir(VID).removesuffix("/"), CONTI], last=CONTI)
    assert out["moved"] == [CONTI]                             # other users' cache: not the user's to move (§3.3)
    assert c.shared.exists(keys.raw(VID)) and c.shared.exists(keys.frames_dir(VID) + "0001.jpg")


def test_server_trash_rows_and_objects_layout(server_c: Ctx, request: pytest.FixtureRequest) -> None:
    c = server_c
    _seed(c)
    backend = request.getfixturevalue("pg_backend")
    bucket = request.getfixturevalue("s3_bucket")
    out = c.trash.move([CONTI, HTML, HIST], last=CONTI)
    stamp = out["trash"].removeprefix(keys.TRASH)
    uid = c.user_id
    with backend.db.user_tx(uid) as conn:
        rows = conn.execute("SELECT stamp, key, kind, raw IS NOT NULL, s3_key FROM trash_items ORDER BY key").fetchall()
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (stamp, HTML, "object", False),
        (stamp, CONTI, "doc", True),
        (stamp, keys.history_version(CONTI, 1), "object", False),
        (stamp, keys.history_version(CONTI, 2), "object", False),
    ]
    by_key = {r[1]: r for r in rows}
    assert by_key[CONTI][4] is None
    assert by_key[HTML][4] == f"trash/{uid}/{stamp}/{HTML}"
    names = bucket.keys("")
    assert f"trash/{uid}/{stamp}/{HTML}" in names
    assert not [n for n in names if n.startswith(f"users/{uid}/")]      # originals deleted


def test_server_trash_is_isolated_per_user(request: pytest.FixtureRequest) -> None:
    a = pg_ctx(request, "a@example.com")
    b = pg_ctx(request, "b@example.com")
    with ctx_mod.use(a):
        _seed(a)
        a.trash.move([CONTI], last=CONTI)
    with ctx_mod.use(b):
        with pytest.raises(ScriptError) as e:
            b.trash.move([HTML], last=HTML)                      # A's object is not B's
        assert e.value.code == "trash_failed"
    backend = request.getfixturevalue("pg_backend")
    with backend.db.user_tx(b.user_id) as conn:
        assert conn.execute("SELECT count(*) FROM trash_items").fetchone() == (0,)


def test_server_copy_failure_rolls_back_and_cleans_copies(server_c: Ctx, request: pytest.FixtureRequest,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    c = server_c
    _seed(c)
    bucket = request.getfixturevalue("s3_bucket")
    real_copy = bucket.copy
    calls: list[str] = []

    def flaky(src: str, dst: str) -> None:
        calls.append(dst)
        if len(calls) == 2:
            raise OSError(5, "boom", src)
        real_copy(src, dst)

    monkeypatch.setattr(bucket, "copy", flaky)
    with pytest.raises(ScriptError) as e:
        c.trash.move([HTML, HIST, CONTI], last=CONTI)
    assert e.value.code == "trash_failed"
    assert c.store.exists(CONTI) and c.objects.exists(HTML) and c.objects.exists(keys.history_version(CONTI, 2))
    assert not [n for n in bucket.keys("") if n.startswith("trash/")]
    assert c.objects.get(HTML) == b"<html>\r\n</html>\r\n"


# --- fix round 1: cleanup after commit (I-001, m-006), ambiguous commit (m-002) --------------

def test_server_put_between_commit_and_cleanup_survives(server_c: Ctx, request: pytest.FixtureRequest,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """커밋과 원래 객체 삭제 사이에 같은 키를 다시 쓰면(동시 put) 새 바이트가 남아야 한다."""
    from server.backend import export_tree
    from server.pgstore import PgTrash

    c = server_c
    _seed(c)
    real = PgTrash._delete_originals

    def racing(self: PgTrash, object_keys: list[str]) -> None:
        c.objects.put(HTML, b"new bytes")                     # another request writes the same key right now
        real(self, object_keys)

    monkeypatch.setattr(PgTrash, "_delete_originals", racing)
    out = c.trash.move([HTML, HIST], last=HTML)
    assert c.objects.get(HTML) == b"new bytes"
    assert not c.objects.exists(keys.history_version(CONTI, 1))
    bucket = request.getfixturevalue("s3_bucket")
    assert not [n for n in bucket.keys(f"users/{c.user_id}/") if "conti_history" in n]   # stale ones deleted
    tree = export_tree(request.getfixturevalue("pg_backend"), c.user_id).files
    assert tree[f"{out['trash']}/{HTML}"] == b"<html>\r\n</html>\r\n"                 # the trash copy is the old one


def test_server_cleanup_failure_after_commit_is_logged_not_raised(server_c: Ctx, request: pytest.FixtureRequest,
                                                                 monkeypatch: pytest.MonkeyPatch,
                                                                 caplog: pytest.LogCaptureFixture) -> None:
    c = server_c
    _seed(c)
    bucket = request.getfixturevalue("s3_bucket")

    real_delete_many = bucket.delete_many

    def broken(names: list[str]) -> None:
        if all(n.startswith("users/") for n in names):           # only the post-commit cleanup (not teardown)
            raise OSError(5, "minio down", names[0])
        real_delete_many(names)

    monkeypatch.setattr(bucket, "delete_many", broken)
    with caplog.at_level("WARNING", logger="server.pgstore"):
        out = c.trash.move([HTML, CONTI], last=CONTI)
    assert out["moved"] == [HTML, CONTI]
    assert not c.objects.exists(HTML) and not c.store.exists(CONTI)
    backend = request.getfixturevalue("pg_backend")
    with backend.db.user_tx(c.user_id) as conn:
        assert conn.execute("SELECT count(*) FROM trash_items").fetchone() == (2,)
    assert f"users/{c.user_id}/{HTML}" in bucket.keys("")                  # orphan left for S10
    assert any("고아" in r.getMessage() and HTML in r.getMessage() for r in caplog.records)


def test_server_ambiguous_commit_keeps_trash_copies(server_c: Ctx, request: pytest.FixtureRequest,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    """커밋 중 연결이 끊기면(결과를 모름) 휴지통 복사본을 지우지 않는다: 커밋됐다면 행이 그 복사본을 가리킨다."""
    import psycopg

    c = server_c
    _seed(c)
    trash = c.trash
    real_db = trash._db                                          # type: ignore[attr-defined]

    class CommitLost:
        calls = 0

        @contextmanager
        def user_tx(self, uid: Any) -> Iterator[Any]:
            CommitLost.calls += 1
            with real_db.user_tx(uid) as conn:
                yield conn
            if CommitLost.calls == 1:
                raise psycopg.OperationalError("server closed the connection during COMMIT")

    trash._db = CommitLost()                                     # type: ignore[attr-defined]
    with caplog.at_level("WARNING", logger="server.pgstore"), pytest.raises(psycopg.OperationalError):
        trash.move([HTML, CONTI], last=CONTI)
    bucket = request.getfixturevalue("s3_bucket")
    assert f"trash/{c.user_id}/" in "".join(bucket.keys("trash/"))           # copies kept
    assert any("S10" in r.getMessage() for r in caplog.records)
    backend = request.getfixturevalue("pg_backend")
    with backend.db.user_tx(c.user_id) as conn:                  # here the commit did happen: rows point at copies
        rows = conn.execute("SELECT s3_key FROM trash_items WHERE kind = 'object'").fetchall()
    assert rows and all(str(r[0]) in bucket.keys("trash/") for r in rows)
