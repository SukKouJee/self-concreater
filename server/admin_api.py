"""관리 화면 API(서버 설계 §4.2 관리 화면, T16 + 0004 사용자 관리): `/api/admin/*`(서버판만, 로컬판에는 경로가 없다).

- 누가: `server.asgi`가 세션의 사용자 행(`is_admin`)을 요청마다 읽어 관리자가 아니면 여기 오기 전에 403 `admin_only`.
  DB도 따로 막는다: `PgAdmin`은 `Db.user_tx(행위자)` 안에서 0003·0004의 SECURITY DEFINER 함수만 부르고, 함수가 `app.user_id`가
  활성 관리자인지 다시 본다(SQLSTATE `YTA01`). 앱 롤이 users에 직접 쓰는 것은 Google 로그인의 `google_sub` 고정뿐이다(0004가 `display_name` UPDATE를 걷음). 바꾸는 함수마다 감사 행이 하나 남는다.
- 비밀번호: 규칙(`passwords.check_policy`) → 앱에서 scrypt 해시 → 함수에는 해시만. 응답·로그·감사 기록 어디에도 비밀번호·해시가 없다.
- 경로(`handle`):
  GET  /api/admin/overview                   → {counts, spend, default_daily_usd, login, features_off}
  GET  /api/admin/users?q=&status=&role=&login=&sort=&order=&page=&per_page=
                                             → {users, total, page, per_page, pages, default_daily_usd}
       status all|active|disabled, role all|admin|member, login all|password|google|none,
       sort email|last_login|created|today, order asc|desc, page ≥ 1, per_page 1~100(기본 50)
  POST /api/admin/users                      {email, display_name?, daily_usd_limit?, password?, is_admin?} → {ok, id, email}
                                             (Google 로그인이 꺼진 서버는 비밀번호 필수 `password_required`)
  GET  /api/admin/users/<id>                 → {user: {..., storage_bytes, effective_daily_usd, is_self}, default_daily_usd}
  POST /api/admin/users/<id>/update          {email?, display_name?, daily_usd_limit?, is_admin?, disabled?,
                                              password? | clear_password?} → {ok, changed, sessions_ended}
                                             (한 트랜잭션. 이메일을 바꾸면 Google 연결이 풀리고 모든 세션이 끊긴다)
  POST /api/admin/users/<id>/delete          {confirm_email} → {ok, email, deleted, storage}
                                             (입력한 이메일이 그 사용자 이메일과 같아야 한다. 자기 자신은 409 `self_delete`.
                                              PG 함수가 행을 지우고 커밋한 뒤 앱이 MinIO `users/{id}/`·`trash/{id}/`를 지운다)
  POST /api/admin/users/<id>/password        {password} → {ok}  (모든 세션 끊김)
  POST /api/admin/users/<id>/clear-password  → {ok}           (모든 세션 끊김)
  POST /api/admin/users/<id>/disable|enable  → {ok}           (disable은 모든 세션 끊김)
  POST /api/admin/users/<id>/logout-all      → {ok}
  POST /api/admin/users/<id>/limit           {daily_usd_limit: 숫자|문자열|null} → {ok}
  POST /api/admin/users/<id>/admin           {is_admin: bool} → {ok}
  GET  /api/admin/usage?days=7               → {days, rows: [...], total_usd}  (1~90일, 지운 사용자는 email null)
  GET  /api/admin/audit?actor=&action=&target=&from=YYYY-MM-DD&to=&page=&per_page=
                                             → {entries, total, page, per_page, pages, actions} (`limit`는 per_page의 옛 이름)
- 거절(함수 SQLSTATE → `AdminError`): 관리자 아님 403, 없는 사용자 404, 자기 비활성·강등 409 `self_protected`,
  자기 삭제 409 `self_delete`, 마지막 활성 관리자 409 `last_admin`, 이미 있는 이메일 409 `user_exists`,
  확인 이메일 다름 400 `confirm_mismatch`, 잘못된 값 400 `bad_input`.
"""
from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

import psycopg

from scripts.common import ScriptError
from server import admin, passwords
from server.db import Conn, Db
from server.s3store import purge_user_objects

