"""서버판 Backend(서버 설계 §2·§3, S3 설계 §3): `Ctx(user_id=<uid>, backend=ServerBackend)`가 사용자별 저장소를 만든다.

- 사용자 id는 **호출자가 넘긴 인증된 값(`Ctx.user_id`)만** 쓴다. env·요청 본문·전역 상태에서 찾지 않는다.
  없거나 UUID가 아니면 `login_required`(fail closed). 실제 격리는 RLS(`Db.user_tx(uid)`)가 맡는다.
- store = PgDocStore(라벨 `ytc:{email}`), objects = S3ObjectStore, shared = S3SharedCache, scratch = ServerScratch,
  trash = PgTrash, usage = NullMeter(T11에서 UsageMeter로 바꾼다).
- `export_tree(backend, uid)`: 한 사용자의 문서·객체·휴지통과 그 사용자의 영상 md가 가리키는 공유 캐시를 작업 폴더 모양
  (`{키: 바이트}` + 폴더 목록)으로 꺼낸다. 동등성 검사(§10-2)·가져오기 검증(§7)·나중의 내보내기가 쓴다.
"""
from __future__ import annotations

import builtins
import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from scripts import keys as _keys
from scripts.common import ScriptError
from scripts.stores import NullMeter, UsageMeter
from server.config import StorageSettings
from server.db import Conn, Db
from server.keyindex import ancestors
from server.pgstore import PgDocStore, PgTrash
from server.s3store import S3Bucket, S3ObjectStore, S3SharedCache
from server.scratch import ServerScratch, clear_scratch_root

_VID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _login_required() -> ScriptError:
    return ScriptError("login_required", "로그인이 필요합니다")


class ServerBackend:
    """`scripts.stores.Backend`의 서버 구현. 저장소 객체는 가볍게 매번 만든다(상태는 Db 풀·버킷 클라이언트뿐)."""

    def __init__(self, db: Db, bucket: S3Bucket, *, scratch_dir: Path, state_dir: Path) -> None:
        self.db = db
        self.bucket = bucket
        self._scratch_dir = Path(scratch_dir)
        self._state_dir = Path(state_dir)
        self._emails: dict[uuid.UUID, str] = {}
        self._emails_lock = threading.Lock()

    @classmethod
    def open(cls, settings: StorageSettings) -> ServerBackend:
        """설정으로 PG 풀을 열고(앱 롤 안전 검사 포함) 버킷 클라이언트를 만들고 임시 폴더를 비운다
        (§3.4 "시작 때 비움")."""
        db = Db(settings.db_dsn, settings.db_owner_dsn, min_size=settings.db_pool_min, max_size=settings.db_pool_max)
        db.open()
        try:
            bucket = S3Bucket.connect(settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key,
                                      settings.s3_bucket, max_pool_connections=settings.s3_max_pool_connections)
            clear_scratch_root(settings.scratch_dir)
        except BaseException:
            db.close()
            raise
        return cls(db, bucket, scratch_dir=settings.scratch_dir, state_dir=settings.state_dir)

    def close(self) -> None:
        self.db.close()

    # --- 사용자 ---

    @staticmethod
    def _uid(user_id: uuid.UUID | str | None) -> uuid.UUID:
        if isinstance(user_id, uuid.UUID):
            return user_id
        if not isinstance(user_id, str) or not user_id:
            raise _login_required()
        try:
            return uuid.UUID(user_id)
        except ValueError as e:
            raise _login_required() from e

    def email(self, user_id: uuid.UUID | str | None) -> str:
        """사용자의 이메일(문서 저장소 라벨). 없는 사용자면 login_required."""
        uid = self._uid(user_id)
        with self._emails_lock:
            cached = self._emails.get(uid)
        if cached is not None:
            return cached
        with self.db.user_tx(uid) as conn:
            row = conn.execute("SELECT email FROM users WHERE id = %s", (uid,)).fetchone()
        if row is None:
            raise _login_required()
        email = str(row[0])
        with self._emails_lock:
            self._emails[uid] = email
        return email

    # --- Backend ---

    def store(self, user_id: str | None) -> PgDocStore:
        uid = self._uid(user_id)
        return PgDocStore(self.db, uid, self.email(uid))

    def objects(self, user_id: str | None) -> S3ObjectStore:
        return S3ObjectStore(self.db, self.bucket, self._uid(user_id))

    def shared(self, user_id: str | None) -> S3SharedCache:
        return S3SharedCache(self.db, self.bucket, self._uid(user_id))

    def scratch(self, user_id: str | None) -> ServerScratch:
        return ServerScratch(self._scratch_dir, self._state_dir, self._uid(user_id))

    def trash(self, user_id: str | None) -> PgTrash:
        return PgTrash(self.db, self.bucket, self._uid(user_id))

    def usage(self, user_id: str | None) -> UsageMeter:
        self._uid(user_id)
        return NullMeter()


