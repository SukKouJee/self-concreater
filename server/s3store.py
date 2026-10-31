"""PG 인덱스 + MinIO 객체 저장소(서버 설계 §3.1~§3.5, ADR-S4): `scripts.stores.ObjectStore`·`SharedCache`.

- 바이트는 MinIO(S3 API, boto3), 목록·stat·exists의 원본은 PG 인덱스 행(`objects`·`shared_objects`).
  사용자 객체는 `users/{uid}/{key}`(키 그대로, `objects.s3_key` CHECK가 강제), 공유 캐시는 `shared_s3_key(key)`
  (`shared/videos/{vid}/{raw.json|cuts.json|shots.json|frames/<name>}`).
- 쓰기 순서: 같은 키 잠금(`pg_advisory_xact_lock`) → MinIO PUT → 인덱스 행 upsert(세이브포인트). 행 upsert가 실패하면
  새 키였을 때 방금 올린 객체를 지운다. 그래서 "행이 있으면 객체가 있다"가 늘 성립하고, 실패가 행을 남기지 않는다.
  잠금은 같은 키의 PUT·행 순서가 엇갈려 행(size·sha256)이 다른 쓰기의 바이트를 가리키는 일도 막는다.
- 삭제: 같은 잠금 안에서 행 삭제 → 객체 삭제 → 커밋. 객체 삭제가 실패하면 롤백되어 행과 객체가 함께 남는다.
- 행이 없는 객체(고아)는 보이지 않는다(exists·get·list 모두 행 기준). 정리는 S10 점검.
- 목록·이름 일치·순서·접두사 검사는 PgDocStore와 같다(`server.pgstore`의 도우미). mtime_ns는 다시 쓸 때 줄지 않는다.
- `local_path`·`write_path`는 임시 파일·폴더(끝나면 지운다). 폴더 키의 `write_path`는 정상 종료 때 `delete_prefix` 후
  하위 파일을 이름순으로 올린다. 예외면 아무것도 올리지 않는다.
- S3·네트워크 오류는 `S3Error`(OSError)로 바꾼다: 호출부가 파일 오류처럼 `except OSError`로 잡는다(ADR-3과 같은 결).
"""
from __future__ import annotations

import builtins
import errno
import re
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Final

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from scripts import keys as _keys
from scripts.common import ScriptError
from scripts.stores import Meta
from server.db import Conn, Db
from server.pgstore import _ancestors, _matcher, _meta, _not_found, _prefix, _sha, _windows_order

MAX_OBJECT_BYTES: Final = 64 * 1024 * 1024        # §3.5: old conti HTML (46MB) fits
DELETE_BATCH: Final = 1000                          # S3 DeleteObjects limit
_LOCK_OBJECTS: Final = 0x79746301                   # advisory lock class ids ("ytc" + 1/2)
_LOCK_SHARED: Final = 0x79746302
_VID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MISSING_CODES: Final = frozenset({"NoSuchKey", "404", "NotFound"})


class S3Error(OSError):
    """MinIO(S3) 요청 실패. 파일 오류처럼 OSError로 잡힌다."""


def _now_ns() -> int:
    """쓰기 시각(`Meta.mtime_ns`). 테스트가 시계를 바꿔 끼우는 자리."""
    return time.time_ns()


