"""관리 API(서버 설계 §4.2 관리 화면, T16 + 0004 사용자 관리) 단위 테스트: 가짜 관리 저장소·가짜 사용자 목록, PG 없이.

- `/api/admin/*`은 로그인한 **관리자**만(아니면 저장소를 부르기 전에 403 `admin_only`). DB 함수도 따로 막는다(test_admin_api_pg).
- 상태를 바꾸는 요청은 다른 POST와 같은 CSRF 검사(Origin + JSON).
- 응답·로그 어디에도 비밀번호·해시가 없다. 비밀번호는 규칙 검사 → 앱에서 해시 → 저장소에는 해시만.
- 목록·감사 기록은 서버 쪽 쪽 나누기·필터·정렬. 값이 틀리면 저장소를 부르기 전에 400.
- 지우기는 그 사용자의 이메일을 그대로 입력해야 하고(앱과 DB가 둘 다 본다), 자기 자신은 안 된다. 지운 뒤 앱이 MinIO를 정리한다.
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
TARGET = uuid.UUID(int=42)
GOOGLE = {"YTC_GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "YTC_GOOGLE_CLIENT_SECRET": "secret"}


class AdminBackend(FakeBackend):
    daily_usd = Decimal("1.00")                     # YTC_DAILY_USD (users.daily_usd_limit NULL)


class FakeAdmin:
    """PgAdmin과 같은 모양. 호출을 기록하고, `refuse`에 넣은 오류를 낸다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.refuse: admin_api.AdminError | None = None
        self.users: list[dict[str, Any]] = []
        self.total: int | None = None
        self.detail: dict[str, Any] = {"id": str(TARGET), "email": "bob@example.com", "display_name": None,
                                       "is_admin": False, "disabled": False, "has_password": True,
                                       "has_google": False, "daily_usd_limit": None, "created_at": "2026-11-01",
                                       "last_login_at": None, "docs": 3, "docs_bytes": 30, "objects": 2,
                                       "objects_bytes": 2000, "trash": 1, "trash_bytes": 5, "today_usd": 0.1,
                                       "month_usd": 0.5}
        self.deleted_keys: list[str] = [f"users/{TARGET}/a.png", f"trash/{TARGET}/s/b.png"]

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.refuse is not None:
            raise self.refuse

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def list_page(self, actor: uuid.UUID, query: admin_api.UserQuery) -> dict[str, Any]:
        self._record("list_page", actor, query)
        return {"total": len(self.users) if self.total is None else self.total, "rows": self.users}

    def user_detail(self, actor: uuid.UUID, user: uuid.UUID) -> dict[str, Any]:
        self._record("user_detail", actor, user)
        return dict(self.detail)

    def overview(self, actor: uuid.UUID) -> dict[str, Any]:
        self._record("overview", actor)
        return {"users": 3, "admins": 1, "disabled": 0, "with_password": 2, "with_google": 1, "never_logged_in": 1,
                "today_usd": 0.5, "month_usd": 4.25, "month_deleted_usd": 0}

    def add_user(self, actor: uuid.UUID, email: str, display_name: str | None, daily_limit: Decimal | None,
                 password_hash: str | None, is_admin: bool) -> uuid.UUID:
        self._record("add_user", actor, email, display_name, daily_limit, password_hash, is_admin)
        return uuid.UUID(int=7)

    def update_user(self, actor: uuid.UUID, user: uuid.UUID, changes: admin_api.UserChanges) -> None:
        self._record("update_user", actor, user, changes)

    def delete_user(self, actor: uuid.UUID, user: uuid.UUID, confirm_email: str) -> dict[str, Any]:
        self._record("delete_user", actor, user, confirm_email)
        return {"email": confirm_email, "display_name": None, "docs": 3, "docs_bytes": 30, "folders": 1,
                "objects": 2, "objects_bytes": 2000, "trash": 1, "trash_bytes": 5, "usage_rows": 4,
                "usage_usd": 1.5, "s3_keys": list(self.deleted_keys)}

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
                 "output_tokens": 20, "cost_usd": 0.5},
                {"email": None, "day": "2026-11-02", "calls": 1, "failed": 0, "input_tokens": 1,
                 "output_tokens": 1, "cost_usd": 0.25}]

    def audit_page(self, actor: uuid.UUID, query: admin_api.AuditQuery) -> dict[str, Any]:
        self._record("audit_page", actor, query)
        return {"total": 0, "rows": []}


