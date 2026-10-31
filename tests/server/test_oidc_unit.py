"""Google OIDC 로그인 단위 테스트(서버 설계 §4.2, T12): 가짜 IdP(`fake_idp`)로 서명한 ID 토큰, PG·MinIO·실제 Google 없이.

흐름 전체(`/auth/google` → 가짜 Google → `/auth/google/callback`)를 ASGI TestClient로 돌린다. 사용자 목록은 가짜(`FakeUsers`),
실제 PG의 sub 고정은 test_auth_pg.py.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("starlette")

from joserfc import jwt  # noqa: E402
from joserfc.jwk import OctKey  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from scripts.ui import server as ui  # noqa: E402
from server import asgi, oidc  # noqa: E402
from server.config import AuthSettings  # noqa: E402
from server.jobs_registry import Registry  # noqa: E402
from tests.server import fake_idp  # noqa: E402
from tests.server.fake_idp import CLIENT_ID, CLIENT_SECRET, DROP, FakeIdp, callback  # noqa: E402
from tests.server.test_asgi_unit import FakeBackend, FakeUsers  # noqa: E402

ORIGIN = "https://ytc.example.com"
SECRET = "g" * 40
SUB = "1100000000001"


def _settings(**over: str) -> AuthSettings:
    env = {"YTC_PUBLIC_URL": ORIGIN, "YTC_SESSION_SECRET": SECRET, "YTC_GOOGLE_CLIENT_ID": CLIENT_ID,
           "YTC_GOOGLE_CLIENT_SECRET": CLIENT_SECRET}
    env.update(over)
    return AuthSettings.from_env(env)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def users() -> FakeUsers:
    return FakeUsers()


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(users: FakeUsers, idp: FakeIdp, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(ui.DISABLE_ENV, raising=False)
    settings = _settings()
    assert settings.google is not None
    google = oidc.GoogleOidc(settings.google, transport=idp.transport(), clock=clock)
    app = asgi.create_app(settings, FakeBackend(), users=users, registry=Registry(), google=google)  # type: ignore[arg-type]
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c


def _start(client: TestClient, path: str = "/auth/google") -> str:
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302, r.text
    return r.headers["location"]


def _login(client: TestClient, idp: FakeIdp, *, claims: dict[str, Any] | None = None, **cb: str | None) -> Any:
    grant = idp.authorize(_start(client), claims=claims)
    return client.get(callback(grant, **cb), follow_redirects=False)


def _logged_in_as(client: TestClient) -> str | None:
    r = client.get("/api/ping")
    return r.json()["user"] if r.status_code == 200 else None


def _refused(r: Any, status: int, text: str) -> None:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert text in r.text and 'href="/auth/login"' in r.text


# ---- happy path ----------------------------------------------------------------------

def test_google_login_happy_path_pins_sub_and_starts_a_session(client, idp, users):
    row = users.add("a@example.com")
    location = _start(client)
    q = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == f"{ORIGIN}/auth/google/callback"
    assert q["scope"] == "openid email profile" and q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32 and len(q["code_challenge"]) == 43
    grant = idp.authorize(location)
    r = client.get(callback(grant), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    for flag in ("ytc_session=", "httponly", "secure", "samesite=lax"):
        assert flag in cookie
    assert _logged_in_as(client) == "a@example.com"
    assert users.rows[row.id].google_sub == SUB
    sent = idp.token_requests[-1]
    assert sent["code"] == grant.code and sent["redirect_uri"] == f"{ORIGIN}/auth/google/callback"
    assert fake_idp.s256(sent["code_verifier"]) == q["code_challenge"] and 43 <= len(sent["code_verifier"]) <= 128


def test_second_login_with_the_pinned_sub_succeeds(client, idp, users):
    users.add("a@example.com", google_sub=SUB)
    assert _login(client, idp).status_code == 302
    assert _logged_in_as(client) == "a@example.com"


def test_email_is_matched_case_insensitively(client, idp, users):
    users.add("a@example.com")
    assert _login(client, idp, claims={"email": "A@Example.COM"}).status_code == 302
    assert _logged_in_as(client) == "a@example.com"


def test_old_google_issuer_form_is_accepted(client, idp, users):
    users.add("a@example.com")
    assert _login(client, idp, claims={"iss": "accounts.google.com"}).status_code == 302


def test_key_rotation_refetches_jwks_once(client, idp, users, clock):
    users.add("a@example.com")
    assert _login(client, idp).status_code == 302
    assert idp.jwks_hits == 1
    rotated = fake_idp.new_key("k2")
    idp.key = rotated
    idp.published = [rotated]
    client.post("/auth/logout", json={})
    assert _login(client, idp).status_code == 400          # unknown kid right after a fetch: no refetch storm
    assert idp.jwks_hits == 1
    clock.offset = oidc.JWKS_REFRESH_MIN + 1
    assert _login(client, idp).status_code == 302          # unknown kid -> refetch -> verify
    assert idp.jwks_hits == 2
    client.post("/auth/logout", json={})
    assert _login(client, idp).status_code == 302          # cached
    assert idp.jwks_hits == 2


def test_jwks_is_refetched_after_its_ttl(client, idp, users, clock):
    users.add("a@example.com")
    assert _login(client, idp).status_code == 302
    client.post("/auth/logout", json={})
    clock.offset = oidc.JWKS_TTL + 1
    assert _login(client, idp, claims={"exp": int(time.time()) + 3 * oidc.JWKS_TTL}).status_code == 302
    assert idp.jwks_hits == 2


# ---- ID token verification -----------------------------------------------------------

def test_bad_signature_is_refused(client, idp, users):
    users.add("a@example.com")
    grant = idp.authorize(_start(client), sign_with=fake_idp.new_key("k1"))   # same kid, other key
    r = client.get(callback(grant), follow_redirects=False)
    _refused(r, 400, "Google 로그인 정보를 확인하지 못했습니다")
    assert _logged_in_as(client) is None


@pytest.mark.parametrize("claims", [
    {"aud": "someone-else.apps.googleusercontent.com", "azp": "someone-else.apps.googleusercontent.com"},
    {"aud": "someone-else.apps.googleusercontent.com"},
    {"aud": [CLIENT_ID, "someone-else"], "azp": CLIENT_ID},
    {"aud": DROP},
    {"iss": "https://evil.example.com"},
    {"iss": DROP},
    {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
    {"exp": DROP},
    {"iat": int(time.time()) + 3600},
    {"iat": DROP},
    {"nonce": "not-the-nonce-we-sent-xxxxxxxxxxxxxxx"},
    {"nonce": DROP},
    {"sub": DROP},
    {"sub": ""},
    {"email": DROP},
    {"azp": "someone-else"},
], ids=["wrong-aud", "wrong-aud-no-azp", "multi-aud", "no-aud", "wrong-iss", "no-iss", "expired", "no-exp",
        "iat-future", "no-iat", "nonce-mismatch", "no-nonce", "no-sub", "empty-sub", "no-email", "wrong-azp"])
def test_bad_id_token_claims_are_refused(client, idp, users, claims):
    row = users.add("a@example.com")
    r = _login(client, idp, claims=claims)
    _refused(r, 400, "Google 로그인 정보를 확인하지 못했습니다")
    assert _logged_in_as(client) is None and users.rows[row.id].google_sub is None


def test_alg_none_and_hmac_tokens_are_refused(client, idp, users):
    users.add("a@example.com")
    for raw in ("eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.",
                jwt.encode({"alg": "HS256", "kid": "k1"}, {"sub": SUB}, OctKey.import_key("x" * 32)),
                "not-a-jwt"):
        grant = idp.authorize(_start(client), raw_id_token=raw)
        _refused(client.get(callback(grant), follow_redirects=False), 400, "Google 로그인 정보를 확인하지 못했습니다")
    assert _logged_in_as(client) is None


def test_missing_id_token_is_refused(client, idp, users):
    users.add("a@example.com")
    idp.omit_id_token = True
    _refused(_login(client, idp), 400, "Google 로그인 정보를 확인하지 못했습니다")


# ---- state · pending login -------------------------------------------------------------

def test_state_mismatch_is_refused(client, idp, users):
    users.add("a@example.com")
    _refused(_login(client, idp, state="x" * 43), 400, "로그인 요청이 만료되었거나 맞지 않습니다")
    assert _logged_in_as(client) is None
    assert idp.token_requests == []            # the code is never exchanged


def test_state_missing_is_refused(client, idp, users):
    users.add("a@example.com")
    _refused(_login(client, idp, state=None), 400, "로그인 요청이 만료되었거나 맞지 않습니다")
    assert idp.token_requests == []


def test_callback_without_a_started_login_is_refused(client, idp, users, monkeypatch):
    users.add("a@example.com")
    location = _start(client)
    client.cookies.clear()                       # another browser: the pending login is not there
    grant = idp.authorize(location)
    _refused(client.get(callback(grant), follow_redirects=False), 400, "로그인 요청이 만료되었거나 맞지 않습니다")
    assert idp.token_requests == []


def test_callback_cannot_be_replayed(client, idp, users):
    users.add("a@example.com")
    grant = idp.authorize(_start(client))
    url = callback(grant)
    assert client.get(url, follow_redirects=False).status_code == 302
    client.post("/auth/logout", json={})
    _refused(client.get(url, follow_redirects=False), 400, "로그인 요청이 만료되었거나 맞지 않습니다")


def test_expired_pending_login_is_refused(client, idp, users, clock):
    users.add("a@example.com")
    grant = idp.authorize(_start(client))
    clock.offset = oidc.PENDING_TTL + 1
    _refused(client.get(callback(grant), follow_redirects=False), 400, "로그인 요청이 만료되었거나 맞지 않습니다")
    assert idp.token_requests == []


def test_pending_login_just_inside_its_ttl_is_accepted(client, idp, users, clock):
    users.add("a@example.com")
    grant = idp.authorize(_start(client))
    clock.offset = oidc.PENDING_TTL - 5
    assert client.get(callback(grant), follow_redirects=False).status_code == 302


# ---- JWKS failures · at_hash -----------------------------------------------------------

@pytest.mark.parametrize("status,body", [
    (503, None), (500, None), (200, b"<html>not json</html>"), (200, b"[1, 2]"),
    (200, b'{"keys": [{"kty": "RSA", "kid": "k1", "n": "@@", "e": "AQAB"}]}'), (200, b'{"keys": "nope"}'),
], ids=["503", "500", "non-json", "json-list", "malformed-key", "keys-not-list"])
def test_jwks_failures_are_idp_unavailable(client, idp, users, status, body):
    row = users.add("a@example.com")
    idp.jwks_status, idp.jwks_body = status, body
    _refused(_login(client, idp), 502, "Google에 연결하지 못했습니다")
    assert _logged_in_as(client) is None and users.rows[row.id].google_sub is None


def test_failed_refresh_after_ttl_falls_back_to_the_cached_keys(client, idp, users, clock):
    users.add("a@example.com")
    assert _login(client, idp).status_code == 302
    client.post("/auth/logout", json={})
    clock.offset = oidc.JWKS_TTL + 1
    idp.jwks_status = 503
    assert _login(client, idp, claims={"exp": int(time.time()) + 3 * oidc.JWKS_TTL}).status_code == 302
    assert idp.jwks_hits == 2                                  # it did try, then used the cache
    client.post("/auth/logout", json={})
    assert _login(client, idp, claims={"exp": int(time.time()) + 3 * oidc.JWKS_TTL}).status_code == 302
    assert idp.jwks_hits == 2                                  # no refetch per login while Google is down
    clock.offset += oidc.JWKS_REFRESH_MIN + 1
    idp.jwks_status = 200
    client.post("/auth/logout", json={})
    assert _login(client, idp, claims={"exp": int(time.time()) + 3 * oidc.JWKS_TTL}).status_code == 302
    assert idp.jwks_hits == 3                                  # retried after the back-off


def test_unknown_kid_with_a_failing_refresh_fails_closed(client, idp, users, clock):
    users.add("a@example.com")
    assert _login(client, idp).status_code == 302
    client.post("/auth/logout", json={})
    rotated = fake_idp.new_key("k2")
    idp.key, idp.published = rotated, [rotated]
    clock.offset = oidc.JWKS_TTL + 1
    idp.jwks_status = 503
    _refused(_login(client, idp, claims={"exp": int(time.time()) + 3 * oidc.JWKS_TTL}), 502,
             "Google에 연결하지 못했습니다")
    assert _logged_in_as(client) is None


def test_at_hash_mismatch_is_refused(client, idp, users):
    users.add("a@example.com")
    _refused(_login(client, idp, claims={"at_hash": "AAAAAAAAAAAAAAAAAAAAAA"}), 400,
             "Google 로그인 정보를 확인하지 못했습니다")


def test_matching_at_hash_is_accepted(client, idp, users):
    import base64
    import hashlib

    users.add("a@example.com")
    grant = idp.authorize(_start(client))
    digest = hashlib.sha256(f"ya29.access-{grant.code}".encode("ascii")).digest()[:16]
    grant.claims["at_hash"] = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert client.get(callback(grant), follow_redirects=False).status_code == 302


def test_only_the_latest_started_login_is_kept(client, idp, users):
    users.add("a@example.com")
    first = idp.authorize(_start(client))
    second = idp.authorize(_start(client))
    _refused(client.get(callback(first), follow_redirects=False), 400, "로그인 요청이 만료되었거나 맞지 않습니다")
    second = idp.authorize(_start(client))
    assert client.get(callback(second), follow_redirects=False).status_code == 302


def test_user_cancel_at_google_shows_a_page(client, idp, users):
    users.add("a@example.com")
    location = _start(client)
    state = parse_qs(urlsplit(location).query)["state"][0]
    r = client.get(f"/auth/google/callback?error=access_denied&state={state}", follow_redirects=False)
    _refused(r, 400, "Google 로그인이 취소되었습니다")
    assert idp.token_requests == []


def test_missing_code_is_refused(client, idp, users):
    users.add("a@example.com")
    _refused(_login(client, idp, code=None), 400, "로그인 요청이 만료되었거나 맞지 않습니다")


def test_token_endpoint_rejection_and_outage(client, idp, users):
    users.add("a@example.com")
    idp.token_status = 400
    _refused(_login(client, idp), 400, "Google 로그인을 마치지 못했습니다")
    idp.token_status = 503
    _refused(_login(client, idp), 502, "Google에 연결하지 못했습니다")
    idp.token_status = 200
    idp.token_error = "network"
    _refused(_login(client, idp), 502, "Google에 연결하지 못했습니다")
    assert _logged_in_as(client) is None


# ---- allowlist · disabled · sub pinning ------------------------------------------------

@pytest.mark.parametrize("verified", [False, "false", DROP, "true-ish", 1])
def test_unverified_email_is_refused(client, idp, users, verified):
    row = users.add("a@example.com")
    _refused(_login(client, idp, claims={"email_verified": verified}), 403, "이메일 인증")
    assert _logged_in_as(client) is None and users.rows[row.id].google_sub is None


def test_string_true_email_verified_is_accepted(client, idp, users):
    users.add("a@example.com")
    assert _login(client, idp, claims={"email_verified": "true"}).status_code == 302


def test_uninvited_email_is_refused_and_no_account_is_created(client, idp, users):
    users.add("a@example.com")
    r = _login(client, idp, claims={"email": "stranger@example.com"})
    _refused(r, 403, "초대되지 않은 계정")
    assert "stranger@example.com" in r.text
    assert [u.email for u in users.rows.values()] == ["a@example.com"]
    assert _logged_in_as(client) is None


def test_disabled_user_is_refused(client, idp, users):
    row = users.add("a@example.com", disabled=True)
    _refused(_login(client, idp), 403, "쓸 수 없게 된 계정")
    assert users.rows[row.id].google_sub is None and _logged_in_as(client) is None


def test_sub_mismatch_is_refused_and_the_pin_is_kept(client, idp, users):
    row = users.add("a@example.com", google_sub="1100000000999")
    _refused(_login(client, idp), 403, "다른 Google 계정에 연결")
    assert users.rows[row.id].google_sub == "1100000000999" and _logged_in_as(client) is None


def test_sub_already_pinned_to_another_user_is_refused(client, idp, users):
    users.add("old@example.com", google_sub=SUB)
    row = users.add("a@example.com")
    _refused(_login(client, idp), 403, "다른 이메일의 사용자에게 연결")
    assert users.rows[row.id].google_sub is None and _logged_in_as(client) is None


# ---- session -------------------------------------------------------------------------

def test_login_replaces_an_existing_session(client, idp, users):
    users.add("b@example.com", google_sub="1100000000002")
    users.add("a@example.com")
    assert _login(client, idp, claims={"email": "b@example.com", "sub": "1100000000002"}).status_code == 302
    assert _logged_in_as(client) == "b@example.com"
    assert _login(client, idp).status_code == 302
    assert _logged_in_as(client) == "a@example.com"


def test_failed_login_keeps_no_pending_state(client, idp, users):
    users.add("a@example.com")
    grant = idp.authorize(_start(client))
    _refused(client.get(callback(grant, state="y" * 43), follow_redirects=False), 400, "맞지 않습니다")
    _refused(client.get(callback(grant), follow_redirects=False), 400, "맞지 않습니다")   # pending was consumed


def test_epoch_bump_and_logout_end_a_google_session(client, idp, users):
    row = users.add("a@example.com")
    assert _login(client, idp).status_code == 302
    users.update(row.id, session_epoch=1)
    assert _logged_in_as(client) is None
    users.update(row.id, session_epoch=1)
    assert _login(client, idp).status_code == 302 and _logged_in_as(client) == "a@example.com"
    r = client.post("/auth/logout", json={})
    assert r.status_code == 200 and _logged_in_as(client) is None


# ---- open redirect ---------------------------------------------------------------------

EVIL = ["https://evil.example", "//evil.example", "/\\evil.example", "%2F%2Fevil.example", "javascript:alert(1)",
        "https:evil.example", "/%09/evil.example"]


@pytest.mark.parametrize("target", EVIL)
def test_no_next_or_return_parameter_changes_where_login_lands(client, idp, users, target):
    users.add("a@example.com")
    location = _start(client, f"/auth/google?next={target}&return_to={target}&redirect_uri={target}"
                              f"&continue={target}")
    q = parse_qs(urlsplit(location).query)
    assert q["redirect_uri"] == [f"{ORIGIN}/auth/google/callback"]
    assert urlsplit(location).netloc == "accounts.google.com"
    grant = idp.authorize(location)
    r = client.get(callback(grant, next=target, return_to=target, redirect_uri=target), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    r = client.get(f"/auth/login?next={target}", follow_redirects=False)        # already logged in
    assert r.status_code == 302 and r.headers["location"] == "/"


# ---- configuration · methods · logging --------------------------------------------------

def test_methods_offer_google_when_configured(client):
    assert client.get("/auth/methods").json() == {"dev_login": False, "google": True, "password": False}


def test_google_routes_are_404_when_not_configured(users):
    settings = AuthSettings.from_env({"YTC_PUBLIC_URL": "http://127.0.0.1:8080", "YTC_SESSION_SECRET": SECRET,
                                      "YTC_ENV": "dev", "YTC_DEV_LOGIN": "1"})
    app = asgi.create_app(settings, FakeBackend(), users=users)   # type: ignore[arg-type]
    with TestClient(app, base_url="http://127.0.0.1:8080") as c:
        assert c.get("/auth/methods").json() == {"dev_login": True, "google": False, "password": False}
        assert c.get("/auth/google", follow_redirects=False).status_code == 404
        assert c.get("/auth/google/callback?code=x&state=y", follow_redirects=False).status_code == 404


def test_tokens_codes_and_secrets_never_reach_the_logs(client, idp, users, caplog, capsys):
    users.add("a@example.com")
    caplog.set_level(logging.DEBUG)
    secrets_seen: list[str] = [CLIENT_SECRET]
    for claims in (None, {"aud": "other"}, {"email": "stranger@example.com"}):
        grant = idp.authorize(_start(client), claims=claims)
        client.get(callback(grant), follow_redirects=False)
        secrets_seen += [grant.code, grant.state, grant.nonce, grant.challenge]
        if idp.token_requests:
            secrets_seen.append(idp.token_requests[-1]["code_verifier"])
        client.post("/auth/logout", json={})
    idp.token_status = 400
    grant = idp.authorize(_start(client))
    client.get(callback(grant), follow_redirects=False)
    secrets_seen += [grant.code, grant.state]
    out = capsys.readouterr()
    # Skipped by logger name, and only these: the httpx client loggers. They carry the TestClient's own request lines
    # (the browser side, whose callback URL has code and state) besides the app's outgoing Google calls (checked below).
    client_loggers = {"httpx", "httpx2"}
    checked = [r for r in caplog.records if r.name not in client_loggers]
    names = {r.name for r in checked}
    assert "ytc.auth" in names                                  # failures are logged, by reason only
    assert all(n == "root" or n.split(".")[0] in {"ytc", "uvicorn", "starlette", "authlib", "joserfc", "anyio",
                                                   "asyncio"} for n in names), names
    text = " | ".join(r.getMessage() for r in checked) + out.out + out.err
    google_calls = [r.getMessage() for r in caplog.records
                    if r.name in client_loggers and "googleapis.com" in r.getMessage()]
    assert google_calls                                         # the app's own token/JWKS requests were logged ...
    text += " | ".join(google_calls)                            # ... and carry no secret either
    assert "google login refused" in text
    for value in secrets_seen:
        assert value not in text, value
    assert "ya29." not in text and "eyJ" not in text


def test_refusal_pages_escape_the_email(client, idp, users):
    users.add("a@example.com")
    r = _login(client, idp, claims={"email": "<script>x</script>@example.com"})
    assert r.status_code == 403 and "<script>x" not in r.text and "&lt;script&gt;" in r.text


def test_login_page_shows_the_google_button_only_when_offered():
    from pathlib import Path

    web = Path(asgi.__file__).with_name("web")
    html = (web / "login.html").read_text(encoding="utf-8")
    js = (web / "login.js").read_text(encoding="utf-8")
    assert 'id="google-login"' in html and 'href="/auth/google"' in html and "<script>" not in html
    assert "methods.google" in js and '$("google-login").hidden = false' in js and "innerHTML" not in js
