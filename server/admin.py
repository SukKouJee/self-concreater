"""서버 관리 CLI(서버 설계 §7 S9 최소): `python -m server.admin <명령>`. 결과는 JSON을 stdout으로, 실패는 `{"error", "message"}` + 종료 코드.

- `add-user <email> [--name N] [--daily-limit USD] [--admin] [--password [-] | --password-stdin]`: 초대. 이메일은 소문자로 저장.
  `--password`(값 없이 또는 `-`)면 터미널에서 두 번 묻고(화면에 보이지 않음), `--password-stdin`이면 표준 입력 한 줄(스크립트용).
  `--password <값>`도 받지만 셸 기록·프로세스 목록에 남으므로 권하지 않는다.
- `set-password <email> [--password-stdin]`: 비밀번호를 정하거나 바꾼다(기본은 터미널에서 묻기). `session_epoch`도 올려 이전 세션을 모두 끊는다.
- `clear-password <email>`: 비밀번호를 지운다(그 계정은 비밀번호로 로그인할 수 없다, Google 로그인은 그대로). `session_epoch`도 올린다.
  비밀번호 규칙은 10자 이상(`server.passwords.check_policy`). 비밀번호와 해시는 출력·로그 어디에도 남기지 않는다.
- `disable-user <email>`: 비활성 + `session_epoch` 증가(이미 나간 세션 쿠키가 다음 요청에서 죽는다).
- `logout-all <email>`: `session_epoch`만 증가(모든 기기의 세션이 끊기고, 다시 로그인하면 된다. 기기 분실 등).
- `make-admin <email>` / `revoke-admin <email>`: 관리자 권한을 주거나 걷는다(owner 경로, 첫 관리자 만들기·잠긴 관리 화면 되살리기).
  감사 기록(`admin_audit`, 행위자 NULL, detail `via: cli`)을 남기고, 출력의 `admins`는 남은 활성 관리자 수다. 웹 관리 화면(T16)과 달리
  마지막 관리자도 걷을 수 있다(운영자의 탈출구). 세션은 끊지 않는다(관리자 여부는 요청마다 읽는다). 출력의 `disabled`가 true면
  그 계정은 로그인할 수 없으니 `enable-user`를 먼저(또는 함께) 실행한다.
- `enable-user <email>`: 비활성을 푼다(owner 경로, 감사 기록 `enable`·`via: cli`). 세션 epoch는 그대로(끊긴 세션은 이미 끊겼다).
- `set-email <email> <새 이메일>`: 이메일을 바꾼다(소문자, 이미 있으면 `user_exists`). Google 연결(`google_sub`)을 풀고 모든 세션을 끊는다.
- `set-name <email> <이름>`: 표시 이름(100자 이하, 빈 값이면 지움).
- `delete-user <email> --confirm <같은 이메일>`: 사용자와 그 데이터(문서·폴더·파일·휴지통 행, MinIO `users/{id}/`·`trash/{id}/`)를
  지운다. 공유 캐시는 그대로, 사용량 기록은 익명으로 남는다. 마지막 관리자도 지울 수 있다(운영자 탈출구, 출력 `admins`).
  앱이 돌리던 그 사용자의 작업은 멈추지 않는다(다른 프로세스) — 먼저 `disable-user`로 세션을 끊어 두거나 웹 관리 화면에서 지운다.
  저장소 설정(`YTC_S3_*` 등)이 필요하다. MinIO 정리가 실패하면 출력 `storage.ok: false`(행은 이미 지워졌다, S10 점검).
  이 세 명령은 0004의 내부 함수(`admin_apply_*`, 웹 관리 화면과 같은 코드)를 owner로 부르고, 감사 기록은 행위자 NULL·`via: cli`.
- `import <작업 폴더> --user <email> [--dry-run] [--overwrite] [--overwrite-memo] [--no-verify]`: 로컬 작업 폴더를 그 사용자 계정으로 올린다.
- `usage [--days N]`: 사용자·날짜(Asia/Seoul)별 LLM 사용량 합계(`llm_usage`, 아직 기록이 없으면 빈 목록).

환경: 사용자 관리·사용량은 `YTC_DB_OWNER_DSN`만. 가져오기는 저장소 설정 전부(`YTC_DB_DSN`, `YTC_DB_OWNER_DSN`, `YTC_S3_*`).
가져오기는 owner 롤로 사용자만 찾고, 쓰기는 모두 앱 롤 `user_tx(uid)`(RLS 경로)로 한다.

가져오기 규칙(§7):
- 작업 폴더를 걸어 파일의 POSIX 상대 경로를 키로 쓴다. `keys.check`를 통과하지 못하면 `skipped.bad_key`.
- 제외: `workspace/{tmp,trash,library}/`, `workspace/{library.lock,ui.lock,ui.log,timings.json,.last_fetch}`, `.git/`, `library.json`.
  심볼릭 링크·Windows 정션(재해석 지점)·일반 파일이 아닌 것은 `skipped.not_regular`, 64MB 초과는(문서도) `skipped.too_large`.
- 비활성 사용자에게는 가져오지 않는다(`user_disabled`).
- `keys.kind`별: doc → `PgDocStore.import_raw`, object → `S3ObjectStore.import_raw`(둘 다 **원래 mtime 보존**),
  shared → 이미 있으면 건너뜀(`S3SharedCache.import_raw`), 빈 폴더 → 폴더 행.
- 같은 sha256이면 건드리지 않는다(다시 실행해도 변경 0). 다르면 `--overwrite`가 없는 한 `conflicts`(reason `differs`)로 보고만 한다.
  `--overwrite`로 덮어쓸 때도 서버 문서의 `## 내 메모`가 바뀌게 되면 건드리지 않고 reason `memo`로 보고한다.
  메모까지 통째로 되돌리려면 `--overwrite-memo`(명시적 복원). 덮어쓴 키는 `replaced`에 나온다.
- 충돌이 하나라도 있으면 종료 코드는 0이지만 보고서는 `ok: false`·`message`이고 검증은 건너뛴다(`verify: "skipped_conflicts"`).
- 쓰기는 키마다 트랜잭션 하나라 중간에 멈춰도 다시 실행하면 이어진다.
- 가져온 뒤(`--dry-run`·`--no-verify` 제외) 같은 프로세스에서 로컬 `Ctx(root=폴더)`와 서버 `Ctx(user, backend)`로 `status.overview()`를
  각각 계산해 `work_root`를 뺀 JSON을 비교한다. 다르면 종료 코드 1(`import_verify_failed`, 보고서가 오류 JSON에 들어간다).
  새 계정에 가져올 때를 전제한다(이미 데이터가 있는 계정이면 합쳐진 결과와 폴더가 달라 실패할 수 있다).
"""
from __future__ import annotations