class S3Bucket:
    """버킷 하나(§3.5). boto3 클라이언트를 감싸 오류를 `S3Error`/`FileNotFoundError`로 바꾼다."""

    def __init__(self, client: Any, name: str) -> None:
        self.client = client
        self.name = name

    @classmethod
    def connect(cls, endpoint: str, access_key: str, secret_key: str, bucket: str,
                region: str = "us-east-1") -> S3Bucket:
        import boto3

        config = Config(signature_version="s3v4", s3={"addressing_style": "path"},
                        retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=10, read_timeout=60)
        client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key,
                              aws_secret_access_key=secret_key, region_name=region, config=config)
        return cls(client, bucket)

    def put(self, s3_key: str, data: bytes) -> None:
        try:
            self.client.put_object(Bucket=self.name, Key=s3_key, Body=data)
        except (ClientError, BotoCoreError) as e:
            raise S3Error(errno.EIO, f"S3 PUT 실패: {e}", s3_key) from e

    def get(self, s3_key: str) -> bytes:
        try:
            body = self.client.get_object(Bucket=self.name, Key=s3_key)["Body"]
            try:
                return bytes(body.read())
            finally:
                body.close()
        except ClientError as e:
            if str(e.response.get("Error", {}).get("Code")) in _MISSING_CODES:
                raise FileNotFoundError(errno.ENOENT, "S3 객체가 없습니다", s3_key) from e
            raise S3Error(errno.EIO, f"S3 GET 실패: {e}", s3_key) from e
        except BotoCoreError as e:
            raise S3Error(errno.EIO, f"S3 GET 실패: {e}", s3_key) from e

    def delete(self, s3_key: str) -> None:
        """없는 객체를 지워도 된다(S3 DeleteObject는 204)."""
        try:
            self.client.delete_object(Bucket=self.name, Key=s3_key)
        except (ClientError, BotoCoreError) as e:
            raise S3Error(errno.EIO, f"S3 DELETE 실패: {e}", s3_key) from e

    def delete_many(self, s3_keys: builtins.list[str]) -> None:
        for i in range(0, len(s3_keys), DELETE_BATCH):
            batch = s3_keys[i: i + DELETE_BATCH]
            try:
                out = self.client.delete_objects(
                    Bucket=self.name, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
            except (ClientError, BotoCoreError) as e:
                raise S3Error(errno.EIO, f"S3 DELETE 실패: {e}", batch[0]) from e
            errors = out.get("Errors") or []
            if errors:
                first = errors[0]
                raise S3Error(errno.EIO, f"S3 DELETE 실패({len(errors)}개): {first.get('Code')} {first.get('Message')}",
                              str(first.get("Key")))

    def keys(self, prefix: str) -> builtins.list[str]:
        """prefix 아래 객체 이름(재귀, 이름순). 점검·테스트용."""
        found: builtins.list[str] = []
        try:
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.name, Prefix=prefix):
                found.extend(str(o["Key"]) for o in page.get("Contents", []))
        except (ClientError, BotoCoreError) as e:
            raise S3Error(errno.EIO, f"S3 LIST 실패: {e}", prefix) from e
        return sorted(found)


def shared_s3_key(key: str) -> str:
    """공유 캐시 키 → MinIO 객체 이름(§3.2 한 곳). `workspace/raw/<vid>.json` → `shared/videos/<vid>/raw.json`,
    cuts·shots도 같은 꼴, `workspace/frames/<vid>/<이름>` → `shared/videos/<vid>/frames/<이름>`.
    그 밖의 꼴(영상 ID가 아닌 이름, 하위 폴더)은 ValueError."""
    _keys.check(key)
    if key.startswith(_keys.FRAMES):
        vid, _, rest = key[len(_keys.FRAMES):].partition("/")
        if _VID_RE.match(vid) and rest and not rest.endswith("/") and key == _keys.frames_dir(vid) + rest:
            return f"shared/videos/{vid}/frames/{rest}"
    else:
        name = PurePosixPath(key).name
        vid = name.removesuffix(".json")
        if _VID_RE.match(vid):
            for kind, make in (("raw", _keys.raw), ("cuts", _keys.cuts), ("shots", _keys.shots)):
                if make(vid) == key:
                    return f"shared/videos/{vid}/{kind}.json"
    raise ValueError(f"공유 캐시 키 형식이 아닙니다: {key!r}")


