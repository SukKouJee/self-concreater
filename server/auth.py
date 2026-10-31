"""서버판 인증: 세션 쿠키 내용·검사, CSRF, 사용자 조회(서버 설계 §4.2). Google OIDC는 T12가 더한다.

- 세션은 Starlette `SessionMiddleware`(itsdangerous 서명 쿠키 `ytc_session`, 14일, `HttpOnly; SameSite=Lax`, https면 `Secure`).
  내용은 `{uid, epoch, iat}`뿐이다. 서명은 위조를 막고, **요청마다** `users`를 다시 읽어 `disabled`·`session_epoch`를 확인한다
  (`session_valid`). 비활성·epoch 증가·만료·없는 사용자면 세션을 비우고 401 `login_required`.
- CSRF(`csrf_error`): POST·PUT·DELETE·PATCH는 ① `Origin`이 `YTC_PUBLIC_URL`의 출처와 같아야 하고(없으면 `Sec-Fetch-Site: same-origin`)
  ② `Content-Type: application/json`이어야 한다. 단순 폼 교차 전송이 둘 다에 걸린다. SameSite=Lax가 한 겹 더.
- 사용자 조회(`PgUsers`)는 `Db.public_tx()`(app.user_id 없이, `users`는 RLS 없음)로 한다. 로그인 전이라 사용자 id가 없다.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from server.db import Db

SESSION_COOKIE: Final = "ytc_session"
SESSION_MAX_AGE: Final = 14 * 24 * 60 * 60
CLOCK_SKEW: Final = 60                     # iat may be this far in the future (same process signs; leave slack)
UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "DELETE", "PATCH"})
JSON_TYPE: Final = "application/json"

LOGIN_REQUIRED: Final = {"error": "login_required", "message": "로그인이 필요합니다"}
BAD_ORIGIN: Final = {"error": "bad_origin", "message": "다른 사이트에서 온 요청은 받지 않습니다"}
BAD_CONTENT_TYPE: Final = {"error": "bad_content_type", "message": "요청 형식이 맞지 않습니다(JSON 요청만 받습니다)"}
LOGIN_DENIED: Final = {"error": "login_denied", "message": "초대되지 않았거나 쓸 수 없는 계정입니다. 관리자에게 문의하세요"}


@dataclass(frozen=True)
class UserRow:
    id: uuid.UUID
    email: str
    disabled: bool
    session_epoch: int


class UserDirectory(Protocol):
    def by_id(self, user_id: uuid.UUID) -> UserRow | None: ...
    def by_email(self, email: str) -> UserRow | None: ...


class PgUsers:
    """`users` 행 조회(앱 롤은 users에 SELECT만). 캐시하지 않는다: 비활성·epoch 변경이 다음 요청부터 바로 먹는다."""

    _COLUMNS: Final = "id, email, disabled, session_epoch"

    def __init__(self, db: Db) -> None:
        self._db = db

    def _one(self, where: str, value: object) -> UserRow | None:
        with self._db.public_tx() as conn:
            row = conn.execute(f"SELECT {self._COLUMNS} FROM users WHERE {where} = %s", (value,)).fetchone()
        if row is None:
            return None
        return UserRow(uuid.UUID(str(row[0])), str(row[1]), bool(row[2]), int(row[3]))

    def by_id(self, user_id: uuid.UUID) -> UserRow | None:
        return self._one("id", user_id)

    def by_email(self, email: str) -> UserRow | None:
        return self._one("email", email.strip().lower())


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
