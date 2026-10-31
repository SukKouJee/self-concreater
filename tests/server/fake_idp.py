"""가짜 Google IdP(T12 테스트 전용): RSA 키로 ID 토큰을 서명하고, httpx `MockTransport`로 JWKS·토큰 엔드포인트를 흉내 낸다.

실제 Google에는 한 번도 붙지 않는다(전송 계층이 이 처리기뿐이라 다른 주소는 404).
흐름: `GET /auth/google`(앱) → 302 Location의 쿼리(state·nonce·code_challenge …)를 `authorize()`가 읽어 코드를 하나 만든다 →
테스트가 `/auth/google/callback?code=…&state=…`를 부른다 → 앱이 토큰 엔드포인트에 코드를 내면 서명한 ID 토큰을 준다.
"""
from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from server import oidc

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-value"


def new_key(kid: str = "k1") -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": kid, "alg": "RS256", "use": "sig"})


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


@dataclass
class Grant:
    """`authorize()`가 만든 코드 하나: 앱이 보낸 인가 요청 값과 내줄 ID 토큰 내용."""

    code: str
    state: str
    nonce: str
    challenge: str
    redirect_uri: str
    query: dict[str, str]
    claims: dict[str, Any]
    sign_with: RSAKey | None = None          # None = the published key
    header: dict[str, Any] | None = None
    raw_id_token: str | None = None          # send this instead of signing


@dataclass
class FakeIdp:
    key: RSAKey = field(default_factory=new_key)
    published: list[RSAKey] = field(default_factory=list)
    grants: dict[str, Grant] = field(default_factory=dict)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    jwks_hits: int = 0
    token_status: int = 200
    token_error: str | None = None            # e.g. "network"
    omit_id_token: bool = False
    _n: int = 0

    def __post_init__(self) -> None:
        if not self.published:
            self.published = [self.key]

    # ---- transport -----------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == oidc.JWKS_URL:
            self.jwks_hits += 1
            return httpx.Response(200, json=KeySet(list(self.published)).as_dict(private=False))
        if request.method == "POST" and url == oidc.TOKEN_URL:
            if self.token_error == "network":
                raise httpx.ConnectError("no route", request=request)
            form = {k: v[0] for k, v in parse_qs(request.content.decode("ascii")).items()}
            self.token_requests.append(form)
            return self._token(form)
        return httpx.Response(404, json={"error": "not_found"})

    def _token(self, form: dict[str, str]) -> httpx.Response:
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_grant", "error_description": "Bad Request"})
        grant = self.grants.pop(form.get("code", ""), None)
        if (grant is None or form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET
                or form.get("grant_type") != "authorization_code" or form.get("redirect_uri") != grant.redirect_uri
                or s256(form.get("code_verifier", "")) != grant.challenge):
            return httpx.Response(400, json={"error": "invalid_grant"})
        body: dict[str, Any] = {"access_token": f"ya29.access-{grant.code}", "token_type": "Bearer", "expires_in": 3599,
                                "scope": "openid email profile"}
        if not self.omit_id_token:
            body["id_token"] = grant.raw_id_token or self.sign(grant.claims, grant.sign_with, grant.header)
        return httpx.Response(200, json=body)

    # ---- tokens --------------------------------------------------------------------

    def sign(self, claims: dict[str, Any], key: RSAKey | None = None, header: dict[str, Any] | None = None) -> str:
        k = key or self.key
        return jwt.encode(header or {"alg": "RS256", "kid": k.kid, "typ": "JWT"}, claims, k)

    def claims(self, nonce_sent: str, /, **over: Any) -> dict[str, Any]:
        now = int(time.time())
        base: dict[str, Any] = {"iss": "https://accounts.google.com", "azp": CLIENT_ID, "aud": CLIENT_ID,
                                "sub": "1100000000001", "email": "a@example.com", "email_verified": True,
                                "name": "에이", "iat": now, "exp": now + 3600, "nonce": nonce_sent}
        base.update(over)
        return {k: v for k, v in base.items() if v is not _DROP}

    # ---- authorization -------------------------------------------------------------

    def authorize(self, location: str, *, claims: dict[str, Any] | None = None, **grant_over: Any) -> Grant:
        """앱이 보낸 Google 인가 URL(302 Location) → 코드. claims는 기본 ID 토큰 내용 위에 덮는다(`DROP`이면 뺀다)."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == oidc.AUTHORIZE_URL, location
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self._n += 1
        code = f"4/0code-{self._n}-secretish"
        grant = Grant(code=code, state=query["state"], nonce=query["nonce"], challenge=query["code_challenge"],
                      redirect_uri=query["redirect_uri"], query=query,
                      claims=self.claims(query["nonce"], **(claims or {})), **grant_over)
        self.grants[code] = grant
        return grant


class _Drop:
    def __repr__(self) -> str:
        return "DROP"


_DROP: Any = _Drop()
DROP: Any = _DROP


def callback(grant: Grant, **over: str | None) -> str:
    params = {"code": grant.code, "state": grant.state, "scope": "openid email profile"}
    params.update(over)
    return oidc.CALLBACK_PATH + "?" + urlencode({k: v for k, v in params.items() if v is not None})