class _S3Store:
    """사용자 객체·공유 캐시 공통 동작. 행 SQL은 하위 클래스가 낸다(테이블·RLS 범위가 다르다)."""

    _lock_class: int = _LOCK_OBJECTS

    def __init__(self, db: Db, bucket: S3Bucket, user_id: uuid.UUID | str) -> None:
        self._db = db
        self._bucket = bucket
        self._uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)

    # --- 하위 클래스 ---

    def _key(self, key: str) -> str:
        raise NotImplementedError

    def _check_prefix(self, prefix: str) -> str:
        return _prefix(prefix)

    def _s3_key(self, key: str) -> str:
        raise NotImplementedError

    def _lock_name(self, key: str) -> str:
        return key

    def _row(self, conn: Conn, key: str) -> tuple[int, int] | None:
        raise NotImplementedError

    def _upsert(self, conn: Conn, key: str, data: bytes, s3_key: str) -> None:
        raise NotImplementedError

    def _delete_rows(self, conn: Conn, keys: builtins.list[str]) -> builtins.list[str]:
        raise NotImplementedError

    def _keys_under(self, conn: Conn, prefix: str) -> builtins.list[tuple[str, int, int]]:
        raise NotImplementedError

    def _any_under(self, conn: Conn, prefix: str) -> bool:
        raise NotImplementedError

    def _touch_folders(self, conn: Conn, prefixes: builtins.list[str]) -> None:
        """폴더 행(FileStore의 mkdir에 해당). 공유 캐시는 폴더 행이 없다."""

    def _forget_folders(self, conn: Conn, prefix: str) -> None:
        """delete_prefix(rmtree)가 지운 폴더 행."""

    # --- 공통 ---

    @contextmanager
    def _tx(self) -> Iterator[Conn]:
        with self._db.user_tx(self._uid) as conn:
            yield conn

    def _lock(self, conn: Conn, key: str) -> None:
        """같은 키의 put·delete를 한 번에 하나씩(프로세스가 달라도). 트랜잭션이 끝나면 풀린다."""
        conn.execute("SELECT pg_advisory_xact_lock(%s::int, hashtext(%s))", (self._lock_class, self._lock_name(key)))

    def get(self, key: str) -> bytes:
        key = self._key(key)
        with self._tx() as conn:
            row = self._row(conn, key)
        if row is None:
            raise _not_found(key)
        try:
            return self._bucket.get(self._s3_key(key))
        except FileNotFoundError as e:          # deleted between the row read and the GET
            raise _not_found(key) from e

    def put(self, key: str, data: bytes) -> None:
        key = self._key(key)
        data = bytes(data)
        if len(data) > MAX_OBJECT_BYTES:
            raise ScriptError("object_too_large",
                              f"파일이 너무 큽니다({len(data) // (1024 * 1024)}MB). "
                              f"서버에는 {MAX_OBJECT_BYTES // (1024 * 1024)}MB까지 저장할 수 있습니다: {key}")
        s3_key = self._s3_key(key)
        with self._tx() as conn:
            self._lock(conn, key)
            existed = self._row(conn, key) is not None
            self._bucket.put(s3_key, data)
            try:
                with conn.transaction():         # savepoint: the outer tx (and the lock) survives a failed upsert
                    self._upsert(conn, key, data, s3_key)
                    self._touch_folders(conn, _ancestors(key))
            except BaseException as e:
                if not existed:
                    self._discard(s3_key, e)
                raise

    def _discard(self, s3_key: str, cause: BaseException) -> None:
        """행을 못 넣은 새 객체를 지운다. 이것도 실패하면 원래 예외에 고아 객체를 적어 둔다(S10 점검)."""
        try:
            self._bucket.delete(s3_key)
        except OSError as e:
            cause.add_note(f"고아 객체가 남았습니다(S10 점검): {s3_key}: {e}")

    def exists(self, key: str) -> bool:
        key = self._key(key)
        with self._tx() as conn:
            return self._row(conn, key) is not None

    def stat(self, key: str) -> Meta:
        key = self._key(key)
        with self._tx() as conn:
            row = self._row(conn, key)
        if row is None:
            raise _not_found(key)
        return _meta(key, *row)

    def list(self, prefix: str, pattern: str = "*") -> builtins.list[Meta]:
        prefix = self._check_prefix(prefix)
        match = _matcher(pattern)
        with self._tx() as conn:
            rows = self._keys_under(conn, prefix)
        metas = {k: _meta(k, size, mtime_ns) for k, size, mtime_ns in rows
                 if "/" not in k[len(prefix):] and match(k[len(prefix):])}
        return [metas[k] for k in _windows_order(metas)]

    def folder_exists(self, prefix: str) -> bool:
        prefix = self._check_prefix(prefix)
        with self._tx() as conn:
            return self._any_under(conn, prefix)

    def delete(self, key: str) -> None:
        """행 삭제 → 객체 삭제 → 커밋(같은 키 잠금 안). 행이 없어도 객체 이름을 지운다(남은 고아 정리)."""
        key = self._key(key)
        with self._tx() as conn:
            self._lock(conn, key)
            self._delete_rows(conn, [key])
            self._bucket.delete(self._s3_key(key))

    def delete_prefix(self, prefix: str) -> None:
        """prefix 아래 전부(rmtree). 지울 키마다 잠금(키 순서라 서로 막지 않는다) → 행 삭제 → 객체 삭제 → 커밋."""
        prefix = self._check_prefix(prefix)
        with self._tx() as conn:
            found = sorted(k for k, _, _ in self._keys_under(conn, prefix))
            for key in found:
                self._lock(conn, key)
            gone = self._delete_rows(conn, found)
            self._bucket.delete_many([self._s3_key(k) for k in sorted(gone)])
            self._forget_folders(conn, prefix)

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        """읽기용 임시 사본(끝나면 지운다). 없는 키면 없는 경로. 폴더 키면 그 아래 파일을 같은 모양으로 받는다."""
        key = self._key(key)
        with tempfile.TemporaryDirectory(prefix="ytc-obj-", ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / PurePosixPath(key).name
            if key.endswith("/"):
                path.mkdir()
                for meta in self._all_under(key):
                    target = path.joinpath(*meta.key[len(key):].split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(self._bucket.get(self._s3_key(meta.key)))
            else:
                try:
                    path.write_bytes(self.get(key))
                except FileNotFoundError:
                    path.unlink(missing_ok=True)   # missing key: hand back a path that does not exist
            yield path

    def _all_under(self, prefix: str) -> builtins.list[Meta]:
        with self._tx() as conn:
            rows = self._keys_under(conn, self._check_prefix(prefix))
        return [_meta(k, size, mtime_ns) for k, size, mtime_ns in sorted(rows)]

    @contextmanager
    def write_path(self, key: str) -> Iterator[Path]:
        """외부 도구가 쓸 임시 경로. 정상 종료면 올리고, 예외면 버린다.
        파일 키: 그 파일이 생겼으면 `put`. 폴더 키(`/`로 끝남): `delete_prefix` 뒤 하위 파일 전부를 이름순으로 `put`."""
        key = self._key(key)
        with tempfile.TemporaryDirectory(prefix="ytc-out-", ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / PurePosixPath(key).name
            if key.endswith("/"):
                path.mkdir()
            yield path
            if key.endswith("/"):
                self._upload_folder(key, path)
            elif path.is_file():
                self.put(key, path.read_bytes())
            else:
                with self._tx() as conn:
                    self._touch_folders(conn, _ancestors(key))

    def _upload_folder(self, key: str, folder: Path) -> None:
        files: builtins.list[tuple[str, Path]] = []
        subfolders: builtins.list[str] = []
        for p in folder.rglob("*"):
            rel = p.relative_to(folder).as_posix()
            if p.is_file():
                files.append((self._key(key + rel), p))
            elif p.is_dir():
                subfolders.append(_keys.check(key + rel + "/"))
        self.delete_prefix(key)
        for file_key, p in sorted(files):
            self.put(file_key, p.read_bytes())
        with self._tx() as conn:
            self._touch_folders(conn, [*_ancestors(key), *sorted(subfolders)])

    def display_path(self, key: str) -> str:
        """화면·CLI 출력용 = 키(§3.3)."""
        return self._key(key)


class S3ObjectStore(_S3Store):
    """한 사용자의 객체(`objects` 행 + `users/{uid}/{key}`). 키가 `.md`면 ValueError(문서는 PgDocStore)."""

    _lock_class = _LOCK_OBJECTS

    def _key(self, key: str) -> str:
        if _keys.is_doc(key):
            raise ValueError(f"md 문서는 DocStore로 다룹니다: {key!r}")
        return _keys.check(key)

    def _s3_key(self, key: str) -> str:
        return f"users/{self._uid}/{key}"

    def _lock_name(self, key: str) -> str:
        return f"{self._uid}/{key}"

    def _row(self, conn: Conn, key: str) -> tuple[int, int] | None:
        row = conn.execute("SELECT size, mtime_ns FROM objects WHERE user_id = %s AND key = %s",
                           (self._uid, key)).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def _upsert(self, conn: Conn, key: str, data: bytes, s3_key: str) -> None:
        conn.execute(
            "INSERT INTO objects (user_id, key, size, sha256, mtime_ns, s3_key) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, key) DO UPDATE SET size = EXCLUDED.size, sha256 = EXCLUDED.sha256, "
            "mtime_ns = GREATEST(EXCLUDED.mtime_ns, objects.mtime_ns + 1), s3_key = EXCLUDED.s3_key",
            (self._uid, key, len(data), _sha(data), _now_ns(), s3_key))

    def _delete_rows(self, conn: Conn, keys: builtins.list[str]) -> builtins.list[str]:
        if not keys:
            return []
        rows = conn.execute("DELETE FROM objects WHERE user_id = %s AND key = ANY(%s) RETURNING key",
                            (self._uid, keys)).fetchall()
        return [str(r[0]) for r in rows]

    def _keys_under(self, conn: Conn, prefix: str) -> builtins.list[tuple[str, int, int]]:
        rows = conn.execute("SELECT key, size, mtime_ns FROM objects WHERE user_id = %s AND starts_with(key, %s)",
                            (self._uid, prefix)).fetchall()
        return [(str(k), int(size), int(mtime_ns)) for k, size, mtime_ns in rows]

    def _any_under(self, conn: Conn, prefix: str) -> bool:
        """작업 폴더 트리 하나(문서·객체 공용): 폴더 행·문서·객체 중 하나라도 prefix 아래에 있으면 참
        (PgDocStore와 같다)."""
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM folders WHERE user_id = %(u)s AND starts_with(prefix, %(p)s)) "
            "OR EXISTS (SELECT 1 FROM docs WHERE user_id = %(u)s AND starts_with(key, %(p)s)) "
            "OR EXISTS (SELECT 1 FROM objects WHERE user_id = %(u)s AND starts_with(key, %(p)s))",
            {"u": self._uid, "p": prefix}).fetchone()
        return bool(row and row[0])

    def _touch_folders(self, conn: Conn, prefixes: builtins.list[str]) -> None:
        if prefixes:
            conn.execute("INSERT INTO folders (user_id, prefix) SELECT %s, p FROM unnest(%s::text[]) AS p "
                         "ON CONFLICT (user_id, prefix) DO NOTHING", (self._uid, prefixes))

    def _forget_folders(self, conn: Conn, prefix: str) -> None:
        conn.execute("DELETE FROM folders WHERE user_id = %s AND starts_with(prefix, %s)", (self._uid, prefix))


class S3SharedCache(_S3Store):
    """영상 ID 기준 공유 캐시(`shared_objects` 행 + `shared/videos/{vid}/…`). 누구나 읽고 덮어쓴다(§3.3).
    키·접두사는 `workspace/{raw,cuts,frames,shots}/` 아래만(아니면 ValueError). `created_by`는 처음 쓴 사용자."""

    _lock_class = _LOCK_SHARED

    def _check_prefix(self, prefix: str) -> str:
        if not prefix.startswith(_keys.SHARED_PREFIXES):
            raise ValueError(f"공유 캐시 키가 아닙니다: {prefix!r}")
        return _prefix(prefix)

    def _key(self, key: str) -> str:
        if not key.startswith(_keys.SHARED_PREFIXES):
            raise ValueError(f"공유 캐시 키가 아닙니다: {key!r}")
        if _keys.is_doc(key):
            raise ValueError(f"md 문서는 DocStore로 다룹니다: {key!r}")
        return _keys.check(key)

    def _s3_key(self, key: str) -> str:
        return shared_s3_key(key)

    def _row(self, conn: Conn, key: str) -> tuple[int, int] | None:
        row = conn.execute("SELECT size, mtime_ns FROM shared_objects WHERE key = %s", (key,)).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def _upsert(self, conn: Conn, key: str, data: bytes, s3_key: str) -> None:
        conn.execute(
            "INSERT INTO shared_objects (key, size, sha256, mtime_ns, s3_key, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET size = EXCLUDED.size, sha256 = EXCLUDED.sha256, "
            "mtime_ns = GREATEST(EXCLUDED.mtime_ns, shared_objects.mtime_ns + 1), s3_key = EXCLUDED.s3_key",
            (key, len(data), _sha(data), _now_ns(), s3_key, self._uid))

    def _delete_rows(self, conn: Conn, keys: builtins.list[str]) -> builtins.list[str]:
        if not keys:
            return []
        rows = conn.execute("DELETE FROM shared_objects WHERE key = ANY(%s) RETURNING key", (keys,)).fetchall()
        return [str(r[0]) for r in rows]

    def _keys_under(self, conn: Conn, prefix: str) -> builtins.list[tuple[str, int, int]]:
        rows = conn.execute("SELECT key, size, mtime_ns FROM shared_objects WHERE starts_with(key, %s)",
                            (prefix,)).fetchall()
        return [(str(k), int(size), int(mtime_ns)) for k, size, mtime_ns in rows]

    def _any_under(self, conn: Conn, prefix: str) -> bool:
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM shared_objects WHERE starts_with(key, %s))",
                           (prefix,)).fetchone()
        return bool(row and row[0])
