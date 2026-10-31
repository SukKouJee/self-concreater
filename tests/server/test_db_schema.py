"""Alembic `0001`(+ `0002` 비밀번호 열, T15)이 서버 설계 §3.1 스키마를 정확히 만들고, 되돌리면 깨끗이 지우는지(T5)."""
from __future__ import annotations

from typing import Any

import pytest

from scripts.common import ScriptError
from server.db import role_password_sql
from tests.server.conftest import TestDb, run_alembic

psycopg = pytest.importorskip("psycopg")

RLS_TABLES = ("docs", "folders", "objects", "trash_items", "llm_usage")
ALL_TABLES = {"users", "docs", "folders", "objects", "shared_objects", "trash_items", "llm_usage"}

# (column, data_type, is_nullable, collation) — 서버 설계 §3.1 그대로
EXPECTED_COLUMNS: dict[str, list[tuple[str, str, str, str | None]]] = {
    "users": [
        ("id", "uuid", "NO", None), ("email", "text", "NO", None), ("google_sub", "text", "YES", None),
        ("display_name", "text", "YES", None), ("is_admin", "boolean", "NO", None),
        ("disabled", "boolean", "NO", None), ("session_epoch", "integer", "NO", None),
        ("daily_usd_limit", "numeric(8,2)", "YES", None), ("created_at", "timestamp with time zone", "NO", None),
        ("password_hash", "text", "YES", None)],                                          # 0002
    "docs": [
        ("user_id", "uuid", "NO", None), ("key", "text", "NO", "C"), ("raw", "bytea", "NO", None),
        ("version", "character(64)", "NO", None), ("size", "integer", "NO", None), ("mtime_ns", "bigint", "NO", None)],
    "folders": [("user_id", "uuid", "NO", None), ("prefix", "text", "NO", "C")],
    "objects": [
        ("user_id", "uuid", "NO", None), ("key", "text", "NO", "C"), ("size", "bigint", "NO", None),
        ("sha256", "character(64)", "NO", None), ("mtime_ns", "bigint", "NO", None), ("s3_key", "text", "NO", None)],
    "shared_objects": [
        ("key", "text", "NO", "C"), ("size", "bigint", "NO", None), ("sha256", "character(64)", "NO", None),
        ("mtime_ns", "bigint", "NO", None), ("s3_key", "text", "NO", None), ("created_by", "uuid", "YES", None)],
    "trash_items": [
        ("id", "bigint", "NO", None), ("user_id", "uuid", "NO", None), ("stamp", "text", "NO", None),
        ("key", "text", "NO", "C"), ("kind", "text", "NO", None), ("raw", "bytea", "YES", None),
        ("s3_key", "text", "YES", None), ("size", "bigint", "NO", None), ("mtime_ns", "bigint", "NO", None),
        ("moved_at", "timestamp with time zone", "NO", None)],
    "llm_usage": [
        ("id", "bigint", "NO", None), ("user_id", "uuid", "NO", None), ("at", "timestamp with time zone", "NO", None),
        ("job_kind", "text", "YES", None), ("model", "text", "NO", None), ("input_tokens", "integer", "NO", None),
        ("output_tokens", "integer", "NO", None), ("cache_read_tokens", "integer", "NO", None),
        ("cache_write_tokens", "integer", "NO", None), ("cost_usd", "numeric(10,6)", "NO", None),
        ("ok", "boolean", "NO", None)],
}

# 테이블별 인덱스 정의(PK·UNIQUE 포함). pg_indexes.indexdef에서 이름을 뺀 "ON … USING …" 부분
EXPECTED_INDEXES: dict[str, set[str]] = {
    "users": {"UNIQUE (id)", "UNIQUE (email)", "UNIQUE (google_sub)"},
    "docs": {"UNIQUE (user_id, key)"},
    "folders": {"UNIQUE (user_id, prefix)"},
    "objects": {"UNIQUE (user_id, key)"},
    "shared_objects": {"UNIQUE (key)"},
    "trash_items": {"UNIQUE (id)", "UNIQUE (user_id, stamp, key)"},
    "llm_usage": {"UNIQUE (id)", "(user_id, at)"},
}


