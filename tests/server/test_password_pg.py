"""이메일+비밀번호 로그인을 실제 PG 16(앱 롤 ytc_app) 위에서(서버 설계 §4.2, T15): 관리 CLI가 정한 비밀번호로
`POST /auth/password`가 세션을 만들고, `set-password`는 기존 세션을 끊고, `clear-password`는 비밀번호 로그인을 막는다.

PG 환경이 없으면 skip(`tests/helpers/pg.py`).
"""
from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from server import admin, auth  # noqa: E402

ORIGIN = "https://testserver"
PW = "correct horse battery"
EMAIL = "pw@example.com"


@pytest.fixture
def env(migrated_db: Any) -> dict[str, str]:
    return {"YTC_DB_OWNER_DSN": migrated_db.owner_dsn}


@pytest.fixture
def app(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    backend = request.getfixturevalue("pg_backend")
    from server.asgi import create_app
    from server.config import AuthSettings

    monkeypatch.delenv("YTC_DISABLE", raising=False)
    settings = AuthSettings.from_env({"YTC_PUBLIC_URL": ORIGIN, "YTC_SESSION_SECRET": "x" * 48, "YTC_ENV": "test",
                                      "YTC_PASSWORD_LOGIN": "1"})
    application = create_app(settings, backend)
    try:
        yield application
    finally:
        application.state.handler.registry.shutdown(timeout=10)


def _client(app: Any) -> TestClient:
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})


def _login(c: TestClient, password: str = PW) -> int:
    return int(c.post("/auth/password", json={"email": EMAIL, "password": password}).status_code)


def _admin(env: dict[str, str], *argv: str, stdin: str = "") -> dict[str, Any]:
    return admin.main(list(argv), env, stdin=io.StringIO(stdin))


def test_pg_users_reads_the_hash_only_through_the_login_lookup(pg_db: Any, env: dict[str, str]) -> None:
    _admin(env, "add-user", EMAIL, "--password-stdin", stdin=PW + "\n")
    users = auth.PgUsers(pg_db)
    found = users.password_login(" PW@Example.com ")
    assert found is not None
    row, stored = found
    assert row.email == EMAIL and stored is not None and stored.startswith("scrypt$")
    assert users.password_login("ghost@example.com") is None
    assert users.by_email(EMAIL) == row


def test_add_user_password_stdin_then_login_works_with_the_app_role(app: Any, env: dict[str, str]) -> None:
    _admin(env, "add-user", EMAIL, "--password-stdin", stdin=PW + "\n")
    c = _client(app)
    assert _login(c, "wrong password!") == 401
    assert _login(c) == 200
    assert c.get("/api/ping").json()["user"] == EMAIL


def test_set_password_kills_existing_sessions_and_only_the_new_password_works(app: Any, env: dict[str, str]) -> None:
    _admin(env, "add-user", EMAIL, "--password-stdin", stdin=PW)
    c = _client(app)
    assert _login(c) == 200 and c.get("/api/ping").status_code == 200

    out = _admin(env, "set-password", EMAIL, "--password-stdin", stdin="another long secret\n")

    assert out["password"] is True
    assert c.get("/api/ping").status_code == 401                   # the old session died (epoch bumped)
    fresh = _client(app)
    assert _login(fresh) == 401
    assert _login(fresh, "another long secret") == 200


def test_clear_password_disables_password_login(app: Any, env: dict[str, str]) -> None:
    _admin(env, "add-user", EMAIL, "--password-stdin", stdin=PW)
    c = _client(app)
    assert _login(c) == 200

    _admin(env, "clear-password", EMAIL)

    assert c.get("/api/ping").status_code == 401
    r = _client(app).post("/auth/password", json={"email": EMAIL, "password": PW})
    assert r.status_code == 401 and r.json() == auth.LOGIN_FAILED


def test_disabled_user_cannot_log_in_with_the_right_password(app: Any, env: dict[str, str]) -> None:
    _admin(env, "add-user", EMAIL, "--password-stdin", stdin=PW)
    _admin(env, "disable-user", EMAIL)
    r = _client(app).post("/auth/password", json={"email": EMAIL, "password": PW})
    assert r.status_code == 401 and r.json() == auth.LOGIN_FAILED
