"""Google OpenID Connect(서버 설계 §4.2, T12): Authorization Code + PKCE(S256) + nonce, ID 토큰을 Google JWKS로 검증한다.

흐름
1. `GET /auth/google`: `new_pending()`(state·nonce·code_verifier, 10분)을 세션에 하나만 두고 Google 인가 URL로 302.
   리디렉션 URI는 설정값 하나뿐이다(`YTC_PUBLIC_URL` + `/auth/google/callback`). 요청의 next·redirect_uri 같은 매개변수는 읽지 않는다.
2. `GET /auth/google/callback`: 세션의 대기 로그인을 **먼저 꺼내 지우고**(한 번만 쓴다) `check_callback`(state 비교·만료·code)
   → `GoogleOidc.login`: 토큰 엔드포인트에 code + code_verifier + client_secret → `id_token` 서명(RS256, JWKS의 kid)과
   iss(`https://accounts.google.com`·`accounts.google.com`)·aud(=클라이언트 id 하나)·azp·exp·iat·nonce·sub를 확인 → `GoogleClaims`.
   허용(초대·비활성·sub 고정)은 `auth.admit_google`이 정한다.

라이브러리: 서명·클레임은 joserfc(authlib의 JOSE 구현) + `authlib.oidc.core.CodeIDToken`(nonce·azp·at_hash 규칙), PKCE 챌린지는
authlib, HTTP는 고정한 httpx를 직접 쓴다. authlib의 httpx 통합(`AsyncOAuth2Client`)은 설치된 패키지에 따라 httpx2/httpx를 골라
(같은 버전이라도 환경마다 다른 HTTP 클라이언트) 쓰지 않았다. 테스트는 `transport`에 가짜 IdP를 넣는다(실제 Google 호출 없음).

로그: 코드·state·nonce·verifier·토큰·client_secret은 어디에도 남기지 않는다. 실패는 `OidcError.reason`(짧은 코드)만 로그로.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet

from server.config import GOOGLE_CALLBACK_PATH, GoogleSettings

AUTHORIZE_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
JWKS_URL: Final = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS: Final = ("https://accounts.google.com", "accounts.google.com")   # Google issues both forms
SCOPE: Final = "openid email profile"
ALGORITHMS: Final = ("RS256",)
CALLBACK_PATH: Final = GOOGLE_CALLBACK_PATH
PENDING_KEY: Final = "oidc"          # session key of the one pending login
PENDING_TTL: Final = 600             # seconds between /auth/google and the callback
LEEWAY: Final = 60                   # clock skew allowed on exp/iat
JWKS_TTL: Final = 3600
JWKS_REFRESH_MIN: Final = 60         # unknown kid refetches the key set at most this often
HTTP_TIMEOUT: Final = 10.0
TOKEN_BYTES: Final = 32              # state, nonce: 43 url-safe chars
VERIFIER_BYTES: Final = 48           # code_verifier: 64 chars (RFC 7636: 43..128)


log = logging.getLogger("ytc.auth")


class OidcError(Exception):
    """로그인 실패. reason은 로그·화면 문구를 고르는 짧은 코드(비밀 값을 담지 않는다).

    state(대기 로그인 없음·불일치·만료·code 없음) · cancelled(Google이 error로 돌려보냄) · token_rejected(토큰 엔드포인트 4xx)
    · idp_unavailable(연결 실패·5xx·JSON 아님·JWKS 오류) · bad_id_token(서명·클레임·형식)
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PendingLogin:
    state: str
    nonce: str
    verifier: str
    expires_at: int

    def to_session(self) -> dict[str, Any]:
        return {"state": self.state, "nonce": self.nonce, "verifier": self.verifier, "exp": self.expires_at}

    @classmethod
    def from_session(cls, value: object) -> PendingLogin | None:
        if not isinstance(value, Mapping):
            return None
        state, nonce, verifier, exp = value.get("state"), value.get("nonce"), value.get("verifier"), value.get("exp")
        if not (isinstance(state, str) and isinstance(nonce, str) and isinstance(verifier, str)
                and isinstance(exp, int) and not isinstance(exp, bool)):
            return None
        return cls(state, nonce, verifier, exp)


def new_pending(now: int) -> PendingLogin:
    return PendingLogin(secrets.token_urlsafe(TOKEN_BYTES), secrets.token_urlsafe(TOKEN_BYTES),
                        secrets.token_urlsafe(VERIFIER_BYTES), now + PENDING_TTL)


def check_callback(pending: PendingLogin, query: Mapping[str, str], now: int) -> str:
    """콜백 쿼리 → code. state 불일치(상수 시간 비교), Google의 error, 만료, code 없음이면 OidcError."""
    state = query.get("state") or ""
    if not state or not hmac.compare_digest(state.encode("utf-8"), pending.state.encode("utf-8")):
        raise OidcError("state")
    if query.get("error"):
        raise OidcError("cancelled")
    if now >= pending.expires_at:
        raise OidcError("state")
    code = query.get("code") or ""
    if not code:
        raise OidcError("state")
    return code


@dataclass(frozen=True)
class GoogleClaims:
    sub: str
    email: str
    email_verified: bool
    name: str | None


def _verified(value: object) -> bool:
    """Google은 불리언 true를 준다(예전 응답은 문자열 "true"). 그 밖은 모두 미인증."""
    return value is True or value == "true"


