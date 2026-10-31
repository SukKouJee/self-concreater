"""PostgreSQL 문서 저장소(서버 설계 §3.1·§3.3, ADR-S3): `scripts.stores.DocStore`를 `docs`·`folders` 테이블로 구현한다.
휴지통(`PgTrash`)도 여기 있다: 문서·객체를 `trash_items` 행(+ MinIO `trash/{uid}/{stamp}/{key}`)으로 옮긴다.

- 원문 바이트(`docs.raw`)를 그대로 저장한다. version = sha256(raw), `Doc.label` = 키, `label()` = `ytc:{email}`.
- 모든 질의는 `Db.user_tx(uid)` 안에서 돈다(RLS가 최종 방어선). WHERE에도 user_id를 적는다(이중 방어, PK 범위 질의).
- 키 검사와 종류 오류는 FileDocStore와 같은 순서·예외: `.md`가 아니면 ValueError, 그다음 `keys.check`(bad_path).
- 목록은 prefix 바로 아래만, 이름 일치는 대소문자 무시 fnmatch(Windows pathlib glob과 같은 정규식),
  순서는 `sorted(key=PureWindowsPath)` — Windows FileStore의 `sorted(glob)`과 같다(§3.3).
- 접두사 질의는 `starts_with(key, prefix)`: 설계 §3.1의 `key < prefix || U&'\\FFFF'` 상한은
  COLLATE "C"(UTF-8 바이트 순)에서 U+FFFF보다 큰 글자(이모지)로 시작하는 이름을 놓친다.
- 폴더는 `folders` 행(쓰기·ensure_folder 때 조상까지 upsert) ∪ 문서·객체 키에서 유도한 폴더. `folder_exists`는
  조상 행에 기대지 않고 prefix 아래의 폴더 행·문서·객체가 하나라도 있으면 참이다.
"""
from __future__ import annotations

import builtins
import errno
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import psycopg

from scripts import keys as _keys
from scripts import refstore
from scripts.common import ScriptError
from scripts.stores import Doc, Meta, VersionConflict
from server.db import Conn, Db
from server.keyindex import (ImportOutcome, ancestors, check_prefix, matcher, meta, not_found, sha256_hex,
                             windows_order)
from server.s3store import S3Bucket, lock_user_object, user_s3_key

log = logging.getLogger(__name__)
_LOCK_TRASH: Final = 0x79746303                     # advisory lock class: one trash move per user at a time


def _now_ns() -> int:
    """쓰기 시각(`Meta.mtime_ns`). 테스트가 시계를 바꿔 끼우는 자리."""
    return time.time_ns()


