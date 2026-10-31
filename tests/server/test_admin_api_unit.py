"""관리 API(서버 설계 §4.2 관리 화면, T16) 단위 테스트: 가짜 관리 저장소·가짜 사용자 목록, PG 없이.

- `/api/admin/*`은 로그인한 **관리자**만(아니면 저장소를 부르기 전에 403 `admin_only`). DB 함수도 따로 막는다(test_admin_api_pg).
- 상태를 바꾸는 요청은 다른 POST와 같은 CSRF 검사(Origin + JSON).
- 응답·로그 어디에도 비밀번호·해시가 없다. 비밀번호는 규칙 검사 → 앱에서 해시 → 저장소에는 해시만.
- 로컬판(stdlib 서버)에는 이 경로가 없다(404).
"""
from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import Any

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from scripts.ui import server as ui  # noqa: E402
from server import admin_api, asgi  # noqa: E402
from server.jobs_registry import Registry  # noqa: E402
from tests.server.test_asgi_unit import ORIGIN, FakeBackend, FakeUsers, _settings  # noqa: E402
from tests.test_ui_server import client, no_browser  # noqa: E402,F401  (local edition fixture 재사용)

PASSWORD = "correct horse battery"


class AdminBackend(FakeBackend):
    daily_usd = Decimal("1.00")                     # YTC_DAILY_USD (users.daily_usd_limit NULL)