import getpass
import os
import re
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, TextIO

import psycopg
from psycopg.errors import UniqueViolation

from scripts import ctx as ctx_mod
from scripts import keys, status
from scripts.common import ArgParser, ScriptError, run
from scripts.ctx import Ctx
from server import passwords
from server.backend import ServerBackend
from server.config import StorageSettings
from server.db import Conn, Db
from server.pgstore import PgDocStore
from server.s3store import MAX_OBJECT_BYTES, S3Bucket, S3ObjectStore, S3SharedCache, purge_user_objects

EMAIL_RE: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_LIMIT: Final = Decimal("999999.99")      # users.daily_usd_limit numeric(8,2)
MAX_NAME: Final = 100
VIA_CLI: Final = "cli"
# 0004 function / constraint SQLSTATE -> CLI error (code, message)
CLI_REFUSALS: Final[dict[str, tuple[str, str]]] = {
    "YTA02": ("user_not_found", "없는 사용자입니다"),
    "23505": ("user_exists", "이미 있는 이메일입니다"),
    "23514": ("bad_input", "값이 올바르지 않습니다"),
    "YTA05": ("bad_input", "값이 올바르지 않습니다"),
}

# 가져오지 않는 것(§7-2). 폴더 규칙은 '/'로 끝나고, 파일 규칙은 정확한 키다.
EXCLUDED_DIRS: Final = (keys.TMP, keys.TRASH, keys.WORKSPACE + "library/", keys.GIT_DIR)
EXCLUDED_FILES: Final = tuple(keys.WORKSPACE + name for name in
                              ("library.lock", "ui.lock", "ui.log", "timings.json", ".last_fetch")) \
    + (keys.LIBRARY_CONFIG,)