# --- 내보내기 --------------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportedTree:
    """작업 폴더 모양의 사용자 데이터. `files` = {키: 바이트}(휴지통은 `workspace/trash/<stamp>/<키>`),
    `folders` = 폴더 키(`/`로 끝남: 폴더 행 ∪ 파일 키의 조상, 공유 캐시 폴더 제외)."""

    files: dict[str, bytes]
    folders: frozenset[str]

    def hashes(self) -> dict[str, str]:
        """`{키: sha256}`(tests/helpers/tree_snapshot.tree와 같은 모양), 키 순서."""
        return {k: hashlib.sha256(self.files[k]).hexdigest() for k in sorted(self.files)}

    def write(self, dest: Path) -> None:
        """dest(비어 있거나 없는 폴더) 아래에 같은 상대 경로로 쓴다. 키는 내보낼 때 `keys.check`를 거쳤다."""
        dest = Path(dest)
        for folder in sorted(self.folders):
            dest.joinpath(*folder.rstrip("/").split("/")).mkdir(parents=True, exist_ok=True)
        for key in sorted(self.files):
            path = dest.joinpath(*key.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.files[key])


def _video_ids(doc_keys: builtins.list[str]) -> builtins.list[str]:
    """영상 md 키(`refs/ref_<cat>/videos/<vid>.md`)의 영상 id들."""
    found: set[str] = set()
    for key in doc_keys:
        parts = key.split("/")
        if not key.startswith(_keys.REFS) or len(parts) < 3:
            continue
        vid = PurePosixPath(key).stem
        if _VID_RE.match(vid) and _keys.video_md_at("/".join(parts[:2]) + "/", vid) == key:
            found.add(vid)
    return sorted(found)


def _shared_rows(conn: Conn, vids: builtins.list[str]) -> builtins.list[tuple[str, str]]:
    if not vids:
        return []
    exact = [k for vid in vids for k in (_keys.raw(vid), _keys.cuts(vid), _keys.shots(vid))]
    frames = [_keys.frames_dir(vid) for vid in vids]
    rows = conn.execute("SELECT key, s3_key FROM shared_objects WHERE key = ANY(%s) "
                        "OR EXISTS (SELECT 1 FROM unnest(%s::text[]) AS p WHERE starts_with(key, p)) ORDER BY key",
                        (exact, frames)).fetchall()
    return [(str(k), str(s)) for k, s in rows]