def _q(dsn: str, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def _tables(dsn: str) -> set[str]:
    return {r[0] for r in _q(dsn, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}


def _columns(dsn: str, table: str) -> list[tuple[str, str, str, str | None]]:
    rows = _q(dsn, """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod),
               CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END,
               CASE WHEN a.attcollation <> t.typcollation THEN c.collname END
          FROM pg_attribute a
          JOIN pg_type t ON t.oid = a.atttypid
          LEFT JOIN pg_collation c ON c.oid = a.attcollation
         WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum""", (f"public.{table}",))
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _indexes(dsn: str, table: str) -> set[str]:
    out: set[str] = set()
    for (indexdef,) in _q(dsn, "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = %s",
                          (table,)):
        unique = "UNIQUE " if indexdef.startswith("CREATE UNIQUE") else ""
        out.add(unique + indexdef[indexdef.index(" USING btree ") + len(" USING btree "):])
    return out


def _role(dsn: str, role: str) -> tuple[Any, ...] | None:
    rows = _q(dsn, """SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin, rolinherit
                        FROM pg_roles WHERE rolname = %s""", (role,))
    return rows[0] if rows else None


def test_upgrade_creates_exactly_the_design_tables(migrated_db: TestDb) -> None:
    assert _tables(migrated_db.owner_dsn) == ALL_TABLES | {"alembic_version"}
    assert _q(migrated_db.owner_dsn, "SELECT version_num FROM alembic_version") == [("0002",)]


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_upgrade_columns_match_design(migrated_db: TestDb, table: str) -> None:
    assert _columns(migrated_db.owner_dsn, table) == EXPECTED_COLUMNS[table]


@pytest.mark.parametrize("table", sorted(EXPECTED_INDEXES))
def test_upgrade_indexes_match_design(migrated_db: TestDb, table: str) -> None:
    assert _indexes(migrated_db.owner_dsn, table) == EXPECTED_INDEXES[table]


def test_upgrade_defaults_constraints_and_foreign_keys(migrated_db: TestDb) -> None:
    dsn = migrated_db.owner_dsn
    checks = {(r[0], r[1]) for r in _q(dsn, """
        SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint
         WHERE contype = 'c' AND connamespace = 'public'::regnamespace""")}
    plain = {("users", "CHECK ((email = lower(email)))"),
             ("users", "CHECK (((password_hash IS NULL) OR (password_hash ~~ 'scrypt$%'::text)))"),
             ("trash_items", "CHECK ((kind = ANY (ARRAY['doc'::text, 'object'::text])))")}
    assert plain <= checks
    # s3_key ties (one per table, behaviour tested in test_db_rls)
    assert sorted(t for t, d in checks - plain if "s3_key" in d) == ["objects", "trash_items"]
    assert len(checks) == 5
    fks = {(r[0], r[1]) for r in _q(dsn, """
        SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint
         WHERE contype = 'f' AND connamespace = 'public'::regnamespace""")}
    cascade = "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
    assert fks == {("docs", cascade), ("folders", cascade), ("objects", cascade), ("trash_items", cascade),
                   ("llm_usage", cascade),
                   ("shared_objects", "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL")}
    with psycopg.connect(dsn) as conn:
        row = conn.execute("INSERT INTO users (email) VALUES ('a@x.com') "
                           "RETURNING id, is_admin, disabled, session_epoch, daily_usd_limit, created_at").fetchone()
        assert row is not None and row[0] is not None and row[1:5] == (False, False, 0, None) and row[5] is not None
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO users (email) VALUES ('Upper@x.com')")


def test_upgrade_enables_and_forces_rls_only_on_user_tables(migrated_db: TestDb) -> None:
    rows = {r[0]: (r[1], r[2]) for r in _q(migrated_db.owner_dsn, """
        SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
         WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' AND relname <> 'alembic_version'""")}
    assert rows == {t: ((True, True) if t in RLS_TABLES else (False, False)) for t in ALL_TABLES}
    policies = {r[0]: r[1:] for r in _q(migrated_db.owner_dsn,
                                        "SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies")}
    assert set(policies) == set(RLS_TABLES)
    for name, cmd, qual, with_check in policies.values():
        assert (name, cmd) == ("own", "ALL")
        assert qual == with_check == "(user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)"


def test_upgrade_creates_app_role_without_bypass(migrated_db: TestDb) -> None:
    # (super, bypassrls, createdb, createrole, login, inherit)
    assert _role(migrated_db.owner_dsn, migrated_db.app_role) == (False, False, False, False, True, False)
    assert _q(migrated_db.app_dsn, "SELECT current_user") == [(migrated_db.app_role,)]
    owns = _q(migrated_db.owner_dsn, "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                                     "WHERE r.rolname = %s", (migrated_db.app_role,))
    assert owns == [(0,)]


def test_downgrade_removes_everything_and_upgrade_reapplies(migrated_db: TestDb) -> None:
    run_alembic(migrated_db, "downgrade", "base")
    assert _tables(migrated_db.owner_dsn) == {"alembic_version"}
    assert _q(migrated_db.owner_dsn, "SELECT count(*) FROM alembic_version") == [(0,)]
    assert _role(migrated_db.owner_dsn, migrated_db.app_role) is None
    leftovers = _q(migrated_db.owner_dsn, """
        SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relname <> 'alembic_version'
           AND relname NOT LIKE 'alembic_version%%'""")
    assert leftovers == []

    run_alembic(migrated_db, "upgrade", "head")
    assert _tables(migrated_db.owner_dsn) == ALL_TABLES | {"alembic_version"}
    assert _role(migrated_db.owner_dsn, migrated_db.app_role) is not None


def test_0002_downgrade_drops_only_the_password_column_and_upgrade_restores_it(migrated_db: TestDb) -> None:
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        conn.execute("INSERT INTO users (email, password_hash) VALUES ('a@x.com', 'scrypt$1$1$1$a$b')")
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE users SET password_hash = 'plain-text'")
    run_alembic(migrated_db, "downgrade", "0001")
    assert _q(migrated_db.owner_dsn, "SELECT version_num FROM alembic_version") == [("0001",)]
    assert [c[0] for c in _columns(migrated_db.owner_dsn, "users")] == [c[0] for c in EXPECTED_COLUMNS["users"][:-1]]
    assert _q(migrated_db.owner_dsn, "SELECT email FROM users") == [("a@x.com",)]          # users are kept
    assert _q(migrated_db.app_dsn, "SELECT count(*) FROM users") == [(1,)]                 # grants unchanged
    run_alembic(migrated_db, "upgrade", "head")
    assert _columns(migrated_db.owner_dsn, "users") == EXPECTED_COLUMNS["users"]
    assert _q(migrated_db.owner_dsn, "SELECT password_hash FROM users") == [(None,)]


def test_upgrade_is_idempotent_for_existing_app_role(test_db: TestDb) -> None:
    """롤은 클러스터 전역이라 이미 있을 수 있다(다른 DB, 이전 시도). 속성을 다시 고정하고 비밀번호를 맞춘다."""
    with psycopg.connect(test_db.owner_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE ROLE "{test_db.app_role}" LOGIN CREATEDB PASSWORD \'old\'')
    run_alembic(test_db, "upgrade", "head")
    assert _role(test_db.owner_dsn, test_db.app_role) == (False, False, False, False, True, False)
    assert _q(test_db.app_dsn, "SELECT current_user") == [(test_db.app_role,)]


def test_upgrade_refuses_app_dsn_that_is_the_owner(test_db: TestDb) -> None:
    same = TestDb(test_db.name, test_db.owner_dsn, test_db.owner_dsn, "ytc_owner")
    with pytest.raises(ScriptError) as e:
        run_alembic(same, "upgrade", "head")
    assert e.value.code == "db_role_unsafe"
    assert _tables(test_db.owner_dsn) <= {"alembic_version"}


def test_upgrade_stores_app_password_as_scram_and_login_works(migrated_db: TestDb) -> None:
    (stored,) = _q(migrated_db.owner_dsn, "SELECT rolpassword FROM pg_authid WHERE rolname = %s",
                   (migrated_db.app_role,))[0]
    assert stored.startswith("SCRAM-SHA-256$")
    assert _q(migrated_db.app_dsn, "SELECT 1") == [(1,)]


def test_role_password_sql_never_carries_the_plaintext(test_db: TestDb) -> None:
    plaintext = "pw-plain-Секрет-1"
    with psycopg.connect(test_db.owner_dsn) as conn:
        stmt = role_password_sql(conn, "ytc_app_x", plaintext).as_string(conn)
    assert plaintext not in stmt
    assert stmt.startswith("ALTER ROLE \"ytc_app_x\" PASSWORD 'SCRAM-SHA-256$")


@pytest.mark.parametrize("grant", ["pg_read_all_data", "pg_write_all_data", "ytc_owner"])
def test_upgrade_refuses_existing_app_role_with_memberships(test_db: TestDb, grant: str) -> None:
    with psycopg.connect(test_db.owner_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE ROLE "{test_db.app_role}" LOGIN')
        conn.execute(f'GRANT "{grant}" TO "{test_db.app_role}"')
    with pytest.raises(ScriptError) as e:
        run_alembic(test_db, "upgrade", "head")
    assert e.value.code == "db_role_unsafe"
    assert grant in e.value.message
    assert _tables(test_db.owner_dsn) <= {"alembic_version"}
