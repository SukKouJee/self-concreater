"""관리 화면 API(서버 설계 §4.2 관리 화면, T16): `/api/admin/*`(서버판만, 로컬판에는 경로가 없다).

- 누가: `server.asgi`가 세션의 사용자 행(`is_admin`)을 요청마다 읽어 관리자가 아니면 여기 오기 전에 403 `admin_only`.
  DB도 따로 막는다: `PgAdmin`은 `Db.user_tx(행위자)` 안에서 0003의 SECURITY DEFINER 함수만 부르고, 함수가 `app.user_id`가
  활성 관리자인지 다시 본다(SQLSTATE `YTA01`). 앱 롤은 users에 직접 쓰지 못한다.
- 비밀번호: 규칙(`passwords.check_policy`) → 앱에서 scrypt 해시 → 함수에는 해시만. 응답·로그·감사 기록 어디에도 비밀번호·해시가 없다.
- 경로(`handle`):
  GET  /api/admin/users                      → {users: [...], default_daily_usd}
  POST /api/admin/users                      {email, display_name?, daily_usd_limit?, password?, is_admin?} → {ok, id}
  POST /api/admin/users/<id>/password        {password} → {ok}  (모든 세션 끊김)
  POST /api/admin/users/<id>/clear-password  → {ok}           (모든 세션 끊김)
  POST /api/admin/users/<id>/disable|enable  → {ok}           (disable은 모든 세션 끊김)
  POST /api/admin/users/<id>/logout-all      → {ok}
  POST /api/admin/users/<id>/limit           {daily_usd_limit: 숫자|문자열|null} → {ok}
  POST /api/admin/users/<id>/admin           {is_admin: bool} → {ok}
  GET  /api/admin/usage?days=7               → {days, rows: [...], total_usd}  (1~90일)
  GET  /api/admin/audit?limit=50             → {entries: [...]}                (1~200개)
- 거절(함수 SQLSTATE → `AdminError`): 관리자 아님 403, 없는 사용자 404, 자기 비활성·강등 409 `self_protected`,
  마지막 활성 관리자 409 `last_admin`, 이미 있는 이메일 409 `user_exists`, 잘못된 값 400 `bad_input`.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

import psycopg

from scripts.common import ScriptError
from server import admin, passwords
from server.db import Conn, Db

PREFIX: Final = "/api/admin/"
MAX_NAME: Final = 100
DEFAULT_DAYS: Final = 7
MAX_DAYS: Final = 90
DEFAULT_AUDIT: Final = 50
MAX_AUDIT: Final = 200
USER_PATH: Final = re.compile(r"^/api/admin/users/([^/]+)/([a-z-]+)$")

# error code -> (HTTP status, message)
REFUSALS: Final[dict[str, tuple[int, str]]] = {
    "admin_only": (403, "관리자만 할 수 있습니다"),
    "user_not_found": (404, "없는 사용자입니다. 목록을 새로 고친 뒤 다시 해 보세요"),
    "self_protected": (409, "자기 계정은 비활성으로 만들거나 관리자에서 뺄 수 없습니다"),
    "last_admin": (409, "마지막 관리자는 비활성으로 만들거나 관리자에서 뺄 수 없습니다. 다른 관리자를 먼저 만드세요"),
    "user_exists": (409, "이미 초대한 이메일입니다"),
    "bad_input": (400, "값이 올바르지 않습니다"),
}
# function / constraint SQLSTATE -> error code (0003 migration)
SQLSTATES: Final[dict[str, str]] = {
    "YTA01": "admin_only", "YTA02": "user_not_found", "YTA03": "self_protected", "YTA04": "last_admin",
    "YTA05": "bad_input", "23505": "user_exists", "23514": "bad_input", "22003": "bad_input",
}


class AdminError(Exception):
    """관리 저장소의 거절. `code`는 REFUSALS 키."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def status(self) -> int:
        return REFUSALS[self.code][0]

    def payload(self) -> dict[str, str]:
        return {"error": self.code, "message": REFUSALS[self.code][1]}