@dataclass(frozen=True)
class TreePlan:
    """내보낼 트리의 계획: 키 목록과 폴더는 바로 알고, **바이트는 키마다 읽는다**(`items()`·`hashes()`·`write()`는 한 번에 한 파일만
    메모리에 둔다). 문서는 키 단위로 따로 읽으므로(트랜잭션 하나의 스냅샷이 아니다) 내보내는 도중 다른 곳에서 지운 키는
    FileNotFoundError가 난다."""

    folders: frozenset[str]
    _loaders: dict[str, Callable[[], bytes]]

    @property
    def names(self) -> builtins.list[str]:
        return sorted(self._loaders)

    def read(self, name: str) -> bytes:
        return self._loaders[name]()

    def items(self) -> Iterator[tuple[str, bytes]]:
        for name in self.names:
            yield name, self.read(name)

    def hashes(self) -> dict[str, str]:
        """`{키: sha256}`(키 순서). 파일을 하나씩 읽어 해시만 남긴다."""
        return {name: hashlib.sha256(data).hexdigest() for name, data in self.items()}

    def write(self, dest: Path) -> None:
        """dest(비어 있거나 없는 폴더) 아래에 같은 상대 경로로 파일마다 읽어 쓴다."""
        dest = Path(dest)
        for folder in sorted(self.folders):
            dest.joinpath(*folder.rstrip("/").split("/")).mkdir(parents=True, exist_ok=True)
        for name, data in self.items():
            path = dest.joinpath(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def collect(self) -> ExportedTree:
        """전부 메모리로(작은 트리·테스트용). 파일 순서는 계획 순서(문서 → 객체·공유 → 휴지통)."""
        return ExportedTree(files={name: loader() for name, loader in self._loaders.items()}, folders=self.folders)


def plan_tree(backend: ServerBackend, user_id: uuid.UUID | str, *, trash: bool = True) -> TreePlan:
    """한 사용자(인증된 uid)의 데이터를 작업 폴더 모양으로 꺼낼 계획을 세운다(RLS 경로 `user_tx(uid)`).

    - 문서(`docs.raw`)·객체(MinIO `users/{uid}/…`)는 그 키로.
    - 공유 캐시는 그 사용자의 영상 md(`refs/ref_*/videos/<vid>.md`)가 있는 영상의 raw·cuts·shots·frames만
      (로컬판 작업 폴더에 있는 것과 같은 범위). 휴지통으로 옮긴 영상의 캐시는 넣지 않는다
      (서버는 공유 캐시를 휴지통으로 옮기지 않는다).
    - `trash=True`면 휴지통 항목을 `workspace/trash/<stamp>/<키>`로.
    """
    uid = ServerBackend._uid(user_id)
    with backend.db.user_tx(uid) as conn:
        doc_keys = [str(r[0]) for r in conn.execute("SELECT key FROM docs WHERE user_id = %s ORDER BY key",
                                                    (uid,)).fetchall()]
        objs = conn.execute("SELECT key, s3_key FROM objects WHERE user_id = %s ORDER BY key", (uid,)).fetchall()
        folder_rows = conn.execute("SELECT prefix FROM folders WHERE user_id = %s", (uid,)).fetchall()
        shared = _shared_rows(conn, _video_ids(doc_keys))
        trashed = conn.execute("SELECT stamp, key, kind, s3_key FROM trash_items WHERE user_id = %s "
                               "ORDER BY stamp, key", (uid,)).fetchall() if trash else []

    def doc_loader(key: str) -> Callable[[], bytes]:
        def load() -> bytes:
            with backend.db.user_tx(uid) as conn:
                row = conn.execute("SELECT raw FROM docs WHERE user_id = %s AND key = %s", (uid, key)).fetchone()
            if row is None:
                raise FileNotFoundError(key)
            return bytes(row[0])
        return load

    def trash_doc_loader(stamp: str, key: str) -> Callable[[], bytes]:
        def load() -> bytes:
            with backend.db.user_tx(uid) as conn:
                row = conn.execute("SELECT raw FROM trash_items WHERE user_id = %s AND stamp = %s AND key = %s",
                                   (uid, stamp, key)).fetchone()
            if row is None or row[0] is None:
                raise FileNotFoundError(f"{stamp}/{key}")
            return bytes(row[0])
        return load

    def object_loader(s3_key: str) -> Callable[[], bytes]:
        return lambda: backend.bucket.get(s3_key)

    loaders: dict[str, Callable[[], bytes]] = {}
    for key in doc_keys:
        loaders[_keys.check(key)] = doc_loader(key)
    for key, s3_key in [*((str(k), str(s)) for k, s in objs), *shared]:
        loaders[_keys.check(key)] = object_loader(s3_key)
    for stamp, key, kind, s3_key in trashed:
        name = _keys.check(f"{_keys.TRASH}{stamp}/{key}")
        loaders[name] = trash_doc_loader(str(stamp), str(key)) if kind == "doc" else object_loader(str(s3_key))
    folders = {str(r[0]) for r in folder_rows}
    for name in loaders:
        folders.update(ancestors(name))
    folders = {f for f in folders if not f.startswith(_keys.SHARED_PREFIXES)}   # cache folders are not the user's
    return TreePlan(folders=frozenset(folders), _loaders=loaders)


def export_tree(backend: ServerBackend, user_id: uuid.UUID | str, *, trash: bool = True) -> ExportedTree:
    """`plan_tree(...).collect()`: 한 사용자의 트리를 전부 메모리로. 큰 트리는 `plan_tree`의 `items()`/`write()`/`hashes()`로
    파일마다 읽는다."""
    return plan_tree(backend, user_id, trash=trash).collect()
