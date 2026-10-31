"""관리 CLI 0004 짝(`delete-user`, `set-email`, `set-name`): owner 경로로 내부 함수(`admin_apply_*`)를 부르고
감사 기록은 행위자 NULL·`via: cli`. 지우기는 MinIO `users/{uid}/`·`trash/{uid}/`까지 지운다(공유 캐시는 그대로).
PG 없이 도는 값 검사와, PG 16 + MinIO 위의 동작(환경이 없으면 skip).
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from scripts import keys
from scripts.common import ScriptError
from server import admin
from server.db import Db
from server.s3store import S3Bucket, S3ObjectStore, S3SharedCache
from tests.helpers.pg import Stack, TestDb, pg_user


@pytest.mark.parametrize("argv", [["delete-user", "a@example.com", "--confirm", "b@example.com"],
                                  ["delete-user", "a@example.com", "--confirm", "nope"]])
def test_delete_user_needs_the_same_email_typed_twice_before_the_db(argv: list[str]) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(argv, {})
    assert e.value.code in ("confirm_mismatch", "bad_email")


def test_delete_user_without_confirm_is_a_usage_error() -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(["delete-user", "a@example.com"], {})
    assert e.value.code != "config_missing"


@pytest.mark.parametrize("argv", [["set-email", "a@example.com", "b@example.com"], ["set-name", "a@example.com", "x"],
                                  ["delete-user", "a@example.com", "--confirm", "A@Example.com"]])
def test_new_commands_need_the_owner_dsn(argv: list[str]) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(argv, {})
    assert e.value.code == "config_missing"


def test_set_name_checks_length_before_the_db() -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(["set-name", "a@example.com", "x" * 101], {})
    assert e.value.code == "bad_name"


# --- PG ---------------------------------------------------------------------------------------

@pytest.fixture
def env(migrated_db: TestDb, pg_stack: Stack, s3_bucket: Any) -> dict[str, str]:
    return {"YTC_DB_DSN": migrated_db.app_dsn, "YTC_DB_OWNER_DSN": migrated_db.owner_dsn,
            "YTC_S3_ENDPOINT": pg_stack.s3_endpoint, "YTC_S3_ACCESS_KEY": pg_stack.s3_access_key,
            "YTC_S3_SECRET_KEY": pg_stack.s3_secret_key, "YTC_S3_BUCKET": s3_bucket.name}


def _owner(env: dict[str, str], sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(env["YTC_DB_OWNER_DSN"]) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _audit(env: dict[str, str]) -> list[tuple[Any, ...]]:
    return _owner(env, "SELECT actor_id, action, detail, target_email FROM admin_audit ORDER BY id")


def test_set_email_unpins_google_bumps_epoch_and_audits_as_cli(env: dict[str, str]) -> None:
    uid = pg_user(env["YTC_DB_OWNER_DSN"], "a@example.com")
    _owner(env, "UPDATE users SET google_sub = 's' WHERE id = %s", uid)
    out = admin.main(["set-email", "A@example.com", " New@Example.com "], env)
    assert out == {"id": str(uid), "email": "new@example.com", "previous": "a@example.com", "session_epoch": 1}
    assert _owner(env, "SELECT email, google_sub, session_epoch FROM users WHERE id = %s", uid) == [
        ("new@example.com", None, 1)]
    ((actor, action, detail, _),) = _audit(env)
    assert (actor, action, detail) == (None, "set_email", {"from": "a@example.com", "to": "new@example.com",
                                                           "google_unlinked": True, "via": "cli"})


def test_set_email_refuses_taken_and_unknown(env: dict[str, str]) -> None:
    pg_user(env["YTC_DB_OWNER_DSN"], "a@example.com")
    pg_user(env["YTC_DB_OWNER_DSN"], "b@example.com")
    with pytest.raises(ScriptError) as e:
        admin.main(["set-email", "a@example.com", "b@example.com"], env)
    assert e.value.code == "user_exists"
    with pytest.raises(ScriptError) as e:
        admin.main(["set-email", "ghost@example.com", "c@example.com"], env)
    assert e.value.code == "user_not_found"
    assert _audit(env) == []


def test_set_name_sets_and_clears(env: dict[str, str]) -> None:
    uid = pg_user(env["YTC_DB_OWNER_DSN"], "a@example.com")
    assert admin.main(["set-name", "a@example.com", "  지수 "], env)["display_name"] == "지수"
    assert admin.main(["set-name", "a@example.com", ""], env)["display_name"] is None
    assert _owner(env, "SELECT display_name FROM users WHERE id = %s", uid) == [(None,)]
    assert [(a[1], a[2]) for a in _audit(env)] == [("set_name", {"from": None, "to": "지수", "via": "cli"}),
                                                    ("set_name", {"from": "지수", "to": None, "via": "cli"})]


def test_delete_user_removes_rows_and_objects_and_keeps_others(env: dict[str, str], s3_bucket: S3Bucket) -> None:
    owner = env["YTC_DB_OWNER_DSN"]
    a, b = pg_user(owner, "a@example.com"), pg_user(owner, "b@example.com")
    with Db(env["YTC_DB_DSN"], owner, max_size=2) as db:
        S3ObjectStore(db, s3_bucket, a).put("drafts/a.png", b"a")
        S3ObjectStore(db, s3_bucket, b).put("drafts/b.png", b"b")
        S3SharedCache(db, s3_bucket, a).put(keys.raw("vid12345678"), b"{}")
    s3_bucket.put(f"trash/{a}/s/x.png", b"orphan")
    _owner(env, "INSERT INTO llm_usage (user_id, model, input_tokens, output_tokens, cost_usd, ok) "
                "VALUES (%s, 'm', 1, 1, 0.5, true)", a)

    out = admin.main(["delete-user", "a@example.com", "--confirm", "A@Example.com"], env)

    assert out["email"] == "a@example.com" and out["deleted"]["objects"] == 1
    assert out["storage"] == {"ok": True, "removed": 2, "left": 0}
    assert s3_bucket.keys(f"users/{a}/") == [] and s3_bucket.keys(f"trash/{a}/") == []
    assert s3_bucket.keys(f"users/{b}/") == [f"users/{b}/drafts/b.png"] and s3_bucket.keys("shared/")
    assert _owner(env, "SELECT email FROM users") == [("b@example.com",)]
    assert _owner(env, "SELECT count(*) FROM llm_usage WHERE user_id IS NULL") == [(1,)]
    ((actor, action, detail, target_email),) = _audit(env)
    assert (actor, action, target_email, detail["via"], detail["email"]) == (
        None, "delete_user", "a@example.com", "cli", "a@example.com")
    with pytest.raises(ScriptError) as e:
        admin.main(["delete-user", "a@example.com", "--confirm", "a@example.com"], env)
    assert e.value.code == "user_not_found"


def test_delete_user_reports_admins_left(env: dict[str, str]) -> None:
    owner = env["YTC_DB_OWNER_DSN"]
    root = pg_user(owner, "root@example.com")
    _owner(env, "UPDATE users SET is_admin = true WHERE id = %s", root)
    out = admin.main(["delete-user", "root@example.com", "--confirm", "root@example.com"], env)
    assert out["admins"] == 0                                    # the operator's escape hatch, like revoke-admin
    assert uuid.UUID(out["id"]) == root