class FakeLife:
    """UserLifecycle: 지우기·이메일 변경 앞뒤로 앱이 하는 일(작업 멈춤, MinIO 정리, 캐시 비우기)을 기록한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.left = 0

    def stop_jobs(self, user: uuid.UUID) -> None:
        self.calls.append(("stop_jobs", user))

    def purge_storage(self, user: uuid.UUID, s3_keys: list[str]) -> dict[str, Any]:
        self.calls.append(("purge_storage", (user, sorted(s3_keys))))
        return {"ok": self.left == 0, "removed": len(s3_keys) - self.left, "left": self.left}

    def forget(self, user: uuid.UUID) -> None:
        self.calls.append(("forget", user))


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
def life() -> FakeLife:
    return FakeLife()


def _make_app(users: FakeUsers, store: FakeAdmin, life: FakeLife, **env: str) -> Any:
    return asgi.create_app(_settings(**env), AdminBackend(), users=users,  # type: ignore[arg-type]
                           registry=Registry(), admin=store, lifecycle=life)


@pytest.fixture
def app(users: FakeUsers, store: FakeAdmin, life: FakeLife, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(ui.DISABLE_ENV, raising=False)
    admin_api.passwords.dummy_hash()          # warm the cached real dummy hash before faking hash_password
    monkeypatch.setattr(admin_api.passwords, "hash_password", lambda pw: "scrypt$fake$" + str(len(pw)))
    return _make_app(users, store, life, YTC_PASSWORD_LOGIN="1", **GOOGLE)


def _client(app: Any, users: FakeUsers, email: str, *, admin: bool) -> TestClient:
    row = users.by_email(email) or users.add(email)
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
    ("GET", f"/api/admin/users/{TARGET}", None),
    ("POST", f"/api/admin/users/{TARGET}/update", {"display_name": "새 이름"}),
    ("POST", f"/api/admin/users/{TARGET}/delete", {"confirm_email": "bob@example.com"}),
    ("GET", "/api/admin/overview", None),
]


def _send(c: TestClient, method: str, path: str, body: Any) -> Any:
    return c.get(path) if method == "GET" else c.post(path, json=body)


# ---- 누가 ------------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", ROUTES)
def test_non_admins_get_403_and_the_store_is_never_called(app: Any, users: FakeUsers, store: FakeAdmin,
                                                         life: FakeLife, method: str, path: str, body: Any) -> None:
    c = _client(app, users, "bob@example.com", admin=False)
    r = _send(c, method, path, body)
    assert r.status_code == 403 and r.json()["error"] == "admin_only"
    assert store.calls == [] and life.calls == []


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
    assert store.calls and all(c[1][0] == _root_id(users) for c in store.calls)


def test_a_demoted_admin_is_refused_on_the_next_request(app: Any, users: FakeUsers, store: FakeAdmin,
                                                        root: TestClient) -> None:
    assert root.get("/api/admin/users").status_code == 200
    users.update(_root_id(users), is_admin=False)
    assert root.get("/api/admin/users").status_code == 403
    assert len(store.calls) == 1


def test_unknown_admin_paths_are_404_for_admins(root: TestClient) -> None:
    for method, path in (("GET", "/api/admin/nope"), ("POST", f"/api/admin/users/{TARGET}/nope"),
                         ("DELETE", "/api/admin/users"), ("GET", f"/api/admin/users/{TARGET}/disable"),
                         ("POST", f"/api/admin/users/{TARGET}"), ("DELETE", f"/api/admin/users/{TARGET}"),
                         ("POST", "/api/admin/overview")):
        r = root.request(method, path, json={} if method != "GET" else None)
        assert r.status_code == 404, (method, path)


def test_api_me_tells_the_screen_whether_to_show_the_admin_entry(app: Any, users: FakeUsers, root: TestClient) -> None:
    assert root.get("/api/me").json() == {"email": "root@example.com", "is_admin": True}
    bob = _client(app, users, "bob@example.com", admin=False)
    assert bob.get("/api/me").json() == {"email": "bob@example.com", "is_admin": False}
    assert TestClient(app, base_url=ORIGIN).get("/api/me").status_code == 401


# ---- CSRF ------------------------------------------------------------------------------

@pytest.mark.parametrize("action, body", [("disable", {}), ("delete", {"confirm_email": "bob@example.com"}),
                                          ("update", {"display_name": "x"})])
def test_admin_posts_need_the_same_origin_and_json(root: TestClient, store: FakeAdmin, life: FakeLife,
                                                   action: str, body: dict[str, Any]) -> None:
    path = f"/api/admin/users/{TARGET}/{action}"
    r = root.post(path, json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    r = root.post(path, content=json.dumps(body).encode(), headers={"Content-Type": "text/plain"})
    assert r.status_code == 403 and r.json()["error"] == "bad_content_type"
    assert store.calls == [] and life.calls == []


# ---- 만들기 ----------------------------------------------------------------------------

def test_create_validates_and_lowercases_before_the_store(root: TestClient, users: FakeUsers, store: FakeAdmin) -> None:
    r = root.post("/api/admin/users", json={"email": " New@Example.com ", "display_name": "  새 사람 ",
                                            "daily_usd_limit": "2.50", "password": PASSWORD, "is_admin": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "id": str(uuid.UUID(int=7)), "email": "new@example.com"}
    name, args = store.calls[0]
    assert name == "add_user"
    assert args == (_root_id(users), "new@example.com", "새 사람", Decimal("2.50"), "scrypt$fake$21", True)
    root.post("/api/admin/users", json={"email": "g@example.com", "password": "", "daily_usd_limit": None})
    assert store.calls[1][1][1:] == ("g@example.com", None, None, None, False)        # Google-only account


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
def test_create_refuses_bad_values(root: TestClient, store: FakeAdmin, body: dict[str, Any], code: str) -> None:
    r = root.post("/api/admin/users", json=body)
    assert r.status_code == 400 and r.json()["error"] == code
    assert store.calls == []


def test_create_needs_a_password_when_google_login_is_off(users: FakeUsers, store: FakeAdmin, life: FakeLife,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ui.DISABLE_ENV, raising=False)
    admin_api.passwords.dummy_hash()          # warm the cached real dummy hash before faking hash_password
    monkeypatch.setattr(admin_api.passwords, "hash_password", lambda pw: "scrypt$fake$" + str(len(pw)))
    c = _client(_make_app(users, store, life, YTC_PASSWORD_LOGIN="1"), users, "root@example.com", admin=True)
    r = c.post("/api/admin/users", json={"email": "a@example.com"})
    assert r.status_code == 400 and r.json()["error"] == "password_required"
    assert store.calls == []
    assert c.post("/api/admin/users", json={"email": "a@example.com", "password": PASSWORD}).status_code == 200


# ---- 비밀번호·한 가지씩 바꾸기 ----------------------------------------------------------------

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
        assert root.get(f"/api/admin/users/{uid}").status_code == 404
    assert store.calls == []


# ---- 고치기(여러 칸 한 번에) --------------------------------------------------------------------

def test_update_sends_only_the_given_fields_in_one_call(root: TestClient, users: FakeUsers, store: FakeAdmin,
                                                        life: FakeLife) -> None:
    r = root.post(f"/api/admin/users/{TARGET}/update", json={
        "email": " Robert@Example.com ", "display_name": "  로버트 ", "daily_usd_limit": "3", "is_admin": True,
        "disabled": False, "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "changed": ["email", "display_name", "daily_usd_limit", "is_admin", "disabled",
                                                "password"], "sessions_ended": True}
    ((name, (actor, target, changes)),) = store.calls
    assert (name, actor, target) == ("update_user", _root_id(users), TARGET)
    assert changes == admin_api.UserChanges(email="robert@example.com", display_name="로버트", set_name=True,
                                            daily_limit=Decimal("3"), set_limit=True, is_admin=True, disabled=False,
                                            password_hash="scrypt$fake$21")
    assert life.calls == [("forget", TARGET)]                       # the email label cache follows the change
    assert PASSWORD not in r.text and "scrypt" not in r.text


@pytest.mark.parametrize("body, fields, ended", [
    ({"display_name": ""}, ["display_name"], False),
    ({"daily_usd_limit": None}, ["daily_usd_limit"], False),
    ({"clear_password": True}, ["password"], True),
    ({"disabled": True}, ["disabled"], True),
    ({"is_admin": False}, ["is_admin"], False),
])
def test_update_single_fields(root: TestClient, store: FakeAdmin, life: FakeLife, body: dict[str, Any],
                              fields: list[str], ended: bool) -> None:
    r = root.post(f"/api/admin/users/{TARGET}/update", json=body)
    assert r.status_code == 200 and r.json()["changed"] == fields and r.json()["sessions_ended"] is ended
    assert life.calls == []


@pytest.mark.parametrize("body, code", [
    ({}, "nothing_to_change"), ({"email": "nope"}, "bad_email"), ({"email": None}, "bad_email"),
    ({"display_name": 5}, "bad_request"), ({"display_name": "x" * 101}, "bad_request"),
    ({"daily_usd_limit": "-1"}, "bad_limit"), ({"is_admin": "true"}, "bad_request"),
    ({"disabled": 0}, "bad_request"), ({"password": "short"}, "weak_password"),
    ({"password": PASSWORD, "clear_password": True}, "bad_request"), ({"clear_password": "yes"}, "bad_request"),
    ({"surprise": 1}, "bad_request"),
])
def test_update_refuses_bad_values_before_the_store(root: TestClient, store: FakeAdmin, body: dict[str, Any],
                                                    code: str) -> None:
    r = root.post(f"/api/admin/users/{TARGET}/update", json=body)
    assert r.status_code == 400 and r.json()["error"] == code
    assert store.calls == []


# ---- 상세 ----------------------------------------------------------------------------------

def test_detail_adds_the_effective_limit_and_self_flag(root: TestClient, store: FakeAdmin) -> None:
    body = root.get(f"/api/admin/users/{TARGET}").json()
    assert body["user"]["email"] == "bob@example.com" and body["user"]["effective_daily_usd"] == 1.0
    assert body["user"]["is_self"] is False and body["user"]["storage_bytes"] == 2035
    assert body["default_daily_usd"] == 1.0
    store.refuse = admin_api.AdminError("user_not_found")
    assert root.get(f"/api/admin/users/{TARGET}").status_code == 404


# ---- 지우기 ---------------------------------------------------------------------------------

def test_delete_checks_the_typed_email_stops_jobs_then_cleans_storage(root: TestClient, users: FakeUsers,
                                                                     store: FakeAdmin, life: FakeLife) -> None:
    r = root.post(f"/api/admin/users/{TARGET}/delete", json={"confirm_email": " Bob@Example.com "})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["email"] == "bob@example.com"
    assert out["deleted"] == {"docs": 3, "folders": 1, "objects": 2, "trash": 1, "bytes": 2035, "usage_rows": 4,
                              "usage_usd": 1.5}
    assert out["storage"] == {"ok": True, "removed": 2, "left": 0}
    assert "s3_keys" not in out
    assert store.names() == ["user_detail", "delete_user"]
    assert store.calls[1][1] == (_root_id(users), TARGET, "bob@example.com")
    assert life.calls == [("stop_jobs", TARGET), ("purge_storage", (TARGET, sorted(store.deleted_keys))),
                          ("forget", TARGET)]


@pytest.mark.parametrize("body, code", [({}, "confirm_required"), ({"confirm_email": ""}, "confirm_required"),
                                        ({"confirm_email": 5}, "confirm_required"),
                                        ({"confirm_email": "cat@example.com"}, "confirm_mismatch")])
def test_delete_without_the_right_typed_email_touches_nothing(root: TestClient, store: FakeAdmin, life: FakeLife,
                                                              body: dict[str, Any], code: str) -> None:
    r = root.post(f"/api/admin/users/{TARGET}/delete", json=body)
    assert r.status_code == 400 and r.json()["error"] == code
    assert "delete_user" not in store.names() and life.calls == []


def test_delete_refuses_self_before_anything_happens(root: TestClient, users: FakeUsers, store: FakeAdmin,
                                                     life: FakeLife) -> None:
    me = _root_id(users)
    store.detail = {**store.detail, "id": str(me), "email": "root@example.com"}
    r = root.post(f"/api/admin/users/{me}/delete", json={"confirm_email": "root@example.com"})
    assert r.status_code == 409 and r.json()["error"] == "self_delete"
    assert "delete_user" not in store.names() and life.calls == []


def test_delete_reports_storage_left_behind(root: TestClient, life: FakeLife) -> None:
    life.left = 1
    r = root.post(f"/api/admin/users/{TARGET}/delete", json={"confirm_email": "bob@example.com"})
    assert r.status_code == 200 and r.json()["storage"] == {"ok": False, "removed": 1, "left": 1}


def test_delete_refused_by_the_database_leaves_storage_alone(root: TestClient, store: FakeAdmin,
                                                             life: FakeLife) -> None:
    def refuse_delete(*args: Any) -> Any:
        raise admin_api.AdminError("last_admin")

    store.delete_user = refuse_delete  # type: ignore[method-assign]
    r = root.post(f"/api/admin/users/{TARGET}/delete", json={"confirm_email": "bob@example.com"})
    assert r.status_code == 409 and r.json()["error"] == "last_admin"
    assert life.calls == [("stop_jobs", TARGET)]


# ---- 목록: 검색·필터·정렬·쪽 ----------------------------------------------------------------------

def test_list_defaults_and_pages(root: TestClient, store: FakeAdmin) -> None:
    store.total = 120
    body = root.get("/api/admin/users").json()
    assert (body["total"], body["page"], body["per_page"], body["pages"]) == (120, 1, 50, 3)
    assert store.calls[-1][1][1] == admin_api.UserQuery()
    root.get("/api/admin/users?q=%20Kim%20&status=disabled&role=admin&login=google&sort=last_login&order=desc"
             "&page=3&per_page=20")
    assert store.calls[-1][1][1] == admin_api.UserQuery(q="Kim", status="disabled", role="admin", login="google",
                                                        sort="last_login", desc=True, page=3, per_page=20)
    assert store.calls[-1][1][1].offset == 40


def test_list_with_no_rows_has_one_page(root: TestClient, store: FakeAdmin) -> None:
    body = root.get("/api/admin/users?q=nobody").json()
    assert body["users"] == [] and body["total"] == 0 and body["pages"] == 1


@pytest.mark.parametrize("query", ["status=gone", "role=owner", "login=sso", "sort=id", "order=up", "page=0",
                                   "page=x", "page=100001", "per_page=0", "per_page=101", "per_page=-5",
                                   "q=" + "x" * 201])
def test_list_refuses_unknown_parameters(root: TestClient, store: FakeAdmin, query: str) -> None:
    r = root.get(f"/api/admin/users?{query}")
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert store.calls == []


def test_list_answer_has_no_secret_fields(root: TestClient, store: FakeAdmin) -> None:
    store.users = [{"id": str(TARGET), "email": "a@example.com", "display_name": None, "is_admin": False,
                    "disabled": False, "has_password": True, "has_google": False, "daily_usd_limit": None,
                    "today_usd": 0.1, "month_usd": 0.2, "created_at": "2026-11-02T00:00:00+00:00",
                    "last_login_at": None}]
    body = root.get("/api/admin/users").json()
    (row,) = body["users"]
    assert row["effective_daily_usd"] == 1.0 and body["default_daily_usd"] == 1.0 and row["is_self"] is False
    text = json.dumps(body)
    for secret in ("password_hash", "google_sub", "scrypt"):
        assert secret not in text


# ---- 사용량·감사 기록·서버 정보 ------------------------------------------------------------------

@pytest.mark.parametrize("query, days", [("", 7), ("?days=30", 30), ("?days=1", 1)])
def test_usage_days(root: TestClient, store: FakeAdmin, query: str, days: int) -> None:
    r = root.get(f"/api/admin/usage{query}")
    assert r.status_code == 200 and r.json()["days"] == days and r.json()["total_usd"] == 0.75
    assert store.calls[-1][1][1] == days


@pytest.mark.parametrize("path", ["/api/admin/usage?days=0", "/api/admin/usage?days=91", "/api/admin/usage?days=x",
                                  "/api/admin/audit?limit=0", "/api/admin/audit?limit=201", "/api/admin/audit?limit=a",
                                  "/api/admin/audit?per_page=201", "/api/admin/audit?page=0",
                                  "/api/admin/audit?action=drop_table", "/api/admin/audit?from=2026-13-01",
                                  "/api/admin/audit?to=yesterday", "/api/admin/audit?from=2026-11-03&to=2026-11-02",
                                  "/api/admin/audit?actor=" + "x" * 201])
def test_usage_and_audit_bounds(root: TestClient, store: FakeAdmin, path: str) -> None:
    assert root.get(path).status_code == 400
    assert store.calls == []


def test_audit_filters_reach_the_store(root: TestClient, store: FakeAdmin) -> None:
    body = root.get("/api/admin/audit?actor=root&action=delete_user&target=bob&from=2026-11-01&to=2026-11-03"
                    "&page=2&per_page=25").json()
    assert (body["entries"], body["total"], body["page"], body["per_page"], body["pages"]) == ([], 0, 2, 25, 1)
    assert "delete_user" in body["actions"] and "set_email" in body["actions"]
    from datetime import date
    assert store.calls[-1][1][1] == admin_api.AuditQuery(actor="root", action="delete_user", target="bob",
                                                         date_from=date(2026, 11, 1), date_to=date(2026, 11, 3),
                                                         page=2, per_page=25)
    root.get("/api/admin/audit?limit=10")                                     # old name for per_page
    assert store.calls[-1][1][1].per_page == 10


def test_overview_reports_counts_settings_and_switched_off_features(root: TestClient, store: FakeAdmin) -> None:
    body = root.get("/api/admin/overview").json()
    assert body["counts"]["users"] == 3 and body["spend"] == {"today_usd": 0.5, "month_usd": 4.25,
                                                               "month_deleted_usd": 0}
    assert body["default_daily_usd"] == 1.0
    assert body["login"] == {"google": True, "password": True, "dev": True}
    names = [f["name"] for f in body["features_off"]]
    assert names == sorted(ui.FEATURE_PREFIXES)
    assert all(f["text"] == ui.FEATURE_TEXT[f["name"]] for f in body["features_off"])


# ---- 저장소 오류 → 응답 -------------------------------------------------------------------

@pytest.mark.parametrize("code, status", [("admin_only", 403), ("user_not_found", 404), ("self_protected", 409),
                                          ("last_admin", 409), ("user_exists", 409), ("bad_input", 400),
                                          ("confirm_mismatch", 400), ("self_delete", 409)])
def test_store_refusals_keep_their_status_and_korean_message(root: TestClient, store: FakeAdmin,
                                                             code: str, status: int) -> None:
    store.refuse = admin_api.AdminError(code)
    r = root.post(f"/api/admin/users/{TARGET}/disable", json={})
    assert r.status_code == status and r.json()["error"] == code
    assert r.json()["message"] == admin_api.REFUSALS[code][1]


def test_sqlstate_mapping_covers_the_function_codes() -> None:
    assert admin_api.SQLSTATES == {"YTA01": "admin_only", "YTA02": "user_not_found", "YTA03": "self_protected",
                                   "YTA04": "last_admin", "YTA05": "bad_input", "YTA06": "confirm_mismatch",
                                   "23505": "user_exists", "23514": "bad_input", "22003": "bad_input"}


# ---- 로컬판 ------------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", ROUTES + [("GET", "/api/me", None)])
def test_local_edition_has_no_admin_routes(local_client: Any, method: str, path: str, body: Any) -> None:
    status, _ = local_client.json(method, path, body if method == "POST" else None)
    assert status == 404


# ---- MinIO 정리·작업 멈춤(단위) ----------------------------------------------------------------

class MemBucket:
    def __init__(self, names: list[str]) -> None:
        self.names = set(names)
        self.fail = False
        self.stuck: set[str] = set()                 # names that survive delete_many (a partial failure)

    def keys(self, prefix: str) -> list[str]:
        if self.fail:
            raise OSError("s3 down")
        return sorted(n for n in self.names if n.startswith(prefix))

    def delete_many(self, s3_keys: list[str]) -> None:
        self.names -= set(s3_keys) - self.stuck


def test_purge_removes_rows_keys_and_orphans_under_the_users_prefixes_only() -> None:
    other = uuid.UUID(int=43)
    bucket = MemBucket([f"users/{TARGET}/a.png", f"users/{TARGET}/orphan", f"trash/{TARGET}/s/b.png",
                        f"users/{other}/a.png", f"trash/{other}/s/c.png", "shared/videos/v/raw.json"])
    out = admin_api.purge_user_objects(bucket, TARGET, [f"users/{TARGET}/a.png", f"users/{other}/a.png",
                                                        "shared/videos/v/raw.json"])
    assert out == {"ok": True, "removed": 3, "left": 0}
    assert bucket.names == {f"users/{other}/a.png", f"trash/{other}/s/c.png", "shared/videos/v/raw.json"}


def test_purge_reports_and_logs_what_is_left(caplog: pytest.LogCaptureFixture) -> None:
    bucket = MemBucket([f"users/{TARGET}/a.png", f"users/{TARGET}/b.png"])
    bucket.stuck = {f"users/{TARGET}/b.png"}
    assert admin_api.purge_user_objects(bucket, TARGET, []) == {"ok": False, "removed": 1, "left": 1}
    bucket.fail = True
    assert admin_api.purge_user_objects(bucket, TARGET, [f"users/{TARGET}/b.png"]) == {
        "ok": False, "removed": 0, "left": 1}
    assert "S10" in caplog.text


def test_registry_drop_cancels_and_forgets_only_that_users_jobs() -> None:
    class Job:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

        def join(self, timeout: float | None) -> None:
            return None

    class Manager:
        def __init__(self) -> None:
            self.jobs = [Job()]

        def cancel_all(self) -> list[Job]:
            for job in self.jobs:
                job.cancel()
            return self.jobs

    reg = Registry(factory=Manager)  # type: ignore[arg-type]
    mine, other = reg.for_user(TARGET), reg.for_user(uuid.UUID(int=43))
    assert reg.drop(TARGET) == 1 and mine.jobs[0].cancelled and not other.jobs[0].cancelled  # type: ignore[attr-defined]
    assert reg.for_user(TARGET) is not mine and reg.drop(uuid.UUID(int=99)) == 0
