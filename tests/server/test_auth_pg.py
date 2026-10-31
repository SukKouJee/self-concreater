"""`PgUsers`의 Google sub 고정(서버 설계 §4.2, T12)을 실제 PG 16 앱 롤(`ytc_app`, users에 UPDATE(google_sub)만)로 확인한다.

PG 환경이 없으면 skip(`tests/helpers/pg.py`).
"""
from __future__ import annotations

import threading
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
    pinned = users.pin_google_sub(uid, "sub-a", "a@example.com")
    assert pinned is not None and pinned.google_sub == "sub-a"
    again = users.pin_google_sub(uid, "sub-other", "a@example.com")          # already pinned: unchanged, caller compares
    assert again is not None and again.google_sub == "sub-a"
    assert _sub_of(migrated_db.owner_dsn, uid) == "sub-a"
    found = users.by_id(uid)
    assert found is not None and found.google_sub == "sub-a"


def test_sub_held_by_another_user_is_refused(users, migrated_db):
    a = pg_user(migrated_db.owner_dsn, "a@example.com")
    b = pg_user(migrated_db.owner_dsn, "b@example.com")
    users.pin_google_sub(a, "sub-a", "a@example.com")
    with pytest.raises(auth.LoginDenied) as e:
        users.pin_google_sub(b, "sub-a", "b@example.com")
    assert e.value.reason == "sub_taken"
    assert _sub_of(migrated_db.owner_dsn, b) is None


def test_pin_for_a_missing_user_returns_none(users):
    assert users.pin_google_sub(uuid.uuid4(), "sub-x", "x@example.com") is None


def test_pin_does_nothing_when_the_email_changed_since_the_lookup(users, migrated_db):
    """admin_set_email between by_email and pin: the old address must not bind a Google account to the renamed row."""
    uid = pg_user(migrated_db.owner_dsn, "a@example.com")
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        conn.execute("UPDATE users SET email = 'renamed@example.com' WHERE id = %s", (uid,))
    row = users.pin_google_sub(uid, "sub-a", "a@example.com")
    assert row is not None and row.google_sub is None and row.email == "renamed@example.com"
    assert _sub_of(migrated_db.owner_dsn, uid) is None


def _race(users: auth.PgUsers, uid: uuid.UUID, subs: tuple[str, ...],
          email: str = "a@example.com") -> dict[str, str | None]:
    """subs마다 스레드 하나가 같은 순간에 pin_google_sub. 스레드의 예외는 여기서 다시 올린다."""
    barrier = threading.Barrier(len(subs))
    seen: dict[str, str | None] = {}
    errors: list[Exception] = []

    def pin(sub: str) -> None:
        try:
            barrier.wait(timeout=10)
            row = users.pin_google_sub(uid, sub, email)
            seen[sub] = None if row is None else row.google_sub
        except Exception as e:  # noqa: BLE001 - collected and re-raised in the calling thread
            errors.append(e)

    threads = [threading.Thread(target=pin, args=(s,)) for s in subs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    if errors:
        raise errors[0]
    return seen


def test_concurrent_first_logins_with_two_subs_pin_exactly_one(users, migrated_db):
    uid = pg_user(migrated_db.owner_dsn, "a@example.com")
    for _ in range(5):
        with psycopg.connect(migrated_db.owner_dsn) as conn:
            conn.execute("UPDATE users SET google_sub = NULL WHERE id = %s", (uid,))
        seen = _race(users, uid, ("sub-1", "sub-2"))
        stored = _sub_of(migrated_db.owner_dsn, uid)
        assert stored in {"sub-1", "sub-2"}
        assert seen == {"sub-1": stored, "sub-2": stored}    # both see the winner; admit_google refuses the loser
