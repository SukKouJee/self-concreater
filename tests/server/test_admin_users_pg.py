"""관리 함수 0004(사용자 만들기·고치기·지우기, 목록 쪽·필터·정렬, 상세, 서버 정보, 감사 기록 필터, 마지막 로그인).

0003과 같은 보안 모델: owner 소유 SECURITY DEFINER, 호출자(`app.user_id`)가 활성 관리자가 아니면 `YTA01`(fail closed),
쓰는 함수는 감사 행 하나. 내부 함수(`admin_apply_*`)는 앱 롤이 부를 수 없다. 앱 롤 `Db.user_tx(uid)`로 직접 두드린다.
PG 환경이 없으면 skip.
"""
from __future__ import annotations

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
SHA = "0" * 64
APP = {
    "admin_list_users_page(text,text,text,text,text,boolean,integer,integer)", "admin_user_detail(uuid)",
    "admin_overview()", "admin_set_email(uuid,text)", "admin_set_name(uuid,text)", "admin_delete_user(uuid,text)",
    "admin_audit_page(text,text,text,date,date,integer,integer)", "user_record_login()",
}
INTERNAL = {"admin_apply_email(uuid,text,uuid,text)", "admin_apply_name(uuid,text,uuid,text)",
            "admin_apply_delete(uuid,uuid,text)"}


def _state(exc: pytest.ExceptionInfo[Any]) -> str | None:
    return getattr(exc.value, "sqlstate", None)


