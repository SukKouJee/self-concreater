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

from scripts import keys  # noqa: E402
from server import admin_api, passwords  # noqa: E402
from server.pgstore import PgTrash  # noqa: E402
from server.s3store import S3ObjectStore, S3SharedCache  # noqa: E402
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
        admin_api.PgAdmin(stack.backend.db).list_page(stack.users["bob@example.com"], admin_api.UserQuery())
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
    # this stack has no Google login, so an account without a password could never log in
    assert root.post("/api/admin/users", json={"email": "g@example.com"}).json()["error"] == "password_required"
    dup = root.post("/api/admin/users", json={"email": "new@example.com", "password": NEW_PW})
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


# ---- 0004: 만들기 → 고치기 → 지우기 --------------------------------------------------------------

def test_login_records_last_login_and_the_list_pages_and_filters(stack: AsgiStack) -> None:
    root = stack.login("root@example.com")
    for i in range(3):
        stack.login(f"user{i}@example.com")
    body = root.get("/api/admin/users?per_page=2&sort=email").json()
    assert (body["total"], body["pages"], len(body["users"])) == (4, 2, 2)
    assert [u["email"] for u in root.get("/api/admin/users?per_page=2&page=2").json()["users"]] == [
        "user1@example.com", "user2@example.com"]
    assert all(u["last_login_at"] for u in body["users"])                   # dev login stamped every row
    assert [u["email"] for u in root.get("/api/admin/users?role=admin").json()["users"]] == ["root@example.com"]
    assert root.get("/api/admin/users?q=USER2").json()["total"] == 1
    assert root.get("/api/admin/users?sort=nope").status_code == 400


def test_edit_email_unpins_google_ends_sessions_and_keeps_the_data(stack: AsgiStack) -> None:
    root = stack.login("root@example.com")
    bob = stack.login("bob@example.com")
    bob_id = stack.users["bob@example.com"]
    stack.owner_sql("UPDATE users SET google_sub = 'sub-bob' WHERE id = %s", bob_id)
    stack.backend.store(str(bob_id)).write("drafts/a.md", b"# a" + bytes([10]))
    r = root.post(f"/api/admin/users/{bob_id}/update", json={"email": "Robert@Example.com", "display_name": "로버트",
                                                             "daily_usd_limit": "2.5"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "changed": ["email", "display_name", "daily_usd_limit"], "sessions_ended": True}
    assert _owner_one(stack, "SELECT email, google_sub, session_epoch, display_name FROM users WHERE id = %s",
                      bob_id) == ("robert@example.com", None, 1, "로버트")
    assert bob.get("/api/ping").status_code == 401                         # old session is gone
    again = stack.client()
    assert again.post("/auth/dev", json={"email": "robert@example.com"}).status_code == 200
    assert again.get("/api/ping").json()["user"] == "robert@example.com"
    detail = root.get(f"/api/admin/users/{bob_id}").json()["user"]
    assert detail["email"] == "robert@example.com" and detail["daily_usd_limit"] == 2.5 and detail["has_google"] is False
    assert detail["docs"] == 1 and detail["storage_bytes"] > 0                # the data stays with the account
    actions = [e["action"] for e in root.get("/api/admin/audit").json()["entries"]]
    assert actions == ["set_limit", "set_email", "set_name"]
    # one refusal rolls the whole edit back
    stack.login("cat@example.com")
    r = root.post(f"/api/admin/users/{bob_id}/update", json={"display_name": "X", "email": "cat@example.com"})
    assert r.status_code == 409 and r.json()["error"] == "user_exists"
    assert _owner_one(stack, "SELECT display_name FROM users WHERE id = %s", bob_id) == ("로버트",)


def test_delete_removes_the_users_rows_and_minio_objects_only(stack: AsgiStack) -> None:
    root = stack.login("root@example.com")
    stack.login("bob@example.com")
    stack.login("cat@example.com")
    bob_id, cat_id = stack.users["bob@example.com"], stack.users["cat@example.com"]
    db, bucket = stack.backend.db, stack.backend.bucket
    S3ObjectStore(db, bucket, bob_id).put("drafts/bob.png", b"bob")
    S3ObjectStore(db, bucket, bob_id).put("drafts/old.png", b"old")
    PgTrash(db, bucket, bob_id).move(["drafts/old.png"], "drafts/old.png")
    bucket.put(f"users/{bob_id}/orphan.bin", b"no row")                    # an orphan from an earlier failure
    S3ObjectStore(db, bucket, cat_id).put("drafts/cat.png", b"cat")
    S3SharedCache(db, bucket, bob_id).put(keys.raw("vid12345678"), b"{}")
    stack.backend.store(str(bob_id)).write("drafts/a.md", b"# a" + bytes([10]))
    stack.owner_sql("INSERT INTO llm_usage (user_id, model, input_tokens, output_tokens, cost_usd, ok) "
                    "VALUES (%s, 'm', 1, 1, 0.5, true)", bob_id)
    shared_before = bucket.keys("shared/")
    assert bucket.keys(f"users/{bob_id}/") and bucket.keys(f"trash/{bob_id}/")

    wrong = root.post(f"/api/admin/users/{bob_id}/delete", json={"confirm_email": "cat@example.com"})
    assert wrong.status_code == 400 and wrong.json()["error"] == "confirm_mismatch"
    assert root.post(f"/api/admin/users/{stack.users['root@example.com']}/delete",
                     json={"confirm_email": "root@example.com"}).json()["error"] == "self_delete"

    bob = stack.login("bob@example.com")
    r = root.post(f"/api/admin/users/{bob_id}/delete", json={"confirm_email": "bob@example.com"})
    assert r.status_code == 200, r.text
    assert bob.get("/api/ping").status_code == 401
    out = r.json()
    assert out["storage"]["ok"] is True and out["storage"]["left"] == 0 and out["storage"]["removed"] == 3
    assert out["deleted"]["objects"] == 1 and out["deleted"]["trash"] == 1 and out["deleted"]["docs"] >= 1
    assert bucket.keys(f"users/{bob_id}/") == [] and bucket.keys(f"trash/{bob_id}/") == []
    assert bucket.keys(f"users/{cat_id}/") == [f"users/{cat_id}/drafts/cat.png"]
    assert bucket.keys("shared/") == shared_before                       # shared cache untouched
    assert _owner_one(stack, "SELECT count(*) FROM users WHERE id = %s", bob_id) == (0,)
    assert _owner_one(stack, "SELECT count(*) FROM shared_objects") == (1,)
    assert _owner_one(stack, "SELECT count(*), sum(cost_usd) FROM llm_usage WHERE user_id IS NULL")[0] == 1
    assert [e["action"] for e in root.get("/api/admin/audit?target=bob@").json()["entries"]][:2] == [
        "delete_user", "disable"]                                      # disabled first, in its own transaction
    entry = root.get("/api/admin/audit?action=delete_user").json()["entries"][0]
    assert (entry["actor_email"], entry["target_email"]) == ("root@example.com", "bob@example.com")
    usage = root.get("/api/admin/usage?days=7").json()
    assert [(row["email"], row["cost_usd"]) for row in usage["rows"]] == [(None, 0.5)]
    assert stack.client().post("/auth/dev", json={"email": "bob@example.com"}).status_code == 403
    overview = root.get("/api/admin/overview").json()
    assert overview["counts"]["users"] == 2 and overview["spend"]["month_deleted_usd"] == 0.5