class GoogleOidc:
    def __init__(self, settings: GoogleSettings, *, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._transport = transport
        self._clock = clock
        self._keys: KeySet | None = None
        self._keys_at = 0.0

    def now(self) -> int:
        """대기 로그인 만료·클레임 검사가 같이 쓰는 시계(테스트는 clock을 넣는다)."""
        return int(self._clock())

    # ---- 1. 인가 요청 ------------------------------------------------------------------

    def authorization_url(self, pending: PendingLogin) -> str:
        params = {"response_type": "code", "client_id": self.settings.client_id,
                  "redirect_uri": self.settings.redirect_uri, "scope": SCOPE, "state": pending.state,
                  "nonce": pending.nonce, "code_challenge": create_s256_code_challenge(pending.verifier),
                  "code_challenge_method": "S256", "prompt": "select_account"}
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    # ---- 2. 콜백 -----------------------------------------------------------------------

    async def login(self, code: str, pending: PendingLogin) -> GoogleClaims:
        token = await self._exchange(code, pending.verifier)
        id_token, access = token.get("id_token"), token.get("access_token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcError("bad_id_token")
        return await self.verify_id_token(id_token, pending.nonce, access if isinstance(access, str) else None)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=HTTP_TIMEOUT, follow_redirects=False)

    async def _exchange(self, code: str, verifier: str) -> dict[str, Any]:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.settings.redirect_uri,
                "client_id": self.settings.client_id, "client_secret": self.settings.client_secret,
                "code_verifier": verifier}
        try:
            async with self._client() as client:
                resp = await client.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise OidcError("idp_unavailable") from e
        if 400 <= resp.status_code < 500:
            raise OidcError("token_rejected")          # invalid_grant (code used/expired), bad client ...
        if resp.status_code != 200:
            raise OidcError("idp_unavailable")
        return _json_object(resp)

    async def _key_set(self, *, force: bool = False) -> KeySet:
        now = self._clock()
        fresh = self._keys is not None and now - self._keys_at < JWKS_TTL
        if self._keys is not None and (fresh and not force or force and now - self._keys_at < JWKS_REFRESH_MIN):
            return self._keys
        try:
            keys = await self._fetch_keys()
        except OidcError:
            if self._keys is not None and not force:
                # TTL refresh failed: keep verifying with the keys we have (Google publishes new keys well before use).
                # A forced refresh (unknown kid) has nothing to fall back to and fails closed.
                log.warning("google jwks refresh failed; using the cached key set")
                self._keys_at = now - JWKS_TTL + JWKS_REFRESH_MIN      # retry the refresh after JWKS_REFRESH_MIN, not per login
                return self._keys
            raise
        self._keys, self._keys_at = keys, now
        return keys

    async def _fetch_keys(self) -> KeySet:
        try:
            async with self._client() as client:
                resp = await client.get(JWKS_URL, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise OidcError("idp_unavailable") from e
        if resp.status_code != 200:
            raise OidcError("idp_unavailable")
        try:
            return KeySet.import_key_set(_json_object(resp))  # type: ignore[arg-type]
        except (JoseError, ValueError, TypeError, KeyError) as e:
            raise OidcError("idp_unavailable") from e

    async def verify_id_token(self, id_token: str, nonce: str, access_token: str | None = None) -> GoogleClaims:
        keys = await self._key_set()
        try:
            try:
                token = jwt.decode(id_token, keys, algorithms=list(ALGORITHMS))
            except InvalidKeyIdError:
                token = jwt.decode(id_token, await self._key_set(force=True), algorithms=list(ALGORITHMS))
            claims = self._validate(token.claims, dict(token.header), nonce, access_token)
        except (JoseError, ValueError, TypeError) as e:
            raise OidcError("bad_id_token") from e
        return claims

    def _validate(self, raw: dict[str, Any], header: dict[str, Any], nonce: str,
                  access_token: str | None) -> GoogleClaims:
        client_id = self.settings.client_id
        options: dict[str, Any] = {"iss": {"essential": True, "values": list(ISSUERS)},
                                   "aud": {"essential": True, "value": client_id},
                                   "sub": {"essential": True}, "exp": {"essential": True},
                                   "iat": {"essential": True}, "nonce": {"essential": True, "value": nonce}}
        params: dict[str, Any] = {"nonce": nonce, "client_id": client_id}
        if access_token:
            params["access_token"] = access_token
        CodeIDToken(raw, header, options, params).validate(now=int(self._clock()), leeway=LEEWAY)
        # explicit on top of the library: one audience, exactly us; nonce in constant time; usable sub/email
        aud = raw.get("aud")
        if aud != client_id and aud != [client_id]:
            raise ValueError("aud")
        claim_nonce = raw.get("nonce")
        if not isinstance(claim_nonce, str) or not hmac.compare_digest(claim_nonce.encode(), nonce.encode()):
            raise ValueError("nonce")
        sub, email, name = raw.get("sub"), raw.get("email"), raw.get("name")
        if not isinstance(sub, str) or not sub or not isinstance(email, str) or "@" not in email:
            raise ValueError("sub/email")
        return GoogleClaims(sub=sub, email=email.strip().lower(), email_verified=_verified(raw.get("email_verified")),
                            name=name if isinstance(name, str) else None)


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError as e:
        raise OidcError("idp_unavailable") from e
    if not isinstance(data, dict):
        raise OidcError("idp_unavailable")
    return data
