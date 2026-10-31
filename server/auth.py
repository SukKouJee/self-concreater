"""서버판 인증: 세션 쿠키 내용·검사, CSRF, 사용자 조회, Google 로그인 허용 판정(서버 설계 §4.2).

- 세션은 Starlette `SessionMiddleware`(itsdangerous 서명 쿠키 `ytc_session`, 14일, `HttpOnly; SameSite=Lax`, https면 `Secure`).
  내용은 `{uid, epoch, iat}`뿐이다. 서명은 위조를 막고, **요청마다** `users`를 다시 읽어 `disabled`·`session_epoch`를 확인한다
  (`session_valid`). 비활성·epoch 증가·만료·없는 사용자면 세션을 비우고 401 `login_required`.
- CSRF(`csrf_error`): POST·PUT·DELETE·PATCH는 ① `Origin`이 `YTC_PUBLIC_URL`의 출처와 같아야 하고(없으면 `Sec-Fetch-Site: same-origin`)
  ② `Content-Type: application/json`이어야 한다. 단순 폼 교차 전송이 둘 다에 걸린다. SameSite=Lax가 한 겹 더.
- 사용자 조회(`PgUsers`)는 `Db.public_tx()`(app.user_id 없이, `users`는 RLS 없음)로 한다. 로그인 전이라 사용자 id가 없다.
- Google 로그인 허용(`admit_google`, T12): 이메일 인증된 Google 계정이고, 그 이메일이 `users`에 있고(초대제: 관리자 `add-user`,
  모르는 이메일로 계정을 만들지 않는다), 비활성이 아니고, `google_sub`가 비었으면 이번 sub로 고정(`pin_google_sub`, 비어 있을 때만
  UPDATE, 조회한 이메일이 그대로일 때만 — 관리자가 그사이 이메일을 바꾸면 `not_invited`), 이미 있으면 같아야 한다. 같은 sub가 다른 사용자 행에 있으면(UNIQUE) 거부. 거부는 `LoginDenied(reason)`.
  프로토콜 검증(state·서명·클레임)은 `server.oidc`.
- 이메일+비밀번호 로그인(T15, `YTC_PASSWORD_LOGIN=1`): `PgUsers.password_login`이 행과 `password_hash`를 함께 읽는다(해시는
  `UserRow`에 넣지 않는다: 세션 확인 경로에는 해시가 오가지 않는다). 해시·검증은 `server.passwords`, 실패 횟수 제한은
  `server.login_limit`, 경로는 `server.asgi`(`POST /auth/password`). 모든 실패는 같은 문구(`LOGIN_FAILED`).
- 마지막 로그인(0004): 로그인에 성공하면(Google·비밀번호·dev) `record_login`이 `users.last_login_at`을 적는다. 실패해도 로그인은 막지 않는다
  (`server.asgi`가 로그만 남긴다).
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from psycopg.errors import UniqueViolation

from server.db import Db
from server.oidc import GoogleClaims

SESSION_COOKIE: Final = "ytc_session"
SESSION_MAX_AGE: Final = 14 * 24 * 60 * 60
CLOCK_SKEW: Final = 60                     # iat may be this far in the future (same process signs; leave slack)
UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "DELETE", "PATCH"})
JSON_TYPE: Final = "application/json"

LOGIN_REQUIRED: Final = {"error": "login_required", "message": "로그인이 필요합니다"}
BAD_ORIGIN: Final = {"error": "bad_origin", "message": "다른 사이트에서 온 요청은 받지 않습니다"}
BAD_CONTENT_TYPE: Final = {"error": "bad_content_type", "message": "요청 형식이 맞지 않습니다(JSON 요청만 받습니다)"}
LOGIN_DENIED: Final = {"error": "login_denied", "message": "초대되지 않았거나 쓸 수 없는 계정입니다. 관리자에게 문의하세요"}
# every password login failure (unknown email, no password, wrong password, disabled) gets exactly this
LOGIN_FAILED: Final = {"error": "login_failed", "message": "이메일 또는 비밀번호가 맞지 않습니다"}


@dataclass(frozen=True)
class UserRow:
    id: uuid.UUID
    email: str
    disabled: bool
    session_epoch: int
    google_sub: str | None = None
    is_admin: bool = False                 # read on every request: the admin screen follows a change at once


class LoginDenied(Exception):
    """로그인 거부. reason: email_unverified · not_invited · disabled · sub_mismatch · sub_taken(그 sub가 다른 사용자에게 고정)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UserDirectory(Protocol):
    def by_id(self, user_id: uuid.UUID) -> UserRow | None: ...
    def by_email(self, email: str) -> UserRow | None: ...
    def pin_google_sub(self, user_id: uuid.UUID, sub: str, email: str) -> UserRow | None: ...
    def password_login(self, email: str) -> tuple[UserRow, str | None] | None: ...
    def record_login(self, user_id: uuid.UUID) -> None: ...