# --- 걷기 -------------------------------------------------------------------------------------

REPARSE_POINT: Final = 0x400      # FILE_ATTRIBUTE_REPARSE_POINT (Windows junctions, mount points, ...)


def _is_link_like(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


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
    """`folder` 아래 모든 파일을 키(POSIX 상대 경로)와 함께 모은다. 심볼릭 링크·정션은 따라가지 않고,
    걷는 폴더가 원본 폴더 밖으로 풀리면(resolve) 건너뛴다."""
    folder = Path(folder)
    real_root = folder.resolve()
    out = Walked()
    for current, dirnames, filenames in os.walk(folder, followlinks=False):
        here = Path(current)
        rel_dir = here.relative_to(folder).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        was_empty = not dirnames and not filenames          # before any filtering
        kept: list[str] = []
        for name in sorted(dirnames):
            path = here / name
            dir_key = f"{prefix}{name}/"
            if path.is_symlink() or _is_link_like(path.lstat()) or not path.resolve().is_relative_to(real_root):
                out.not_regular.append(dir_key)
            elif dir_key in EXCLUDED_DIRS:
                _note(out.excluded, dir_key)
            else:
                kept.append(name)
        dirnames[:] = kept
        if was_empty and prefix and not prefix.startswith((*keys.SHARED_PREFIXES, keys.TMP)):
            out.folders.append(prefix)
        for name in sorted(filenames):
            key = f"{prefix}{name}"
            path = here / name
            if key in EXCLUDED_FILES:
                _note(out.excluded, key)
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or _is_link_like(info):
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
            if info.st_size > MAX_OBJECT_BYTES:
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
    replaced: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)       # grew past the limit between the walk and the read

    def changes(self) -> int:
        return self.docs + self.objects + self.shared_new + self.folders


def _read(src: SourceFile) -> bytes:
    try:
        return src.path.read_bytes()
    except OSError as e:
        raise ScriptError("import_unreadable", f"파일을 읽을 수 없습니다({src.key}): {e}. 고친 뒤 다시 실행하면 이어서 올립니다.") from e


def import_files(walked: Walked, *, docs: PgDocStore, objects: S3ObjectStore, shared: S3SharedCache,
                 overwrite: bool, overwrite_memo: bool = False, dry_run: bool) -> Tally:
    """걸은 결과를 저장소에 올린다. 쓰기는 키마다 트랜잭션 하나(중간에 멈춰도 다시 실행하면 이어진다)."""
    tally = Tally()
    for src in walked.files:
        data = _read(src)
        if len(data) > MAX_OBJECT_BYTES:
            tally.too_large.append(src.key)
            continue
        kind = keys.kind(src.key)
        if kind == "doc":
            outcome = docs.import_raw(src.key, data, src.mtime_ns, overwrite=overwrite,
                                      overwrite_memo=overwrite_memo, dry_run=dry_run)
        elif kind == "shared":
            outcome = shared.import_raw(src.key, data, src.mtime_ns, dry_run=dry_run)
        else:
            outcome = objects.import_raw(src.key, data, src.mtime_ns, overwrite=overwrite, dry_run=dry_run)
        if outcome in ("conflict", "memo"):
            tally.conflicts.append({"key": src.key, "kind": kind, "reason": "memo" if outcome == "memo" else "differs"})
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
        else:
            if outcome == "replaced":
                tally.replaced.append(src.key)
            if kind == "doc":
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
    """JSON 값 비교. 실수는 1µs 안이면 같다: 서버 `Meta.mtime`은 `mtime_ns / 1e9`, 로컬은 `st_mtime`이라 마지막 자리가 다르다."""
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 1e-6
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


Prompt = Callable[[str], str]
PROMPT_DASH: Final = "-"