class PgDocStore:
    """한 사용자의 md 문서(`docs` 테이블). 가볍게 매번 만들어도 된다(상태는 `Db` 풀뿐, ADR-7)."""

    def __init__(self, db: Db, user_id: uuid.UUID | str, email: str) -> None:
        self._db = db
        self._uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
        self._email = email

    # --- 키 ---

    def _doc_key(self, key: str) -> str:
        if not _keys.is_doc(key):
            raise ValueError(f"문서 키는 .md로 끝나야 합니다: {key!r}")
        return _keys.check(key)

    @contextmanager
    def _tx(self) -> Iterator[Conn]:
        with self._db.user_tx(self._uid) as conn:
            yield conn

    # --- 읽기 ---

    def read(self, key: str) -> Doc:
        key = self._doc_key(key)
        with self._tx() as conn:
            row = conn.execute("SELECT raw FROM docs WHERE user_id = %s AND key = %s", (self._uid, key)).fetchone()
        if row is None:
            raise not_found(key)
        return Doc(key=key, raw=bytes(row[0]), label=key)

    def exists(self, key: str) -> bool:
        key = self._doc_key(key)
        with self._tx() as conn:
            row = conn.execute("SELECT 1 FROM docs WHERE user_id = %s AND key = %s", (self._uid, key)).fetchone()
        return row is not None

    def stat(self, key: str) -> Meta:
        key = self._doc_key(key)
        with self._tx() as conn:
            row = conn.execute("SELECT size, mtime_ns FROM docs WHERE user_id = %s AND key = %s",
                               (self._uid, key)).fetchone()
        if row is None:
            raise not_found(key)
        return meta(key, int(row[0]), int(row[1]))

    # --- 쓰기 ---

    def write(self, key: str, data: bytes, *, expect_version: str | None = None) -> str:
        """한 트랜잭션: 행 잠금(`FOR UPDATE`) → ① expect_version 비교(행이 없거나 다르면 VersionConflict)
        → ② 내 메모 가드(`refstore.check_memo`) → 덮어쓰기 → 조상 폴더 upsert. 예외면 롤백.
        행이 없을 때는 `INSERT … ON CONFLICT DO NOTHING`으로 넣고, 그 사이 다른 쓰기가 먼저 넣었으면 그 행을 잠가
        다시 검사한다(동시 쓰기가 메모 가드를 건너뛰지 않게)."""
        key = self._doc_key(key)
        data = bytes(data)
        version = sha256_hex(data)
        with self._tx() as conn:
            while True:
                row = conn.execute("SELECT raw, mtime_ns FROM docs WHERE user_id = %s AND key = %s FOR UPDATE",
                                   (self._uid, key)).fetchone()
                if expect_version is not None:
                    if row is None:
                        raise VersionConflict(f"{key}: 파일이 없습니다")
                    if sha256_hex(bytes(row[0])) != expect_version:
                        raise VersionConflict(f"{key}: 다른 곳에서 바뀌었습니다")
                if row is not None:
                    refstore.check_memo(key, bytes(row[0]), data)
                    mtime_ns = max(_now_ns(), int(row[1]) + 1)   # rewrite always moves the cache stamp
                    conn.execute("UPDATE docs SET raw = %s, version = %s, size = %s, mtime_ns = %s "
                                 "WHERE user_id = %s AND key = %s",
                                 (data, version, len(data), mtime_ns, self._uid, key))
                    break
                if self._insert(conn, key, data, version):
                    break
            self._upsert_folders(conn, ancestors(key))
        return version

    def create(self, key: str, data: bytes) -> str:
        """없을 때만 만든다(O_EXCL): `INSERT … ON CONFLICT DO NOTHING`이 행을 못 넣으면 FileExistsError.
        동시에 만들면 PK 충돌 대기 뒤 하나만 이긴다. 기존 문서를 덮지 않으므로 메모 가드가 필요 없다."""
        key = self._doc_key(key)
        data = bytes(data)
        version = sha256_hex(data)
        with self._tx() as conn:
            if not self._insert(conn, key, data, version):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), key)
            self._upsert_folders(conn, ancestors(key))
        return version

    def import_raw(self, key: str, data: bytes, mtime_ns: int, *, overwrite: bool = False,
                   dry_run: bool = False) -> ImportOutcome:
        """관리 가져오기(Protocol 밖, `server.admin import`): 원본 mtime을 **그대로** 넣는다(미션·진단의 mtime 정렬, `api_refs`의
        "added"가 바뀌지 않게). 같은 내용(sha256)이면 건드리지 않고 `same`, 다르면 `overwrite`일 때만 덮어쓴다(`replaced`),
        아니면 `conflict`. 사용자가 고른 원본 폴더로 되돌리는 일이라 내 메모 가드(`check_memo`)는 거치지 않는다.
        `dry_run`이면 같은 판정만 하고 쓰지 않는다."""
        key = self._doc_key(key)
        data = bytes(data)
        version = sha256_hex(data)
        with self._tx() as conn:
            while True:
                row = conn.execute("SELECT version FROM docs WHERE user_id = %s AND key = %s FOR UPDATE",
                                   (self._uid, key)).fetchone()
                if row is not None and str(row[0]) == version:
                    return "same"
                if row is not None and not overwrite:
                    return "conflict"
                outcome: ImportOutcome = "new" if row is None else "replaced"
                if dry_run:
                    return outcome
                if row is None:
                    inserted = conn.execute(
                        "INSERT INTO docs (user_id, key, raw, version, size, mtime_ns) VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (user_id, key) DO NOTHING RETURNING 1",
                        (self._uid, key, data, version, len(data), mtime_ns)).fetchone()
                    if inserted is None:        # another writer got there first: look again (and lock) its row
                        continue
                else:
                    conn.execute("UPDATE docs SET raw = %s, version = %s, size = %s, mtime_ns = %s "
                                 "WHERE user_id = %s AND key = %s",
                                 (data, version, len(data), mtime_ns, self._uid, key))
                self._upsert_folders(conn, ancestors(key))
                return outcome

    def _insert(self, conn: Conn, key: str, data: bytes, version: str) -> bool:
        row = conn.execute("INSERT INTO docs (user_id, key, raw, version, size, mtime_ns) "
                           "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, key) DO NOTHING RETURNING 1",
                           (self._uid, key, data, version, len(data), _now_ns())).fetchone()
        return row is not None

    def _upsert_folders(self, conn: Conn, prefixes: builtins.list[str]) -> None:
        if prefixes:
            conn.execute("INSERT INTO folders (user_id, prefix) SELECT %s, p FROM unnest(%s::text[]) AS p "
                         "ON CONFLICT (user_id, prefix) DO NOTHING", (self._uid, prefixes))

    # --- 목록·폴더 ---

    def list(self, prefix: str, pattern: str = "*.md") -> builtins.list[Meta]:
        prefix = check_prefix(prefix)
        match = matcher(pattern)
        with self._tx() as conn:
            rows = conn.execute("SELECT key, size, mtime_ns FROM docs WHERE user_id = %s AND starts_with(key, %s)",
                                (self._uid, prefix)).fetchall()
        metas = {str(k): meta(str(k), int(size), int(mtime_ns)) for k, size, mtime_ns in rows
                 if "/" not in str(k)[len(prefix):] and match(str(k)[len(prefix):])}
        return [metas[k] for k in windows_order(metas)]

    def folders(self, prefix: str, pattern: str) -> builtins.list[str]:
        prefix = check_prefix(prefix)
        match = matcher(pattern)
        with self._tx() as conn:
            found = self._under(conn, prefix)
        names = {rest.split("/", 1)[0] for rest in (k[len(prefix):] for k in found) if "/" in rest}
        return windows_order(prefix + name + "/" for name in names if match(name))

    def folder_exists(self, prefix: str) -> bool:
        prefix = check_prefix(prefix)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM folders WHERE user_id = %(u)s AND starts_with(prefix, %(p)s)) "
                "OR EXISTS (SELECT 1 FROM docs WHERE user_id = %(u)s AND starts_with(key, %(p)s)) "
                "OR EXISTS (SELECT 1 FROM objects WHERE user_id = %(u)s AND starts_with(key, %(p)s))",
                {"u": self._uid, "p": prefix}).fetchone()
        return bool(row and row[0])

    def ensure_folder(self, prefix: str) -> None:
        prefix = check_prefix(prefix)
        with self._tx() as conn:
            self._upsert_folders(conn, ancestors(prefix))

    def _under(self, conn: Conn, prefix: str) -> builtins.list[str]:
        """prefix 아래의 폴더 행·문서 키·객체 키(재귀)."""
        rows = conn.execute(
            "SELECT prefix FROM folders WHERE user_id = %(u)s AND starts_with(prefix, %(p)s) "
            "UNION SELECT key FROM docs WHERE user_id = %(u)s AND starts_with(key, %(p)s) "
            "UNION SELECT key FROM objects WHERE user_id = %(u)s AND starts_with(key, %(p)s)",
            {"u": self._uid, "p": prefix}).fetchall()
        return [str(r[0]) for r in rows]

    # --- 표시·로컬 파일 ---

    def label(self) -> str:
        return f"ytc:{self._email}"

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        """임시 폴더에 키의 파일 이름으로 바이트를 써서 준다(읽기용). 끝나면 지운다.
        없는 키면 없는 경로를 준다(FileDocStore처럼 읽을 때 FileNotFoundError)."""
        key = self._doc_key(key)
        with self._tx() as conn:
            row = conn.execute("SELECT raw FROM docs WHERE user_id = %s AND key = %s", (self._uid, key)).fetchone()
        with tempfile.TemporaryDirectory(prefix="ytc-doc-", ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / key.rsplit("/", 1)[-1]
            if row is not None:
                path.write_bytes(bytes(row[0]))
            yield path


# --- 휴지통 ---------------------------------------------------------------------------------

def trash_s3_key(uid: uuid.UUID, stamp: str, key: str) -> str:
    """휴지통 객체 이름 `trash/{uid}/{stamp}/{key}`(`trash_items.s3_key` CHECK와 같은 꼴)."""
    return f"trash/{uid}/{stamp}/{key}"


def _trash_failed(cause: BaseException) -> ScriptError:
    """FileTrash(`filestore.move_paths`)와 같은 코드·문구."""
    return ScriptError("trash_failed", f"파일을 옮기지 못했습니다(다른 프로그램이 열어 두었는지 확인하세요): {cause}.")


@dataclass
class _Plan:
    """한 번의 이동에서 옮길 행들."""

    docs: builtins.list[tuple[str, bytes, int, int]] = field(default_factory=builtins.list)  # key, raw, size, mtime
    objects: builtins.list[str] = field(default_factory=builtins.list)
    folders: builtins.list[str] = field(default_factory=builtins.list)    # moved folder prefixes ('/')


class PgTrash:
    """한 사용자의 휴지통(`scripts.stores.Trash`). 반환 형식·문자열·오류는 FileTrash와 같다:
    `{"trash": "workspace/trash/<stamp>[-n]", "moved": [키(폴더는 끝 / 없이), …]}`, last는 마지막,
    없는 키는 trash_failed.

    - 문서는 `trash_items.raw`로, 객체는 MinIO 서버 쪽 복사(`trash/{uid}/{stamp}/{key}`) 뒤 행으로 옮긴다.
      폴더 키는 그 아래 문서·객체 전부와 폴더 행을 옮긴다(디스크에서 폴더째 옮기는 것과 같다). 조상 폴더 행은 남는다.
    - 공유 캐시 키(`workspace/{raw,cuts,frames,shots}/`)는 옮기지 않고 `moved`에서 뺀다:
      다른 사용자도 쓰는 원본 데이터다(§3.3).
    - 한 트랜잭션: 사용자 잠금 → 스탬프 고르기 → 행 잠금(문서 FOR UPDATE, 객체는 S3ObjectStore와 같은 키 잠금, 키 순서)
      → 휴지통 객체 복사 → trash_items 넣기 → 원래 행 지우기 → 커밋 → 원래 MinIO 객체 지우기
      (`_delete_originals`: 따로 트랜잭션, 같은 키 잠금, 그사이 다시 생긴 행의 객체는 남긴다).
      커밋 중 연결이 끊기면(결과 불명) 복사본을 지우지 않고 경고를 남긴 뒤 예외를 그대로 낸다(S10 점검).
      커밋 전 실패면 롤백하고 복사본을 지운다(아무것도 옮기지 않음).
      원래 객체 삭제는 커밋 뒤라 "행이 있으면 객체가 있다"가 늘 성립한다.
      그 삭제가 실패하면 행 없는 고아 객체(S10 점검)로 남고 경고 로그를 남긴다(이동은 이미 끝났다).
    """

    def __init__(self, db: Db, bucket: S3Bucket, user_id: uuid.UUID | str) -> None:
        self._db = db
        self._bucket = bucket
        self._uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)

    @staticmethod
    def _name(key: str) -> str:
        name = _keys.check(key).removesuffix("/")
        if _keys.kind(name) == "scratch":
            raise ValueError(f"임시 파일 키는 휴지통으로 옮기지 않습니다: {key!r}")
        return name

    def move(self, keys: builtins.list[str], last: str) -> dict[str, Any]:
        names = [self._name(k) for k in keys]
        last_name = self._name(last)
        names.sort(key=lambda n: n == last_name)
        moving = [n for n in names if _keys.kind(n) != "shared"]
        copied: builtins.list[str] = []
        committing = False
        try:
            with self._db.user_tx(self._uid) as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s::int, hashtext(%s))", (_LOCK_TRASH, str(self._uid)))
                stamp = self._stamp(conn)
                plan = self._plan(conn, moving)
                for key in plan.objects:
                    dst = trash_s3_key(self._uid, stamp, key)
                    self._bucket.copy(user_s3_key(self._uid, key), dst)
                    copied.append(dst)
                self._record(conn, stamp, plan)
                committing = True                 # leaving the block commits
        except BaseException as e:
            if committing and isinstance(e, psycopg.OperationalError):
                # The connection died during COMMIT: the move may or may not have committed. If it did, the
                # trash rows point at these copies, so they must not be deleted. Keep them (and the originals)
                # and leave it to the S10 check, which can see which side has rows.
                log.warning("휴지통 이동의 커밋 결과를 알 수 없습니다. "
                            "복사본·원래 객체를 그대로 둡니다(S10 점검): %s: %s",
                            copied, e)
                e.add_note(f"휴지통 이동 커밋 결과 불명(S10 점검): {copied}")
                raise
            if copied:
                self._discard(copied, e)
            if isinstance(e, OSError):
                raise _trash_failed(e) from e
            raise
        self._delete_originals(plan.objects)
        return {"trash": f"{_keys.TRASH}{stamp}", "moved": moving}

    def _delete_originals(self, object_keys: builtins.list[str]) -> None:
        """커밋 뒤 원래 MinIO 객체를 지운다. 이동 트랜잭션은 끝났으므로 S3ObjectStore.put·delete와 같은
        키 잠금을 키 순서로 다시 잡고, 그사이 같은 키에 행이 다시 생긴 것(동시 put의 새 바이트)은 건드리지 않고
        행이 없는 것만 지운다. 실패하면 행 없는 고아 객체(S10 점검)로 남기고 경고 로그만 남긴다
        (이동은 이미 끝났고 행은 휴지통 복사본을 가리킨다)."""
        if not object_keys:
            return
        wanted = sorted(set(object_keys))
        stale: builtins.list[str] = []
        try:
            with self._db.user_tx(self._uid) as conn:
                for key in wanted:
                    lock_user_object(conn, self._uid, key)
                rows = conn.execute("SELECT key FROM objects WHERE user_id = %s AND key = ANY(%s)",
                                    (self._uid, wanted)).fetchall()
                alive = {str(r[0]) for r in rows}
                stale = [user_s3_key(self._uid, k) for k in wanted if k not in alive]
                if stale:
                    self._bucket.delete_many(stale)       # inside the locks: no put of these keys can interleave
        except (OSError, psycopg.Error) as e:
            log.warning("휴지통 이동 뒤 원래 객체를 지우지 못했습니다(고아, S10 점검): %s: %s", stale or wanted, e)

    def _stamp(self, conn: Conn) -> str:
        """`YYYYmmdd-HHMMSS`, 같은 초에 이미 쓴 스탬프면 `-2`, `-3` …(FileTrash의 폴더 이름과 같다)."""
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        n = 1
        while True:
            stamp = base if n == 1 else f"{base}-{n}"
            row = conn.execute("SELECT 1 FROM trash_items WHERE user_id = %s AND stamp = %s LIMIT 1",
                               (self._uid, stamp)).fetchone()
            if row is None:
                return stamp
            n += 1

    def _plan(self, conn: Conn, names: builtins.list[str]) -> _Plan:
        """옮길 행을 잠그고 모은다. 없는 키는 trash_failed(FileTrash: 옮길 파일이 없으면 shutil.move가 실패)."""
        plan = _Plan()
        candidates: builtins.list[str] = []
        explicit: builtins.list[str] = []
        for name in names:
            if _keys.is_doc(name):
                row = conn.execute("SELECT raw, size, mtime_ns FROM docs WHERE user_id = %s AND key = %s FOR UPDATE",
                                   (self._uid, name)).fetchone()
                if row is None:
                    raise _trash_failed(not_found(name))
                plan.docs.append((name, bytes(row[0]), int(row[1]), int(row[2])))
                continue
            if conn.execute("SELECT 1 FROM objects WHERE user_id = %s AND key = %s", (self._uid, name)).fetchone():
                candidates.append(name)
                explicit.append(name)
                continue
            prefix = name + "/"
            docs = conn.execute("SELECT key, raw, size, mtime_ns FROM docs WHERE user_id = %s AND starts_with(key, %s) "
                                "ORDER BY key FOR UPDATE", (self._uid, prefix)).fetchall()
            objs = conn.execute("SELECT key FROM objects WHERE user_id = %s AND starts_with(key, %s)",
                                (self._uid, prefix)).fetchall()
            folder = conn.execute("SELECT 1 FROM folders WHERE user_id = %s AND starts_with(prefix, %s) LIMIT 1",
                                  (self._uid, prefix)).fetchone()
            if not docs and not objs and folder is None:
                raise _trash_failed(not_found(name))
            plan.docs.extend((str(k), bytes(raw), int(size), int(m)) for k, raw, size, m in docs)
            candidates.extend(str(r[0]) for r in objs)
            plan.folders.append(prefix)
        wanted = sorted(set(candidates))
        # Same per-key lock as S3ObjectStore.put/delete, in key order (delete_prefix also locks in key order)
        for key in wanted:
            lock_user_object(conn, self._uid, key)
        rows = conn.execute("SELECT key FROM objects WHERE user_id = %s AND key = ANY(%s)",
                            (self._uid, wanted)).fetchall()
        present = {str(r[0]) for r in rows}
        for name in explicit:
            if name not in present:                              # deleted while we waited for its lock
                raise _trash_failed(not_found(name))
        plan.objects = sorted(present)
        return plan

    def _record(self, conn: Conn, stamp: str, plan: _Plan) -> None:
        for key, raw, size, mtime_ns in plan.docs:
            conn.execute("INSERT INTO trash_items (user_id, stamp, key, kind, raw, size, mtime_ns) "
                         "VALUES (%s, %s, %s, 'doc', %s, %s, %s)", (self._uid, stamp, key, raw, size, mtime_ns))
        if plan.objects:
            conn.execute(
                "INSERT INTO trash_items (user_id, stamp, key, kind, s3_key, size, mtime_ns) "
                "SELECT user_id, %s, key, 'object', %s || key, size, mtime_ns FROM objects "
                "WHERE user_id = %s AND key = ANY(%s)",
                (stamp, trash_s3_key(self._uid, stamp, ""), self._uid, plan.objects))
            conn.execute("DELETE FROM objects WHERE user_id = %s AND key = ANY(%s)", (self._uid, plan.objects))
        if plan.docs:
            conn.execute("DELETE FROM docs WHERE user_id = %s AND key = ANY(%s)",
                         (self._uid, [d[0] for d in plan.docs]))
        for prefix in plan.folders:
            conn.execute("DELETE FROM folders WHERE user_id = %s AND starts_with(prefix, %s)", (self._uid, prefix))

    def _discard(self, copied: builtins.list[str], cause: BaseException) -> None:
        """롤백된 이동의 휴지통 복사본을 지운다. 실패하면 원래 예외에 고아 객체를 적어 둔다(S10 점검)."""
        try:
            self._bucket.delete_many(copied)
        except OSError as e:
            cause.add_note(f"휴지통 복사본이 고아로 남았습니다(S10 점검): {copied}: {e}")