class PgUsers:
    """`users` 행 조회(앱 롤은 users에 SELECT만). 캐시하지 않는다: 비활성·epoch 변경이 다음 요청부터 바로 먹는다."""

    _COLUMNS: Final = "id, email, disabled, session_epoch, google_sub, is_admin"

    def __init__(self, db: Db) -> None:
        self._db = db

    def _one(self, where: str, value: object) -> UserRow | None:
        with self._db.public_tx() as conn:
            row = conn.execute(f"SELECT {self._COLUMNS} FROM users WHERE {where} = %s", (value,)).fetchone()
        if row is None:
            return None
        return _row(row)

    def by_id(self, user_id: uuid.UUID) -> UserRow | None:
        return self._one("id", user_id)

    def by_email(self, email: str) -> UserRow | None:
        return self._one("email", email.strip().lower())

    def password_login(self, email: str) -> tuple[UserRow, str | None] | None:
        """비밀번호 로그인용: (행, `password_hash` 또는 None). 없는 이메일이면 None. 앱 롤은 users를 읽기만 한다."""
        with self._db.public_tx() as conn:
            row = conn.execute(f"SELECT {self._COLUMNS}, password_hash FROM users WHERE email = %s",
                               (email.strip().lower(),)).fetchone()
        if row is None:
            return None
        return _row(row[:6]), None if row[6] is None else str(row[6])

    def record_login(self, user_id: uuid.UUID) -> None:
        """로그인 성공 기록(`users.last_login_at`, 0004 `user_record_login()`). 그 사용자 자신의 `user_tx` 안에서 부르므로
        함수는 그 행만 바꿀 수 있다(앱 롤의 users 쓰기 권한은 늘지 않는다)."""
        with self._db.user_tx(user_id) as conn:
            conn.execute("SELECT user_record_login()")

    def pin_google_sub(self, user_id: uuid.UUID, sub: str, email: str) -> UserRow | None:
        """`google_sub`가 비었고 이메일이 조회 때(`email`) 그대로일 때만 sub를 넣고(경합해도 먼저 넣은 쪽이 남는다) 그 뒤의 행을
        돌려준다. 이미 있거나 그사이 관리자가 이메일을 바꿨으면 바꾸지 않는다(호출부가 비교한다). 없는 사용자면 None.
        같은 sub가 다른 사용자에게 있으면 LoginDenied("sub_taken")."""
        try:
            with self._db.public_tx() as conn:
                conn.execute("UPDATE users SET google_sub = %s WHERE id = %s AND email = %s AND google_sub IS NULL",
                             (sub, user_id, email))
                row = conn.execute(f"SELECT {self._COLUMNS} FROM users WHERE id = %s", (user_id,)).fetchone()
        except UniqueViolation as e:
            raise LoginDenied("sub_taken") from e
        return None if row is None else _row(row)


def _row(row: tuple[Any, ...]) -> UserRow:
    return UserRow(uuid.UUID(str(row[0])), str(row[1]), bool(row[2]), int(row[3]),
                   None if row[4] is None else str(row[4]), bool(row[5]))


def admit_google(users: UserDirectory, claims: GoogleClaims) -> UserRow:
    """검증된 Google ID 토큰 → 로그인할 사용자 행(초대·비활성·sub 고정 판정, 모듈 머리말). 아니면 LoginDenied."""
    if not claims.email_verified:
        raise LoginDenied("email_unverified")
    row = users.by_email(claims.email)
    if row is None:
        raise LoginDenied("not_invited")
    if row.disabled:
        raise LoginDenied("disabled")
    if row.google_sub is None:
        looked_up = row.email
        row = users.pin_google_sub(row.id, claims.sub, looked_up)
        if row is None or row.email != looked_up:
            raise LoginDenied("not_invited")          # deleted or renamed by an admin between the two reads
    if row.google_sub != claims.sub:
        raise LoginDenied("sub_mismatch")
    if row.disabled:
        raise LoginDenied("disabled")
    return row


