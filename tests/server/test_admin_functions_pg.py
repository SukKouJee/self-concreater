"""관리 함수(Alembic `0003`, T16): owner 소유 SECURITY DEFINER 함수가 호출자(`app.user_id`)가 활성 관리자인지 먼저 확인하고
(없거나 틀리면 거부 = fail closed), 사용자 관리·사용량·감사 기록을 한다. 앱 롤은 이 함수들로만 users를 바꾸고
admin_audit을 읽고 쓴다(표 직접 권한 없음).

DB를 직접 두드린다(앱 롤 `Db.user_tx(uid)` = 요청 경로와 같은 연결). PG 환경이 없으면 skip.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from tests.server.conftest import TestDb

psycopg = pytest.importorskip("psycopg")

from server.db import Db  # noqa: E402

DENIED = psycopg.errors.InsufficientPrivilege
HASH = "scrypt$32768$8$1$c2FsdA$aGFzaA"
FUNCTIONS = {
    "admin_list_users()", "admin_add_user(text, text, numeric, text, boolean)", "admin_set_password(uuid, text)",
    "admin_clear_password(uuid)", "admin_set_disabled(uuid, boolean)", "admin_logout_all(uuid)",
    "admin_set_limit(uuid, numeric)", "admin_set_admin(uuid, boolean)", "admin_usage(integer)",
    "admin_audit_log(integer)",
}
SIGNATURES = {sig.replace(", ", ",") for sig in FUNCTIONS}          # regprocedure text has no spaces


def _sqlstate(exc: pytest.ExceptionInfo[Any]) -> str | None:
    return getattr(exc.value, "sqlstate", None)


@pytest.fixture
def people(migrated_db: TestDb) -> dict[str, uuid.UUID]:
    """owner로: 관리자 root, 일반 사용자 bob(비밀번호·Google 연결), 비활성 관리자 old."""
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        def add(email: str, **cols: Any) -> uuid.UUID:
            names = ["email", *cols]
            row = conn.execute(f"INSERT INTO users ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) "
                               "RETURNING id", (email, *cols.values())).fetchone()
            assert row is not None
            return uuid.UUID(str(row[0]))
        return {"root": add("root@example.com", is_admin=True, display_name="루트"),
                "bob": add("bob@example.com", password_hash=HASH, google_sub="sub-bob", daily_usd_limit=2),
                "old": add("old@example.com", is_admin=True, disabled=True)}


@pytest.fixture
def db(migrated_db: TestDb) -> Iterator[Db]:
    with Db(migrated_db.app_dsn, migrated_db.owner_dsn, max_size=4) as d:
        yield d


def _call(db: Db, actor: uuid.UUID | None, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    if actor is None:
        with db.public_tx() as conn:
            return conn.execute(sql, params).fetchall()
    with db.user_tx(actor) as conn:
        return conn.execute(sql, params).fetchall()


def _owner(dsn: str, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, params).fetchall()


def _user(dsn: str, uid: uuid.UUID) -> dict[str, Any]:
    row = _owner(dsn, "SELECT is_admin, disabled, session_epoch, password_hash, daily_usd_limit FROM users "
                      "WHERE id = %s", uid)[0]
    return dict(zip(("is_admin", "disabled", "epoch", "hash", "limit"), row, strict=True))


def _audit(dsn: str) -> list[tuple[Any, ...]]:
    return _owner(dsn, "SELECT actor_id, action, target_user_id, detail FROM admin_audit ORDER BY id")


ALL_CALLS = [
    ("SELECT * FROM admin_list_users()", ()),
    ("SELECT admin_add_user('new@example.com', NULL, NULL, NULL, false)", ()),
    ("SELECT admin_set_password(%s, %s)", ("bob", HASH)),
    ("SELECT admin_clear_password(%s)", ("bob",)),
    ("SELECT admin_set_disabled(%s, true)", ("bob",)),
    ("SELECT admin_logout_all(%s)", ("bob",)),
    ("SELECT admin_set_limit(%s, 5)", ("bob",)),
    ("SELECT admin_set_admin(%s, true)", ("bob",)),
    ("SELECT * FROM admin_usage(7)", ()),
    ("SELECT * FROM admin_audit_log(50)", ()),
]


def _params(people: dict[str, uuid.UUID], params: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(people.get(p, p) if isinstance(p, str) else p for p in params)


# ---- 스키마·권한 -------------------------------------------------------------------------

def test_functions_are_owner_owned_security_definer_with_pinned_search_path(migrated_db: TestDb) -> None:
    rows = _owner(migrated_db.owner_dsn, """
        SELECT p.oid::regprocedure::text, p.prosecdef, r.rolname, p.proconfig
          FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
         WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'admin\\_%%'""")
    public = {r[0]: r for r in rows if r[0] in SIGNATURES}
    assert set(public) == SIGNATURES
    for sig, secdef, owner, config in public.values():
        assert secdef, sig
        assert owner == "ytc_owner", sig
        assert config == ["search_path=pg_catalog, public, pg_temp"], sig


def test_app_role_may_execute_only_the_admin_functions(migrated_db: TestDb) -> None:
    rows = _owner(migrated_db.owner_dsn, """
        SELECT p.oid::regprocedure::text, has_function_privilege(%s, p.oid, 'EXECUTE'),
               has_function_privilege('public', p.oid, 'EXECUTE')
          FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'admin\\_%%'""",
                  migrated_db.app_role)
    for sig, app, public in rows:
        assert not public, sig                                  # never PUBLIC
        assert app == (sig in SIGNATURES), sig                   # helpers (admin_actor) are not callable by the app


def test_app_role_has_no_direct_access_to_the_audit_table(db: Db, people: dict[str, uuid.UUID]) -> None:
    for stmt in ("SELECT * FROM admin_audit", "INSERT INTO admin_audit (action) VALUES ('x')",
                 "UPDATE admin_audit SET action = 'x'", "DELETE FROM admin_audit"):
        with pytest.raises(DENIED), db.user_tx(people["root"]) as conn:
            conn.execute(stmt)


@pytest.mark.parametrize("stmt", [
    "UPDATE users SET is_admin = true", "UPDATE users SET disabled = true", "UPDATE users SET session_epoch = 9",
    "UPDATE users SET password_hash = NULL", "UPDATE users SET daily_usd_limit = 1",
    "INSERT INTO users (email) VALUES ('x@example.com')", "DELETE FROM users",
])
def test_app_role_still_cannot_write_users_directly_even_as_an_admin(db: Db, people: dict[str, uuid.UUID],
                                                                     stmt: str) -> None:
    with pytest.raises(DENIED), db.user_tx(people["root"]) as conn:
        conn.execute(stmt)


def test_app_role_cannot_call_the_internal_actor_check(db: Db, people: dict[str, uuid.UUID]) -> None:
    with pytest.raises(DENIED), db.user_tx(people["root"]) as conn:
        conn.execute("SELECT admin_actor()")


# ---- fail closed -------------------------------------------------------------------------

@pytest.mark.parametrize("sql, params", ALL_CALLS)
def test_every_function_refuses_without_a_user(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb,
                                               sql: str, params: tuple[Any, ...]) -> None:
    before = _owner(migrated_db.owner_dsn, "SELECT * FROM users ORDER BY id")
    with pytest.raises(psycopg.Error) as e:
        _call(db, None, sql, *_params(people, params))
    assert _sqlstate(e) == "YTA01"
    assert _owner(migrated_db.owner_dsn, "SELECT * FROM users ORDER BY id") == before
    assert _audit(migrated_db.owner_dsn) == []


@pytest.mark.parametrize("who", ["bob", "old", "ghost", "garbage"])
@pytest.mark.parametrize("sql, params", ALL_CALLS)
def test_every_function_refuses_non_admins_disabled_admins_and_unknown_ids(
        db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb, who: str, sql: str, params: tuple[Any, ...]) -> None:
    before = _owner(migrated_db.owner_dsn, "SELECT * FROM users ORDER BY id")
    with pytest.raises(psycopg.Error) as e:
        if who == "garbage":
            with db.public_tx() as conn:
                conn.execute("SELECT set_config('app.user_id', 'not-a-uuid', true)")
                conn.execute(sql, _params(people, params))
        else:
            _call(db, people.get(who, uuid.uuid4()), sql, *_params(people, params))
    assert _sqlstate(e) == "YTA01"
    assert _owner(migrated_db.owner_dsn, "SELECT * FROM users ORDER BY id") == before
    assert _audit(migrated_db.owner_dsn) == []


# ---- 읽기 --------------------------------------------------------------------------------

def test_list_users_shows_status_login_methods_and_todays_spend(db: Db, people: dict[str, uuid.UUID],
                                                                migrated_db: TestDb) -> None:
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        for cost, at in ((0.25, "now()"), (0.5, "now()"), (9, "now() - interval '2 days'")):
            conn.execute(f"INSERT INTO llm_usage (user_id, at, model, input_tokens, output_tokens, cost_usd, ok) "
                         f"VALUES (%s, {at}, 'm', 1, 1, %s, true)", (people["bob"], cost))
    with db.user_tx(people["root"]) as conn:
        cur = conn.execute("SELECT * FROM admin_list_users()")
        cols = [c.name for c in cur.description or []]
        rows = {r[1]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}
    assert cols == ["id", "email", "display_name", "is_admin", "disabled", "has_password", "has_google",
                    "daily_usd_limit", "today_usd", "created_at"]
    assert list(rows) == ["bob@example.com", "old@example.com", "root@example.com"]       # by email
    bob = rows["bob@example.com"]
    assert (bob["has_password"], bob["has_google"], bob["is_admin"], bob["disabled"]) == (True, True, False, False)
    assert float(bob["daily_usd_limit"]) == 2.0 and float(bob["today_usd"]) == 0.75
    root = rows["root@example.com"]
    assert (root["display_name"], root["has_password"], root["has_google"], root["is_admin"]) == (
        "루트", False, False, True)
    assert root["daily_usd_limit"] is None and float(root["today_usd"]) == 0
    assert "password_hash" not in cols and "google_sub" not in cols
    assert _audit(migrated_db.owner_dsn) == []                       # reads are not audited


def test_usage_sums_per_user_and_day_and_checks_days(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        for uid, cost, ok, at in ((people["bob"], 0.25, True, "now()"), (people["bob"], 0.5, False, "now()"),
                                  (people["root"], 1, True, "now() - interval '3 days'"),
                                  (people["root"], 7, True, "now() - interval '30 days'")):
            conn.execute(f"INSERT INTO llm_usage (user_id, at, model, input_tokens, output_tokens, cost_usd, ok) "
                         f"VALUES (%s, {at}, 'm', 10, 20, %s, %s)", (uid, cost, ok))
    rows = _call(db, people["root"], "SELECT email, calls, failed, input_tokens, output_tokens, cost_usd "
                                     "FROM admin_usage(7) ORDER BY day, email")
    assert [(r[0], r[1], r[2], r[3], r[4], float(r[5])) for r in rows] == [
        ("root@example.com", 1, 0, 10, 20, 1.0), ("bob@example.com", 2, 1, 20, 40, 0.75)]
    for days in (0, 91, -1):
        with pytest.raises(psycopg.Error) as e:
            _call(db, people["root"], "SELECT * FROM admin_usage(%s)", days)
        assert _sqlstate(e) == "YTA05"


# ---- 쓰기 + 감사 -------------------------------------------------------------------------

def test_add_user_creates_the_row_and_audits_without_the_hash(db: Db, people: dict[str, uuid.UUID],
                                                              migrated_db: TestDb) -> None:
    (new_id,), = _call(db, people["root"], "SELECT admin_add_user(%s, %s, %s, %s, %s)",
                       "new@example.com", "새 사람", Decimal("3.5"), HASH, True)
    row = _owner(migrated_db.owner_dsn, "SELECT email, display_name, daily_usd_limit, password_hash, is_admin, "
                                        "disabled "
                                        "FROM users WHERE id = %s", new_id)[0]
    assert (row[0], row[1], float(row[2]), row[3], row[4], row[5]) == (
        "new@example.com", "새 사람", 3.5, HASH, True, False)
    (audit,) = _audit(migrated_db.owner_dsn)
    assert audit[:3] == (people["root"], "add_user", new_id)
    assert audit[3] == {"email": "new@example.com", "is_admin": True, "password": True, "daily_usd_limit": 3.5}
    assert "scrypt" not in str(audit)


def test_add_user_refuses_duplicates_and_bad_values(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        _call(db, people["root"], "SELECT admin_add_user('bob@example.com', NULL, NULL, NULL, false)")
    for args in (("Upper@example.com", None, None, None, False), ("x@example.com", None, None, "plain", False),
                 ("y@example.com", None, -1, None, False)):
        with pytest.raises(psycopg.Error):
            _call(db, people["root"], "SELECT admin_add_user(%s, %s, %s, %s, %s)", *args)
    assert _audit(migrated_db.owner_dsn) == []


@pytest.mark.parametrize("limit", [Decimal("-0.01"), Decimal("1000000"), Decimal("1.234")])
def test_add_user_limit_uses_the_same_rule_as_set_limit(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb,
                                                        limit: Decimal) -> None:
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_add_user('n@example.com', NULL, %s, NULL, false)", limit)
    assert _sqlstate(e) == "YTA05"
    assert _owner(migrated_db.owner_dsn, "SELECT count(*) FROM users WHERE email = 'n@example.com'") == [(0,)]
    assert _audit(migrated_db.owner_dsn) == []


def test_usage_window_edges_follow_kst_days(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    """days=N covers KST today and the N-1 days before it: the first instant of the window counts, the one before not."""
    start = "(date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') - interval '6 days') AT TIME ZONE 'Asia/Seoul'"
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        for at, cost in ((start, 1), (f"{start} - interval '1 microsecond'", 100)):
            conn.execute(f"INSERT INTO llm_usage (user_id, at, model, input_tokens, output_tokens, cost_usd, ok) "
                         f"VALUES (%s, {at}, 'm', 1, 1, %s, true)", (people["bob"], cost))
    rows = _call(db, people["root"], "SELECT cost_usd FROM admin_usage(7)")
    assert [float(r[0]) for r in rows] == [1.0]
    assert sum(float(r[0]) for r in _call(db, people["root"], "SELECT cost_usd FROM admin_usage(8)")) == 101.0


def test_set_and_clear_password_bump_the_epoch(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    bob = people["bob"]
    new_hash = "scrypt$32768$8$1$bmV3$bmV3"
    assert _call(db, people["root"], "SELECT admin_set_password(%s, %s)", bob, new_hash) == [(1,)]
    assert (_user(migrated_db.owner_dsn, bob)["hash"], _user(migrated_db.owner_dsn, bob)["epoch"]) == (new_hash, 1)
    assert _call(db, people["root"], "SELECT admin_clear_password(%s)", bob) == [(2,)]
    assert (_user(migrated_db.owner_dsn, bob)["hash"], _user(migrated_db.owner_dsn, bob)["epoch"]) == (None, 2)
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_set_password(%s, NULL)", bob)
    assert _sqlstate(e) == "YTA05"
    assert [a[1:] for a in _audit(migrated_db.owner_dsn)] == [("set_password", bob, {}), ("clear_password", bob, {})]


def test_disable_bumps_epoch_enable_does_not(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    bob = people["bob"]
    assert _call(db, people["root"], "SELECT admin_set_disabled(%s, true)", bob) == [(1,)]
    assert _user(migrated_db.owner_dsn, bob)["disabled"] is True
    assert _call(db, people["root"], "SELECT admin_set_disabled(%s, false)", bob) == [(1,)]
    assert _user(migrated_db.owner_dsn, bob)["disabled"] is False
    assert [a[1:] for a in _audit(migrated_db.owner_dsn)] == [("disable", bob, {}), ("enable", bob, {})]


def test_logout_all_bumps_epoch_and_limit_and_admin_change(db: Db, people: dict[str, uuid.UUID],
                                                           migrated_db: TestDb) -> None:
    bob, root = people["bob"], people["root"]
    assert _call(db, root, "SELECT admin_logout_all(%s)", bob) == [(1,)]
    _call(db, root, "SELECT admin_set_limit(%s, %s)", bob, Decimal("4.25"))
    assert float(_user(migrated_db.owner_dsn, bob)["limit"]) == 4.25
    _call(db, root, "SELECT admin_set_limit(%s, NULL)", bob)
    assert _user(migrated_db.owner_dsn, bob)["limit"] is None
    for bad in (Decimal("-0.01"), Decimal("1000000"), Decimal("1.234")):
        with pytest.raises(psycopg.Error) as e:
            _call(db, root, "SELECT admin_set_limit(%s, %s)", bob, bad)
        assert _sqlstate(e) == "YTA05"
    _call(db, root, "SELECT admin_set_admin(%s, true)", bob)
    assert _user(migrated_db.owner_dsn, bob)["is_admin"] is True
    assert _user(migrated_db.owner_dsn, bob)["epoch"] == 1                 # limit/admin changes keep sessions
    assert [a[1:] for a in _audit(migrated_db.owner_dsn)] == [
        ("logout_all", bob, {}), ("set_limit", bob, {"from": 2.0, "to": 4.25}),
        ("set_limit", bob, {"from": 4.25, "to": None}), ("set_admin", bob, {"is_admin": True})]


@pytest.mark.parametrize("sql", ["SELECT admin_set_password(%s, 'scrypt$1$1$1$a$b')", "SELECT admin_clear_password(%s)",
                                 "SELECT admin_set_disabled(%s, true)", "SELECT admin_logout_all(%s)",
                                 "SELECT admin_set_limit(%s, 1)", "SELECT admin_set_admin(%s, true)"])
def test_unknown_target_is_user_not_found(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb, sql: str) -> None:
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], sql, uuid.uuid4())
    assert _sqlstate(e) == "YTA02"
    assert _audit(migrated_db.owner_dsn) == []


# ---- 자기 보호·마지막 관리자 -------------------------------------------------------------------

def test_admin_cannot_disable_or_demote_self(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    root = people["root"]
    for sql in ("SELECT admin_set_disabled(%s, true)", "SELECT admin_set_admin(%s, false)"):
        with pytest.raises(psycopg.Error) as e:
            _call(db, root, sql, root)
        assert _sqlstate(e) == "YTA03"
    after = _user(migrated_db.owner_dsn, root)
    assert after["is_admin"] is True and after["disabled"] is False
    # harmless self changes are fine (no-op enable, own limit, own logout)
    _call(db, root, "SELECT admin_set_disabled(%s, false)", root)
    _call(db, root, "SELECT admin_set_admin(%s, true)", root)
    _call(db, root, "SELECT admin_set_limit(%s, 9)", root)


def test_sole_enabled_admin_is_kept_by_self_protection_and_a_demoted_actor_is_refused(
        db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    """Through the functions the last enabled admin can only be removed by themself, which YTA03 refuses first;
    an actor demoted behind their back is no longer an admin (YTA01). YTA04 is the backstop below."""
    root, bob = people["root"], people["bob"]
    _call(db, root, "SELECT admin_set_admin(%s, true)", bob)
    _owner(migrated_db.owner_dsn, "UPDATE users SET is_admin = false WHERE id = %s RETURNING id", root)
    # bob is now the only enabled admin; root was demoted behind his back (owner) and is refused as an actor
    with pytest.raises(psycopg.Error) as e:
        _call(db, root, "SELECT admin_set_admin(%s, false)", bob)
    assert _sqlstate(e) == "YTA01"
    # a disabled admin does not count: making old an admin again (still disabled) leaves bob the last enabled one
    _call(db, bob, "SELECT admin_set_admin(%s, true)", people["old"])
    for sql in ("SELECT admin_set_admin(%s, false)", "SELECT admin_set_disabled(%s, true)"):
        with pytest.raises(psycopg.Error) as e:
            _call(db, bob, sql, bob)
        assert _sqlstate(e) == "YTA03"


def test_keep_one_backstop_refuses_removing_the_only_enabled_admin(people: dict[str, uuid.UUID],
                                                                   migrated_db: TestDb) -> None:
    """admin_keep_one (internal, owner only) is a backstop under the self-protection rule: called directly it refuses."""
    with pytest.raises(psycopg.Error) as e:
        _owner(migrated_db.owner_dsn, "SELECT public.admin_keep_one(%s)", people["root"])
    assert _sqlstate(e) == "YTA04"
    assert _owner(migrated_db.owner_dsn, "SELECT public.admin_keep_one(%s)", people["bob"]) == [("",)]


def test_last_admin_check_counts_enabled_admins(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    root, bob = people["root"], people["bob"]
    _call(db, root, "SELECT admin_set_admin(%s, true)", bob)
    _call(db, root, "SELECT admin_set_disabled(%s, true)", bob)        # two admins -> removing one is fine
    _call(db, root, "SELECT admin_set_disabled(%s, false)", bob)
    _call(db, root, "SELECT admin_set_admin(%s, false)", bob)
    assert _owner(migrated_db.owner_dsn, "SELECT count(*) FROM users WHERE is_admin AND NOT disabled") == [(1,)]


def test_two_admins_removing_each_other_at_once_leave_one(db: Db, people: dict[str, uuid.UUID],
                                                          migrated_db: TestDb) -> None:
    """서로를 동시에 강등해도(각자 '나는 관리자, 남는 관리자 1명'을 본다) 직렬화되어 관리자가 0명이 되지 않는다."""
    root, bob = people["root"], people["bob"]
    _call(db, root, "SELECT admin_set_admin(%s, true)", bob)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def demote(actor: uuid.UUID, target: uuid.UUID, name: str) -> None:
        try:
            with db.user_tx(actor) as conn:
                barrier.wait(timeout=10)
                conn.execute("SELECT admin_set_admin(%s, false)", (target,))
            results[name] = "ok"
        except psycopg.Error as e:
            results[name] = str(e.sqlstate)

    threads = [threading.Thread(target=demote, args=(root, bob, "root")),
               threading.Thread(target=demote, args=(bob, root, "bob"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(results.values()) == ["YTA01", "ok"]
    assert _owner(migrated_db.owner_dsn, "SELECT count(*) FROM users WHERE is_admin AND NOT disabled") == [(1,)]


# ---- 감사 기록 읽기 ------------------------------------------------------------------------

def test_audit_log_lists_newest_first_with_emails_and_checks_limit(db: Db, people: dict[str, uuid.UUID]) -> None:
    root, bob = people["root"], people["bob"]
    _call(db, root, "SELECT admin_logout_all(%s)", bob)
    _call(db, root, "SELECT admin_set_limit(%s, 1)", bob)
    rows = _call(db, root, "SELECT actor_email, action, target_email, detail FROM admin_audit_log(50)")
    assert rows == [("root@example.com", "set_limit", "bob@example.com", {"from": 2.0, "to": 1.0}),
                    ("root@example.com", "logout_all", "bob@example.com", {})]
    assert len(_call(db, root, "SELECT * FROM admin_audit_log(1)")) == 1
    for bad in (0, 201):
        with pytest.raises(psycopg.Error) as e:
            _call(db, root, "SELECT * FROM admin_audit_log(%s)", bad)
        assert _sqlstate(e) == "YTA05"