def _owner(dsn: str, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


@pytest.fixture
def people(migrated_db: TestDb) -> dict[str, uuid.UUID]:
    """owner로: 관리자 root, 사용자 bob(비밀번호·Google), 사용자 cat(로그인 방법 없음), 비활성 관리자 old."""
    out: dict[str, uuid.UUID] = {}
    with psycopg.connect(migrated_db.owner_dsn) as conn:
        for name, email, cols in (
                ("root", "root@example.com", {"is_admin": True, "display_name": "루트"}),
                ("bob", "bob@example.com", {"password_hash": HASH, "google_sub": "sub-bob", "display_name": "밥 Kim"}),
                ("cat", "cat@example.com", {}),
                ("old", "old@example.com", {"is_admin": True, "disabled": True})):
            names = ["email", *cols]
            row = conn.execute(f"INSERT INTO users ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) "
                               "RETURNING id", (email, *cols.values())).fetchone()
            assert row is not None
            out[name] = uuid.UUID(str(row[0]))
    return out


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


def _one(db: Db, actor: uuid.UUID, sql: str, *params: Any) -> Any:
    rows = _call(db, actor, sql, *params)
    assert len(rows) == 1
    return rows[0][0]


def _audit(dsn: str) -> list[tuple[Any, ...]]:
    return _owner(dsn, "SELECT actor_id, action, target_user_id, detail, actor_email, target_email "
                       "FROM admin_audit ORDER BY id")


def _fill(dsn: str, uid: uuid.UUID, *, docs: int = 2, objects: int = 2, trash: int = 1) -> None:
    """한 사용자의 문서·폴더·파일 인덱스·휴지통·사용량 행(owner로)."""
    with psycopg.connect(dsn) as conn:
        for i in range(docs):
            conn.execute("INSERT INTO docs (user_id, key, raw, version, size, mtime_ns) VALUES (%s, %s, %s, %s, %s, 1)",
                         (uid, f"refs/d{i}.md", b"x" * 10, SHA, 10))
        conn.execute("INSERT INTO folders (user_id, prefix) VALUES (%s, 'refs/')", (uid,))
        for i in range(objects):
            conn.execute("INSERT INTO objects (user_id, key, size, sha256, mtime_ns, s3_key) "
                         "VALUES (%s, %s, 100, %s, 1, %s)",
                         (uid, f"drafts/o{i}.png", SHA, f"users/{uid}/drafts/o{i}.png"))
        for i in range(trash):
            conn.execute("INSERT INTO trash_items (user_id, stamp, key, kind, s3_key, size, mtime_ns) "
                         "VALUES (%s, 's1', %s, 'object', %s, 7, 1)", (uid, f"t{i}.png", f"trash/{uid}/s1/t{i}.png"))
        conn.execute("INSERT INTO trash_items (user_id, stamp, key, kind, raw, size, mtime_ns) "
                     "VALUES (%s, 's1', 'refs/gone.md', 'doc', 'abc', 3, 1)", (uid,))
        conn.execute("INSERT INTO llm_usage (user_id, model, input_tokens, output_tokens, cost_usd, ok) "
                     "VALUES (%s, 'm', 1, 1, 0.25, true)", (uid,))


def _counts(dsn: str, uid: uuid.UUID) -> dict[str, int]:
    out = {}
    for table in ("docs", "folders", "objects", "trash_items", "llm_usage"):
        out[table] = _owner(dsn, f"SELECT count(*) FROM {table} WHERE user_id = %s", uid)[0][0]
    return out


# ---- 스키마·권한 -------------------------------------------------------------------------

def test_new_functions_are_owner_owned_definers_and_only_the_app_ones_are_granted(migrated_db: TestDb) -> None:
    rows = _owner(migrated_db.owner_dsn, """
        SELECT p.oid::regprocedure::text, p.prosecdef, r.rolname, p.proconfig,
               has_function_privilege(%s, p.oid, 'EXECUTE'), has_function_privilege('public', p.oid, 'EXECUTE')
          FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner WHERE p.pronamespace = 'public'::regnamespace""",
                  migrated_db.app_role)
    by_sig = {r[0]: r for r in rows}
    assert APP | INTERNAL <= set(by_sig)
    for sig in APP | INTERNAL:
        _, secdef, owner, config, app, public = by_sig[sig]
        assert secdef and owner == "ytc_owner" and config == ["search_path=pg_catalog, public, pg_temp"], sig
        assert not public, sig
        assert app == (sig in APP), sig


@pytest.mark.parametrize("sql", ["SELECT admin_apply_email(%s, 'x@example.com', NULL, NULL)",
                                 "SELECT admin_apply_name(%s, 'x', NULL, NULL)",
                                 "SELECT admin_apply_delete(%s, NULL, NULL)"])
def test_app_role_cannot_call_the_internal_apply_functions(db: Db, people: dict[str, uuid.UUID], sql: str) -> None:
    with pytest.raises(DENIED), db.user_tx(people["root"]) as conn:
        conn.execute(sql, (people["bob"],))


# ---- fail closed -------------------------------------------------------------------------

CALLS = [
    ("SELECT admin_list_users_page(NULL, 'all', 'all', 'all', 'email', false, 50, 0)", ()),
    ("SELECT admin_user_detail(%s)", ("bob",)),
    ("SELECT admin_overview()", ()),
    ("SELECT admin_set_email(%s, 'new@example.com')", ("bob",)),
    ("SELECT admin_set_name(%s, '새 이름')", ("bob",)),
    ("SELECT admin_delete_user(%s, 'bob@example.com')", ("bob",)),
    ("SELECT admin_audit_page(NULL, NULL, NULL, NULL, NULL, 50, 0)", ()),
]


@pytest.mark.parametrize("who", [None, "bob", "old", "ghost", "garbage"])
@pytest.mark.parametrize("sql, params", CALLS)
def test_every_new_admin_function_fails_closed(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb,
                                               who: str | None, sql: str, params: tuple[Any, ...]) -> None:
    args = tuple(people.get(p, p) if isinstance(p, str) else p for p in params)
    before = _owner(migrated_db.owner_dsn, "SELECT * FROM users ORDER BY id")
    with pytest.raises(psycopg.Error) as e:
        if who == "garbage":
            with db.public_tx() as conn:
                conn.execute("SELECT set_config('app.user_id', 'not-a-uuid', true)")
                conn.execute(sql, args)
        else:
            _call(db, None if who is None else people.get(who, uuid.uuid4()), sql, *args)
    assert _state(e) == "YTA01"
    assert _owner(migrated_db.owner_dsn, "SELECT * FROM users ORDER BY id") == before
    assert _audit(migrated_db.owner_dsn) == []


# ---- 마지막 로그인 ------------------------------------------------------------------------

def test_login_hook_stamps_only_the_callers_row(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    assert _one(db, people["bob"], "SELECT user_record_login()") is True
    stamps = dict(_owner(migrated_db.owner_dsn, "SELECT email, last_login_at IS NOT NULL FROM users"))
    assert stamps == {"root@example.com": False, "bob@example.com": True, "cat@example.com": False,
                      "old@example.com": False}
    assert _one(db, people["old"], "SELECT user_record_login()") is False          # disabled: not stamped
    assert _one(db, uuid.uuid4(), "SELECT user_record_login()") is False
    assert _audit(migrated_db.owner_dsn) == []                                     # not an admin action


def test_login_hook_refuses_without_a_user(db: Db) -> None:
    with pytest.raises(psycopg.Error) as e:
        _call(db, None, "SELECT user_record_login()")
    assert _state(e) == "YTA07"
    with pytest.raises(psycopg.Error) as e, db.public_tx() as conn:
        conn.execute("SELECT set_config('app.user_id', 'nope', true)")
        conn.execute("SELECT user_record_login()")
    assert _state(e) == "YTA07"


# ---- 이메일·이름 ---------------------------------------------------------------------------

def test_email_change_unpins_google_bumps_epoch_and_audits(db: Db, people: dict[str, uuid.UUID],
                                                           migrated_db: TestDb) -> None:
    bob = people["bob"]
    assert _one(db, people["root"], "SELECT admin_set_email(%s, 'robert@example.com')", bob) == 1
    row = _owner(migrated_db.owner_dsn, "SELECT email, google_sub, session_epoch, password_hash FROM users "
                                        "WHERE id = %s", bob)[0]
    assert row == ("robert@example.com", None, 1, HASH)
    ((actor, action, target, detail, _, _),) = _audit(migrated_db.owner_dsn)
    assert (actor, action, target) == (people["root"], "set_email", bob)
    assert detail == {"from": "bob@example.com", "to": "robert@example.com", "google_unlinked": True}
    # the same address again: nothing changes, nothing is audited
    assert _one(db, people["root"], "SELECT admin_set_email(%s, 'robert@example.com')", bob) == 1
    assert len(_audit(migrated_db.owner_dsn)) == 1


@pytest.mark.parametrize("email, state", [("cat@example.com", "23505"), ("Upper@example.com", "23514"),
                                          (None, "YTA05")])
def test_email_change_refuses_taken_uppercase_and_empty(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb,
                                                        email: str | None, state: str) -> None:
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_set_email(%s, %s)", people["bob"], email)
    assert _state(e) == state
    assert _owner(migrated_db.owner_dsn, "SELECT email FROM users WHERE id = %s", people["bob"]) == [
        ("bob@example.com",)]
    assert _audit(migrated_db.owner_dsn) == []


def test_name_change_audits_and_checks_length(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    bob = people["bob"]
    _call(db, people["root"], "SELECT admin_set_name(%s, %s)", bob, "로버트")
    _call(db, people["root"], "SELECT admin_set_name(%s, %s)", bob, "로버트")              # same: no audit
    _call(db, people["root"], "SELECT admin_set_name(%s, NULL)", bob)
    assert [a[1:4] for a in _audit(migrated_db.owner_dsn)] == [
        ("set_name", bob, {"from": "밥 Kim", "to": "로버트"}), ("set_name", bob, {"from": "로버트", "to": None})]
    for bad in ("x" * 101, ""):
        with pytest.raises(psycopg.Error) as e:
            _call(db, people["root"], "SELECT admin_set_name(%s, %s)", bob, bad)
        assert _state(e) == "YTA05"
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_set_name(%s, 'x')", uuid.uuid4())
    assert _state(e) == "YTA02"


# ---- 지우기 --------------------------------------------------------------------------------

def test_delete_removes_every_row_of_that_user_only_and_keeps_spend_anonymous(
        db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    dsn = migrated_db.owner_dsn
    bob, cat, root = people["bob"], people["cat"], people["root"]
    _fill(dsn, bob)
    _fill(dsn, cat, docs=1, objects=1, trash=0)
    _call(db, root, "SELECT admin_set_limit(%s, 2)", bob)                    # an older audit row about bob
    cat_before = _counts(dsn, cat)

    out = _one(db, root, "SELECT admin_delete_user(%s, 'bob@example.com')", bob)

    assert out["email"] == "bob@example.com"
    assert (out["docs"], out["folders"], out["objects"], out["trash"], out["usage_rows"]) == (2, 1, 2, 2, 1)
    assert (out["docs_bytes"], out["objects_bytes"], out["trash_bytes"]) == (20, 200, 10)
    assert sorted(out["s3_keys"]) == sorted([f"users/{bob}/drafts/o0.png", f"users/{bob}/drafts/o1.png",
                                             f"trash/{bob}/s1/t0.png"])
    assert _owner(dsn, "SELECT count(*) FROM users WHERE id = %s", bob) == [(0,)]
    assert _counts(dsn, bob) == {"docs": 0, "folders": 0, "objects": 0, "trash_items": 0, "llm_usage": 0}
    assert _counts(dsn, cat) == cat_before                                   # others untouched
    assert _owner(dsn, "SELECT count(*), sum(cost_usd) FROM llm_usage WHERE user_id IS NULL") == [(1, Decimal("0.25"))]
    rows = _audit(dsn)
    assert [r[1] for r in rows] == ["set_limit", "delete_user"]
    old, gone = rows
    assert old[2] is None and old[5] == "bob@example.com"                   # id cleared, address kept
    assert gone[0] == root and gone[2] is None and gone[5] == "bob@example.com"
    assert gone[3]["email"] == "bob@example.com" and gone[3]["docs"] == 2 and "s3_keys" not in gone[3]
    # the app role still sees only its own rows: anonymous usage is invisible to everyone
    with db.user_tx(cat) as conn:
        assert conn.execute("SELECT count(*) FROM llm_usage").fetchone() == (1,)


def test_audit_reader_shows_deleted_users_by_their_kept_email(db: Db, people: dict[str, uuid.UUID]) -> None:
    root, bob = people["root"], people["bob"]
    _call(db, root, "SELECT admin_set_admin(%s, true)", bob)
    _call(db, bob, "SELECT admin_logout_all(%s)", people["cat"])           # bob acts as an admin
    _call(db, root, "SELECT admin_delete_user(%s, 'bob@example.com')", bob)
    page = _one(db, root, "SELECT admin_audit_page(NULL, NULL, NULL, NULL, NULL, 50, 0)")
    lines = [(r["actor_email"], r["action"], r["target_email"]) for r in page["rows"]]
    assert lines == [("root@example.com", "delete_user", "bob@example.com"),
                     ("bob@example.com", "logout_all", "cat@example.com"),
                     ("root@example.com", "set_admin", "bob@example.com")]


@pytest.mark.parametrize("confirm", [None, "", "BOB@example.com", "cat@example.com", " bob@example.com"])
def test_delete_needs_the_exact_typed_email(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb,
                                            confirm: str | None) -> None:
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_delete_user(%s, %s)", people["bob"], confirm)
    assert _state(e) == "YTA06"
    assert _owner(migrated_db.owner_dsn, "SELECT count(*) FROM users") == [(4,)]
    assert _audit(migrated_db.owner_dsn) == []


def test_delete_refuses_self_and_unknown(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_delete_user(%s, 'root@example.com')", people["root"])
    assert _state(e) == "YTA03"
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_delete_user(%s, 'x@example.com')", uuid.uuid4())
    assert _state(e) == "YTA02"
    assert _owner(migrated_db.owner_dsn, "SELECT count(*) FROM users") == [(4,)]


def test_another_admin_can_be_deleted_while_one_enabled_admin_stays(db: Db, people: dict[str, uuid.UUID],
                                                                    migrated_db: TestDb) -> None:
    """The actor is an enabled admin and cannot delete themself, so the last enabled admin always stays
    (admin_keep_one is the backstop under that rule, tested in test_admin_functions_pg)."""
    root, bob = people["root"], people["bob"]
    _call(db, root, "SELECT admin_set_admin(%s, true)", bob)
    _call(db, root, "SELECT admin_delete_user(%s, 'bob@example.com')", bob)
    _call(db, root, "SELECT admin_delete_user(%s, 'old@example.com')", people["old"])       # disabled admin
    assert _owner(migrated_db.owner_dsn, "SELECT email FROM users WHERE is_admin AND NOT disabled") == [
        ("root@example.com",)]


# ---- 목록 -------------------------------------------------------------------------------------

def _page(db: Db, actor: uuid.UUID, q: str | None = None, status: str = "all", role: str = "all", login: str = "all",
          sort: str = "email", desc: bool = False, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    out = _one(db, actor, "SELECT admin_list_users_page(%s, %s, %s, %s, %s, %s, %s, %s)",
               q, status, role, login, sort, desc, limit, offset)
    assert isinstance(out, dict)
    return out


def _emails(page: dict[str, Any]) -> list[str]:
    return [r["email"].split("@")[0] for r in page["rows"]]


def test_list_page_filters_search_sort_and_pages(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    root = people["root"]
    dsn = migrated_db.owner_dsn
    _owner(dsn, "UPDATE users SET last_login_at = now() - interval '1 day' WHERE email = 'bob@example.com'")
    _owner(dsn, "UPDATE users SET last_login_at = now() WHERE email = 'root@example.com'")
    _owner(dsn, "UPDATE users SET created_at = now() - interval '9 days' WHERE email = 'cat@example.com'")
    for uid, cost in ((people["cat"], 3), (people["bob"], 1)):
        _owner(dsn, "INSERT INTO llm_usage (user_id, model, input_tokens, output_tokens, cost_usd, ok) "
                    "VALUES (%s, 'm', 1, 1, %s, true)", uid, cost)

    full = _page(db, root)
    assert full["total"] == 4 and _emails(full) == ["bob", "cat", "old", "root"]
    bob = full["rows"][0]
    assert set(bob) == {"id", "email", "display_name", "is_admin", "disabled", "has_password", "has_google",
                        "daily_usd_limit", "today_usd", "month_usd", "created_at", "last_login_at"}
    assert (bob["has_password"], bob["has_google"], bob["today_usd"], bob["month_usd"]) == (True, True, 1, 1)
    assert _emails(_page(db, root, q="KIM")) == ["bob"]                                   # name, any case
    assert _emails(_page(db, root, q="CAT@")) == ["cat"]
    assert _emails(_page(db, root, q="%")) == []                                          # no LIKE wildcards
    assert _emails(_page(db, root, status="disabled")) == ["old"]
    assert _emails(_page(db, root, status="active")) == ["bob", "cat", "root"]
    assert _emails(_page(db, root, role="admin")) == ["old", "root"]
    assert _emails(_page(db, root, role="member")) == ["bob", "cat"]
    assert _emails(_page(db, root, login="password")) == ["bob"]
    assert _emails(_page(db, root, login="google")) == ["bob"]
    assert _emails(_page(db, root, login="none")) == ["cat", "old", "root"]
    assert _emails(_page(db, root, sort="email", desc=True)) == ["root", "old", "cat", "bob"]
    assert _emails(_page(db, root, sort="last_login", desc=True)) == ["root", "bob", "cat", "old"]   # never: last
    assert _emails(_page(db, root, sort="last_login")) == ["bob", "root", "cat", "old"]
    assert _emails(_page(db, root, sort="created"))[0] == "cat"
    assert _emails(_page(db, root, sort="today", desc=True)) == ["cat", "bob", "old", "root"]
    two = _page(db, root, limit=2, offset=2)
    assert two["total"] == 4 and _emails(two) == ["old", "root"]
    past = _page(db, root, limit=2, offset=10)
    assert past == {"total": 4, "rows": []}


@pytest.mark.parametrize("args", [
    ("x" * 201, "all", "all", "all", "email", False, 50, 0), (None, "nope", "all", "all", "email", False, 50, 0),
    (None, "all", None, "all", "email", False, 50, 0), (None, "all", "all", "sso", "email", False, 50, 0),
    (None, "all", "all", "all", "id", False, 50, 0), (None, "all", "all", "all", "email", None, 50, 0),
    (None, "all", "all", "all", "email", False, 0, 0), (None, "all", "all", "all", "email", False, 201, 0),
    (None, "all", "all", "all", "email", False, 50, -1),
])
def test_list_page_refuses_bad_parameters(db: Db, people: dict[str, uuid.UUID], args: tuple[Any, ...]) -> None:
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_list_users_page(%s, %s, %s, %s, %s, %s, %s, %s)", *args)
    assert _state(e) == "YTA05"


# ---- 상세·서버 정보 -------------------------------------------------------------------------

def test_detail_counts_storage_and_spend(db: Db, people: dict[str, uuid.UUID], migrated_db: TestDb) -> None:
    _fill(migrated_db.owner_dsn, people["bob"])
    _owner(migrated_db.owner_dsn,
           "INSERT INTO llm_usage (user_id, at, model, input_tokens, output_tokens, cost_usd, ok) "
           "VALUES (%s, now() - interval '40 days', 'm', 1, 1, 5, true)", people["bob"])
    d = _one(db, people["root"], "SELECT admin_user_detail(%s)", people["bob"])
    assert (d["email"], d["display_name"], d["has_password"], d["has_google"]) == (
        "bob@example.com", "밥 Kim", True, True)
    assert (d["docs"], d["docs_bytes"], d["objects"], d["objects_bytes"], d["trash"], d["trash_bytes"]) == (
        2, 20, 2, 200, 2, 10)
    assert (d["today_usd"], d["month_usd"]) == (0.25, 0.25)
    assert d["created_at"] and d["last_login_at"] is None
    assert "password_hash" not in d and "google_sub" not in d
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_user_detail(%s)", uuid.uuid4())
    assert _state(e) == "YTA02"
    assert _audit(migrated_db.owner_dsn) == []                                  # reads are not audited


def test_overview_counts_people_and_spend_including_deleted_users(db: Db, people: dict[str, uuid.UUID],
                                                                  migrated_db: TestDb) -> None:
    _fill(migrated_db.owner_dsn, people["bob"])
    _fill(migrated_db.owner_dsn, people["cat"], docs=0, objects=0, trash=0)
    _call(db, people["root"], "SELECT admin_delete_user(%s, 'cat@example.com')", people["cat"])
    o = _one(db, people["root"], "SELECT admin_overview()")
    assert (o["users"], o["admins"], o["disabled"], o["with_password"], o["with_google"], o["never_logged_in"]) == (
        3, 1, 1, 1, 1, 3)
    assert (o["today_usd"], o["month_usd"], o["month_deleted_usd"]) == (0.5, 0.5, 0.25)


def test_usage_table_keeps_deleted_users_spend_with_no_email(db: Db, people: dict[str, uuid.UUID],
                                                             migrated_db: TestDb) -> None:
    _fill(migrated_db.owner_dsn, people["cat"], docs=0, objects=0, trash=0)
    _call(db, people["root"], "SELECT admin_delete_user(%s, 'cat@example.com')", people["cat"])
    rows = _call(db, people["root"], "SELECT email, calls, cost_usd FROM admin_usage(7)")
    assert [(r[0], r[1], float(r[2])) for r in rows] == [(None, 1, 0.25)]


# ---- 감사 기록 쪽 -----------------------------------------------------------------------------

def test_audit_page_filters_by_actor_action_target_and_kst_dates(db: Db, people: dict[str, uuid.UUID],
                                                                 migrated_db: TestDb) -> None:
    root, bob, cat = people["root"], people["bob"], people["cat"]
    _call(db, root, "SELECT admin_logout_all(%s)", bob)
    _call(db, root, "SELECT admin_set_limit(%s, 1)", cat)
    _call(db, root, "SELECT admin_logout_all(%s)", cat)
    _owner(migrated_db.owner_dsn, "UPDATE admin_audit SET at = now() - interval '10 days' WHERE action = 'set_limit'")

    def page(*args: Any) -> dict[str, Any]:
        out = _one(db, root, "SELECT admin_audit_page(%s, %s, %s, %s::date, %s::date, %s, %s)", *args)
        assert isinstance(out, dict)
        return out

    every = page(None, None, None, None, None, 50, 0)
    assert every["total"] == 3 and [r["action"] for r in every["rows"]] == ["logout_all", "set_limit", "logout_all"]
    assert set(every["rows"][0]) == {"id", "at", "actor_email", "action", "target_email", "detail"}
    assert page(None, "logout_all", None, None, None, 50, 0)["total"] == 2
    assert [r["target_email"] for r in page(None, None, "CAT", None, None, 50, 0)["rows"]] == [
        "cat@example.com", "cat@example.com"]
    assert page("bob", None, None, None, None, 50, 0)["total"] == 0
    assert page("root@", None, None, None, None, 50, 0)["total"] == 3
    today = _owner(migrated_db.owner_dsn, "SELECT (now() AT TIME ZONE 'Asia/Seoul')::date")[0][0]
    assert page(None, None, None, today, today, 50, 0)["total"] == 2
    old = page(None, None, None, None, today.fromordinal(today.toordinal() - 5), 50, 0)
    assert [r["action"] for r in old["rows"]] == ["set_limit"]
    one = page(None, None, None, None, None, 1, 1)
    assert one["total"] == 3 and [r["action"] for r in one["rows"]] == ["set_limit"]


@pytest.mark.parametrize("args", [
    (None, None, None, None, None, 0, 0), (None, None, None, None, None, 201, 0), (None, None, None, None, None, 5, -1),
    ("x" * 201, None, None, None, None, 5, 0), (None, "x" * 41, None, None, None, 5, 0),
    (None, None, None, "2026-11-03", "2026-11-02", 5, 0),
])
def test_audit_page_refuses_bad_parameters(db: Db, people: dict[str, uuid.UUID], args: tuple[Any, ...]) -> None:
    with pytest.raises(psycopg.Error) as e:
        _call(db, people["root"], "SELECT admin_audit_page(%s, %s, %s, %s::date, %s::date, %s, %s)", *args)
    assert _state(e) == "YTA05"