class AdminStore(Protocol):
    def list_users(self, actor: uuid.UUID) -> list[dict[str, Any]]: ...
    def add_user(self, actor: uuid.UUID, email: str, display_name: str | None, daily_limit: Decimal | None,
                 password_hash: str | None, is_admin: bool) -> uuid.UUID: ...
    def set_password(self, actor: uuid.UUID, user: uuid.UUID, password_hash: str) -> None: ...
    def clear_password(self, actor: uuid.UUID, user: uuid.UUID) -> None: ...
    def set_disabled(self, actor: uuid.UUID, user: uuid.UUID, disabled: bool) -> None: ...
    def logout_all(self, actor: uuid.UUID, user: uuid.UUID) -> None: ...
    def set_limit(self, actor: uuid.UUID, user: uuid.UUID, usd: Decimal | None) -> None: ...
    def set_admin(self, actor: uuid.UUID, user: uuid.UUID, is_admin: bool) -> None: ...
    def usage(self, actor: uuid.UUID, days: int) -> list[dict[str, Any]]: ...
    def audit(self, actor: uuid.UUID, limit: int) -> list[dict[str, Any]]: ...


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class PgAdmin:
    """0003 관리 함수 호출. 모든 호출은 `user_tx(행위자)`(= app.user_id) 안에서 한다."""

    def __init__(self, db: Db) -> None:
        self._db = db

    def _rows(self, actor: uuid.UUID, query: str, *params: Any) -> list[dict[str, Any]]:
        try:
            with self._db.user_tx(actor) as conn:
                cur = conn.execute(query, params)
                names = [c.name for c in cur.description or []]
                return [{n: _json_value(v) for n, v in zip(names, row, strict=True)} for row in cur.fetchall()]
        except psycopg.Error as e:
            raise _refusal(e) from e

    def _call(self, actor: uuid.UUID, query: str, *params: Any) -> Any:
        try:
            with self._db.user_tx(actor) as conn:
                row = _one(conn, query, params)
        except psycopg.Error as e:
            raise _refusal(e) from e
        return row

    def list_users(self, actor: uuid.UUID) -> list[dict[str, Any]]:
        return self._rows(actor, "SELECT * FROM admin_list_users()")

    def add_user(self, actor: uuid.UUID, email: str, display_name: str | None, daily_limit: Decimal | None,
                 password_hash: str | None, is_admin: bool) -> uuid.UUID:
        value = self._call(actor, "SELECT admin_add_user(%s, %s, %s, %s, %s)", email, display_name, daily_limit,
                           password_hash, is_admin)
        return uuid.UUID(str(value))

    def set_password(self, actor: uuid.UUID, user: uuid.UUID, password_hash: str) -> None:
        self._call(actor, "SELECT admin_set_password(%s, %s)", user, password_hash)

    def clear_password(self, actor: uuid.UUID, user: uuid.UUID) -> None:
        self._call(actor, "SELECT admin_clear_password(%s)", user)

    def set_disabled(self, actor: uuid.UUID, user: uuid.UUID, disabled: bool) -> None:
        self._call(actor, "SELECT admin_set_disabled(%s, %s)", user, disabled)

    def logout_all(self, actor: uuid.UUID, user: uuid.UUID) -> None:
        self._call(actor, "SELECT admin_logout_all(%s)", user)

    def set_limit(self, actor: uuid.UUID, user: uuid.UUID, usd: Decimal | None) -> None:
        self._call(actor, "SELECT admin_set_limit(%s, %s::numeric)", user, usd)

    def set_admin(self, actor: uuid.UUID, user: uuid.UUID, is_admin: bool) -> None:
        self._call(actor, "SELECT admin_set_admin(%s, %s)", user, is_admin)

    def usage(self, actor: uuid.UUID, days: int) -> list[dict[str, Any]]:
        return self._rows(actor, "SELECT * FROM admin_usage(%s)", days)

    def audit(self, actor: uuid.UUID, limit: int) -> list[dict[str, Any]]:
        return self._rows(actor, "SELECT * FROM admin_audit_log(%s)", limit)


def _one(conn: Conn, query: str, params: tuple[Any, ...]) -> Any:
    row = conn.execute(query, params).fetchone()
    return None if row is None else row[0]


def _refusal(exc: psycopg.Error) -> Exception:
    """함수·제약의 SQLSTATE → AdminError. 그 밖(연결 끊김 등)은 원래 오류 그대로(호출부가 503)."""
    code = SQLSTATES.get(exc.sqlstate or "")
    return AdminError(code) if code is not None else exc


# ---- 요청 → 응답 ----------------------------------------------------------------------------

Reply = tuple[int, dict[str, Any]]


def _bad(message: str) -> ScriptError:
    return ScriptError("bad_request", message)


def _target(raw: str) -> uuid.UUID | None:
    try:
        value = uuid.UUID(raw)
    except ValueError:
        return None
    return value if str(value) == raw.lower() else None          # canonical form only (no braces, urn:, ...)


def _email(value: Any) -> str:
    if not isinstance(value, str):
        raise ScriptError("bad_email", "이메일을 넣어 주세요")
    return admin._email(value)


