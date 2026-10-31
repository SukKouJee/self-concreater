"""`PgUsers`의 Google sub 고정(서버 설계 §4.2, T12)을 실제 PG 16 앱 롤(`ytc_app`, users에 UPDATE(google_sub)만)로 확인한다.

PG 환경이 없으면 skip(`tests/helpers/pg.py`).
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from tests.helpers.pg import pg_user

pytest.importorskip("starlette")

from server import auth  # noqa: E402


@pytest.fixture
def users(pg_db) -> auth.PgUsers:
    return auth.PgUsers(pg_db)


def _sub_of(owner_dsn: str, uid: uuid.UUID) -> str | None:
    with psycopg.connect(owner_dsn) as conn:
        row = conn.execute("SELECT google_sub FROM users WHERE id = %s", (uid,)).fetchone()
    assert row is not None
    return row[0]


def test_first_login_pins_the_sub_and_later_pins_keep_it(users, migrated_db):
    uid = pg_user(migrated_db.owner_dsn, "a@example.com")
    row = users.by_email("A@example.com")
    assert row is not None and row.google_sub is None
    pinned = users.pin_google_sub(uid, "sub-a")
    assert pinned is not None and pinned.google_sub == "sub-a"
    again = users.pin_google_sub(uid, "sub-other")          # already pinned: unchanged, caller compares
    assert again is not None and again.google_sub == "sub-a"
    assert _sub_of(migrated_db.owner_dsn, uid) == "sub-a"
    found = users.by_id(uid)
    assert found is not None and found.google_sub == "sub-a"


def test_sub_held_by_another_user_is_refused(users, migrated_db):
    a = pg_user(migrated_db.owner_dsn, "a@example.com")
    b = pg_user(migrated_db.owner_dsn, "b@example.com")
    users.pin_google_sub(a, "sub-a")
    with pytest.raises(auth.LoginDenied) as e:
        users.pin_google_sub(b, "sub-a")
    assert e.value.reason == "sub_taken"
    assert _sub_of(migrated_db.owner_dsn, b) is None


def test_pin_for_a_missing_user_returns_none(users):
    assert users.pin_google_sub(uuid.uuid4(), "sub-x") is None
