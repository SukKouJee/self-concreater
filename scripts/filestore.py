"""파일 저장소(S3 설계 §4): 로컬판의 DocStore·ObjectStore·SharedCache·Scratch·Trash.

작업 폴더 경로(`Path`)는 이 모듈에서만 만든다: 모두 `root / key`(키 검증 후). 키를 만드는 곳은 `scripts.keys`
(여기서는 `_keys`로 부른다: `Trash.move`의 인자 이름 `keys`와 겹치지 않게).
- 읽기는 `path.read_bytes()`, 쓰기는 `refstore.write_atomic`(임시 파일 + 교체). 바이트는 손대지 않는다.
- 목록 순서는 `sorted(Path(root/prefix).glob(pattern))`와 같다(Windows는 대소문자 무시 정렬. 문자열 정렬로 바꾸지 않는다).
- `local_path`/`write_path`는 진짜 경로를 그대로 준다(복사 없음).
- 저장소 객체는 가볍게 매번 만든다(ADR-7). 키별 쓰기 잠금은 객체가 아니라 이 모듈이 가진다.
"""
from __future__ import annotations

import builtins
import hashlib
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts import keys as _keys
from scripts import refstore
from scripts.common import ScriptError
from scripts.stores import Doc, Meta, VersionConflict

LOCK_STRIPES = 64
_write_locks = [threading.Lock() for _ in range(LOCK_STRIPES)]


def _write_lock(path: Path) -> threading.Lock:
    """같은 파일(절대 경로 기준)의 읽기-비교-쓰기를 한 번에 하나씩. 저장소 객체가 달라도 같은 잠금.
    잠금은 고정 개수(줄무늬)라 파일이 많아도 늘지 않는다. 다른 파일이 같은 잠금을 나눠 쓰면 잠깐 기다릴 뿐이다."""
    name = os.path.normcase(os.path.abspath(path))
    return _write_locks[hash(name) % LOCK_STRIPES]


def _meta(root: Path, path: Path) -> Meta:
    st = path.stat()
    return Meta(key=path.relative_to(root).as_posix(), size=st.st_size, mtime=st.st_mtime, mtime_ns=st.st_mtime_ns)


def _prefix(prefix: str) -> str:
    if not prefix.endswith("/"):
        raise ValueError(f"접두사는 '/'로 끝나야 합니다: {prefix!r}")
    return _keys.check(prefix)


class _FileBase:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / _keys.check(key)

    def _list(self, prefix: str, pattern: str) -> builtins.list[Meta]:
        folder = self.root / _prefix(prefix)
        return [_meta(self.root, p) for p in sorted(folder.glob(pattern)) if p.is_file()]

    def _folder_exists(self, prefix: str) -> bool:
        return (self.root / _prefix(prefix)).is_dir()


class FileDocStore(_FileBase):
    """사용자 md 문서. 키는 `.md`로 끝나야 한다(아니면 ValueError)."""

    def _doc_path(self, key: str) -> Path:
        if not _keys.is_doc(key):
            raise ValueError(f"문서 키는 .md로 끝나야 합니다: {key!r}")
        return self._path(key)

    def read(self, key: str) -> Doc:
        path = self._doc_path(key)
        return Doc(key=key, raw=path.read_bytes(), label=str(path))

    def exists(self, key: str) -> bool:
        return self._doc_path(key).is_file()

    def stat(self, key: str) -> Meta:
        return _meta(self.root, self._doc_path(key))

    def write(self, key: str, data: bytes, *, expect_version: str | None = None) -> str:
        path = self._doc_path(key)
        with _write_lock(path):
            if expect_version is not None:
                try:
                    current = hashlib.sha256(path.read_bytes()).hexdigest()
                except FileNotFoundError as e:
                    raise VersionConflict(f"{key}: 파일이 없습니다") from e
                if current != expect_version:
                    raise VersionConflict(f"{key}: 다른 곳에서 바뀌었습니다")
            path.parent.mkdir(parents=True, exist_ok=True)
            refstore.write_atomic(path, data)
        return hashlib.sha256(data).hexdigest()

    def create(self, key: str, data: bytes) -> str:
        """없을 때만 만든다(`open(path, "xb")`와 같은 O_EXCL: 프로세스가 달라도 하나만 이긴다). 있으면 FileExistsError."""
        path = self._doc_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
        try:
            try:
                f = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)                         # fdopen이 실패하면 원시 fd가 남는다
                raise
            with f:
                f.write(data)
        except BaseException:
            path.unlink(missing_ok=True)             # 반쯤 쓴 파일을 남기지 않는다
            raise
        return hashlib.sha256(data).hexdigest()

    def list(self, prefix: str, pattern: str = "*.md") -> builtins.list[Meta]:
        return self._list(prefix, pattern)

    def folders(self, prefix: str, pattern: str) -> builtins.list[str]:
        folder = self.root / _prefix(prefix)
        return [p.relative_to(self.root).as_posix() + "/" for p in sorted(folder.glob(pattern)) if p.is_dir()]

    def folder_exists(self, prefix: str) -> bool:
        return self._folder_exists(prefix)

    def ensure_folder(self, prefix: str) -> None:
        (self.root / _prefix(prefix)).mkdir(parents=True, exist_ok=True)

    def label(self) -> str:
        return str(self.root)

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        yield self._doc_path(key)


