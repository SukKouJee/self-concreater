"""init: 서버 설계 §3.1 스키마, 앱 롤, RLS(ADR-S3·S4·S9).

Revision ID: 0001
Revises:
Create Date: 2026-11-01

- owner(이 마이그레이션을 실행하는 롤) = 테이블 소유자. 슈퍼유저이거나 BYPASSRLS여야 한다(관리 CLI가 모든 행을 본다).
- 앱 롤(이름·비밀번호는 YTC_DB_DSN): LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT, DML만.
- 개인 테이블(docs, folders, objects, trash_items, llm_usage)은 ENABLE + FORCE RLS, 정책 `own` 하나.
  `app.user_id`가 없거나 빈 문자열이면 NULL과 비교 → 행 0개, INSERT 거부(fail closed).
- `users`·`shared_objects`는 RLS 없음. 앱 롤은 users에 SELECT와 UPDATE(google_sub, display_name)만.
"""
from __future__ import annotations

from typing import Any

from alembic import context, op
from psycopg import sql

from scripts.common import ScriptError

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

RLS_TABLES = ("docs", "folders", "objects", "trash_items", "llm_usage")
# 앱 롤 권한(최소): 사용량 기록은 고치거나 지울 수 없다
APP_GRANTS: dict[str, str] = {
    "docs": "SELECT, INSERT, UPDATE, DELETE",
    "folders": "SELECT, INSERT, UPDATE, DELETE",
    "objects": "SELECT, INSERT, UPDATE, DELETE",
    "shared_objects": "SELECT, INSERT, UPDATE, DELETE",
    "trash_items": "SELECT, INSERT, UPDATE, DELETE",
    "llm_usage": "SELECT, INSERT",
    "users": "SELECT, UPDATE (google_sub, display_name)",
}
SEQUENCES = ("trash_items_id_seq", "llm_usage_id_seq")

TABLES_SQL = """
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text NOT NULL UNIQUE CHECK (email = lower(email)),
  google_sub text UNIQUE,
  display_name text,
  is_admin boolean NOT NULL DEFAULT false,
  disabled boolean NOT NULL DEFAULT false,
  session_epoch int NOT NULL DEFAULT 0,
  daily_usd_limit numeric(8,2),
  created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE docs (
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  key text COLLATE "C" NOT NULL,
  raw bytea NOT NULL,
  version char(64) NOT NULL,
  size int NOT NULL,
  mtime_ns bigint NOT NULL,
  PRIMARY KEY (user_id, key));

CREATE TABLE folders (
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  prefix text COLLATE "C" NOT NULL,
  PRIMARY KEY (user_id, prefix));

CREATE TABLE objects (
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  key text COLLATE "C" NOT NULL,
  size bigint NOT NULL, sha256 char(64) NOT NULL, mtime_ns bigint NOT NULL,
  s3_key text NOT NULL,
  PRIMARY KEY (user_id, key));

CREATE TABLE shared_objects (
  key text COLLATE "C" PRIMARY KEY,
  size bigint NOT NULL, sha256 char(64) NOT NULL, mtime_ns bigint NOT NULL,
  s3_key text NOT NULL, created_by uuid REFERENCES users ON DELETE SET NULL);

CREATE TABLE trash_items (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  stamp text NOT NULL, key text COLLATE "C" NOT NULL,
  kind text NOT NULL CHECK (kind IN ('doc','object')),
  raw bytea, s3_key text, size bigint NOT NULL, mtime_ns bigint NOT NULL,
  moved_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, stamp, key));

CREATE TABLE llm_usage (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  at timestamptz NOT NULL DEFAULT now(),
  job_kind text, model text NOT NULL,
  input_tokens int NOT NULL, output_tokens int NOT NULL,
  cache_read_tokens int NOT NULL DEFAULT 0, cache_write_tokens int NOT NULL DEFAULT 0,
  cost_usd numeric(10,6) NOT NULL, ok boolean NOT NULL);
CREATE INDEX llm_usage_user_at ON llm_usage (user_id, at);
"""

POLICY_EXPR = "user_id = nullif(current_setting('app.user_id', true), '')::uuid"


def _driver() -> Any:
    """현재 마이그레이션 트랜잭션의 psycopg 연결(식별자·리터럴 인용을 psycopg.sql에 맡긴다)."""
    return op.get_bind().connection.driver_connection


def _app_role() -> tuple[str, str | None]:
    attrs = context.config.attributes
    return str(attrs["app_role"]), attrs.get("app_password")


def _check_roles(drv: Any, app_role: str) -> None:
    row = drv.execute("SELECT current_user, rolsuper OR rolbypassrls FROM pg_roles "
                      "WHERE rolname = current_user").fetchone()
    owner, can_bypass = row
    if app_role == owner:
        raise ScriptError("db_role_unsafe", f"YTC_DB_DSN의 롤({app_role})이 owner와 같습니다. 앱 롤은 따로 둡니다(ytc_app).")
    if not can_bypass:
        raise ScriptError("db_role_unsafe",
                          f"owner 롤({owner})이 슈퍼유저도 BYPASSRLS도 아닙니다. FORCE RLS 때문에 관리 작업이 행을 못 봅니다.")


def upgrade() -> None:
    app_role, password = _app_role()
    drv = _driver()
    _check_roles(drv, app_role)
    role = sql.Identifier(app_role)

    exists = drv.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,)).fetchone()
    if not exists:
        drv.execute(sql.SQL("CREATE ROLE {}").format(role))
    drv.execute(sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT "
                        "NOREPLICATION").format(role))
    if password is not None:
        drv.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(role, sql.Literal(password)))

    op.execute(TABLES_SQL)
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY own ON {table} USING ({POLICY_EXPR}) WITH CHECK ({POLICY_EXPR})")

    drv.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    for table, privileges in APP_GRANTS.items():
        drv.execute(sql.SQL("GRANT " + privileges + " ON {} TO {}").format(sql.Identifier(table), role))
    for seq in SEQUENCES:
        drv.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(sql.Identifier(seq), role))


def downgrade() -> None:
    app_role, _ = _app_role()
    drv = _driver()
    op.execute("DROP TABLE llm_usage, trash_items, shared_objects, objects, folders, docs, users")
    if drv.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,)).fetchone():
        role = sql.Identifier(app_role)
        drv.execute(sql.SQL("DROP OWNED BY {}").format(role))  # 이 DB 안 남은 권한(스키마 USAGE)
        drv.execute(sql.SQL("DROP ROLE {}").format(role))