def read_password(*, stdin: TextIO, from_stdin: bool, prompt: Prompt, tty: bool | None = None) -> str:
    """새 비밀번호를 읽는다: 표준 입력 한 줄(줄 끝만 뗀다) 또는 터미널에서 두 번(같아야 한다). 규칙 검사 전의 값."""
    if from_stdin:
        return stdin.readline().removesuffix("\n").removesuffix("\r")
    if not (stdin.isatty() if tty is None else tty):
        raise ScriptError("no_tty", "비밀번호를 물을 터미널이 없습니다. 스크립트에서는 --password-stdin으로 표준 입력에 넘기세요.")
    first = prompt("새 비밀번호(10자 이상): ")
    if prompt("한 번 더: ") != first:
        raise ScriptError("password_mismatch", "두 번 입력한 비밀번호가 다릅니다. 다시 실행하세요.")
    return first


def new_password_hash(value: str | None, *, from_stdin: bool, stdin: TextIO, prompt: Prompt,
                      tty: bool | None = None) -> str | None:
    """`--password`의 값(None이면 비밀번호 없음, `-`면 묻기)·`--password-stdin` → 규칙을 통과한 비밀번호의 해시."""
    if value is None and not from_stdin:
        return None
    if from_stdin or value == PROMPT_DASH:
        value = read_password(stdin=stdin, from_stdin=from_stdin, prompt=prompt, tty=tty)
    return passwords.hash_password(passwords.check_policy(value))


