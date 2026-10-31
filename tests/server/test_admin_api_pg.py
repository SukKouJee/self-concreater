"""관리 API를 실제 PG 16 + MinIO 위의 서버 앱으로(T16): 앱 롤이 0003 함수만으로 사용자를 관리하고, 세션·감사 기록이 따라온다.

PG 환경이 없으면 skip(`tests/helpers/pg.py`).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

pytest.importorskip("starlette")

import psycopg  # noqa: E402

from server import admin_api, passwords  # noqa: E402
from tests.server.conftest import AsgiStack  # noqa: E402

NEW_PW = "a brand new passphrase"


@pytest.fixture
def stack(asgi_stack: AsgiStack) -> AsgiStack:
    asgi_stack.login("root@example.com")
    asgi_stack.owner_sql("UPDATE users SET is_admin = true WHERE email = 'root@example.com'")
    return asgi_stack


def _owner_one(stack: AsgiStack, sql: str, *params: Any) -> tuple[Any, ...]:
    with psycopg.connect(stack.owner_dsn) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return tuple(row)


def _users(c: Any) -> dict[str, dict[str, Any]]:
    r = c.get("/api/admin/users")
    assert r.status_code == 200, r.text
    return {u["email"]: u for u in r.json()["users"]}


def test_admin_lists_users_and_non_admin_is_refused_by_app_and_db(stack: AsgiStack) -> None:
    root = stack.login("root@example.com")
    bob = stack.login("bob@example.com")
    rows = _users(root)
    assert set(rows) == {"root@example.com", "bob@example.com"}
    assert rows["root@example.com"]["is_self"] is True and rows["root@example.com"]["is_admin"] is True
    assert rows["bob@example.com"]["effective_daily_usd"] == float(stack.backend.daily_usd)
    assert bob.get("/api/admin/users").status_code == 403
    assert root.get("/api/me").json() == {"email": "root@example.com", "is_admin": True}
    with pytest.raises(admin_api.AdminError) as e:                     # the database refuses on its own as well
        admin_api.PgAdmin(stack.backend.db).list_users(stack.users["bob@example.com"])
    assert e.value.code == "admin_only"


def test_invite_with_password_stores_only_a_hash_and_audits(stack: AsgiStack) -> None:
    root = stack.login("root@example.com")
    r = root.post("/api/admin/users", json={"email": "New@Example.com", "display_name": "새 사람",
                                            "daily_usd_limit": "2.5", "password": NEW_PW, "is_admin": False})
    assert r.status_code == 200 and NEW_PW not in r.text and "scrypt" not in r.text
    new_id = r.json()["id"]
    stored, limit = _owner_one(stack, "SELECT password_hash, daily_usd_limit FROM users WHERE id = %s", new_id)
    assert passwords.verify_password(NEW_PW, stored) and float(limit) == 2.5
    row = _users(root)["new@example.com"]
    assert (row["has_password"], row["has_google"], row["display_name"]) == (True, False, "새 사람")
    dup = root.post("/api/admin/users", json={"email": "new@example.com"})
    assert dup.status_code == 409 and dup.json()["error"] == "user_exists"
    (entry,) = root.get("/api/admin/audit").json()["entries"]
    assert (entry["actor_email"], entry["action"], entry["target_email"]) == ("root@example.com", "add_user",
                                                                             "new@example.com")
    everything = json.dumps([root.get("/api/admin/users").json(), root.get("/api/admin/audit").json()])
    assert "scrypt" not in everything and NEW_PW not in everything


def test_password_reset_and_disable_end_the_targets_sessions(stack: AsgiStack) -> None:
    root = stack.login("root@example.com")
    bob = stack.login("bob@example.com")
    bob_id = stack.users["bob@example.com"]
    assert root.post(f"/api/admin/users/{bob_id}/password", json={"password": NEW_PW}).status_code == 200
    assert bob.get("/api/ping").status_code == 401
    (stored,) = _owner_one(stack, "SELECT password_hash FROM users WHERE id = %s", bob_id)
    assert passwords.verify_password(NEW_PW, stored)

    bob = stack.login("bob@example.com")
    assert root.post(f"/api/admin/users/{bob_id}/disable", json={}).status_code == 200
    assert bob.get("/api/ping").status_code == 401
    assert stack.client().post("/auth/dev", json={"email": "bob@example.com"}).status_code == 403
    assert root.post(f"/api/admin/users/{bob_id}/enable", json={}).status_code == 200
    assert stack.login("bob@example.com").get("/api/ping").status_code == 200

    bob = stack.login("bob@example.com")
    assert root.post(f"/api/admin/users/{bob_id}/logout-all", json={}).status_code == 200
    assert bob.get("/api/ping").status_code == 401
    assert root.post(f"/api/admin/users/{bob_id}/clear-password", json={}).status_code == 200
    assert _owner_one(stack, "SELECT password_hash FROM users WHERE id = %s", bob_id) == (None,)
    actions = [e["action"] for e in root.get("/api/admin/audit").json()["entries"]]
    assert actions == ["clear_password", "logout_all", "enable", "disable", "set_password"]


def test_self_protection_and_last_admin_reach_the_screen_as_409(stack: AsgiStack) -> None:
    root = stack.login("root@example.com")
    root_id = stack.users["root@example.com"]
    for path, body in (("disable", {}), ("admin", {"is_admin": False})):
        r = root.post(f"/api/admin/users/{root_id}/{path}", json=body)
        assert r.status_code == 409 and r.json()["error"] == "self_protected"
    assert root.post(f"/api/admin/users/{uuid.uuid4()}/disable", json={}).status_code == 404
    assert _owner_one(stack, "SELECT is_admin, disabled FROM users WHERE id = %s", root_id) == (True, False)


def test_limit_and_admin_toggle_and_usage(stack: AsgiStack) -> None:
    root = stack.login("root@example.com")
    bob = stack.login("bob@example.com")
    bob_id = stack.users["bob@example.com"]
    assert root.post(f"/api/admin/users/{bob_id}/limit", json={"daily_usd_limit": 3.25}).status_code == 200
    assert _users(root)["bob@example.com"]["daily_usd_limit"] == 3.25
    assert root.post(f"/api/admin/users/{bob_id}/limit", json={"daily_usd_limit": None}).status_code == 200
    assert _users(root)["bob@example.com"]["daily_usd_limit"] is None

    assert root.post(f"/api/admin/users/{bob_id}/admin", json={"is_admin": True}).status_code == 200
    assert bob.get("/api/admin/users").status_code == 200                       # the same session, next request
    assert bob.post(f"/api/admin/users/{stack.users['root@example.com']}/admin",
                    json={"is_admin": False}).status_code == 200
    assert root.get("/api/admin/users").status_code == 403

    stack.owner_sql("INSERT INTO llm_usage (user_id, model, input_tokens, output_tokens, cost_usd, ok) "
                    "VALUES (%s, 'm', 100, 50, 0.125, true)", bob_id)
    usage = bob.get("/api/admin/usage?days=7").json()
    assert usage["days"] == 7 and usage["total_usd"] == 0.125
    assert [(r["email"], r["calls"], r["cost_usd"]) for r in usage["rows"]] == [("bob@example.com", 1, 0.125)]
    assert _users(bob)["bob@example.com"]["today_usd"] == 0.125