PREFIX: Final = "/api/admin/"
MAX_NAME: Final = 100
MAX_QUERY: Final = 200
DEFAULT_DAYS: Final = 7
MAX_DAYS: Final = 90
DEFAULT_PER_PAGE: Final = 50
MAX_PER_PAGE: Final = 100
MAX_AUDIT_PER_PAGE: Final = 200
MAX_PAGE: Final = 100_000
USER_PATH: Final = re.compile(r"^/api/admin/users/([^/]+)/([a-z-]+)$")
USER_DETAIL_PATH: Final = re.compile(r"^/api/admin/users/([^/]+)$")
DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")

STATUSES: Final = ("all", "active", "disabled")
ROLES: Final = ("all", "admin", "member")
LOGINS: Final = ("all", "password", "google", "none")
SORTS: Final = ("email", "last_login", "created", "today")
ORDERS: Final = ("asc", "desc")
# every action an audit row can carry (0003 + 0004); the audit filter accepts only these
AUDIT_ACTIONS: Final = ("add_user", "set_email", "set_name", "set_password", "clear_password", "disable", "enable",
                        "logout_all", "set_limit", "set_admin", "delete_user")
UPDATE_KEYS: Final = frozenset({"email", "display_name", "daily_usd_limit", "is_admin", "disabled", "password",
                                "clear_password"})
COUNT_KEYS: Final = ("users", "admins", "disabled", "with_password", "with_google", "never_logged_in")
SPEND_KEYS: Final = ("today_usd", "month_usd", "month_deleted_usd")

__all__ = ["AdminError", "AdminInfo", "AdminStore", "AuditQuery", "PgAdmin", "UserChanges", "UserLifecycle",
           "UserQuery", "handle", "needs_hash", "purge_user_objects"]