def add_user(env: Mapping[str, str], email: str, name: str | None, daily_limit: str | None,
             admin: bool, password_hash: str | None = None) -> dict[str, Any]:
    address = _email(email)
    limit = _limit(daily_limit)
    try:
        with _owner_tx(env) as conn:
            row = conn.execute("INSERT INTO users (email, display_name, daily_usd_limit, is_admin, password_hash) "
                               "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                               (address, name, limit, admin, password_hash)).fetchone()
    except UniqueViolation as e:
        raise ScriptError("user_exists", f"이미 있는 사용자입니다: {address}") from e
    assert row is not None
    return {"id": str(row[0]), "email": address, "display_name": name, "is_admin": admin,
            "daily_usd_limit": None if limit is None else float(limit), "password": password_hash is not None}


def set_password(env: Mapping[str, str], email: str, password_hash: str | None) -> dict[str, Any]:
    """비밀번호를 정하거나(해시) 지우고(None) `session_epoch`를 올린다(이전 세션은 다음 요청에서 끊긴다)."""
    address = _email(email)
    with _owner_tx(env) as conn:
        row = conn.execute("UPDATE users SET password_hash = %s, session_epoch = session_epoch + 1 "
                           "WHERE email = %s RETURNING id, session_epoch", (password_hash, address)).fetchone()
    if row is None:
        raise ScriptError("user_not_found", f"없는 사용자입니다: {address}. 먼저 add-user로 만드세요.")
    return {"id": str(row[0]), "email": address, "password": password_hash is not None, "session_epoch": int(row[1])}


def disable_user(env: Mapping[str, str], email: str) -> dict[str, Any]:
    address = _email(email)
    with _owner_tx(env) as conn:
        row = conn.execute("UPDATE users SET disabled = true, session_epoch = session_epoch + 1 "
                           "WHERE email = %s RETURNING id, session_epoch", (address,)).fetchone()
    if row is None:
        raise ScriptError("user_not_found", f"없는 사용자입니다: {address}")
    return {"id": str(row[0]), "email": address, "disabled": True, "session_epoch": int(row[1])}


def logout_all(env: Mapping[str, str], email: str) -> dict[str, Any]:
    """그 사용자의 모든 세션을 끊는다(`session_epoch` 증가). 비활성으로 만들지 않는다."""
    address = _email(email)
    with _owner_tx(env) as conn:
        row = conn.execute("UPDATE users SET session_epoch = session_epoch + 1 "
                           "WHERE email = %s RETURNING id, session_epoch, disabled", (address,)).fetchone()
    if row is None:
        raise ScriptError("user_not_found", f"없는 사용자입니다: {address}")
    return {"id": str(row[0]), "email": address, "disabled": bool(row[2]), "session_epoch": int(row[1])}


def set_admin(env: Mapping[str, str], email: str, is_admin: bool) -> dict[str, Any]:
    """owner로 `is_admin`을 바꾸고 감사 행(행위자 없음)을 남긴다. 남은 활성 관리자 수를 함께 돌려준다."""
    address = _email(email)
    with _owner_tx(env) as conn:
        row = conn.execute("UPDATE users SET is_admin = %s WHERE email = %s RETURNING id, disabled",
                           (is_admin, address)).fetchone()
        if row is None:
            raise ScriptError("user_not_found", f"없는 사용자입니다: {address}. 먼저 add-user로 만드세요.")
        conn.execute("INSERT INTO admin_audit (actor_id, action, target_user_id, detail) "
                     "VALUES (NULL, 'set_admin', %s, jsonb_build_object('is_admin', %s::boolean, 'via', 'cli'))",
                     (row[0], is_admin))
        left = conn.execute("SELECT count(*) FROM users WHERE is_admin AND NOT disabled").fetchone()
    assert left is not None
    return {"id": str(row[0]), "email": address, "is_admin": is_admin, "disabled": bool(row[1]), "admins": int(left[0])}


def enable_user(env: Mapping[str, str], email: str) -> dict[str, Any]:
    """owner로 비활성을 푼다(감사 행, 행위자 없음). epoch는 바꾸지 않는다."""
    address = _email(email)
    with _owner_tx(env) as conn:
        row = conn.execute("UPDATE users SET disabled = false WHERE email = %s RETURNING id, session_epoch",
                           (address,)).fetchone()
        if row is None:
            raise ScriptError("user_not_found", f"없는 사용자입니다: {address}")
        conn.execute("INSERT INTO admin_audit (actor_id, action, target_user_id, detail) "
                     "VALUES (NULL, 'enable', %s, jsonb_build_object('via', 'cli'))", (row[0],))
    return {"id": str(row[0]), "email": address, "disabled": False, "session_epoch": int(row[1])}


def _refused(e: psycopg.Error, address: str) -> ScriptError | None:
    found = CLI_REFUSALS.get(e.sqlstate or "")
    return None if found is None else ScriptError(found[0], f"{found[1]}: {address}")


def _user_id(conn: Conn, address: str) -> uuid.UUID:
    row = conn.execute("SELECT id FROM users WHERE email = %s", (address,)).fetchone()
    if row is None:
        raise ScriptError("user_not_found", f"없는 사용자입니다: {address}")
    return uuid.UUID(str(row[0]))


def set_email(env: Mapping[str, str], email: str, new_email: str) -> dict[str, Any]:
    """owner로 `admin_apply_email`(0004): 소문자·UNIQUE, Google 연결 풀기, epoch 증가, 감사 `via: cli`."""
    address, target = _email(email), _email(new_email)
    try:
        with _owner_tx(env) as conn:
            uid = _user_id(conn, address)
            row = conn.execute("SELECT public.admin_apply_email(%s, %s, NULL, %s)", (uid, target, VIA_CLI)).fetchone()
    except psycopg.Error as e:
        refused = _refused(e, target)
        if refused is None:
            raise
        raise refused from e
    assert row is not None
    return {"id": str(uid), "email": target, "previous": address, "session_epoch": int(row[0])}


def _display_name(text: str) -> str | None:
    name = text.strip()
    if len(name) > MAX_NAME:
        raise ScriptError("bad_name", f"이름은 {MAX_NAME}자 이하여야 합니다")
    return name or None


def set_name(env: Mapping[str, str], email: str, name: str) -> dict[str, Any]:
    """owner로 `admin_apply_name`(0004). 빈 이름은 지운다."""
    address, value = _email(email), _display_name(name)
    with _owner_tx(env) as conn:
        uid = _user_id(conn, address)
        conn.execute("SELECT public.admin_apply_name(%s, %s, NULL, %s)", (uid, value, VIA_CLI))
    return {"id": str(uid), "email": address, "display_name": value}


def delete_user(env: Mapping[str, str], email: str, confirm: str) -> dict[str, Any]:
    """owner로 `admin_apply_delete`(0004, 행·감사, 커밋) → MinIO `users/{id}/`·`trash/{id}/` 정리.
    `--confirm`은 같은 이메일이어야 한다(대소문자·앞뒤 공백 무시)."""
    address = _email(email)
    if _email(confirm) != address:
        raise ScriptError("confirm_mismatch", f"--confirm에 지울 사용자의 이메일을 그대로 넣으세요: {address}")
    settings = StorageSettings.from_env(env)
    with _owner_tx(env) as conn:
        uid = _user_id(conn, address)
        row = conn.execute("SELECT public.admin_apply_delete(%s, NULL, %s)", (uid, VIA_CLI)).fetchone()
        left = conn.execute("SELECT count(*) FROM users WHERE is_admin AND NOT disabled").fetchone()
    assert row is not None and left is not None
    summary: dict[str, Any] = dict(row[0])
    bucket = S3Bucket.connect(settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key, settings.s3_bucket,
                              max_pool_connections=settings.s3_max_pool_connections)
    storage = purge_user_objects(bucket, uid, [str(k) for k in summary.pop("s3_keys", [])])
    deleted = {k: summary.get(k, 0) for k in ("docs", "folders", "objects", "trash", "usage_rows", "usage_usd")}
    return {"id": str(uid), "email": address, "deleted": deleted, "storage": storage, "admins": int(left[0])}


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
                      overwrite_memo: bool = False, verify: bool) -> dict[str, Any]:
    address = _email(user)
    source = Path(folder).resolve()
    if not source.is_dir():
        raise ScriptError("folder_not_found", f"작업 폴더가 없습니다: {folder}")
    settings = StorageSettings.from_env(env)
    with _owner_tx(env) as conn:
        row = conn.execute("SELECT id, disabled FROM users WHERE email = %s", (address,)).fetchone()
    if row is None:
        raise ScriptError("user_not_found", f"없는 사용자입니다: {address}. 먼저 add-user로 만드세요.")
    if row[1]:
        raise ScriptError("user_disabled", f"비활성 사용자에게는 가져올 수 없습니다: {address}")
    uid = uuid.UUID(str(row[0]))
    walked = walk_workfolder(source)
    db = Db(settings.db_dsn, settings.db_owner_dsn, min_size=settings.db_pool_min, max_size=settings.db_pool_max)
    db.open()
    try:
        bucket = S3Bucket.connect(settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key,
                                  settings.s3_bucket, max_pool_connections=settings.s3_max_pool_connections)
        tally = import_files(walked, docs=PgDocStore(db, uid, address), objects=S3ObjectStore(db, bucket, uid),
                             shared=S3SharedCache(db, bucket, uid), overwrite=overwrite,
                             overwrite_memo=overwrite_memo, dry_run=dry_run)
        too_large = sorted({*walked.too_large, *tally.too_large})
        report: dict[str, Any] = {
            "user": address, "dry_run": dry_run, "ok": not tally.conflicts, "docs": tally.docs,
            "objects": tally.objects,
            "shared_new": tally.shared_new, "shared_skipped": tally.shared_skipped, "folders": tally.folders,
            "unchanged": {"docs": tally.unchanged_docs, "objects": tally.unchanged_objects},
            "skipped": {"bad_key": walked.bad_key, "too_large": too_large, "not_regular": walked.not_regular},
            "excluded": walked.excluded, "replaced": tally.replaced, "conflicts": tally.conflicts,
            "changes": tally.changes(),
        }
        if tally.conflicts:
            report["message"] = (f"{len(tally.conflicts)}개 파일은 서버와 내용이 달라 건드리지 않았습니다. 서버 것을 원본으로 되돌리려면 "
                                 "--overwrite, `## 내 메모`까지 되돌리려면 --overwrite-memo로 다시 실행하세요.")
            if verify and not dry_run:
                report["verify"] = "skipped_conflicts"
        elif verify and not dry_run:
            with tempfile.TemporaryDirectory(prefix="ytc-admin-") as tmp:
                backend = ServerBackend(db, bucket, scratch_dir=Path(tmp) / "scratch", state_dir=Path(tmp) / "state")
                report["verify"] = verify_status(source, backend, uid)
            if not report["verify"]["ok"]:
                report["ok"] = False
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
    secret = add.add_mutually_exclusive_group()
    secret.add_argument("--password", nargs="?", const=PROMPT_DASH,
                        help="비밀번호 로그인용 비밀번호. 값 없이(또는 -) 주면 터미널에서 묻는다")
    secret.add_argument("--password-stdin", dest="password_stdin", action="store_true",
                        help="비밀번호를 표준 입력 한 줄에서 읽는다(스크립트용)")
    setpw = sub.add_parser("set-password", help="비밀번호를 정하거나 바꾼다(모든 기기의 세션도 끊는다)")
    setpw.add_argument("email")
    setpw.add_argument("--password-stdin", dest="password_stdin", action="store_true")
    clear = sub.add_parser("clear-password", help="비밀번호를 지워 비밀번호 로그인을 막는다(세션도 끊는다)")
    clear.add_argument("email")
    dis = sub.add_parser("disable-user")
    dis.add_argument("email")
    out = sub.add_parser("logout-all", help="그 사용자의 모든 기기 세션을 끊는다(계정은 그대로)")
    out.add_argument("email")
    make = sub.add_parser("make-admin", help="관리자로 만든다(웹 관리 화면을 쓸 수 있다)")
    make.add_argument("email")
    revoke = sub.add_parser("revoke-admin", help="관리자 권한을 걷는다")
    revoke.add_argument("email")
    enable = sub.add_parser("enable-user", help="비활성을 푼다(관리자로 되살릴 계정은 먼저 이것으로)")
    enable.add_argument("email")
    mail = sub.add_parser("set-email", help="이메일을 바꾼다(Google 연결을 풀고 모든 세션을 끊는다)")
    mail.add_argument("email")
    mail.add_argument("new_email")
    name = sub.add_parser("set-name", help="표시 이름을 바꾼다(빈 값이면 지운다)")
    name.add_argument("email")
    name.add_argument("name")
    delete = sub.add_parser("delete-user", help="사용자와 그 데이터를 지운다(되돌릴 수 없다)")
    delete.add_argument("email")
    delete.add_argument("--confirm", required=True, help="확인을 위해 같은 이메일을 한 번 더")
    imp = sub.add_parser("import")
    imp.add_argument("folder")
    imp.add_argument("--user", required=True)
    imp.add_argument("--dry-run", dest="dry_run", action="store_true")
    imp.add_argument("--overwrite", action="store_true",
                     help="서버와 내용이 다른 파일을 원본으로 덮어쓴다(서버의 내 메모가 바뀌는 문서는 건드리지 않는다)")
    imp.add_argument("--overwrite-memo", dest="overwrite_memo", action="store_true",
                     help="내 메모까지 원본으로 통째로 되돌린다(--overwrite 포함)")
    imp.add_argument("--no-verify", dest="verify", action="store_false")
    use = sub.add_parser("usage")
    use.add_argument("--days", type=int, default=7)
    return parser