# ---- 세션 --------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionClaim:
    user_id: uuid.UUID
    epoch: int
    iat: int


def start_session(session: dict[str, Any], user: UserRow, *, now: int | None = None) -> None:
    """로그인: 이전 내용을 모두 버리고 `{uid, epoch, iat}`만 넣는다."""
    session.clear()
    session.update({"uid": str(user.id), "epoch": user.session_epoch, "iat": int(time.time()) if now is None else now})


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def session_claim(session: Mapping[str, Any]) -> SessionClaim | None:
    """서명이 맞은 세션 내용 → 주장. 형식이 틀리면 None(로그인 안 됨과 같다)."""
    uid, epoch, iat = session.get("uid"), _int(session.get("epoch")), _int(session.get("iat"))
    if not isinstance(uid, str) or epoch is None or iat is None:
        return None
    try:
        return SessionClaim(uuid.UUID(uid), epoch, iat)
    except ValueError:
        return None


def session_valid(claim: SessionClaim, user: UserRow | None, *, now: int | None = None) -> bool:
    """사용자가 있고, 비활성이 아니고, epoch가 같고, 발급 뒤 14일이 지나지 않았으면 True."""
    t = int(time.time()) if now is None else now
    return (user is not None and user.id == claim.user_id and not user.disabled
            and user.session_epoch == claim.epoch and claim.iat - CLOCK_SKEW <= t < claim.iat + SESSION_MAX_AGE)


# ---- CSRF -------------------------------------------------------------------------

def _origin_of(value: str) -> str:
    return value.strip().lower().rstrip("/")


def csrf_error(method: str, headers: Mapping[str, str], public_origin: str) -> tuple[int, dict[str, str]] | None:
    """안전하지 않은 메서드의 교차 출처·비JSON 요청이면 403 응답, 아니면 None. headers 키는 소문자."""
    if method.upper() not in UNSAFE_METHODS:
        return None
    origin = headers.get("origin")
    if origin is not None:
        if _origin_of(origin) != public_origin:
            return 403, dict(BAD_ORIGIN)
    elif (headers.get("sec-fetch-site") or "").strip().lower() != "same-origin":
        return 403, dict(BAD_ORIGIN)
    media = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != JSON_TYPE:
        return 403, dict(BAD_CONTENT_TYPE)
    return None


# ---- 로그인 실패 화면 문구 ------------------------------------------------------------

# reason -> (status, message). `{email}`은 Google이 알려 준 이메일(화면에서 HTML 이스케이프).
LOGIN_REFUSALS: Final[dict[str, tuple[int, str]]] = {
    "state": (400, "로그인 요청이 만료되었거나 맞지 않습니다. 처음부터 다시 로그인하세요."),
    "cancelled": (400, "Google 로그인이 취소되었습니다. 다시 로그인하려면 아래 버튼을 누르세요."),
    "token_rejected": (400, "Google 로그인을 마치지 못했습니다. 처음부터 다시 로그인하세요."),
    "idp_unavailable": (502, "Google에 연결하지 못했습니다. 잠시 뒤 다시 로그인하세요."),
    "bad_id_token": (400, "Google 로그인 정보를 확인하지 못했습니다. 처음부터 다시 로그인하세요."),
    "email_unverified": (403, "Google 계정의 이메일 인증이 끝나지 않았습니다({email}). 이메일 인증을 마친 뒤 다시 로그인하세요."),
    "not_invited": (403, "초대되지 않은 계정입니다({email}). 이 이메일로 초대해 달라고 관리자에게 요청하세요."),
    "disabled": (403, "쓸 수 없게 된 계정입니다({email}). 관리자에게 문의하세요."),
    "sub_mismatch": (403, "이 이메일({email})은 다른 Google 계정에 연결되어 있습니다. 처음 로그인한 Google 계정으로 로그인하거나 "
                          "관리자에게 문의하세요."),
    "sub_taken": (403, "이 Google 계정은 다른 이메일의 사용자에게 연결되어 있습니다({email}). 관리자에게 문의하세요."),
}
