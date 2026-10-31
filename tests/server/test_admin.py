"""server.admin(서버 설계 §7, T9): 걷기·제외(PG 없이), add-user·disable-user·usage·import(PG 16 + MinIO가 없으면 skip).

가져오기 검증의 핵심: 픽스처 작업 폴더를 새 사용자에게 올린 뒤 `export_tree`가 원본과 바이트까지 같고(제외한 `library.json`만 빠짐),
`status.overview()`가 FileStore Ctx와 ServerBackend Ctx에서 같으며(`work_root` 제외), 다시 실행하면 변경 0이다.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scripts import ctx as ctx_mod
from scripts import keys, refstore, status
from scripts.common import ScriptError
from scripts.ctx import Ctx
from server import admin
from server.backend import ServerBackend, export_tree, plan_tree
from tests.helpers.pg import Stack, TestDb, pg_user
from tests.helpers.tree_snapshot import copy_workfolder, tree

EMAIL = "Fresh@Example.com"
LOWER = "fresh@example.com"
VID = "Q8PSsikq8PA"


# --- 걷기(PG 없이) ---------------------------------------------------------------------------

def _touch(root: Path, rel: str, data: bytes = b"x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_walk_uses_posix_relative_keys_and_applies_exclusions(tmp_path: Path) -> None:
    for rel in ("profile.md", "refs/ref_a/index.md", "workspace/raw/abc.json", "workspace/tmp/x.json",
                "workspace/trash/20260101-000000/a.md", "workspace/library/r/a.md", "workspace/ui.lock",
                "workspace/ui.log", "workspace/timings.json", "workspace/.last_fetch", "workspace/library.lock",
                ".git/HEAD", "library.json", "drafts/한글 이름.md"):
        _touch(tmp_path, rel)

    walked = admin.walk_workfolder(tmp_path)

    assert [f.key for f in walked.files] == ["drafts/한글 이름.md", "profile.md", "refs/ref_a/index.md",
                                             "workspace/raw/abc.json"]
    assert set(walked.excluded) == {"workspace/tmp/", "workspace/trash/", "workspace/library/", ".git/", "library.json",
                                    "workspace/ui.lock", "workspace/ui.log", "workspace/timings.json",
                                    "workspace/.last_fetch", "workspace/library.lock"}
    assert walked.bad_key == [] and walked.too_large == [] and walked.not_regular == []


def test_walk_reports_bad_keys_and_empty_folders_and_size_limit(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    _touch(tmp_path, "mine/CON.md")                  # Windows reserved name -> keys.check fails
    _touch(tmp_path, "drafts/ok-conti.html", b"12345")
    _touch(tmp_path, "drafts/note.md", b"x" * 50)
    (tmp_path / "refs" / "ref_empty" / "videos").mkdir(parents=True)
    (tmp_path / "reports").mkdir()
    monkeypatch.setattr(admin, "MAX_OBJECT_BYTES", 4)

    walked = admin.walk_workfolder(tmp_path)

    assert walked.bad_key == ["mine/CON.md"]
    assert walked.too_large == ["drafts/note.md", "drafts/ok-conti.html"]          # docs have the same limit
    assert walked.files == []
    assert walked.folders == ["refs/ref_empty/videos/", "reports/"]


def test_walk_skips_windows_reparse_points(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch(tmp_path, "real/a.md")
    _touch(tmp_path, "junction/b.md")
    _touch(tmp_path, "file.md")
    real_lstat = Path.lstat

    class _Reparse:
        def __init__(self, st: os.stat_result) -> None:
            self.st_mode, self.st_size, self.st_mtime_ns = st.st_mode, st.st_size, st.st_mtime_ns
            self.st_file_attributes = 0x400

    monkeypatch.setattr(Path, "lstat", lambda self: _Reparse(real_lstat(self)) if self.name in {"junction", "file.md"}
                        else real_lstat(self))

    walked = admin.walk_workfolder(tmp_path)

    assert [f.key for f in walked.files] == ["real/a.md"]
    assert sorted(walked.not_regular) == ["file.md", "junction/"]


def test_walk_only_makes_folders_for_dirs_that_were_empty_before_filtering(tmp_path: Path) -> None:
    (tmp_path / "workspace" / "tmp").mkdir(parents=True)             # only an excluded dir inside: not an empty folder
    (tmp_path / "workspace" / "frames" / "abc").mkdir(parents=True)   # shared cache folders are not the user's
    (tmp_path / "reviews" / "archive").mkdir(parents=True)

    walked = admin.walk_workfolder(tmp_path)

    assert walked.folders == ["reviews/archive/"]


def test_walk_skips_symlinks(tmp_path: Path) -> None:
    _touch(tmp_path, "real/a.md")
    try:
        (tmp_path / "link.md").symlink_to(tmp_path / "real" / "a.md")
        (tmp_path / "linkdir").symlink_to(tmp_path / "real", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")

    walked = admin.walk_workfolder(tmp_path)

    assert [f.key for f in walked.files] == ["real/a.md"]
    assert sorted(walked.not_regular) == ["link.md", "linkdir/"]


def test_walk_records_original_mtime_ns(tmp_path: Path) -> None:
    _touch(tmp_path, "profile.md")
    os.utime(tmp_path / "profile.md", ns=(1_700_000_000_123_456_700, 1_700_000_000_123_456_700))

    (only,) = admin.walk_workfolder(tmp_path).files

    assert only.mtime_ns == (tmp_path / "profile.md").stat().st_mtime_ns


@pytest.mark.parametrize("bad", ["nope", "a@b", "a b@c.com", ""])
def test_add_user_rejects_malformed_email_before_touching_the_db(bad: str) -> None:
    with pytest.raises(ScriptError) as e:
        admin.add_user({}, bad, None, None, False)
    assert e.value.code == "bad_email"


@pytest.mark.parametrize("bad", ["abc", "-1", "1.234", "1000000", "NaN"])
def test_add_user_rejects_bad_daily_limit(bad: str) -> None:
    with pytest.raises(ScriptError) as e:
        admin.add_user({}, "a@example.com", None, bad, False)
    assert e.value.code == "bad_limit"


@pytest.mark.parametrize("command", ["disable-user", "logout-all", "make-admin", "revoke-admin"])
def test_admin_commands_need_the_owner_dsn(command: str) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main([command, "a@example.com"], {})
    assert e.value.code == "config_missing"


# --- PG ---------------------------------------------------------------------------------------

@pytest.fixture
def env(migrated_db: TestDb, pg_stack: Stack, s3_bucket: Any) -> dict[str, str]:
    return {"YTC_DB_DSN": migrated_db.app_dsn, "YTC_DB_OWNER_DSN": migrated_db.owner_dsn,
            "YTC_S3_ENDPOINT": pg_stack.s3_endpoint, "YTC_S3_ACCESS_KEY": pg_stack.s3_access_key,
            "YTC_S3_SECRET_KEY": pg_stack.s3_secret_key, "YTC_S3_BUCKET": s3_bucket.name}


def _user_row(env: dict[str, str], email: str) -> tuple[Any, ...]:
    with psycopg.connect(env["YTC_DB_OWNER_DSN"]) as conn:
        row = conn.execute("SELECT id, email, display_name, is_admin, disabled, session_epoch, daily_usd_limit "
                           "FROM users WHERE email = %s", (email,)).fetchone()
    assert row is not None
    return tuple(row)


def test_add_user_creates_lowercased_row_with_options(env: dict[str, str]) -> None:
    out = admin.main(["add-user", EMAIL, "--name", "지수", "--daily-limit", "2.50", "--admin"], env)

    assert out["email"] == LOWER and out["is_admin"] is True and out["daily_usd_limit"] == 2.5
    row = _user_row(env, LOWER)
    assert str(row[0]) == out["id"] and row[2] == "지수" and row[3] is True and row[4] is False and float(row[6]) == 2.5


def test_add_user_twice_is_user_exists(env: dict[str, str]) -> None:
    admin.main(["add-user", LOWER], env)

    with pytest.raises(ScriptError) as e:
        admin.main(["add-user", EMAIL], env)

    assert e.value.code == "user_exists"


def _audit_rows(env: dict[str, str]) -> list[tuple[Any, ...]]:
    with psycopg.connect(env["YTC_DB_OWNER_DSN"]) as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT actor_id, action, target_user_id, detail FROM admin_audit ORDER BY id").fetchall()]


def test_make_admin_and_revoke_admin_flip_is_admin_and_audit_as_the_owner(env: dict[str, str]) -> None:
    admin.main(["add-user", LOWER], env)
    uid = _user_row(env, LOWER)[0]

    made = admin.main(["make-admin", EMAIL], env)
    assert made == {"id": str(uid), "email": LOWER, "is_admin": True, "admins": 1}
    assert _user_row(env, LOWER)[3] is True
    again = admin.main(["make-admin", LOWER], env)                 # idempotent
    assert again["is_admin"] is True and again["admins"] == 1

    revoked = admin.main(["revoke-admin", LOWER], env)
    assert revoked == {"id": str(uid), "email": LOWER, "is_admin": False, "admins": 0}
    assert _user_row(env, LOWER)[3] is False and _user_row(env, LOWER)[5] == 0          # sessions are kept
    assert _audit_rows(env) == [(None, "set_admin", uid, {"is_admin": True, "via": "cli"}),
                                (None, "set_admin", uid, {"is_admin": True, "via": "cli"}),
                                (None, "set_admin", uid, {"is_admin": False, "via": "cli"})]


@pytest.mark.parametrize("command", ["make-admin", "revoke-admin"])
def test_make_admin_unknown_user_is_user_not_found(env: dict[str, str], command: str) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main([command, "ghost@example.com"], env)
    assert e.value.code == "user_not_found"
    assert _audit_rows(env) == []


def test_disable_user_disables_and_bumps_session_epoch(env: dict[str, str]) -> None:
    admin.main(["add-user", LOWER], env)
    before = _user_row(env, LOWER)

    out = admin.main(["disable-user", EMAIL], env)

    after = _user_row(env, LOWER)
    assert before[4] is False and after[4] is True
    assert out["session_epoch"] == after[5] == before[5] + 1


def test_disable_unknown_user_is_user_not_found(env: dict[str, str]) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(["disable-user", "ghost@example.com"], env)
    assert e.value.code == "user_not_found"


def test_logout_all_bumps_session_epoch_and_keeps_the_user_enabled(env: dict[str, str]) -> None:
    admin.main(["add-user", LOWER], env)
    before = _user_row(env, LOWER)

    out = admin.main(["logout-all", EMAIL], env)

    after = _user_row(env, LOWER)
    assert after[4] is False                                  # still allowed to sign in again
    assert out == {"id": str(before[0]), "email": LOWER, "disabled": False, "session_epoch": before[5] + 1}
    assert after[5] == before[5] + 1


def test_logout_all_unknown_user_is_user_not_found(env: dict[str, str]) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(["logout-all", "ghost@example.com"], env)
    assert e.value.code == "user_not_found"


def test_logout_all_invalidates_a_session_issued_before_it(env: dict[str, str]) -> None:
    from server.auth import SessionClaim, UserRow, session_valid
    admin.main(["add-user", LOWER], env)

    def user() -> UserRow:
        row = _user_row(env, LOWER)
        return UserRow(id=row[0], email=row[1], disabled=row[4], session_epoch=row[5])

    claim = SessionClaim(user().id, user().session_epoch, 1000)
    assert session_valid(claim, user(), now=1001)

    admin.main(["logout-all", LOWER], env)

    assert not session_valid(claim, user(), now=1001)


def test_usage_is_empty_without_records(env: dict[str, str]) -> None:
    assert admin.main(["usage"], env) == {"days": 7, "rows": [], "total_usd": 0.0}


def test_usage_sums_by_user_and_day_in_seoul_time(env: dict[str, str]) -> None:
    a = admin.main(["add-user", "a@example.com"], env)["id"]
    b = admin.main(["add-user", "b@example.com"], env)["id"]
    rows = [(a, "now() - interval '1 hour'", 100, 10, "0.010000", True),
            (a, "now() - interval '2 hours'", 50, 5, "0.005000", False),
            (b, "now() - interval '1 hour'", 7, 3, "0.250000", True),
            (a, "now() - interval '30 days'", 1, 1, "9.000000", True)]
    with psycopg.connect(env["YTC_DB_OWNER_DSN"]) as conn:
        for uid, at, tin, tout, cost, ok in rows:
            conn.execute(f"INSERT INTO llm_usage (user_id, at, job_kind, model, input_tokens, output_tokens, cost_usd, "
                         f"ok) "
                         f"VALUES (%s, {at}, 'analyze', 'm', %s, %s, %s, %s)", (uuid.UUID(uid), tin, tout, cost, ok))

    week = admin.main(["usage", "--days", "7"], env)

    by_user: dict[str, dict[str, Any]] = {}
    for r in week["rows"]:
        by_user.setdefault(r["email"], {"calls": 0, "failed": 0, "in": 0, "cost": 0.0})
        by_user[r["email"]]["calls"] += r["calls"]
        by_user[r["email"]]["failed"] += r["failed"]
        by_user[r["email"]]["in"] += r["input_tokens"]
        by_user[r["email"]]["cost"] += r["cost_usd"]
    # the two a-rows may straddle midnight (KST), so compare per user rather than per day
    assert by_user["a@example.com"]["calls"] == 2 and by_user["a@example.com"]["failed"] == 1
    assert by_user["a@example.com"]["in"] == 150 and round(by_user["a@example.com"]["cost"], 6) == 0.015
    assert by_user["b@example.com"]["cost"] == 0.25
    assert round(week["total_usd"], 6) == 0.265            # the 30-day-old row is outside --days 7
    assert admin.main(["usage", "--days", "60"], env)["total_usd"] == pytest.approx(9.265)


def test_usage_splits_days_at_kst_midnight(env: dict[str, str]) -> None:
    uid = uuid.UUID(admin.main(["add-user", "a@example.com"], env)["id"])
    # 23:59:59, 00:00:01 and 00:00:02 KST
    stamps = ["2026-10-30 14:59:59+00", "2026-10-30 15:00:01+00", "2026-10-30 15:00:02+00"]
    with psycopg.connect(env["YTC_DB_OWNER_DSN"]) as conn:
        for at in stamps:
            conn.execute("INSERT INTO llm_usage (user_id, at, model, input_tokens, output_tokens, cost_usd, ok) "
                         "VALUES (%s, %s, 'm', 1, 1, 0.5, true)", (uid, at))

    out = admin.main(["usage", "--days", "36500"], env)

    assert [(r["day"], r["calls"], r["cost_usd"]) for r in out["rows"]] == [("2026-10-30", 1, 0.5),
                                                                          ("2026-10-31", 2, 1.0)]


def test_usage_rejects_zero_days(env: dict[str, str]) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(["usage", "--days", "0"], env)
    assert e.value.code == "bad_days"


# --- import -----------------------------------------------------------------------------------

@pytest.fixture
def source(tmp_path: Path) -> Path:
    return copy_workfolder(tmp_path / "work")


def _import(env: dict[str, str], folder: Path, *extra: str, user: str = LOWER) -> dict[str, Any]:
    return admin.main(["import", str(folder), "--user", user, *extra], env)


def _uid(env: dict[str, str], email: str = LOWER) -> uuid.UUID:
    return uuid.UUID(str(_user_row(env, email)[0]))


def _backend(request: pytest.FixtureRequest) -> ServerBackend:
    backend = request.getfixturevalue("pg_backend")
    assert isinstance(backend, ServerBackend)
    return backend


def test_import_unknown_user_is_user_not_found(env: dict[str, str], source: Path) -> None:
    with pytest.raises(ScriptError) as e:
        _import(env, source)
    assert e.value.code == "user_not_found"


def test_import_into_disabled_user_is_refused(env: dict[str, str], source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    admin.main(["disable-user", LOWER], env)

    with pytest.raises(ScriptError) as e:
        _import(env, source)

    assert e.value.code == "user_disabled"


def test_import_missing_folder(env: dict[str, str], tmp_path: Path) -> None:
    admin.main(["add-user", LOWER], env)
    with pytest.raises(ScriptError) as e:
        _import(env, tmp_path / "nope")
    assert e.value.code == "folder_not_found"


def test_import_fixture_is_byte_identical_and_status_matches_then_rerun_changes_nothing(
        request: pytest.FixtureRequest, env: dict[str, str], source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    uid = _uid(env)
    backend = _backend(request)

    report = _import(env, source)

    expected = {k: v for k, v in tree(source).items() if k != keys.LIBRARY_CONFIG}
    assert plan_tree(backend, uid).hashes() == expected                               # byte-identical
    assert export_tree(backend, uid).hashes() == expected
    assert report["excluded"] == [keys.LIBRARY_CONFIG]
    assert report["skipped"] == {"bad_key": [], "too_large": [], "not_regular": []} and report["conflicts"] == []
    assert report["docs"] + report["objects"] + report["shared_new"] == len(expected)
    assert report["shared_new"] == 5 and report["docs"] > 0 and report["objects"] > 0
    assert report["verify"] == {"ok": True, "differing": []} and report["ok"] is True and report["replaced"] == []
    # status overview: FileStore Ctx vs ServerBackend Ctx, only the label differs
    with ctx_mod.use(Ctx(root=source)):
        local = status.overview()
    with ctx_mod.use(Ctx(user_id=str(uid), backend=backend)):
        server = status.overview()
    assert local["work_root"] == str(source) and server["work_root"] == f"ytc:{LOWER}"
    assert admin._same({**local, "work_root": ""}, {**server, "work_root": ""})
    assert [r["path"] for r in local["recent"]] == [r["path"] for r in server["recent"]]       # same order
    assert local["categories"] and local["recent"]                                   # not a vacuous comparison

    again = _import(env, source)

    assert (again["changes"], again["docs"], again["objects"], again["shared_new"], again["folders"]) == (0, 0, 0, 0, 0)
    assert again["conflicts"] == [] and again["verify"]["ok"] is True
    assert again["unchanged"]["docs"] + again["unchanged"]["objects"] + again["shared_skipped"] == len(expected)
    assert plan_tree(backend, uid).hashes() == expected


def test_import_preserves_original_mtime(request: pytest.FixtureRequest, env: dict[str, str], source: Path) -> None:
    stamps = {"profile.md": 1_600_000_000_000_000_100, "reports/travel-2026-10-30.html": 1_600_000_100_000_000_200,
              f"workspace/raw/{VID}.json": 1_600_000_200_000_000_300}
    for rel, ns in stamps.items():
        os.utime(source / rel, ns=(ns, ns))
    on_disk = {rel: (source / rel).stat().st_mtime_ns for rel in stamps}      # the file system may round (NTFS: 100ns)
    assert all(abs(on_disk[rel] - ns) < 1000 for rel, ns in stamps.items())
    admin.main(["add-user", LOWER], env)
    uid = _uid(env)
    backend = _backend(request)

    _import(env, source)

    assert backend.store(str(uid)).stat("profile.md").mtime_ns == on_disk["profile.md"]
    report_mtime = backend.objects(str(uid)).stat("reports/travel-2026-10-30.html").mtime_ns
    assert report_mtime == on_disk["reports/travel-2026-10-30.html"]
    assert backend.shared(str(uid)).stat(f"workspace/raw/{VID}.json").mtime_ns == on_disk[f"workspace/raw/{VID}.json"]


def test_dry_run_reports_counts_and_writes_nothing(request: pytest.FixtureRequest, env: dict[str, str],
                                                   source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    uid = _uid(env)
    backend = _backend(request)
    (source / "reviews" / "archive" / "deep").mkdir(parents=True)

    plan = _import(env, source, "--dry-run")

    assert plan["dry_run"] is True and plan["docs"] > 0 and plan["objects"] > 0 and plan["folders"] >= 1
    assert "verify" not in plan
    assert plan_tree(backend, uid).names == []
    assert backend.shared(str(uid)).exists(f"workspace/raw/{VID}.json") is False

    done = _import(env, source)

    assert {k: done[k] for k in ("docs", "objects", "shared_new", "folders")} == \
        {k: plan[k] for k in ("docs", "objects", "shared_new", "folders")}


def test_empty_folders_become_folders(request: pytest.FixtureRequest, env: dict[str, str], source: Path) -> None:
    (source / "reviews" / "archive" / "deep").mkdir(parents=True)
    admin.main(["add-user", LOWER], env)
    uid = _uid(env)

    report = _import(env, source, "--no-verify")

    store = _backend(request).store(str(uid))
    # only the leaf is a folder to create (its parents are implied)
    assert report["folders"] == 1 and "verify" not in report
    assert store.folder_exists("reviews/archive/deep/")
    assert "reviews/archive/" in store.folders("reviews/", "*")
    assert _import(env, source, "--no-verify")["folders"] == 0


def test_different_content_is_a_conflict_until_overwrite(request: pytest.FixtureRequest, env: dict[str, str],
                                                         source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    uid = _uid(env)
    backend = _backend(request)
    _import(env, source)
    server_profile = backend.store(str(uid)).read("profile.md").raw
    (source / "profile.md").write_bytes(server_profile.replace("제주, 한 달 살기".encode(), "제주 수정".encode()))
    (source / "reports" / "travel-2026-10-30.html").write_bytes(b"<html>changed</html>")

    held = _import(env, source, "--no-verify")

    assert sorted(c["key"] for c in held["conflicts"]) == ["profile.md", "reports/travel-2026-10-30.html"]
    assert {c["kind"] for c in held["conflicts"]} == {"doc", "object"} and held["changes"] == 0
    assert backend.store(str(uid)).read("profile.md").raw == server_profile

    forced = _import(env, source, "--overwrite")

    assert forced["conflicts"] == [] and forced["docs"] == 1 and forced["objects"] == 1
    assert backend.store(str(uid)).read("profile.md").raw == (source / "profile.md").read_bytes()
    assert backend.objects(str(uid)).get("reports/travel-2026-10-30.html") == b"<html>changed</html>"
    assert forced["verify"]["ok"] is True
    assert _import(env, source)["changes"] == 0


def _memo_doc(memo: str, body: str = "본문") -> bytes:
    return f"# 메모 문서{chr(10)}{chr(10)}{body}{chr(10)}{chr(10)}{refstore.MEMO_HEADING}{chr(10)}{memo}{chr(10)}".encode()


NOTE = "mine/memo-note.md"


def test_overwrite_never_replaces_a_changed_server_memo_without_overwrite_memo(
        request: pytest.FixtureRequest, env: dict[str, str], source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    uid = _uid(env)
    backend = _backend(request)
    (source / NOTE).write_bytes(_memo_doc("서버에서 쓴 메모"))
    _import(env, source)
    server_raw = backend.store(str(uid)).read(NOTE).raw
    (source / NOTE).write_bytes(_memo_doc("로컬 메모", "새 본문"))
    profile = (source / "profile.md").read_bytes()
    (source / "profile.md").write_bytes(profile.replace("제주, 한 달 살기".encode(), "제주 수정".encode()))   # memo kept

    held = _import(env, source, "--overwrite")

    assert held["conflicts"] == [{"key": NOTE, "kind": "doc", "reason": "memo"}]
    assert held["replaced"] == ["profile.md"] and held["ok"] is False and held["verify"] == "skipped_conflicts"
    assert "message" in held and "overwrite-memo" in held["message"]
    assert backend.store(str(uid)).read(NOTE).raw == server_raw                          # memo untouched

    forced = _import(env, source, "--overwrite-memo")

    assert forced["conflicts"] == [] and forced["replaced"] == [NOTE] and forced["ok"] is True
    assert backend.store(str(uid)).read(NOTE).raw == (source / NOTE).read_bytes()


def test_overwrite_replaces_a_doc_whose_memo_is_unchanged(request: pytest.FixtureRequest, env: dict[str, str],
                                                          source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    (source / NOTE).write_bytes(_memo_doc("같은 메모"))
    _import(env, source)
    (source / NOTE).write_bytes(_memo_doc("같은 메모", "고친 본문"))

    out = _import(env, source, "--overwrite")

    assert out["replaced"] == [NOTE] and out["conflicts"] == [] and out["ok"] is True


def test_plain_conflict_reports_a_reason_and_ok_false(env: dict[str, str], source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    _import(env, source)
    (source / "reports" / "travel-2026-10-30.html").write_bytes(b"<html>changed</html>")

    held = _import(env, source)

    assert held["conflicts"] == [{"key": "reports/travel-2026-10-30.html", "kind": "object", "reason": "differs"}]
    assert held["ok"] is False and held["verify"] == "skipped_conflicts"


def test_oversized_doc_is_reported_and_size_is_rechecked_after_reading(
        env: dict[str, str], source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin.main(["add-user", LOWER], env)
    monkeypatch.setattr(admin, "MAX_OBJECT_BYTES", 300)
    big = source / "mine" / "big.md"
    big.write_bytes(b"x" * 301)
    grow = source / "mine" / "grows.md"
    grow.write_bytes(b"y" * 10)
    real_walk = admin.walk_workfolder

    def walk_then_grow(folder: Path) -> admin.Walked:
        walked = real_walk(folder)
        grow.write_bytes(b"y" * 400)                       # grows between the walk and the read
        return walked

    monkeypatch.setattr(admin, "walk_workfolder", walk_then_grow)

    out = _import(env, source, "--no-verify")

    assert "mine/big.md" in out["skipped"]["too_large"] and "mine/grows.md" in out["skipped"]["too_large"]


def test_shared_cache_first_writer_wins_even_with_overwrite(request: pytest.FixtureRequest, env: dict[str, str],
                                                            source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    admin.main(["add-user", "second@example.com"], env)
    backend = _backend(request)
    _import(env, source, "--no-verify")
    original = (source / "workspace" / "raw" / f"{VID}.json").read_bytes()
    (source / "workspace" / "raw" / f"{VID}.json").write_bytes(b'{"id": "poison"}')

    second = _import(env, source, "--overwrite", "--no-verify", user="second@example.com")

    assert second["shared_new"] == 0 and second["shared_skipped"] == 5 and second["conflicts"] == []
    assert backend.shared(str(_uid(env))).get(f"workspace/raw/{VID}.json") == original


def test_import_verify_failure_is_an_error_carrying_the_report(env: dict[str, str], source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    uid = _uid(env)
    raw = b"# extra"
    with psycopg.connect(env["YTC_DB_OWNER_DSN"]) as conn:        # data the folder does not have: not a fresh account
        conn.execute("INSERT INTO docs (user_id, key, raw, version, size, mtime_ns) VALUES (%s, %s, %s, %s, %s, 1)",
                     (uid, "refs/ref_extra/index.md", raw, "0" * 64, len(raw)))
        conn.execute("INSERT INTO folders (user_id, prefix) VALUES (%s, 'refs/'), (%s, 'refs/ref_extra/')", (uid, uid))

    with pytest.raises(ScriptError) as e:
        _import(env, source)

    assert e.value.code == "import_verify_failed"
    report = e.value.details["report"]
    assert report["verify"]["ok"] is False and "categories" in report["verify"]["differing"]
    assert report["docs"] > 0                                      # the import itself went through


def test_two_users_import_independently(request: pytest.FixtureRequest, env: dict[str, str], source: Path) -> None:
    admin.main(["add-user", LOWER], env)
    admin.main(["add-user", "other@example.com"], env)
    backend = _backend(request)

    _import(env, source)

    assert plan_tree(backend, _uid(env, "other@example.com")).names == []
    assert backend.store(str(_uid(env))).exists("profile.md")


def test_import_rejects_a_user_with_unusable_folder_content_by_reading_error(
        env: dict[str, str], source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin.main(["add-user", LOWER], env)
    original = Path.read_bytes

    def broken(self: Path) -> bytes:
        if self.name == "profile.md":
            raise PermissionError("denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", broken)

    with pytest.raises(ScriptError) as e:
        _import(env, source)

    assert e.value.code == "import_unreadable" and "profile.md" in e.value.message


# --- plan_tree: 키마다 읽기(m-009) ----------------------------------------------------------------

def test_plan_tree_reads_bytes_per_key_not_at_planning_time(request: pytest.FixtureRequest, env: dict[str, str],
                                                            source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin.main(["add-user", LOWER], env)
    uid = _uid(env)
    backend = _backend(request)
    _import(env, source)
    gets: list[str] = []
    real_get = backend.bucket.get

    def counting(s3_key: str) -> bytes:
        gets.append(s3_key)
        return real_get(s3_key)

    monkeypatch.setattr(backend.bucket, "get", counting)

    plan = plan_tree(backend, uid)
    assert gets == [] and len(plan.names) > 10                    # names and folders are known without any download
    streamed = iter(plan.items())
    first_name, first_bytes = next(streamed)
    assert first_name == plan.names[0] and first_bytes == (source / first_name).read_bytes()
    downloads_after_first = len(gets)
    assert downloads_after_first <= 1                              # only the first key was read so far
    assert dict(streamed) | {first_name: first_bytes} == {n: (source / n).read_bytes() for n in plan.names}
    assert plan.collect().files == export_tree(backend, uid).files


def test_plan_tree_write_streams_to_disk(request: pytest.FixtureRequest, env: dict[str, str], source: Path,
                                         tmp_path: Path) -> None:
    admin.main(["add-user", LOWER], env)
    _import(env, source)

    plan_tree(_backend(request), _uid(env)).write(tmp_path / "out")

    assert tree(tmp_path / "out") == {k: v for k, v in tree(source).items() if k != keys.LIBRARY_CONFIG}


def test_pg_user_helper_users_are_visible_to_admin_usage(env: dict[str, str], migrated_db: TestDb) -> None:
    pg_user(migrated_db.owner_dsn, "helper@example.com")
    assert admin.main(["usage"], env)["rows"] == []


# --- 비밀번호(T15) ---------------------------------------------------------------------------

import io  # noqa: E402

from server import passwords  # noqa: E402

PW = "correct horse battery"


def _stdin(text: str) -> io.StringIO:
    return io.StringIO(text)


def _no_prompt(_label: str) -> str:
    raise AssertionError("prompted although the password came from stdin")


def _prompts(*answers: str) -> Any:
    left = list(answers)

    def prompt(_label: str) -> str:
        return left.pop(0)

    return prompt


def _hash_of(env: dict[str, str], email: str) -> str | None:
    with psycopg.connect(env["YTC_DB_OWNER_DSN"]) as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE email = %s", (email,)).fetchone()
    assert row is not None
    return row[0]


def test_read_password_from_stdin_strips_only_the_line_ending() -> None:
    assert admin.read_password(stdin=_stdin("  spaced pass  \r\n"), from_stdin=True, prompt=_no_prompt) == "  spaced pass  "


def test_read_password_prompts_twice_and_needs_the_same_answer() -> None:
    assert admin.read_password(stdin=_stdin(""), from_stdin=False, prompt=_prompts(PW, PW), tty=True) == PW
    with pytest.raises(ScriptError) as e:
        admin.read_password(stdin=_stdin(""), from_stdin=False, prompt=_prompts(PW, PW + "x"), tty=True)
    assert e.value.code == "password_mismatch" and PW not in e.value.message


def test_read_password_without_a_terminal_asks_for_password_stdin() -> None:
    with pytest.raises(ScriptError) as e:
        admin.read_password(stdin=_stdin(PW), from_stdin=False, prompt=_no_prompt, tty=False)
    assert e.value.code == "no_tty" and "--password-stdin" in e.value.message


@pytest.mark.parametrize("argv", [["add-user", "a@example.com", "--password-stdin"],
                                  ["set-password", "a@example.com", "--password-stdin"]])
def test_short_password_is_refused_before_touching_the_db(argv: list[str]) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(argv, {}, stdin=_stdin("short\n"), prompt=_no_prompt)
    assert e.value.code == "weak_password"


def test_password_and_password_stdin_are_exclusive() -> None:
    with pytest.raises(ScriptError) as e:
        admin.main(["add-user", "a@example.com", "--password", "-", "--password-stdin"], {}, stdin=_stdin(PW))
    assert e.value.code == "usage"


@pytest.mark.parametrize("command", ["set-password", "clear-password"])
def test_password_commands_need_the_owner_dsn(command: str) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main([command, "a@example.com", *(["--password-stdin"] if command == "set-password" else [])], {},
                   stdin=_stdin(PW + "\n"))
    assert e.value.code == "config_missing"


def test_add_user_with_password_stdin_stores_a_scrypt_hash_and_never_prints_it(env: dict[str, str],
                                                                               capsys: pytest.CaptureFixture[str]) -> None:
    out = admin.main(["add-user", EMAIL, "--password-stdin"], env, stdin=_stdin(PW + "\n"), prompt=_no_prompt)

    stored = _hash_of(env, LOWER)
    assert out["password"] is True and stored is not None and stored.startswith("scrypt$")
    assert passwords.verify_password(PW, stored)
    text = repr(out) + capsys.readouterr().out
    assert PW not in text and stored not in text


@pytest.mark.parametrize("argv", [["--password"], ["--password", "-"]])
def test_add_user_password_flag_without_value_or_dash_prompts(env: dict[str, str], argv: list[str]) -> None:
    out = admin.main(["add-user", LOWER, *argv], env, stdin=_stdin(""), prompt=_prompts(PW, PW), tty=True)
    assert out["password"] is True and passwords.verify_password(PW, _hash_of(env, LOWER))


def test_add_user_without_password_has_no_hash(env: dict[str, str]) -> None:
    out = admin.main(["add-user", LOWER], env)
    assert out["password"] is False and _hash_of(env, LOWER) is None


def test_set_password_replaces_the_hash_and_bumps_session_epoch(env: dict[str, str]) -> None:
    from server.auth import SessionClaim, UserRow, session_valid
    admin.main(["add-user", LOWER, "--password-stdin"], env, stdin=_stdin(PW))
    before = _user_row(env, LOWER)
    claim = SessionClaim(before[0], before[5], 1000)
    assert session_valid(claim, UserRow(before[0], before[1], before[4], before[5]), now=1001)

    out = admin.main(["set-password", EMAIL, "--password-stdin"], env, stdin=_stdin("a brand new secret\n"))

    after = _user_row(env, LOWER)
    assert out == {"id": str(before[0]), "email": LOWER, "password": True, "session_epoch": before[5] + 1}
    assert not session_valid(claim, UserRow(after[0], after[1], after[4], after[5]), now=1001)   # old sessions die
    stored = _hash_of(env, LOWER)
    assert passwords.verify_password("a brand new secret", stored) and not passwords.verify_password(PW, stored)


def test_set_password_prompts_by_default(env: dict[str, str]) -> None:
    admin.main(["add-user", LOWER], env)
    admin.main(["set-password", LOWER], env, stdin=_stdin(""), prompt=_prompts(PW, PW), tty=True)
    assert passwords.verify_password(PW, _hash_of(env, LOWER))


def test_clear_password_disables_password_login_and_bumps_epoch(env: dict[str, str]) -> None:
    admin.main(["add-user", LOWER, "--password-stdin"], env, stdin=_stdin(PW))
    before = _user_row(env, LOWER)

    out = admin.main(["clear-password", EMAIL], env)

    assert out == {"id": str(before[0]), "email": LOWER, "password": False, "session_epoch": before[5] + 1}
    assert _hash_of(env, LOWER) is None and _user_row(env, LOWER)[4] is False      # account stays enabled


@pytest.mark.parametrize("command", ["set-password", "clear-password"])
def test_password_commands_for_unknown_user_are_user_not_found(env: dict[str, str], command: str) -> None:
    with pytest.raises(ScriptError) as e:
        admin.main([command, "ghost@example.com", *(["--password-stdin"] if command == "set-password" else [])], env,
                   stdin=_stdin(PW))
    assert e.value.code == "user_not_found"