class FakeAdmin:
    """PgAdmin과 같은 모양. 호출을 기록하고, `refuse`에 넣은 오류를 낸다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.refuse: admin_api.AdminError | None = None
        self.users: list[dict[str, Any]] = []

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.refuse is not None:
            raise self.refuse

    def list_users(self, actor: uuid.UUID) -> list[dict[str, Any]]:
        self._record("list_users", actor)
        return self.users

    def add_user(self, actor: uuid.UUID, email: str, display_name: str | None, daily_limit: Decimal | None,
                 password_hash: str | None, is_admin: bool) -> uuid.UUID:
        self._record("add_user", actor, email, display_name, daily_limit, password_hash, is_admin)
        return uuid.UUID(int=7)

    def set_password(self, actor: uuid.UUID, user: uuid.UUID, password_hash: str) -> None:
        self._record("set_password", actor, user, password_hash)

    def clear_password(self, actor: uuid.UUID, user: uuid.UUID) -> None:
        self._record("clear_password", actor, user)

    def set_disabled(self, actor: uuid.UUID, user: uuid.UUID, disabled: bool) -> None:
        self._record("set_disabled", actor, user, disabled)

    def logout_all(self, actor: uuid.UUID, user: uuid.UUID) -> None:
        self._record("logout_all", actor, user)

    def set_limit(self, actor: uuid.UUID, user: uuid.UUID, usd: Decimal | None) -> None:
        self._record("set_limit", actor, user, usd)

    def set_admin(self, actor: uuid.UUID, user: uuid.UUID, is_admin: bool) -> None:
        self._record("set_admin", actor, user, is_admin)

    def usage(self, actor: uuid.UUID, days: int) -> list[dict[str, Any]]:
        self._record("usage", actor, days)
        return [{"email": "a@example.com", "day": "2026-11-02", "calls": 2, "failed": 0, "input_tokens": 10,
                 "output_tokens": 20, "cost_usd": 0.5}]

    def audit(self, actor: uuid.UUID, limit: int) -> list[dict[str, Any]]:
        self._record("audit", actor, limit)
        return []


@pytest.fixture
def local_client(client: Any) -> Any:  # noqa: F811
    return client


@pytest.fixture
def users() -> FakeUsers:
    return FakeUsers()


@pytest.fixture
def store() -> FakeAdmin:
    return FakeAdmin()


@pytest.fixture
def app(users: FakeUsers, store: FakeAdmin, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(ui.DISABLE_ENV, raising=False)
    monkeypatch.setattr(admin_api.passwords, "hash_password", lambda pw: "scrypt$fake$" + str(len(pw)))
    return asgi.create_app(_settings(), AdminBackend(), users=users, registry=Registry(),  # type: ignore[arg-type]
                           admin=store)


def _client(app: Any, users: FakeUsers, email: str, *, admin: bool) -> TestClient:
    row = users.add(email)
    if admin:
        users.update(row.id, is_admin=True)
    c = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    assert c.post("/auth/dev", json={"email": email}).status_code == 200
    return c


@pytest.fixture
def root(app: Any, users: FakeUsers) -> TestClient:
    return _client(app, users, "root@example.com", admin=True)


def _root_id(users: FakeUsers) -> uuid.UUID:
    row = users.by_email("root@example.com")
    assert row is not None
    return row.id


TARGET = uuid.UUID(int=42)
ROUTES = [
    ("GET", "/api/admin/users", None),
    ("POST", "/api/admin/users", {"email": "n@example.com"}),
    ("POST", f"/api/admin/users/{TARGET}/password", {"password": PASSWORD}),
    ("POST", f"/api/admin/users/{TARGET}/clear-password", {}),
    ("POST", f"/api/admin/users/{TARGET}/disable", {}),
    ("POST", f"/api/admin/users/{TARGET}/enable", {}),
    ("POST", f"/api/admin/users/{TARGET}/logout-all", {}),
    ("POST", f"/api/admin/users/{TARGET}/limit", {"daily_usd_limit": 3}),
    ("POST", f"/api/admin/users/{TARGET}/admin", {"is_admin": True}),
    ("GET", "/api/admin/usage?days=7", None),
    ("GET", "/api/admin/audit?limit=50", None),
]


def _send(c: TestClient, method: str, path: str, body: Any) -> Any:
    return c.get(path) if method == "GET" else c.post(path, json=body)


# ---- 누가 ------------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", ROUTES)
def test_non_admins_get_403_and_the_store_is_never_called(app: Any, users: FakeUsers, store: FakeAdmin,
                                                         method: str, path: str, body: Any) -> None:
    c = _client(app, users, "bob@example.com", admin=False)
    r = _send(c, method, path, body)
    assert r.status_code == 403 and r.json()["error"] == "admin_only"
    assert store.calls == []


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_logged_out_requests_get_401(app: Any, store: FakeAdmin, method: str, path: str, body: Any) -> None:
    c = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    assert _send(c, method, path, body).status_code == 401
    assert store.calls == []


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_admins_reach_the_store_as_themselves(root: TestClient, users: FakeUsers, store: FakeAdmin,
                                              method: str, path: str, body: Any) -> None:
    r = _send(root, method, path, body)
    assert r.status_code == 200, r.text
    assert len(store.calls) == 1 and store.calls[0][1][0] == _root_id(users)


def test_a_demoted_admin_is_refused_on_the_next_request(app: Any, users: FakeUsers, store: FakeAdmin,
                                                        root: TestClient) -> None:
    assert root.get("/api/admin/users").status_code == 200
    users.update(_root_id(users), is_admin=False)
    assert root.get("/api/admin/users").status_code == 403
    assert len(store.calls) == 1


def test_unknown_admin_paths_are_404_for_admins(root: TestClient) -> None:
    for method, path in (("GET", "/api/admin/nope"), ("POST", f"/api/admin/users/{TARGET}/nope"),
                         ("DELETE", "/api/admin/users"), ("GET", f"/api/admin/users/{TARGET}/disable")):
        r = root.request(method, path, json={} if method != "GET" else None)
        assert r.status_code == 404, (method, path)


def test_api_me_tells_the_screen_whether_to_show_the_admin_entry(app: Any, users: FakeUsers, root: TestClient) -> None:
    assert root.get("/api/me").json() == {"email": "root@example.com", "is_admin": True}
    bob = _client(app, users, "bob@example.com", admin=False)
    assert bob.get("/api/me").json() == {"email": "bob@example.com", "is_admin": False}
    assert TestClient(app, base_url=ORIGIN).get("/api/me").status_code == 401


# ---- CSRF ------------------------------------------------------------------------------

def test_admin_posts_need_the_same_origin_and_json(root: TestClient, store: FakeAdmin) -> None:
    path = f"/api/admin/users/{TARGET}/disable"
    r = root.post(path, json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    r = root.post(path, content=b"x=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403 and r.json()["error"] == "bad_content_type"
    assert store.calls == []


# ---- 값 검사 ----------------------------------------------------------------------------

def test_invite_validates_and_lowercases_before_the_store(root: TestClient, users: FakeUsers, store: FakeAdmin) -> None:
    r = root.post("/api/admin/users", json={"email": " New@Example.com ", "display_name": "  새 사람 ",
                                            "daily_usd_limit": "2.50", "password": PASSWORD, "is_admin": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "id": str(uuid.UUID(int=7))}
    name, args = store.calls[0]
    assert name == "add_user"
    assert args == (_root_id(users), "new@example.com", "새 사람", Decimal("2.50"), "scrypt$fake$21", True)
    root.post("/api/admin/users", json={"email": "g@example.com", "password": "", "daily_usd_limit": None})
    assert store.calls[1][1][1:] == ("g@example.com", None, None, None, False)        # google-only invite


@pytest.mark.parametrize("body, code", [
    ({}, "bad_email"), ({"email": "nope"}, "bad_email"), ({"email": 5}, "bad_email"),
    ({"email": "a@example.com", "password": "short"}, "weak_password"),
    ({"email": "a@example.com", "password": 123}, "bad_request"),
    ({"email": "a@example.com", "daily_usd_limit": -1}, "bad_limit"),
    ({"email": "a@example.com", "daily_usd_limit": "1.234"}, "bad_limit"),
    ({"email": "a@example.com", "daily_usd_limit": True}, "bad_limit"),
    ({"email": "a@example.com", "is_admin": "yes"}, "bad_request"),
    ({"email": "a@example.com", "display_name": "x" * 101}, "bad_request"),
])
def test_invite_refuses_bad_values(root: TestClient, store: FakeAdmin, body: dict[str, Any], code: str) -> None:
    r = root.post("/api/admin/users", json=body)
    assert r.status_code == 400 and r.json()["error"] == code
    assert store.calls == []


def test_password_reset_checks_policy_hashes_and_never_echoes(root: TestClient, users: FakeUsers, store: FakeAdmin,
                                                             caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    r = root.post(f"/api/admin/users/{TARGET}/password", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert store.calls == [("set_password", (_root_id(users), TARGET, "scrypt$fake$21"))]
    for bad in ({"password": "short"}, {}, {"password": None}):
        r = root.post(f"/api/admin/users/{TARGET}/password", json=bad)
        assert r.status_code == 400 and PASSWORD not in r.text
    assert PASSWORD not in caplog.text and "scrypt" not in caplog.text


@pytest.mark.parametrize("path, body, call", [
    ("clear-password", {}, ("clear_password",)),
    ("disable", {}, ("set_disabled", True)),
    ("enable", {}, ("set_disabled", False)),
    ("logout-all", {}, ("logout_all",)),
    ("limit", {"daily_usd_limit": 4.5}, ("set_limit", Decimal("4.5"))),
    ("limit", {"daily_usd_limit": None}, ("set_limit", None)),
    ("limit", {"daily_usd_limit": ""}, ("set_limit", None)),
    ("admin", {"is_admin": False}, ("set_admin", False)),
])
def test_row_actions_call_the_matching_store_method(root: TestClient, users: FakeUsers, store: FakeAdmin,
                                                    path: str, body: dict[str, Any], call: tuple[Any, ...]) -> None:
    assert root.post(f"/api/admin/users/{TARGET}/{path}", json=body).status_code == 200
    assert store.calls == [(call[0], (_root_id(users), TARGET, *call[1:]))]


@pytest.mark.parametrize("path, body", [("limit", {}), ("limit", {"daily_usd_limit": "abc"}), ("admin", {}),
                                        ("admin", {"is_admin": 1})])
def test_row_actions_refuse_bad_values(root: TestClient, store: FakeAdmin, path: str, body: dict[str, Any]) -> None:
    assert root.post(f"/api/admin/users/{TARGET}/{path}", json=body).status_code == 400
    assert store.calls == []


def test_a_malformed_user_id_is_404(root: TestClient, store: FakeAdmin) -> None:
    for uid in ("not-a-uuid", "1", "%00"):
        assert root.post(f"/api/admin/users/{uid}/disable", json={}).status_code == 404
    assert store.calls == []


@pytest.mark.parametrize("query, days", [("", 7), ("?days=30", 30), ("?days=1", 1)])
def test_usage_days(root: TestClient, store: FakeAdmin, query: str, days: int) -> None:
    r = root.get(f"/api/admin/usage{query}")
    assert r.status_code == 200 and r.json()["days"] == days and r.json()["total_usd"] == 0.5
    assert store.calls[-1][1][1] == days


@pytest.mark.parametrize("path", ["/api/admin/usage?days=0", "/api/admin/usage?days=91", "/api/admin/usage?days=x",
                                  "/api/admin/audit?limit=0", "/api/admin/audit?limit=201", "/api/admin/audit?limit=a"])
def test_usage_and_audit_bounds(root: TestClient, store: FakeAdmin, path: str) -> None:
    assert root.get(path).status_code == 400
    assert store.calls == []


# ---- 저장소 오류 → 응답 -------------------------------------------------------------------

@pytest.mark.parametrize("code, status", [("admin_only", 403), ("user_not_found", 404), ("self_protected", 409),
                                          ("last_admin", 409), ("user_exists", 409), ("bad_input", 400)])
def test_store_refusals_keep_their_status_and_korean_message(root: TestClient, store: FakeAdmin,
                                                             code: str, status: int) -> None:
    store.refuse = admin_api.AdminError(code)
    r = root.post(f"/api/admin/users/{TARGET}/disable", json={})
    assert r.status_code == status and r.json()["error"] == code
    assert r.json()["message"] == admin_api.REFUSALS[code][1]


def test_list_answer_has_no_secret_fields(root: TestClient, store: FakeAdmin) -> None:
    store.users = [{"id": str(TARGET), "email": "a@example.com", "display_name": None, "is_admin": False,
                    "disabled": False, "has_password": True, "has_google": False, "daily_usd_limit": None,
                    "today_usd": 0.1, "created_at": "2026-11-02T00:00:00+00:00"}]
    body = root.get("/api/admin/users").json()
    (row,) = body["users"]
    assert row["effective_daily_usd"] == 1.0 and body["default_daily_usd"] == 1.0
    text = json.dumps(body)
    for secret in ("password_hash", "google_sub", "scrypt"):
        assert secret not in text


def test_sqlstate_mapping_covers_the_function_codes() -> None:
    assert admin_api.SQLSTATES == {"YTA01": "admin_only", "YTA02": "user_not_found", "YTA03": "self_protected",
                                   "YTA04": "last_admin", "YTA05": "bad_input", "23505": "user_exists",
                                   "23514": "bad_input", "22003": "bad_input"}


# ---- 로컬판 ------------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", ROUTES + [("GET", "/api/me", None)])
def test_local_edition_has_no_admin_routes(local_client: Any, method: str, path: str, body: Any) -> None:
    status, _ = local_client.json(method, path, body if method == "POST" else None)
    assert status == 404