class FileObjectStore(_FileBase):
    """사용자별 바이너리·JSON·HTML. 키가 `.md`로 끝나면 ValueError(문서는 FileDocStore)."""

    def _obj_path(self, key: str) -> Path:
        if _keys.is_doc(key):
            raise ValueError(f"md 문서는 DocStore로 다룹니다: {key!r}")
        return self._path(key)

    def _obj_prefix(self, prefix: str) -> str:
        return prefix

    def get(self, key: str) -> bytes:
        return self._obj_path(key).read_bytes()

    def put(self, key: str, data: bytes) -> None:
        path = self._obj_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock(path):
            refstore.write_atomic(path, data)

    def exists(self, key: str) -> bool:
        return self._obj_path(key).is_file()

    def stat(self, key: str) -> Meta:
        return _meta(self.root, self._obj_path(key))

    def list(self, prefix: str, pattern: str = "*") -> builtins.list[Meta]:
        return self._list(self._obj_prefix(prefix), pattern)

    def folder_exists(self, prefix: str) -> bool:
        return self._folder_exists(self._obj_prefix(prefix))

    def delete(self, key: str) -> None:
        self._obj_path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self.root / _prefix(self._obj_prefix(prefix)), ignore_errors=True)

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        yield self._obj_path(key)

    @contextmanager
    def write_path(self, key: str) -> Iterator[Path]:
        """외부 도구(OpenCV·PyAV·Remotion)가 쓸 최종 경로. 폴더 키(`/`로 끝남)면 그 폴더를, 아니면 부모를 만든다."""
        path = self._obj_path(key)
        (path if key.endswith("/") else path.parent).mkdir(parents=True, exist_ok=True)
        yield path

    def display_path(self, key: str) -> str:
        return str(self._obj_path(key))


class FileSharedCache(FileObjectStore):
    """영상 ID 기준 캐시. 키는 `workspace/{raw,cuts,frames,shots}/` 아래만."""

    def _obj_prefix(self, prefix: str) -> str:
        if not prefix.startswith(_keys.SHARED_PREFIXES):
            raise ValueError(f"공유 캐시 키가 아닙니다: {prefix!r}")
        return prefix

    def _obj_path(self, key: str) -> Path:
        return super()._obj_path(self._obj_prefix(key))


class FileScratch:
    """로컬 실행 보조(임시 핸드오프 파일, 기계별 파일, CLI 인자 경로)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def tmp_path(self, name: str) -> Path:
        path = self.root / _keys.check(f"workspace/tmp/{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def local_path(self, *parts: str) -> Path:
        return self.root / _keys.check("/".join(("workspace", *parts)))

    def arg_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def key_of(self, value: str | Path) -> str:
        """절대·상대 경로 → 키. resolve 기준으로 root 밖이면(`..`·심볼릭 링크 포함) ScriptError("bad_path")."""
        try:
            rel = self.arg_path(value).resolve().relative_to(self.root.resolve()).as_posix()
        except (OSError, ValueError) as e:
            raise ScriptError("bad_path", f"작업 폴더 밖의 경로입니다: {value}") from e
        if rel == ".":
            raise ScriptError("bad_path", f"작업 폴더 자체는 파일이 아닙니다: {value}")
        return _keys.check(rel)

    def workdir(self) -> Path:
        return self.root


def move_paths(root: Path, files: builtins.list[Path], last: Path) -> dict[str, Any]:
    """files(파일이나 폴더, 작업 폴더 안)를 `workspace/trash/<시각>/` 아래 같은 상대 경로로 옮긴다.
    되돌릴 때는 그 경로 그대로 작업 폴더에 다시 놓으면 된다. last(지울 대상의 md)는 마지막에 옮겨서,
    중간에 실패하면 옮긴 것을 되돌리고 md가 남아 화면에서 다시 지울 수 있게 한다. -> {"trash", "moved"}"""
    resolved = root.resolve()
    try:
        rels = [Path(f).resolve().relative_to(resolved) for f in files]
        last_rel = Path(last).resolve().relative_to(resolved)
    except ValueError as e:
        raise ScriptError("bad_path", f"작업 폴더 밖의 파일은 옮길 수 없습니다: {e}") from e
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:                                  # 같은 초에 두 번 지워도 서로 덮지 않는다
        folder = root.joinpath("workspace", "trash", stamp if n == 1 else f"{stamp}-{n}")
        try:
            folder.mkdir(parents=True)
            break
        except FileExistsError:
            n += 1
    rels.sort(key=lambda r: r == last_rel)
    done: builtins.list[tuple[Path, Path]] = []
    try:
        for rel in rels:
            src, dst = resolved / rel, folder / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            done.append((src, dst))
    except OSError as e:
        stuck = []
        for src, dst in reversed(done):
            try:
                shutil.move(str(dst), str(src))
            except OSError:
                stuck.append(dst.relative_to(root).as_posix())
        if not stuck:
            shutil.rmtree(folder, ignore_errors=True)  # 되돌리기가 다 끝났으면 남은 것은 빈 폴더뿐이다
        more = f" 되돌리지 못한 파일은 휴지통에 남아 있습니다: {', '.join(stuck)}" if stuck else ""
        raise ScriptError("trash_failed",
                          f"파일을 옮기지 못했습니다(다른 프로그램이 열어 두었는지 확인하세요): {e}.{more}") from e
    return {"trash": folder.relative_to(root).as_posix(),
            "moved": [rel.as_posix() for rel in rels[: len(done)]]}


class FileTrash:
    """`workspace/trash/<시각>[-n]/<원래 키>`로 옮긴다. 폴더 키는 끝 `/`가 있어도 없어도 된다(`moved`에는 `/` 없이)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / _keys.check(key).removesuffix("/")

    def move(self, keys: builtins.list[str], last: str) -> dict[str, Any]:
        return move_paths(self.root, [self._path(k) for k in keys], self._path(last))
