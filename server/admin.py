"""서버 관리 CLI(서버 설계 §7 S9 최소): `python -m server.admin <명령>`. 결과는 JSON을 stdout으로, 실패는 `{"error", "message"}` + 종료 코드.

- `add-user <email> [--name N] [--daily-limit USD] [--admin]`: 초대. 이메일은 소문자로 저장.
- `disable-user <email>`: 비활성 + `session_epoch` 증가(이미 나간 세션 쿠키가 다음 요청에서 죽는다).
- `import <작업 폴더> --user <email> [--dry-run] [--overwrite] [--no-verify]`: 로컬 작업 폴더를 그 사용자 계정으로 올린다.
- `usage [--days N]`: 사용자·날짜(Asia/Seoul)별 LLM 사용량 합계(`llm_usage`, 아직 기록이 없으면 빈 목록).

환경: 사용자 관리·사용량은 `YTC_DB_OWNER_DSN`만. 가져오기는 저장소 설정 전부(`YTC_DB_DSN`, `YTC_DB_OWNER_DSN`, `YTC_S3_*`).
가져오기는 owner 롤로 사용자만 찾고, 쓰기는 모두 앱 롤 `user_tx(uid)`(RLS 경로)로 한다.

가져오기 규칙(§7):
- 작업 폴더를 걸어 파일의 POSIX 상대 경로를 키로 쓴다. `keys.check`를 통과하지 못하면 `skipped.bad_key`.
- 제외: `workspace/{tmp,trash,library}/`, `workspace/{library.lock,ui.lock,ui.log,timings.json,.last_fetch}`, `.git/`, `library.json`.
  심볼릭 링크·일반 파일이 아닌 것은 `skipped.not_regular`, 객체·공유 캐시 64MB 초과는 `skipped.too_large`.
- `keys.kind`별: doc → `PgDocStore.import_raw`, object → `S3ObjectStore.import_raw`(둘 다 **원래 mtime 보존**),
  shared → 이미 있으면 건너뜀(`S3SharedCache.import_raw`), 빈 폴더 → 폴더 행.
- 같은 sha256이면 건드리지 않는다(다시 실행해도 변경 0). 다르면 `--overwrite`가 없는 한 `conflicts`로 보고만 한다.
  `--overwrite`는 사용자가 고른 원본으로 서버 문서를 되돌리는 일이라 내 메모 가드를 거치지 않는다.
- 쓰기는 키마다 트랜잭션 하나라 중간에 멈춰도 다시 실행하면 이어진다.
- 가져온 뒤(`--dry-run`·`--no-verify` 제외) 같은 프로세스에서 로컬 `Ctx(root=폴더)`와 서버 `Ctx(user, backend)`로 `status.overview()`를
  각각 계산해 `work_root`를 뺀 JSON을 비교한다. 다르면 종료 코드 1(`import_verify_failed`, 보고서가 오류 JSON에 들어간다).
  새 계정에 가져올 때를 전제한다(이미 데이터가 있는 계정이면 합쳐진 결과와 폴더가 달라 실패할 수 있다).
"""
from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg.errors import UniqueViolation

from scripts import ctx as ctx_mod
from scripts import keys, status
from scripts.common import ArgParser, ScriptError, run
from scripts.ctx import Ctx
from server.backend import ServerBackend
from server.config import StorageSettings
from server.db import Conn, Db
from server.pgstore import PgDocStore
from server.s3store import MAX_OBJECT_BYTES, S3Bucket, S3ObjectStore, S3SharedCache

EMAIL_RE: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_LIMIT: Final = Decimal("999999.99")      # users.daily_usd_limit numeric(8,2)

# 가져오지 않는 것(§7-2). 폴더 규칙은 '/'로 끝나고, 파일 규칙은 정확한 키다.
EXCLUDED_DIRS: Final = (keys.TMP, keys.TRASH, keys.WORKSPACE + "library/", keys.GIT_DIR)
EXCLUDED_FILES: Final = tuple(keys.WORKSPACE + name for name in
                              ("library.lock", "ui.lock", "ui.log", "timings.json", ".last_fetch")) \
    + (keys.LIBRARY_CONFIG,)


# --- 걷기 -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceFile:
    key: str
    path: Path
    size: int
    mtime_ns: int