def main(argv: list[str], env: Mapping[str, str] | None = None, *, stdin: TextIO | None = None,
         prompt: Prompt | None = None, tty: bool | None = None) -> dict[str, Any]:
    """stdin·prompt·tty는 테스트용(기본: sys.stdin, getpass.getpass, stdin이 터미널인지)."""
    args = _parser().parse_args(argv)
    environ = os.environ if env is None else env
    reader = {"stdin": sys.stdin if stdin is None else stdin, "prompt": getpass.getpass if prompt is None else prompt,
              "tty": tty}
    if args.command == "add-user":
        hashed = new_password_hash(args.password, from_stdin=args.password_stdin, **reader)
        return add_user(environ, args.email, args.name, args.daily_limit, args.admin, hashed)
    if args.command == "set-password":
        return set_password(environ, args.email,
                            new_password_hash(PROMPT_DASH, from_stdin=args.password_stdin, **reader))
    if args.command == "clear-password":
        return set_password(environ, args.email, None)
    if args.command == "disable-user":
        return disable_user(environ, args.email)
    if args.command == "logout-all":
        return logout_all(environ, args.email)
    if args.command == "enable-user":
        return enable_user(environ, args.email)
    if args.command == "set-email":
        return set_email(environ, args.email, args.new_email)
    if args.command == "set-name":
        return set_name(environ, args.email, args.name)
    if args.command == "delete-user":
        return delete_user(environ, args.email, args.confirm)
    if args.command in ("make-admin", "revoke-admin"):
        return set_admin(environ, args.email, args.command == "make-admin")
    if args.command == "import":
        return import_workfolder(environ, args.folder, args.user, dry_run=args.dry_run, overwrite=args.overwrite,
                                 overwrite_memo=args.overwrite_memo, verify=args.verify)
    return usage(environ, args.days)


if __name__ == "__main__":
    run(main, sys.argv[1:])
