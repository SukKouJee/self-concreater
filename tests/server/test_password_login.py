"""`POST /auth/password`(서버 설계 §4.2, T15)를 ASGI로: 가짜 사용자 목록·가짜 저장소, PG·MinIO 없이.

성공·틀린 비밀번호·모르는 이메일·비밀번호 없는 계정·비활성 계정은 모두 같은 401 문구, 실패 횟수 제한(이메일·IP) 429,
CSRF, 프록시 헤더(신뢰한 프록시에서 온 X-Forwarded-For만 클라이언트 IP로)를 확인한다. 실제 PG 위의 경로는 test_password_pg.py.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: E402

from scripts.ui import server as ui  # noqa: E402
from server import asgi, auth, login_limit, passwords  # noqa: E402
from server.config import AuthSettings  # noqa: E402
from tests.server.test_asgi_unit import FakeBackend, FakeUsers  # noqa: E402

ORIGIN = "https://testserver"
SECRET = "p" * 40
PW = "correct horse battery"
FAILED = {"error": "login_failed", "message": "이메일 또는 비밀번호가 맞지 않습니다"}
_HASH = passwords.hash_password(PW)          # one real hash for the whole module (scrypt is slow on purpose)


class PwUsers(FakeUsers):
    def __init__(self) -> None:
        super().__init__()
        self.hashes: dict[uuid.UUID, str | None] = {}

    def add_pw(self, email: str, password_hash: str | None = _HASH, **over: Any) -> auth.UserRow:
        row = self.add(email, **over)
        self.hashes[row.id] = password_hash
        return row

    def password_login(self, email: str) -> tuple[auth.UserRow, str | None] | None:
        row = self.by_email(email)
        return None if row is None else (row, self.hashes.get(row.id))


def _settings(**over: str) -> AuthSettings:
    env = {"YTC_PUBLIC_URL": ORIGIN, "YTC_SESSION_SECRET": SECRET, "YTC_ENV": "test", "YTC_PASSWORD_LOGIN": "1"}
    env.update(over)
    return AuthSettings.from_env(env)


@pytest.fixture
def users() -> PwUsers:
    return PwUsers()


@pytest.fixture
def limits() -> login_limit.LoginLimiter:
    return login_limit.LoginLimiter(email_failures=3, ip_failures=5, window=600)


def _app(users: PwUsers, limits: login_limit.LoginLimiter, **over: str) -> Any:
    return asgi.create_app(_settings(**over), FakeBackend(), users=users, login_limits=limits)  # type: ignore[arg-type]


@pytest.fixture
def client(users, limits, monkeypatch):
    monkeypatch.delenv(ui.DISABLE_ENV, raising=False)
    with TestClient(_app(users, limits), base_url=ORIGIN, headers={"Origin": ORIGIN},
                    client=("203.0.113.10", 50000)) as c:
        yield c


def _login(client: TestClient, email: str, password: str = PW, **kw: Any) -> Any:
    return client.post("/auth/password", json={"email": email, "password": password}, **kw)


# ---- 성공 ---------------------------------------------------------------------------------

def test_right_password_starts_a_session(client, users):
    users.add_pw("a@example.com")
    r = _login(client, " A@Example.com ")
    assert r.status_code == 200 and r.json() == {"ok": True, "email": "a@example.com"}
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=lax"):
        assert flag in cookie
    assert client.get("/api/ping").json()["user"] == "a@example.com"


def test_login_replaces_any_previous_session(client, users):
    users.add_pw("a@example.com")
    users.add_pw("b@example.com")
    assert _login(client, "a@example.com").status_code == 200
    assert _login(client, "b@example.com").status_code == 200
    assert client.get("/api/ping").json()["user"] == "b@example.com"


def test_methods_report_password(client):
    assert client.get("/auth/methods").json() == {"dev_login": False, "google": False, "password": True}


# ---- 실패는 모두 같은 응답 -------------------------------------------------------------------

@pytest.mark.parametrize("case", ["wrong_password", "unknown_email", "no_password", "disabled"])
def test_every_failure_is_the_same_generic_401(client, users, case):
    email = "a@example.com"
    password = PW
    if case == "wrong_password":
        users.add_pw(email)
        password = "wrong password!"
    elif case == "no_password":
        users.add_pw(email, None)
    elif case == "disabled":
        users.add_pw(email, disabled=True)
    r = _login(client, email, password)
    assert r.status_code == 401 and r.json() == FAILED
    assert "set-cookie" not in r.headers
    assert client.get("/api/ping").status_code == 401


@pytest.mark.parametrize("case", ["wrong_password", "unknown_email", "no_password", "disabled", "ok"])
def test_every_attempt_costs_one_full_scrypt(client, users, case, monkeypatch):
    """모르는 이메일·비밀번호 없는 계정도 같은 비용(더미 해시): 응답 시간으로 계정 유무를 알 수 없다."""
    if case != "unknown_email":
        users.add_pw("a@example.com", None if case == "no_password" else _HASH, disabled=case == "disabled")
    passwords.dummy_hash()
    calls: list[int] = []
    real = passwords.hashlib.scrypt

    def counting(*args: Any, **kwargs: Any) -> bytes:
        calls.append(kwargs["n"])
        return real(*args, **kwargs)

    monkeypatch.setattr(passwords.hashlib, "scrypt", counting)
    _login(client, "a@example.com", "wrong password!" if case == "wrong_password" else PW)
    assert calls == [passwords.SCRYPT_N]


def test_failures_log_only_a_reason_code(client, users, caplog):
    users.add_pw("a@example.com")
    with caplog.at_level(logging.INFO, logger="ytc.auth"):
        _login(client, "a@example.com", "wrong password!")
        _login(client, "ghost@example.com", "wrong password!")
    text = caplog.text
    assert "wrong_password" in text and "not_invited" in text
    assert "wrong password!" not in text and PW not in text and "ghost@example.com" not in text


def test_a_malformed_stored_hash_is_logged_and_refused(client, users, caplog):
    users.add_pw("a@example.com", "scrypt$broken")
    with caplog.at_level(logging.ERROR, logger="ytc.auth"):
        r = _login(client, "a@example.com")
    assert r.status_code == 401 and r.json() == FAILED
    assert "bad_hash" in caplog.text


@pytest.mark.parametrize("body", [{}, {"email": "a@example.com"}, {"password": PW}, {"email": 1, "password": PW},
                                  {"email": "a@example.com", "password": ["x"]}])
def test_malformed_body_is_400(client, body):
    r = client.post("/auth/password", json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_route_is_absent_when_password_login_is_off(users, limits):
    users.add_pw("a@example.com")
    app = asgi.create_app(_settings(YTC_PASSWORD_LOGIN="0", YTC_DEV_LOGIN="1"), FakeBackend(), users=users,
                          login_limits=limits)  # type: ignore[arg-type]
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        assert c.get("/auth/methods").json() == {"dev_login": True, "google": False, "password": False}
        r = _login(c, "a@example.com")
        assert r.status_code == 404 and "set-cookie" not in r.headers


# ---- CSRF ---------------------------------------------------------------------------------

def test_cross_origin_password_login_is_refused(client, users):
    users.add_pw("a@example.com")
    r = _login(client, "a@example.com", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin" and "set-cookie" not in r.headers


def test_form_post_password_login_is_refused(client, users):
    users.add_pw("a@example.com")
    r = client.post("/auth/password", content=b"email=a%40example.com&password=x",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403 and r.json()["error"] == "bad_content_type"


def test_password_login_without_origin_needs_same_origin_fetch_site(client, users):
    users.add_pw("a@example.com")
    client.headers.pop("Origin")
    assert _login(client, "a@example.com").status_code == 403
    assert _login(client, "a@example.com", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


# ---- 실패 횟수 제한 -----------------------------------------------------------------------------

def test_too_many_failures_for_an_email_is_429_with_retry_after(client, users):
    users.add_pw("a@example.com")
    for _ in range(3):
        assert _login(client, "a@example.com", "wrong password!").status_code == 401
    r = _login(client, "a@example.com")                    # right password, but blocked
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "too_many_attempts" and "분 뒤 다시 시도하세요" in body["message"]
    assert int(r.headers["retry-after"]) == body["retry_after"] > 0
    assert "set-cookie" not in r.headers


def test_unknown_emails_are_throttled_the_same_way(client):
    for _ in range(3):
        assert _login(client, "ghost@example.com").status_code == 401
    assert _login(client, "ghost@example.com").status_code == 429


def test_blocked_attempts_do_not_hash(client, users, monkeypatch):
    users.add_pw("a@example.com")
    for _ in range(3):
        _login(client, "a@example.com", "wrong password!")
    monkeypatch.setattr(passwords.hashlib, "scrypt", lambda *a, **k: pytest.fail("hashed while blocked"))
    assert _login(client, "a@example.com").status_code == 429


def test_success_resets_the_email_counter(client, users):
    users.add_pw("a@example.com")
    for _ in range(2):
        _login(client, "a@example.com", "wrong password!")
    assert _login(client, "a@example.com").status_code == 200
    for _ in range(2):
        assert _login(client, "a@example.com", "wrong password!").status_code == 401
    assert _login(client, "a@example.com").status_code == 200


def test_one_ip_trying_many_emails_is_blocked(users, limits):
    users.add_pw("victim@example.com")
    app = _app(users, limits)
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=("198.51.100.7", 1)) as attacker:
        for i in range(5):
            assert _login(attacker, f"user{i}@example.com").status_code == 401
        assert _login(attacker, "victim@example.com").status_code == 429
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=("198.51.100.8", 1)) as other:
        assert _login(other, "victim@example.com").status_code == 200


def test_429_message_is_korean_and_says_minutes():
    status, payload, headers = asgi.too_many_attempts(61)
    assert status == 429 and payload["retry_after"] == 61 and headers == [("Retry-After", "61")]
    assert payload["message"] == "로그인 시도가 너무 많습니다. 2분 뒤 다시 시도하세요"


# ---- 프록시 뒤의 클라이언트 IP ---------------------------------------------------------------------

CADDY = "172.31.254.2"


def _proxied(users: PwUsers, limits: login_limit.LoginLimiter, peer: str) -> TestClient:
    """uvicorn `--proxy-headers`와 같은 미들웨어, FORWARDED_ALLOW_IPS = caddy의 고정 IP."""
    app = ProxyHeadersMiddleware(_app(users, limits), trusted_hosts=CADDY)
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=(peer, 1))


def test_forwarded_for_from_the_trusted_proxy_is_the_client_ip(users, limits):
    users.add_pw("victim@example.com")
    with _proxied(users, limits, CADDY) as c:
        for i in range(5):
            _login(c, f"user{i}@example.com", headers={"X-Forwarded-For": "192.0.2.1"})
        assert _login(c, "victim@example.com", headers={"X-Forwarded-For": "192.0.2.1"}).status_code == 429
        # a different real client behind the same proxy is not affected
        assert _login(c, "victim@example.com", headers={"X-Forwarded-For": "192.0.2.2"}).status_code == 200


def test_forwarded_for_from_a_direct_client_is_ignored(users, limits):
    """프록시가 아닌 곳에서 온 X-Forwarded-For는 믿지 않는다: 매번 다른 값을 보내도 같은 IP로 센다."""
    users.add_pw("victim@example.com")
    with _proxied(users, limits, "198.51.100.66") as c:
        for i in range(5):
            _login(c, f"user{i}@example.com", headers={"X-Forwarded-For": f"192.0.2.{i + 10}"})
        assert _login(c, "victim@example.com", headers={"X-Forwarded-For": "192.0.2.99"}).status_code == 429


def test_session_users_row_does_not_carry_the_hash():
    assert "password_hash" not in auth.UserRow.__dataclass_fields__     # only the login lookup reads the hash


# ---- 로그인 화면 ---------------------------------------------------------------------------

def test_dom_helper_is_served_without_a_session_for_the_login_screen(client):
    r = client.get("/auth/dom.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.content == (ui.STATIC_DIR / "dom.js").read_bytes()
    assert client.get("/static/dom.js").status_code == 401          # the rest of /static still needs a session


def test_login_js_builds_the_password_form_with_h_and_covers_its_states():
    from pathlib import Path as _P
    web = _P(asgi.__file__).with_name("web")
    js = (web / "login.js").read_text(encoding="utf-8")
    page = (web / "login.html").read_text(encoding="utf-8")
    assert 'import { h } from "/auth/dom.js"' in js and "innerHTML" not in js
    assert '"/auth/password"' in js and "methods.password" in js
    assert 'autocomplete: "current-password"' in js and 'type: "password"' in js
    assert "res.status === 429" in js and "OFFLINE" in js and 'role: "alert"' in js
    assert 'id="password-slot"' in page and 'id="password-or"' in page and "<script>" not in page


def test_a_database_error_is_503_and_does_not_count_as_a_failed_guess(client, users, monkeypatch):
    import psycopg
    users.add_pw("a@example.com")
    real = users.password_login

    def down(email: str) -> Any:
        raise psycopg.OperationalError("db down")

    monkeypatch.setattr(users, "password_login", down)
    for _ in range(5):
        r = _login(client, "a@example.com")
        assert r.status_code == 503 and r.json()["error"] == "unavailable"
    monkeypatch.setattr(users, "password_login", real)
    assert _login(client, "a@example.com").status_code == 200       # nothing was recorded, nothing is pending