def _name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.strip()) > MAX_NAME:
        raise _bad(f"이름은 {MAX_NAME}자 이하 글자여야 합니다")
    return value.strip() or None


def _limit(value: Any) -> Decimal | None:
    """JSON 값(숫자·문자열·null·빈 문자열) → 일 한도. 규칙은 CLI `--daily-limit`과 같다."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ScriptError("bad_limit", "일 한도는 달러 숫자여야 합니다")
    return admin._limit(str(value).strip())


def _flag(body: Mapping[str, Any], key: str, *, default: bool | None = None) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise _bad(f"{key}는 true 또는 false여야 합니다")
    return value


def _password(value: Any, *, required: bool) -> str | None:
    """규칙을 통과한 비밀번호 또는 None(초대에서 비워 두면 Google 로그인만). 값은 돌려주지도 남기지도 않는다."""
    if value is None or value == "":
        if required:
            raise ScriptError("weak_password", f"비밀번호는 {passwords.MIN_LENGTH}자 이상이어야 합니다.")
        return None
    if not isinstance(value, str):
        raise _bad("비밀번호는 글자여야 합니다")
    return passwords.check_policy(value)


def _int_query(query: Mapping[str, str], key: str, default: int, top: int) -> int:
    raw = query.get(key)
    if raw is None or raw == "":
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= top:
        raise _bad(f"{key}는 1 이상 {top} 이하의 숫자여야 합니다")
    return int(raw)


def needs_hash(method: str, path: str) -> bool:
    """scrypt를 계산할 수 있는 요청(호출부가 해시 슬롯 안에서 돌린다)."""
    return method == "POST" and (path == PREFIX + "users" or path.endswith("/password"))


def handle(store: AdminStore, actor: uuid.UUID, method: str, path: str, query: Mapping[str, str],
           body: Mapping[str, Any], *, default_daily_usd: Decimal,
           hash_password: Callable[[str], str] | None = None) -> Reply:
    """관리 요청 하나. 값이 틀리면 ScriptError(400), 저장소가 거절하면 AdminError.
    행위자가 관리자인지는 호출부와 DB가 본다."""
    hasher = hash_password or passwords.hash_password
    if path == PREFIX + "users":
        if method == "GET":
            users = [{**row, "effective_daily_usd": row["daily_usd_limit"] if row["daily_usd_limit"] is not None
                      else float(default_daily_usd), "is_self": row["id"] == str(actor)}
                     for row in store.list_users(actor)]
            return 200, {"users": users, "default_daily_usd": float(default_daily_usd)}
        if method == "POST":
            email, name, limit = _email(body.get("email")), _name(body.get("display_name")), _limit(
                body.get("daily_usd_limit"))
            is_admin = _flag(body, "is_admin", default=False)
            secret = _password(body.get("password"), required=False)
            new_id = store.add_user(actor, email, name, limit, None if secret is None else hasher(secret), is_admin)
            return 200, {"ok": True, "id": str(new_id)}
    if method == "GET" and path == PREFIX + "usage":
        days = _int_query(query, "days", DEFAULT_DAYS, MAX_DAYS)
        rows = store.usage(actor, days)
        total = sum((Decimal(str(r["cost_usd"])) for r in rows), Decimal(0))
        return 200, {"days": days, "rows": rows, "total_usd": float(total)}
    if method == "GET" and path == PREFIX + "audit":
        return 200, {"entries": store.audit(actor, _int_query(query, "limit", DEFAULT_AUDIT, MAX_AUDIT))}
    found = USER_PATH.match(path)
    if method != "POST" or found is None:
        return 404, {"error": "not_found", "message": "없는 경로입니다"}
    target = _target(found.group(1))
    action = found.group(2)
    if target is None:
        return 404, AdminError("user_not_found").payload()
    if action == "password":
        secret = _password(body.get("password"), required=True)
        assert secret is not None
        store.set_password(actor, target, hasher(secret))
    elif action == "clear-password":
        store.clear_password(actor, target)
    elif action in ("disable", "enable"):
        store.set_disabled(actor, target, action == "disable")
    elif action == "logout-all":
        store.logout_all(actor, target)
    elif action == "limit":
        if "daily_usd_limit" not in body:
            raise ScriptError("bad_limit", "daily_usd_limit가 필요합니다(기본값으로 되돌리려면 null)")
        store.set_limit(actor, target, _limit(body.get("daily_usd_limit")))
    elif action == "admin":
        store.set_admin(actor, target, _flag(body, "is_admin"))
    else:
        return 404, {"error": "not_found", "message": "없는 경로입니다"}
    return 200, {"ok": True}
