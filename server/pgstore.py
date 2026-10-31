"""PostgreSQL 문서 저장소(서버 설계 §3.1·§3.3, ADR-S3): `scripts.stores.DocStore`를 `docs`·`folders` 테이블로 구현한다.

- 원문 바이트(`docs.raw`)를 그대로 저장한다. version = sha256(raw), `Doc.label` = 키, `label()` = `ytc:{email}`.
- 모든 질의는 `Db.user_tx(uid)` 안에서 돈다(RLS가 최종 방어선). WHERE에도 user_id를 적는다(이중 방어, PK 범위 질의).
- 키 검사와 종류 오류는 FileDocStore와 같은 순서·예외: `.md`가 아니면 ValueError, 그다음 `keys.check`(bad_path).
- 목록은 prefix 바로 아래만, 이름 일치는 대소문자 무시 fnmatch(Windows pathlib glob과 같은 정규식),
  순서는 `sorted(key=PureWindowsPath)` — Windows FileStore의 `sorted(glob)`과 같다(§3.3).
- 접두사 질의는 `starts_with(key, prefix)`: 설계 §3.1의 `key < prefix || U&'\\FFFF'` 상한은
  COLLATE "C"(UTF-8 바이트 순)에서 U+FFFF보다 큰 글자(이모지)로 시작하는 이름을 놓친다.
- 폴더는 `folders` 행(쓰기·ensure_folder 때 조상까지 upsert) ∪ 문서·객체 키에서 유도한 폴더.
"""
from __future__ import annotations

import builtins
import errno
import fnmatch
import hashlib
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path, PureWindowsPath

from scripts import keys as _keys
from scripts import refstore
from scripts.stores import Doc, Meta, VersionConflict
from server.db import Conn, Db


@lru_cache(maxsize=256)
def _matcher(pattern: str) -> Callable[[str], re.Match[str] | None]:
    """Windows pathlib glob(3.12 `_compile_pattern`, case_sensitive=False)과 같은 일치 함수."""
    if not pattern or "/" in pattern or "\\" in pattern:
        raise ValueError(f"패턴은 이름 하나에 대한 glob이어야 합니다: {pattern!r}")
    return re.compile(fnmatch.translate(pattern), re.IGNORECASE).match


def _windows_order(names: Iterable[str]) -> builtins.list[str]:
    return sorted(names, key=PureWindowsPath)


def _prefix(prefix: str) -> str:
    if not prefix.endswith("/"):
        raise ValueError(f"접두사는 '/'로 끝나야 합니다: {prefix!r}")
    return _keys.check(prefix)


def _ancestors(key: str) -> builtins.list[str]:
    """키를 담는 폴더 접두사들(바깥부터). `refs/ref_a/videos/x.md` → refs/, refs/ref_a/, refs/ref_a/videos/.
    폴더 키(`/`로 끝남)는 자기 자신까지."""
    parts = key.split("/")[:-1]
    return ["/".join(parts[: i + 1]) + "/" for i in range(len(parts))]


def _not_found(key: str) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), key)


def _now_ns() -> int:
    """쓰기 시각(`Meta.mtime_ns`). 테스트가 시계를 바꿔 끼우는 자리."""
    return time.time_ns()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
            raise _not_found(key)
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
            raise _not_found(key)
        return _meta(key, int(row[0]), int(row[1]))

    # --- 쓰기 ---

    def write(self, key: str, data: bytes, *, expect_version: str | None = None) -> str:
        """한 트랜잭션: 행 잠금(`FOR UPDATE`) → ① expect_version 비교(행이 없거나 다르면 VersionConflict)
        → ② 내 메모 가드(`refstore.check_memo`) → 덮어쓰기 → 조상 폴더 upsert. 예외면 롤백.
        행이 없을 때는 `INSERT … ON CONFLICT DO NOTHING`으로 넣고, 그 사이 다른 쓰기가 먼저 넣었으면 그 행을 잠가
        다시 검사한다(동시 쓰기가 메모 가드를 건너뛰지 않게)."""
        key = self._doc_key(key)
        data = bytes(data)
        version = _sha(data)
        with self._tx() as conn:
            while True:
                row = conn.execute("SELECT raw, mtime_ns FROM docs WHERE user_id = %s AND key = %s FOR UPDATE",
                                   (self._uid, key)).fetchone()
                if expect_version is not None:
                    if row is None:
                        raise VersionConflict(f"{key}: 파일이 없습니다")
                    if _sha(bytes(row[0])) != expect_version:
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
            self._upsert_folders(conn, _ancestors(key))
        return version

    def create(self, key: str, data: bytes) -> str:
        """없을 때만 만든다(O_EXCL): `INSERT … ON CONFLICT DO NOTHING`이 행을 못 넣으면 FileExistsError.
        동시에 만들면 PK 충돌 대기 뒤 하나만 이긴다. 기존 문서를 덮지 않으므로 메모 가드가 필요 없다."""
        key = self._doc_key(key)
        data = bytes(data)
        version = _sha(data)
        with self._tx() as conn:
            if not self._insert(conn, key, data, version):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), key)
            self._upsert_folders(conn, _ancestors(key))
        return version

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
        prefix = _prefix(prefix)
        match = _matcher(pattern)
        with self._tx() as conn:
            rows = conn.execute("SELECT key, size, mtime_ns FROM docs WHERE user_id = %s AND starts_with(key, %s)",
                                (self._uid, prefix)).fetchall()
        metas = {str(k): _meta(str(k), int(size), int(mtime_ns)) for k, size, mtime_ns in rows
                 if "/" not in str(k)[len(prefix):] and match(str(k)[len(prefix):])}
        return [metas[k] for k in _windows_order(metas)]

    def folders(self, prefix: str, pattern: str) -> builtins.list[str]:
        prefix = _prefix(prefix)
        match = _matcher(pattern)
        with self._tx() as conn:
            found = self._under(conn, prefix)
        names = {rest.split("/", 1)[0] for rest in (k[len(prefix):] for k in found) if "/" in rest}
        return _windows_order(prefix + name + "/" for name in names if match(name))

    def folder_exists(self, prefix: str) -> bool:
        prefix = _prefix(prefix)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM folders WHERE user_id = %(u)s AND prefix = %(p)s) "
                "OR EXISTS (SELECT 1 FROM docs WHERE user_id = %(u)s AND starts_with(key, %(p)s)) "
                "OR EXISTS (SELECT 1 FROM objects WHERE user_id = %(u)s AND starts_with(key, %(p)s))",
                {"u": self._uid, "p": prefix}).fetchone()
        return bool(row and row[0])

    def ensure_folder(self, prefix: str) -> None:
        prefix = _prefix(prefix)
        with self._tx() as conn:
            self._upsert_folders(conn, _ancestors(prefix))

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
        with tempfile.TemporaryDirectory(prefix="ytc-doc-") as tmp:
            path = Path(tmp) / key.rsplit("/", 1)[-1]
            if row is not None:
                path.write_bytes(bytes(row[0]))
            yield path


def _meta(key: str, size: int, mtime_ns: int) -> Meta:
    return Meta(key=key, size=size, mtime=mtime_ns / 1e9, mtime_ns=mtime_ns)
