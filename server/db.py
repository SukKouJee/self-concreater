"""PostgreSQL 접속(서버 설계 §2·§3.1, ADR-S8·S9).

- 앱 요청은 `Db.user_tx(uid)`만 쓴다: 앱 롤(`YTC_DB_DSN`, NOBYPASSRLS) 풀에서 연결을 빌려 한 트랜잭션을 열고
  `set_config('app.user_id', uid, true)`를 먼저 실행한다. 설정은 트랜잭션이 끝나면 사라지고(로컬), 연결이 풀로
  돌아갈 때 `RESET ALL`로 한 번 더 지운다. 설정이 없으면 RLS 정책이 NULL과 비교해 행 0개(fail closed).
- 관리 작업(사용자 추가·비활성, 전체 사용량)은 `Db.admin_tx()`: owner 롤(`YTC_DB_OWNER_DSN`)로 연결 하나를 따로 연다.
  풀에 섞지 않는다(앱 요청이 owner 연결을 빌리는 일이 없게).
- `open()`은 앱 롤이 슈퍼유저·BYPASSRLS이거나 다른 롤에 속해 있으면 거부한다(`db_role_unsafe`). 격리의 최종 방어선이 RLS이기 때문이다.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Final

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from scripts.common import ScriptError

USER_SETTING: Final = "app.user_id"
# 앱 롤 이름은 마이그레이션이 GRANT·CREATE ROLE에 그대로 쓴다. 따옴표가 필요 없는 소문자 식별자만 받는다
ROLE_NAME_RE: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

Conn = psycopg.Connection[TupleRow]


def app_role_from_dsn(dsn: str) -> tuple[str, str | None]:
    """`YTC_DB_DSN`(URL 또는 key=value)에서 (앱 롤 이름, 비밀번호|None)."""
    params = conninfo_to_dict(dsn)
    user = params.get("user")
    if not user:
        raise ScriptError("config_missing", "YTC_DB_DSN에 사용자(앱 롤 이름)가 없습니다. 예: postgresql://ytc_app:<비밀번호>@postgres/ytc")
    name = str(user)
    if not ROLE_NAME_RE.match(name):
        raise ScriptError("config_invalid", f"앱 롤 이름은 소문자·숫자·밑줄로 63자 이내여야 합니다: {name!r}")
    password = params.get("password")
    return name, (str(password) if password is not None else None)


def role_password_sql(conn: Conn, role: str, password: str) -> sql.Composed:
    """`ALTER ROLE <role> PASSWORD '<SCRAM 검증자>'`. 평문은 클라이언트에서 해시하고 서버로 보내지 않는다
    (문장 로그·pg_stat_statements·오류 메시지에 평문이 남지 않게)."""
    verifier = conn.pgconn.encrypt_password(password.encode("utf-8"), role.encode("utf-8"), b"scram-sha-256")
    return sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(verifier.decode("ascii")))


def role_memberships(conn: Conn, role: str) -> list[str]:
    """`role`이 속한 롤 이름들(pg_auth_members). 앱 롤은 비어 있어야 한다: 소속 롤로 SET ROLE 하거나
    pg_read_all_data 같은 내장 롤 권한을 얻으면 RLS·최소 권한이 무너진다."""
    rows = conn.execute("SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                        "JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = %s ORDER BY 1", (role,)).fetchall()
    return [str(r[0]) for r in rows]


def _as_uuid(user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(user_id, uuid.UUID):
        return user_id
    if isinstance(user_id, str):
        return uuid.UUID(user_id)  # ValueError on anything that is not a UUID
    raise TypeError(f"user_id must be uuid.UUID or str, not {type(user_id).__name__}")


def _reset(conn: Conn) -> None:
    """풀로 돌아온 연결에서 세션 설정(실수로 세션 단위로 건 app.user_id 포함)을 지운다."""
    conn.execute("RESET ALL")
    conn.commit()


class Db:
    """앱 롤 연결 풀 + owner 연결. `with Db(...) as db:` 또는 `open()`/`close()`."""

    def __init__(self, app_dsn: str, owner_dsn: str | None = None, *, min_size: int = 1, max_size: int = 8,
                 timeout: float = 10.0) -> None:
        self._owner_dsn = owner_dsn
        self._pool: ConnectionPool[Conn] = ConnectionPool(
            app_dsn, min_size=min_size, max_size=max_size, timeout=timeout, open=False, reset=_reset,
            check=ConnectionPool.check_connection, name="ytc-app")

    def open(self) -> None:
        self._pool.open(wait=True)
        try:
            self._check_app_role()
        except BaseException:
            self._pool.close()
            raise

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Db:
        self.open()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    def _check_app_role(self) -> None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                               "WHERE rolname = current_user").fetchone()
            name = str(row[0]) if row else "?"
            groups = role_memberships(conn, name)
        if row is None or row[1] or row[2]:
            raise ScriptError("db_role_unsafe",
                              f"YTC_DB_DSN의 롤({name})이 슈퍼유저이거나 RLS를 건너뜁니다. 앱은 NOBYPASSRLS 롤(ytc_app)로 접속해야 합니다.")
        if groups:
            raise ScriptError("db_role_unsafe",
                              f"YTC_DB_DSN의 롤({name})이 다른 롤에 속해 있습니다({', '.join(groups)}). 앱 롤은 어떤 롤에도 속하지 않아야 합니다.")

    @contextmanager
    def user_tx(self, user_id: uuid.UUID | str) -> Iterator[Conn]:
        """사용자 한 명의 트랜잭션. 정상 종료면 커밋, 예외면 롤백. RLS가 그 사용자 행만 보이게 한다."""
        uid = _as_uuid(user_id)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT set_config(%s, %s, true)", (USER_SETTING, str(uid)))
            yield conn

    @contextmanager
    def admin_tx(self) -> Iterator[Conn]:
        """owner 롤 트랜잭션(관리 CLI 전용). 모든 사용자 행이 보인다."""
        if not self._owner_dsn:
            raise ScriptError("config_missing", "관리 작업에는 YTC_DB_OWNER_DSN(owner 롤 접속 정보)이 필요합니다.")
        with psycopg.connect(self._owner_dsn) as conn, conn.transaction():
            yield conn