@dataclass
class Walked:
    """작업 폴더를 걸은 결과. 올릴 파일, 빈 폴더 키, 건너뛴 것(이유별)."""

    files: list[SourceFile] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)          # 비어 있는 폴더(`…/`)
    excluded: list[str] = field(default_factory=list)         # 실제로 걸린 제외 규칙
    bad_key: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)
    not_regular: list[str] = field(default_factory=list)      # 심볼릭 링크·소켓 등


def _note(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def walk_workfolder(folder: Path) -> Walked:
    """`folder` 아래 모든 파일을 키(POSIX 상대 경로)와 함께 모은다. 심볼릭 링크는 따라가지 않는다."""
    folder = Path(folder)
    out = Walked()
    for current, dirnames, filenames in os.walk(folder, followlinks=False):
        here = Path(current)
        rel_dir = here.relative_to(folder).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        kept: list[str] = []
        for name in sorted(dirnames):
            path = here / name
            dir_key = f"{prefix}{name}/"
            if path.is_symlink():
                out.not_regular.append(dir_key)
            elif dir_key in EXCLUDED_DIRS:
                _note(out.excluded, dir_key)
            else:
                kept.append(name)
        dirnames[:] = kept
        if not kept and not filenames and prefix:
            out.folders.append(prefix)
        for name in sorted(filenames):
            key = f"{prefix}{name}"
            path = here / name
            if key in EXCLUDED_FILES:
                _note(out.excluded, key)
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                out.not_regular.append(key)
                continue
            try:
                keys.check(key)
            except ScriptError:
                out.bad_key.append(key)
                continue
            if keys.kind(key) == "scratch":
                _note(out.excluded, keys.TMP)
                continue
            if keys.kind(key) != "doc" and info.st_size > MAX_OBJECT_BYTES:
                out.too_large.append(key)
                continue
            out.files.append(SourceFile(key, path, info.st_size, info.st_mtime_ns))
    valid_folders: list[str] = []
    for key in out.folders:
        try:
            valid_folders.append(keys.check(key))
        except ScriptError:
            out.bad_key.append(key)
    out.folders = valid_folders
    out.files.sort(key=lambda f: f.key)
    out.folders.sort()
    return out


# --- 가져오기 ---------------------------------------------------------------------------------

@dataclass
class Tally:
    docs: int = 0
    objects: int = 0
    shared_new: int = 0
    shared_skipped: int = 0
    folders: int = 0
    unchanged_docs: int = 0
    unchanged_objects: int = 0
    conflicts: list[dict[str, str]] = field(default_factory=list)

    def changes(self) -> int:
        return self.docs + self.objects + self.shared_new + self.folders


def _read(src: SourceFile) -> bytes:
    try:
        return src.path.read_bytes()
    except OSError as e:
        raise ScriptError("import_unreadable", f"파일을 읽을 수 없습니다({src.key}): {e}. 고친 뒤 다시 실행하면 이어서 올립니다.") from e


def import_files(walked: Walked, *, docs: PgDocStore, objects: S3ObjectStore, shared: S3SharedCache,
                 overwrite: bool, dry_run: bool) -> Tally:
    """걸은 결과를 저장소에 올린다. 쓰기는 키마다 트랜잭션 하나(중간에 멈춰도 다시 실행하면 이어진다)."""
    tally = Tally()
    for src in walked.files:
        data = _read(src)
        kind = keys.kind(src.key)
        if kind == "doc":
            outcome = docs.import_raw(src.key, data, src.mtime_ns, overwrite=overwrite, dry_run=dry_run)
        elif kind == "shared":
            outcome = shared.import_raw(src.key, data, src.mtime_ns, dry_run=dry_run)
        else:
            outcome = objects.import_raw(src.key, data, src.mtime_ns, overwrite=overwrite, dry_run=dry_run)
        if outcome == "conflict":
            tally.conflicts.append({"key": src.key, "kind": kind})
        elif kind == "shared":
            if outcome == "new":
                tally.shared_new += 1
            else:
                tally.shared_skipped += 1
        elif outcome == "same":
            if kind == "doc":
                tally.unchanged_docs += 1
            else:
                tally.unchanged_objects += 1
        elif kind == "doc":
            tally.docs += 1
        else:
            tally.objects += 1
    for prefix in walked.folders:
        if docs.folder_exists(prefix):
            continue
        tally.folders += 1
        if not dry_run:
            docs.ensure_folder(prefix)
    return tally


def _strip_label(overview: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in overview.items() if k != "work_root"}


def _same(a: Any, b: Any) -> bool:
    """JSON 값 비교. 실수는 1ms 안이면 같다: 서버 `Meta.mtime`은 `mtime_ns / 1e9`, 로컬은 `st_mtime`이라 마지막 자리가 다르다."""
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 1e-3
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def verify_status(folder: Path, backend: ServerBackend, user_id: uuid.UUID) -> dict[str, Any]:
    """같은 작업 폴더를 로컬 Ctx와 서버 Ctx로 읽은 `status.overview()`가 같은가(`work_root` 제외)."""
    with ctx_mod.use(Ctx(root=Path(folder))):
        local = _strip_label(status.overview())
    with ctx_mod.use(Ctx(user_id=str(user_id), backend=backend)):
        server = _strip_label(status.overview())
    differing = sorted(k for k in local.keys() | server.keys() if not _same(local.get(k), server.get(k)))
    return {"ok": not differing, "differing": differing}


# --- 명령 -------------------------------------------------------------------------------------

def _owner_dsn(env: Mapping[str, str]) -> str:
    dsn = (env.get("YTC_DB_OWNER_DSN") or "").strip()
    if not dsn:
        raise ScriptError("config_missing", "서버 설정 YTC_DB_OWNER_DSN(owner 롤 접속 정보)이 없습니다. 관리 명령에 필요합니다.")
    return dsn


@contextmanager
def _owner_tx(env: Mapping[str, str]) -> Iterator[Conn]:
    with psycopg.connect(_owner_dsn(env)) as conn, conn.transaction():
        yield conn


def _email(text: str) -> str:
    email = text.strip().lower()
    if not EMAIL_RE.match(email):
        raise ScriptError("bad_email", f"이메일 형식이 아닙니다: {text!r}")
    return email


def _limit(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as e:
        raise ScriptError("bad_limit", f"일 한도는 달러 숫자여야 합니다: {text!r}") from e
    if not value.is_finite() or value < 0 or value > MAX_LIMIT or value != value.quantize(Decimal("0.01")):
        raise ScriptError("bad_limit", f"일 한도는 0 이상 {MAX_LIMIT} 이하, 소수 둘째 자리까지입니다: {text!r}")
    return value


def add_user(env: Mapping[str, str], email: str, name: str | None, daily_limit: str | None,
             admin: bool) -> dict[str, Any]:
    address = _email(email)
    limit = _limit(daily_limit)
    try:
        with _owner_tx(env) as conn:
            row = conn.execute("INSERT INTO users (email, display_name, daily_usd_limit, is_admin) "
                               "VALUES (%s, %s, %s, %s) RETURNING id", (address, name, limit, admin)).fetchone()
    except UniqueViolation as e:
        raise ScriptError("user_exists", f"이미 있는 사용자입니다: {address}") from e
    assert row is not None
    return {"id": str(row[0]), "email": address, "display_name": name, "is_admin": admin,
            "daily_usd_limit": None if limit is None else float(limit)}


def disable_user(env: Mapping[str, str], email: str) -> dict[str, Any]:
    address = _email(email)
    with _owner_tx(env) as conn:
        row = conn.execute("UPDATE users SET disabled = true, session_epoch = session_epoch + 1 "
                           "WHERE email = %s RETURNING id, session_epoch", (address,)).fetchone()
    if row is None:
        raise ScriptError("user_not_found", f"없는 사용자입니다: {address}")
    return {"id": str(row[0]), "email": address, "disabled": True, "session_epoch": int(row[1])}


def usage(env: Mapping[str, str], days: int) -> dict[str, Any]:
    if days < 1:
        raise ScriptError("bad_days", "--days는 1 이상이어야 합니다")
    with _owner_tx(env) as conn:
        rows = conn.execute(
            "SELECT u.email, (l.at AT TIME ZONE 'Asia/Seoul')::date AS day, count(*), "
            "count(*) FILTER (WHERE NOT l.ok), sum(l.input_tokens), sum(l.output_tokens), sum(l.cost_usd) "
            "FROM llm_usage l JOIN users u ON u.id = l.user_id "
            "WHERE (l.at AT TIME ZONE 'Asia/Seoul')::date > (now() AT TIME ZONE 'Asia/Seoul')::date - %s "
            "GROUP BY u.email, day ORDER BY day, u.email", (days,)).fetchall()
    out: list[dict[str, Any]] = []
    total = Decimal(0)
    for email, day, calls, failed, tokens_in, tokens_out, cost in rows:
        assert isinstance(day, date)
        total += cost
        out.append({"email": str(email), "day": day.isoformat(), "calls": int(calls), "failed": int(failed),
                    "input_tokens": int(tokens_in), "output_tokens": int(tokens_out), "cost_usd": float(cost)})
    return {"days": days, "rows": out, "total_usd": float(total)}


def import_workfolder(env: Mapping[str, str], folder: str, user: str, *, dry_run: bool, overwrite: bool,
                      verify: bool) -> dict[str, Any]:
    address = _email(user)
    source = Path(folder).resolve()
    if not source.is_dir():
        raise ScriptError("folder_not_found", f"작업 폴더가 없습니다: {folder}")
    settings = StorageSettings.from_env(env)
    with _owner_tx(env) as conn:
        row = conn.execute("SELECT id FROM users WHERE email = %s", (address,)).fetchone()
    if row is None:
        raise ScriptError("user_not_found", f"없는 사용자입니다: {address}. 먼저 add-user로 만드세요.")
    uid = uuid.UUID(str(row[0]))
    walked = walk_workfolder(source)
    db = Db(settings.db_dsn, settings.db_owner_dsn, min_size=settings.db_pool_min, max_size=settings.db_pool_max)
    db.open()
    try:
        bucket = S3Bucket.connect(settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key,
                                  settings.s3_bucket, max_pool_connections=settings.s3_max_pool_connections)
        tally = import_files(walked, docs=PgDocStore(db, uid, address), objects=S3ObjectStore(db, bucket, uid),
                             shared=S3SharedCache(db, bucket, uid), overwrite=overwrite, dry_run=dry_run)
        report: dict[str, Any] = {
            "user": address, "dry_run": dry_run, "docs": tally.docs, "objects": tally.objects,
            "shared_new": tally.shared_new, "shared_skipped": tally.shared_skipped, "folders": tally.folders,
            "unchanged": {"docs": tally.unchanged_docs, "objects": tally.unchanged_objects},
            "skipped": {"bad_key": walked.bad_key, "too_large": walked.too_large, "not_regular": walked.not_regular},
            "excluded": walked.excluded, "conflicts": tally.conflicts, "changes": tally.changes(),
        }
        if verify and not dry_run:
            with tempfile.TemporaryDirectory(prefix="ytc-admin-") as tmp:
                backend = ServerBackend(db, bucket, scratch_dir=Path(tmp) / "scratch", state_dir=Path(tmp) / "state")
                report["verify"] = verify_status(source, backend, uid)
            if not report["verify"]["ok"]:
                raise ScriptError("import_verify_failed",
                                  "가져온 뒤 현황이 원본 폴더와 다릅니다(status 비교). 새 계정에 가져왔는지 확인하세요.",
                                  report=report)
    finally:
        db.close()
    return report


# --- 진입점 -----------------------------------------------------------------------------------

def _parser() -> ArgParser:
    parser = ArgParser(prog="python -m server.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add-user")
    add.add_argument("email")
    add.add_argument("--name")
    add.add_argument("--daily-limit", dest="daily_limit")
    add.add_argument("--admin", action="store_true")
    dis = sub.add_parser("disable-user")
    dis.add_argument("email")
    imp = sub.add_parser("import")
    imp.add_argument("folder")
    imp.add_argument("--user", required=True)
    imp.add_argument("--dry-run", dest="dry_run", action="store_true")
    imp.add_argument("--overwrite", action="store_true")
    imp.add_argument("--no-verify", dest="verify", action="store_false")
    use = sub.add_parser("usage")
    use.add_argument("--days", type=int, default=7)
    return parser


def main(argv: list[str], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    args = _parser().parse_args(argv)
    environ = os.environ if env is None else env
    if args.command == "add-user":
        return add_user(environ, args.email, args.name, args.daily_limit, args.admin)
    if args.command == "disable-user":
        return disable_user(environ, args.email)
    if args.command == "import":
        return import_workfolder(environ, args.folder, args.user, dry_run=args.dry_run, overwrite=args.overwrite,
                                 verify=args.verify)
    return usage(environ, args.days)


if __name__ == "__main__":
    run(main, sys.argv[1:])
