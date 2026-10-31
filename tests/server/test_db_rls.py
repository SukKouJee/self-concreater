"""RLS 격리(ADR-S9): 사용자 미설정이면 행 0개(fail closed), 사용자끼리 보기·고치기·지우기 불가, owner는 전부(T5).

DB를 직접 두드린다. 저장소 코드(T6~)가 WHERE를 빠뜨려도 이 정책이 막는다는 것이 요점이다.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from tests.server.conftest import TestDb

psycopg = pytest.importorskip("psycopg")

from scripts.common import ScriptError  # noqa: E402
from server.db import Db  # noqa: E402

DENIED = psycopg.errors.InsufficientPrivilege  # 42501: RLS 위반과 권한 없음 모두

# 테이블별: 한 행 넣기, 사용자 열을 건드리지 않는 수정, 앱 롤에 UPDATE·DELETE 권한이 있는지
INSERTS: dict[str, str] = {
    "docs": "INSERT INTO docs (user_id, key, raw, version, size, mtime_ns) "
            "VALUES (%(uid)s, %(key)s, '\\x78', repeat('0', 64), 1, 1)",
    "folders": "INSERT INTO folders (user_id, prefix) VALUES (%(uid)s, %(key)s || '/')",
    "objects": "INSERT INTO objects (user_id, key, size, sha256, mtime_ns, s3_key) "
               "VALUES (%(uid)s::uuid, %(key)s, 1, repeat('0', 64), 1, 'users/' || %(uid)s::text || '/' || %(key)s)",
    "trash_items": "INSERT INTO trash_items (user_id, stamp, key, kind, raw, size, mtime_ns) "
                   "VALUES (%(uid)s, '20261101-000000', %(key)s, 'doc', '\\x78', 1, 1)",
    "llm_usage": "INSERT INTO llm_usage (user_id, model, input_tokens, output_tokens, cost_usd, ok) "
                 "VALUES (%(uid)s, %(key)s, 1, 1, 0.01, true)",
}
UPDATES: dict[str, str] = {
    "docs": "UPDATE docs SET size = size + 1",
    "folders": "UPDATE folders SET prefix = prefix || 'x/'",
    "objects": "UPDATE objects SET size = size + 1",
    "trash_items": "UPDATE trash_items SET size = size + 1",
    "llm_usage": "UPDATE llm_usage SET ok = false",
}
APPEND_ONLY = {"llm_usage"}  # 앱 롤은 SELECT·INSERT만(사용량 기록을 고치거나 지울 수 없다)
TABLES = sorted(INSERTS)


@pytest.fixture
def seeded(migrated_db: TestDb) -> tuple[TestDb, uuid.UUID, uuid.UUID]:
    """owner로 사용자 A·B와 각 테이블에 사용자마다 한 행."""
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        a, b = (conn.execute("INSERT INTO users (email) VALUES (%s) RETURNING id", (e,)).fetchone()[0]
                for e in ("a@example.com", "b@example.com"))
        for table in TABLES:
            for uid, key in ((a, "drafts/a"), (b, "drafts/b")):
                conn.execute(INSERTS[table], {"uid": str(uid), "key": key})
    return migrated_db, a, b


@pytest.fixture
def db(seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> Iterator[Db]:
    with Db(seeded[0].app_dsn, seeded[0].owner_dsn, max_size=2) as d:
        yield d


def _owner_rows(dsn: str, table: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY user_id").fetchall()


# ---- fail closed: app.user_id 없음 ----

@pytest.mark.parametrize("table", TABLES)
def test_app_without_user_sees_zero_rows(seeded: tuple[TestDb, uuid.UUID, uuid.UUID], table: str) -> None:
    tdb, _, _ = seeded
    with psycopg.connect(tdb.app_dsn) as conn:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        conn.execute("SELECT set_config('app.user_id', '', false)")
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert len(_owner_rows(tdb.owner_dsn, table)) == 2


@pytest.mark.parametrize("table", TABLES)
def test_app_without_user_cannot_insert(seeded: tuple[TestDb, uuid.UUID, uuid.UUID], table: str) -> None:
    tdb, a, _ = seeded
    with psycopg.connect(tdb.app_dsn) as conn, pytest.raises(DENIED, match="row-level security"):
        conn.execute(INSERTS[table], {"uid": str(a), "key": "drafts/new"})


@pytest.mark.parametrize("table", TABLES)
def test_app_without_user_changes_nothing(seeded: tuple[TestDb, uuid.UUID, uuid.UUID], table: str) -> None:
    tdb, _, _ = seeded
    before = _owner_rows(tdb.owner_dsn, table)
    with psycopg.connect(tdb.app_dsn, autocommit=True) as conn:
        for stmt in (UPDATES[table], f"DELETE FROM {table}"):
            if table in APPEND_ONLY:
                with pytest.raises(DENIED):
                    conn.execute(stmt)
            else:
                assert conn.execute(stmt).rowcount == 0
    assert _owner_rows(tdb.owner_dsn, table) == before


# ---- 사용자 A는 B의 행을 못 보고 못 바꾼다 ----

@pytest.mark.parametrize("table", TABLES)
def test_user_sees_only_own_rows(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID], table: str) -> None:
    _, a, b = seeded
    for uid in (a, b):
        with db.user_tx(uid) as conn:
            assert conn.execute(f"SELECT user_id FROM {table}").fetchall() == [(uid,)]


@pytest.mark.parametrize("table", TABLES)
def test_user_cannot_update_or_delete_other_users_rows(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID],
                                                       table: str) -> None:
    tdb, a, b = seeded
    before = _owner_rows(tdb.owner_dsn, table)
    for stmt in (UPDATES[table] + " WHERE user_id = %(b)s", f"DELETE FROM {table} WHERE user_id = %(b)s"):
        if table in APPEND_ONLY:
            with pytest.raises(DENIED), db.user_tx(a) as conn:
                conn.execute(stmt, {"b": b})
        else:
            with db.user_tx(a) as conn:
                assert conn.execute(stmt, {"b": b}).rowcount == 0
    assert _owner_rows(tdb.owner_dsn, table) == before


@pytest.mark.parametrize("table", TABLES)
def test_user_cannot_insert_rows_for_other_user(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID],
                                                table: str) -> None:
    tdb, a, b = seeded
    with pytest.raises(DENIED, match="row-level security"), db.user_tx(a) as conn:
        conn.execute(INSERTS[table], {"uid": str(b), "key": "drafts/forged"})
    assert len(_owner_rows(tdb.owner_dsn, table)) == 2


@pytest.mark.parametrize("table", sorted(set(TABLES) - APPEND_ONLY))
def test_user_cannot_hand_own_row_to_other_user(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID],
                                                table: str) -> None:
    tdb, a, b = seeded
    before = _owner_rows(tdb.owner_dsn, table)
    with pytest.raises(DENIED, match="row-level security"), db.user_tx(a) as conn:
        conn.execute(f"UPDATE {table} SET user_id = %s", (b,))
    assert _owner_rows(tdb.owner_dsn, table) == before


@pytest.mark.parametrize("table", TABLES)
def test_user_can_write_own_rows(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID], table: str) -> None:
    tdb, a, b = seeded
    with db.user_tx(a) as conn:
        conn.execute(INSERTS[table], {"uid": str(a), "key": "drafts/mine"})
    with db.user_tx(a) as conn:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (2,)
    if table not in APPEND_ONLY:
        with db.user_tx(a) as conn:
            assert conn.execute(UPDATES[table]).rowcount == 2
            assert conn.execute(f"DELETE FROM {table}").rowcount == 2
        with psycopg.connect(tdb.owner_dsn) as conn:
            assert conn.execute(f"SELECT user_id FROM {table}").fetchall() == [(b,)]


# ---- owner(admin_tx)는 전부 본다 ----

@pytest.mark.parametrize("table", TABLES)
def test_owner_sees_all_users_rows(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID], table: str) -> None:
    _, a, b = seeded
    with db.admin_tx() as conn:
        assert sorted(r[0] for r in conn.execute(f"SELECT user_id FROM {table}").fetchall()) == sorted([a, b])


# ---- users·shared_objects: RLS 없음, 권한으로 묶는다 ----

def test_app_reads_users_and_updates_only_login_columns(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    tdb, a, _ = seeded
    with db.user_tx(a) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (2,)
        assert conn.execute("UPDATE users SET google_sub = 'sub-a', display_name = 'A' WHERE id = %s",
                            (a,)).rowcount == 1
    for stmt in ("UPDATE users SET is_admin = true", "UPDATE users SET disabled = false",
                 "UPDATE users SET session_epoch = 0", "UPDATE users SET daily_usd_limit = 99",
                 "UPDATE users SET email = 'z@example.com'", "DELETE FROM users",
                 "INSERT INTO users (email) VALUES ('c@example.com')"):
        with pytest.raises(DENIED), db.user_tx(a) as conn:
            conn.execute(stmt)
    with psycopg.connect(tdb.owner_dsn) as conn:
        assert conn.execute("SELECT google_sub, is_admin FROM users WHERE id = %s", (a,)).fetchone() == ("sub-a", False)
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (2,)


def test_app_reads_and_writes_shared_objects_without_user(seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    tdb, a, _ = seeded
    with psycopg.connect(tdb.app_dsn) as conn:
        conn.execute("INSERT INTO shared_objects (key, size, sha256, mtime_ns, s3_key, created_by) "
                     "VALUES ('workspace/raw/v1.json', 1, repeat('0', 64), 1, 'shared/videos/v1/raw.json', %s)", (a,))
        conn.execute("UPDATE shared_objects SET size = 2")
    with psycopg.connect(tdb.owner_dsn) as conn:
        assert conn.execute("SELECT key, size, created_by FROM shared_objects").fetchall() == [
            ("workspace/raw/v1.json", 2, a)]


@pytest.mark.parametrize("stmt", [
    "ALTER TABLE docs DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE docs NO FORCE ROW LEVEL SECURITY",
    "DROP POLICY own ON docs",
    "CREATE TABLE sneaky (id int)",
    "TRUNCATE docs",
])
def test_app_cannot_weaken_the_schema(seeded: tuple[TestDb, uuid.UUID, uuid.UUID], stmt: str) -> None:
    tdb, _, _ = seeded
    with psycopg.connect(tdb.app_dsn) as conn, pytest.raises(DENIED):
        conn.execute(stmt)


# ---- Db: user_tx·admin_tx ----

def test_user_setting_does_not_outlive_the_transaction(seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    tdb, a, _ = seeded
    with Db(tdb.app_dsn, max_size=1) as d:
        with d.user_tx(a) as conn:
            assert conn.execute("SELECT count(*) FROM docs").fetchone() == (1,)
            pid = conn.info.backend_pid
        with d._pool.connection() as conn:  # same pooled connection, no user set
            assert conn.info.backend_pid == pid
            assert conn.execute("SELECT nullif(current_setting('app.user_id', true), '')").fetchone() == (None,)
            assert conn.execute("SELECT count(*) FROM docs").fetchone() == (0,)


def test_session_level_setting_is_reset_when_connection_returns(seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    """코드 실수로 세션 단위 set_config를 해도 다음 대여자에게 새지 않는다(pool reset)."""
    tdb, a, _ = seeded
    with Db(tdb.app_dsn, max_size=1) as d:
        with d.user_tx(a) as conn:
            conn.execute("SELECT set_config('app.user_id', %s, false)", (str(a),))
        with d._pool.connection() as conn:
            assert conn.execute("SELECT count(*) FROM docs").fetchone() == (0,)


def test_user_tx_commits_on_success_and_rolls_back_on_error(db: Db,
                                                            seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    tdb, a, _ = seeded
    with db.user_tx(a) as conn:
        conn.execute(INSERTS["docs"], {"uid": str(a), "key": "drafts/kept"})
    with pytest.raises(RuntimeError, match="boom"), db.user_tx(str(a)) as conn:
        conn.execute(INSERTS["docs"], {"uid": str(a), "key": "drafts/lost"})
        raise RuntimeError("boom")
    with psycopg.connect(tdb.owner_dsn) as conn:
        keys = [r[0] for r in conn.execute("SELECT key FROM docs WHERE user_id = %s ORDER BY key", (a,))]
    assert keys == ["drafts/a", "drafts/kept"]


def test_admin_tx_commits_as_owner(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    tdb, _, _ = seeded
    with db.admin_tx() as conn:
        conn.execute("INSERT INTO users (email) VALUES ('c@example.com')")
        assert conn.execute("SELECT current_user").fetchone() == ("ytc_owner",)
    with psycopg.connect(tdb.owner_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (3,)


def test_db_open_refuses_app_role_that_bypasses_rls(seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    tdb, _, _ = seeded
    with pytest.raises(ScriptError) as e, Db(tdb.owner_dsn):
        pass
    assert e.value.code == "db_role_unsafe"


def test_db_open_refuses_app_role_with_memberships(seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    tdb, _, _ = seeded
    with psycopg.connect(tdb.owner_dsn, autocommit=True) as conn:
        conn.execute(f'GRANT pg_read_all_data TO "{tdb.app_role}"')
    with pytest.raises(ScriptError) as e, Db(tdb.app_dsn):
        pass
    assert e.value.code == "db_role_unsafe"
    assert "pg_read_all_data" in e.value.message


# ---- s3_key는 그 행 사용자의 접두사만(서버 설계 §3.1·§3.2) ----

OBJECT_ROW = ("INSERT INTO objects (user_id, key, size, sha256, mtime_ns, s3_key) "
              "VALUES (%s, 'reports/x.html', 1, repeat('0', 64), 1, %s)")
TRASH_ROW = ("INSERT INTO trash_items (user_id, stamp, key, kind, s3_key, size, mtime_ns) "
             "VALUES (%s, '20261101-000000', 'reports/x.html', 'object', %s, 1, 1)")


@pytest.mark.parametrize("s3_key", [
    "users/{b}/reports/x.html", "users/{a}/reports/other.html", "users/{a}/../{b}/reports/x.html",
    "shared/videos/v1/raw.json", "trash/{a}/20261101-000000/reports/x.html", "users/{a}reports/x.html",
])
def test_objects_s3_key_must_be_the_rows_own_location(seeded: tuple[TestDb, uuid.UUID, uuid.UUID],
                                                      s3_key: str) -> None:
    tdb, a, b = seeded
    with psycopg.connect(tdb.owner_dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(OBJECT_ROW, (a, s3_key.format(a=a, b=b)))


@pytest.mark.parametrize("s3_key", [
    "trash/{b}/20261101-000000/reports/x.html", "trash/{a}/20261101-000001/reports/x.html",
    "users/{a}/reports/x.html", "trash/{a}/20261101-000000/reports/y.html",
])
def test_trash_s3_key_must_be_the_rows_own_location(seeded: tuple[TestDb, uuid.UUID, uuid.UUID], s3_key: str) -> None:
    tdb, a, b = seeded
    with psycopg.connect(tdb.owner_dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(TRASH_ROW, (a, s3_key.format(a=a, b=b)))


def test_own_s3_keys_are_accepted_through_user_tx(db: Db, seeded: tuple[TestDb, uuid.UUID, uuid.UUID]) -> None:
    _, a, b = seeded
    with db.user_tx(a) as conn:
        conn.execute(OBJECT_ROW, (a, f"users/{a}/reports/x.html"))
        conn.execute(TRASH_ROW, (a, f"trash/{a}/20261101-000000/reports/x.html"))
    with pytest.raises(psycopg.errors.CheckViolation), db.user_tx(a) as conn:
        conn.execute(OBJECT_ROW.replace("x.html", "y.html"), (a, f"users/{b}/reports/y.html"))