# error code -> (HTTP status, message)
REFUSALS: Final[dict[str, tuple[int, str]]] = {
    "admin_only": (403, "관리자만 할 수 있습니다"),
    "user_not_found": (404, "없는 사용자입니다. 목록을 새로 고친 뒤 다시 해 보세요"),
    "self_protected": (409, "자기 계정은 비활성으로 만들거나 관리자에서 뺄 수 없습니다"),
    "self_delete": (409, "자기 계정은 지울 수 없습니다. 다른 관리자에게 부탁하세요"),
    "last_admin": (409, "마지막 관리자는 비활성으로 만들거나 관리자에서 빼거나 지울 수 없습니다. 다른 관리자를 먼저 만드세요"),
    "user_exists": (409, "이미 등록된 이메일입니다"),
    "confirm_mismatch": (400, "입력한 이메일이 지울 사용자의 이메일과 다릅니다"),
    "bad_input": (400, "값이 올바르지 않습니다"),
}
# function / constraint SQLSTATE -> error code (0003, 0004 migrations)
SQLSTATES: Final[dict[str, str]] = {
    "YTA01": "admin_only", "YTA02": "user_not_found", "YTA03": "self_protected", "YTA04": "last_admin",
    "YTA05": "bad_input", "YTA06": "confirm_mismatch", "23505": "user_exists", "23514": "bad_input",
    "22003": "bad_input",
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


@dataclass(frozen=True)
class UserQuery:
    """사용자 목록 한 쪽(검색·필터·정렬). 값은 `_user_query`가 검사한 뒤의 것."""

    q: str = ""
    status: str = "all"
    role: str = "all"
    login: str = "all"
    sort: str = "email"
    desc: bool = False
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


@dataclass(frozen=True)
class AuditQuery:
    actor: str = ""
    action: str = ""
    target: str = ""
    date_from: date | None = None
    date_to: date | None = None
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


@dataclass(frozen=True)
class UserChanges:
    """한 사용자에게 한 번에 적용할 변경. None·False는 "그대로"(이름·한도는 None으로 지울 수 있어 set_* 표시를 따로 둔다)."""

    email: str | None = None
    display_name: str | None = None
    set_name: bool = False
    daily_limit: Decimal | None = None
    set_limit: bool = False
    is_admin: bool | None = None
    disabled: bool | None = None
    password_hash: str | None = None
    clear_password: bool = False

    def fields(self) -> list[str]:
        """바뀌는 칸 이름(응답 `changed`, 화면 알림용). 순서 고정."""
        out = []
        if self.email is not None:
            out.append("email")
        if self.set_name:
            out.append("display_name")
        if self.set_limit:
            out.append("daily_usd_limit")
        if self.is_admin is not None:
            out.append("is_admin")
        if self.disabled is not None:
            out.append("disabled")
        if self.password_hash is not None or self.clear_password:
            out.append("password")
        return out

    def ends_sessions(self) -> bool:
        """이 변경이 그 사용자의 모든 세션을 끊는가(epoch 증가: 이메일·비밀번호·비활성)."""
        return (self.email is not None or self.password_hash is not None or self.clear_password
                or self.disabled is True)


@dataclass(frozen=True)
class AdminInfo:
    """서버 정보 탭과 규칙에 쓰는 설정값(env에서 읽은 것, 화면에서는 바꾸지 않는다)."""

    default_daily_usd: Decimal
    google: bool = False
    password_login: bool = False
    dev_login: bool = False
    features_off: tuple[tuple[str, str], ...] = field(default_factory=tuple)     # (name, Korean note)


class AdminStore(Protocol):
    def list_page(self, actor: uuid.UUID, query: UserQuery) -> dict[str, Any]: ...
    def user_detail(self, actor: uuid.UUID, user: uuid.UUID) -> dict[str, Any]: ...
    def overview(self, actor: uuid.UUID) -> dict[str, Any]: ...
    def add_user(self, actor: uuid.UUID, email: str, display_name: str | None, daily_limit: Decimal | None,
                 password_hash: str | None, is_admin: bool) -> uuid.UUID: ...
    def update_user(self, actor: uuid.UUID, user: uuid.UUID, changes: UserChanges) -> None: ...
    def delete_user(self, actor: uuid.UUID, user: uuid.UUID, confirm_email: str) -> dict[str, Any]: ...
    def set_password(self, actor: uuid.UUID, user: uuid.UUID, password_hash: str) -> None: ...
    def clear_password(self, actor: uuid.UUID, user: uuid.UUID) -> None: ...
    def set_disabled(self, actor: uuid.UUID, user: uuid.UUID, disabled: bool) -> None: ...
    def logout_all(self, actor: uuid.UUID, user: uuid.UUID) -> None: ...
    def set_limit(self, actor: uuid.UUID, user: uuid.UUID, usd: Decimal | None) -> None: ...
    def set_admin(self, actor: uuid.UUID, user: uuid.UUID, is_admin: bool) -> None: ...
    def usage(self, actor: uuid.UUID, days: int) -> list[dict[str, Any]]: ...
    def audit_page(self, actor: uuid.UUID, query: AuditQuery) -> dict[str, Any]: ...


class UserLifecycle(Protocol):
    """DB 밖의 정리. 지우기: (비활성, 커밋) → `stop_jobs` → (PG 함수, 커밋) → `purge_storage` → `forget`.
    DB가 지우기를 거절하면 `resume_jobs`. 이메일 변경: `forget`."""

    def stop_jobs(self, user: uuid.UUID) -> None: ...
    def resume_jobs(self, user: uuid.UUID) -> None: ...
    def purge_storage(self, user: uuid.UUID, s3_keys: list[str]) -> dict[str, Any]: ...
    def forget(self, user: uuid.UUID) -> None: ...


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class PgAdmin:
    """0003·0004 관리 함수 호출. 모든 호출은 `user_tx(행위자)`(= app.user_id) 안에서 한다."""

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

    def _object(self, actor: uuid.UUID, query: str, *params: Any) -> dict[str, Any]:
        value = self._call(actor, query, *params)
        if not isinstance(value, dict):
            raise TypeError(f"admin function returned {type(value).__name__}, not a JSON object")
        return value

    def list_page(self, actor: uuid.UUID, query: UserQuery) -> dict[str, Any]:
        return self._object(actor, "SELECT admin_list_users_page(%s, %s, %s, %s, %s, %s, %s, %s)",
                            query.q or None, query.status, query.role, query.login, query.sort, query.desc,
                            query.per_page, query.offset)

    def user_detail(self, actor: uuid.UUID, user: uuid.UUID) -> dict[str, Any]:
        return self._object(actor, "SELECT admin_user_detail(%s)", user)

    def overview(self, actor: uuid.UUID) -> dict[str, Any]:
        return self._object(actor, "SELECT admin_overview()")

    def add_user(self, actor: uuid.UUID, email: str, display_name: str | None, daily_limit: Decimal | None,
                 password_hash: str | None, is_admin: bool) -> uuid.UUID:
        value = self._call(actor, "SELECT admin_add_user(%s, %s, %s, %s, %s)", email, display_name, daily_limit,
                           password_hash, is_admin)
        return uuid.UUID(str(value))

    def update_user(self, actor: uuid.UUID, user: uuid.UUID, changes: UserChanges) -> None:
        """여러 칸을 한 트랜잭션에서(하나라도 거절되면 아무것도 바뀌지 않는다). 함수마다 감사 행이 남는다.
        순서: 이름 → 이메일 → 한도 → 관리자 → 활성 → 비밀번호(자기 보호 검사는 함수가 한다)."""
        steps: list[tuple[str, tuple[Any, ...]]] = []
        if changes.set_name:
            steps.append(("SELECT admin_set_name(%s, %s)", (user, changes.display_name)))
        if changes.email is not None:
            steps.append(("SELECT admin_set_email(%s, %s)", (user, changes.email)))
        if changes.set_limit:
            steps.append(("SELECT admin_set_limit(%s, %s::numeric)", (user, changes.daily_limit)))
        if changes.is_admin is not None:
            steps.append(("SELECT admin_set_admin(%s, %s)", (user, changes.is_admin)))
        if changes.disabled is not None:
            steps.append(("SELECT admin_set_disabled(%s, %s)", (user, changes.disabled)))
        if changes.password_hash is not None:
            steps.append(("SELECT admin_set_password(%s, %s)", (user, changes.password_hash)))
        elif changes.clear_password:
            steps.append(("SELECT admin_clear_password(%s)", (user,)))
        try:
            with self._db.user_tx(actor) as conn:
                for query, params in steps:
                    conn.execute(query, params)
        except psycopg.Error as e:
            raise _refusal(e) from e

    def delete_user(self, actor: uuid.UUID, user: uuid.UUID, confirm_email: str) -> dict[str, Any]:
        return self._object(actor, "SELECT admin_delete_user(%s, %s)", user, confirm_email)

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

    def audit_page(self, actor: uuid.UUID, query: AuditQuery) -> dict[str, Any]:
        return self._object(actor, "SELECT admin_audit_page(%s, %s, %s, %s, %s, %s, %s)",
                            query.actor or None, query.action or None, query.target or None, query.date_from,
                            query.date_to, query.per_page, query.offset)


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
    """규칙을 통과한 비밀번호 또는 None(비워 두면 Google 로그인만). 값은 돌려주지도 남기지도 않는다."""
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


def _choice(query: Mapping[str, str], key: str, allowed: tuple[str, ...]) -> str:
    raw = query.get(key) or allowed[0]
    if raw not in allowed:
        raise _bad(f"{key}는 {', '.join(allowed)} 중 하나여야 합니다")
    return raw


def _text_query(query: Mapping[str, str], key: str) -> str:
    raw = (query.get(key) or "").strip()
    if len(raw) > MAX_QUERY:
        raise _bad(f"{key}는 {MAX_QUERY}자 이하여야 합니다")
    return raw


def _date_query(query: Mapping[str, str], key: str) -> date | None:
    raw = (query.get(key) or "").strip()
    if not raw:
        return None
    if not DATE_RE.match(raw):
        raise _bad(f"{key}는 YYYY-MM-DD 날짜여야 합니다")
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise _bad(f"{key}는 있는 날짜여야 합니다") from e


def _user_query(query: Mapping[str, str]) -> UserQuery:
    return UserQuery(q=_text_query(query, "q"), status=_choice(query, "status", STATUSES),
                     role=_choice(query, "role", ROLES), login=_choice(query, "login", LOGINS),
                     sort=_choice(query, "sort", SORTS), desc=_choice(query, "order", ORDERS) == "desc",
                     page=_int_query(query, "page", 1, MAX_PAGE),
                     per_page=_int_query(query, "per_page", DEFAULT_PER_PAGE, MAX_PER_PAGE))


def _audit_query(query: Mapping[str, str]) -> AuditQuery:
    action = (query.get("action") or "").strip()
    if action and action not in AUDIT_ACTIONS:
        raise _bad("action은 감사 기록에 있는 동작 이름이어야 합니다")
    per_key = "per_page" if query.get("per_page") else "limit"
    out = AuditQuery(actor=_text_query(query, "actor"), action=action, target=_text_query(query, "target"),
                     date_from=_date_query(query, "from"), date_to=_date_query(query, "to"),
                     page=_int_query(query, "page", 1, MAX_PAGE),
                     per_page=_int_query(query, per_key, DEFAULT_PER_PAGE, MAX_AUDIT_PER_PAGE))
    if out.date_from and out.date_to and out.date_from > out.date_to:
        raise _bad("시작 날짜가 끝 날짜보다 늦습니다")
    return out


def _pages(total: int, per_page: int) -> int:
    return max(1, math.ceil(total / per_page))


def _changes(body: Mapping[str, Any], hasher: Callable[[str], str]) -> UserChanges:
    """`/update` 본문 → UserChanges. 없는 키는 그대로, 모르는 키·틀린 값은 400. 비밀번호는 여기서 해시된다."""
    unknown = sorted(set(body) - UPDATE_KEYS)
    if unknown:
        raise _bad(f"바꿀 수 없는 항목입니다: {', '.join(unknown)}")
    kw: dict[str, Any] = {}
    if "email" in body:
        kw["email"] = _email(body["email"])
    if "display_name" in body:
        kw["display_name"], kw["set_name"] = _name(body["display_name"]), True
    if "daily_usd_limit" in body:
        kw["daily_limit"], kw["set_limit"] = _limit(body["daily_usd_limit"]), True
    for key in ("is_admin", "disabled"):
        if key in body:
            kw[key] = _flag(body, key)
    clear = _flag(body, "clear_password") if "clear_password" in body else False
    if "password" in body and clear:
        raise _bad("비밀번호 정하기와 지우기를 함께 할 수 없습니다")
    if "password" in body:
        secret = _password(body["password"], required=True)
        assert secret is not None
        kw["password_hash"] = hasher(secret)
    kw["clear_password"] = clear
    changes = UserChanges(**kw)
    if not changes.fields():
        raise ScriptError("nothing_to_change", "바꿀 항목이 없습니다")
    return changes


def _with_limits(row: dict[str, Any], actor: uuid.UUID, info: AdminInfo) -> dict[str, Any]:
    limit = row.get("daily_usd_limit")
    return {**row, "effective_daily_usd": limit if limit is not None else float(info.default_daily_usd),
            "is_self": row.get("id") == str(actor)}


def _storage_bytes(row: Mapping[str, Any]) -> int:
    return int(row.get("docs_bytes") or 0) + int(row.get("objects_bytes") or 0) + int(row.get("trash_bytes") or 0)


def needs_hash(method: str, path: str) -> bool:
    """scrypt를 계산할 수 있는 요청(호출부가 해시 슬롯 안에서 돌린다)."""
    return method == "POST" and (path == PREFIX + "users" or path.endswith(("/password", "/update")))


def _delete(store: AdminStore, life: UserLifecycle, actor: uuid.UUID, target: uuid.UUID,
            body: Mapping[str, Any]) -> Reply:
    """입력한 이메일 확인 → 자기 자신 거절 → 비활성(따로 커밋: epoch 증가로 그 사람의 다음 요청은 401) → 작업 멈춤(표시를 남겨
    새 작업 매니저도 막음) → PG 함수(행·감사, 커밋) → MinIO 정리 → 캐시 비우기. DB가 지우기를 거절하면 작업 표시를 걷고,
    원래 활성이었으면 다시 활성으로(둘 다 감사 기록)."""
    typed = body.get("confirm_email")
    if not isinstance(typed, str) or not typed.strip():
        raise ScriptError("confirm_required", "지우려면 그 사용자의 이메일을 그대로 입력하세요")
    detail = store.user_detail(actor, target)
    if target == actor:
        raise AdminError("self_delete")
    email = str(detail["email"])
    if typed.strip().lower() != email:
        raise AdminError("confirm_mismatch")
    was_enabled = not detail.get("disabled")
    if was_enabled:
        store.set_disabled(actor, target, True)
    life.stop_jobs(target)
    try:
        summary = store.delete_user(actor, target, email)
    except AdminError as e:
        life.resume_jobs(target)
        if was_enabled:
            store.set_disabled(actor, target, False)
        if e.code == "self_protected":
            raise AdminError("self_delete") from e
        raise
    storage = life.purge_storage(target, [str(k) for k in summary.get("s3_keys") or []])
    life.forget(target)
    deleted = {key: summary.get(key, 0) for key in ("docs", "folders", "objects", "trash", "usage_rows", "usage_usd")}
    deleted["bytes"] = _storage_bytes(summary)
    return 200, {"ok": True, "email": email, "deleted": deleted, "storage": storage}


def handle(store: AdminStore, actor: uuid.UUID, method: str, path: str, query: Mapping[str, str],
           body: Mapping[str, Any], *, info: AdminInfo, life: UserLifecycle,
           hash_password: Callable[[str], str] | None = None) -> Reply:
    """관리 요청 하나. 값이 틀리면 ScriptError(400), 저장소가 거절하면 AdminError.
    행위자가 관리자인지는 호출부와 DB가 본다."""
    hasher = hash_password or passwords.hash_password
    if method == "GET" and path == PREFIX + "overview":
        found = store.overview(actor)
        return 200, {"counts": {k: found.get(k, 0) for k in COUNT_KEYS},
                     "spend": {k: found.get(k, 0) for k in SPEND_KEYS},
                     "default_daily_usd": float(info.default_daily_usd),
                     "login": {"google": info.google, "password": info.password_login, "dev": info.dev_login},
                     "features_off": [{"name": name, "text": text} for name, text in info.features_off]}
    if path == PREFIX + "users":
        if method == "GET":
            wanted = _user_query(query)
            page = store.list_page(actor, wanted)
            total = int(page.get("total") or 0)
            return 200, {"users": [_with_limits(row, actor, info) for row in page.get("rows") or []],
                         "total": total, "page": wanted.page, "per_page": wanted.per_page,
                         "pages": _pages(total, wanted.per_page), "default_daily_usd": float(info.default_daily_usd)}
        if method == "POST":
            email, name, limit = _email(body.get("email")), _name(body.get("display_name")), _limit(
                body.get("daily_usd_limit"))
            is_admin = _flag(body, "is_admin", default=False)
            secret = _password(body.get("password"), required=False)
            if secret is None and not info.google:
                raise ScriptError("password_required", "이 서버는 Google 로그인이 꺼져 있어 비밀번호가 있어야 로그인할 수 있습니다. "
                                                       f"{passwords.MIN_LENGTH}자 이상으로 정해 주세요.")
            new_id = store.add_user(actor, email, name, limit, None if secret is None else hasher(secret), is_admin)
            return 200, {"ok": True, "id": str(new_id), "email": email}
    if method == "GET" and path == PREFIX + "usage":
        days = _int_query(query, "days", DEFAULT_DAYS, MAX_DAYS)
        rows = store.usage(actor, days)
        total = sum((Decimal(str(r["cost_usd"])) for r in rows), Decimal(0))
        return 200, {"days": days, "rows": rows, "total_usd": float(total)}
    if method == "GET" and path == PREFIX + "audit":
        wanted_audit = _audit_query(query)
        page = store.audit_page(actor, wanted_audit)
        total = int(page.get("total") or 0)
        return 200, {"entries": page.get("rows") or [], "total": total, "page": wanted_audit.page,
                     "per_page": wanted_audit.per_page, "pages": _pages(total, wanted_audit.per_page),
                     "actions": list(AUDIT_ACTIONS)}
    detail_path = USER_DETAIL_PATH.match(path)
    if method == "GET" and detail_path is not None:
        target = _target(detail_path.group(1))
        if target is None:
            return 404, AdminError("user_not_found").payload()
        row = store.user_detail(actor, target)
        return 200, {"user": {**_with_limits(row, actor, info), "storage_bytes": _storage_bytes(row)},
                     "default_daily_usd": float(info.default_daily_usd)}
    found = USER_PATH.match(path)
    if method != "POST" or found is None:
        return 404, {"error": "not_found", "message": "없는 경로입니다"}
    target = _target(found.group(1))
    action = found.group(2)
    if target is None:
        return 404, AdminError("user_not_found").payload()
    if action == "update":
        changes = _changes(body, hasher)
        store.update_user(actor, target, changes)
        if changes.email is not None:
            life.forget(target)
        return 200, {"ok": True, "changed": changes.fields(), "sessions_ended": changes.ends_sessions()}
    if action == "delete":
        return _delete(store, life, actor, target, body)
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
