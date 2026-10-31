"""PG 인덱스 저장소 공용 도우미(서버 설계 §3.3): PgDocStore·S3ObjectStore·S3SharedCache가 같은 규칙을 쓰게 한 곳에 둔다.

- 이름 일치: Windows pathlib glob과 같은 대소문자 무시 fnmatch(`matcher`).
- 순서: `sorted(key=PureWindowsPath)` — Windows FileStore의 `sorted(glob)`과 같다.
  대소문자만 다른 두 키는 키 문자열로 고정.
- 접두사: `/`로 끝나야 하고(아니면 ValueError) `keys.check`를 거친다(bad_path).
"""
from __future__ import annotations

import builtins
import errno
import fnmatch
import hashlib
import os
import re
from collections.abc import Callable, Iterable
from functools import lru_cache
from pathlib import PureWindowsPath
from typing import Literal

from scripts import keys as _keys
from scripts.stores import Meta


ImportOutcome = Literal["new", "same", "replaced", "conflict", "memo", "skipped"]
"""관리 가져오기(`import_raw`) 결과: 새로 넣음 / 같은 내용이라 그대로 / 덮어씀 / 다른 내용이 있어 건드리지 않음(conflict) / 덮어쓰면 서버의 `## 내 메모`가 바뀌어 건드리지 않음(memo) /
공유 캐시에 이미 있어 건너뜀(skipped)."""


@lru_cache(maxsize=256)
def matcher(pattern: str) -> Callable[[str], re.Match[str] | None]:
    """Windows pathlib glob(3.12 `_compile_pattern`, case_sensitive=False)과 같은 일치 함수."""
    if not pattern or "/" in pattern or "\\" in pattern:
        raise ValueError(f"패턴은 이름 하나에 대한 glob이어야 합니다: {pattern!r}")
    return re.compile(fnmatch.translate(pattern), re.IGNORECASE).match


def windows_order(names: Iterable[str]) -> builtins.list[str]:
    """Windows FileStore 순서. 대소문자만 다른 두 키(Windows에는 없다)는 키 문자열로 순서를 고정한다."""
    return sorted(names, key=lambda k: (PureWindowsPath(k), k))


def check_prefix(prefix: str) -> str:
    if not prefix.endswith("/"):
        raise ValueError(f"접두사는 '/'로 끝나야 합니다: {prefix!r}")
    return _keys.check(prefix)


def ancestors(key: str) -> builtins.list[str]:
    """키를 담는 폴더 접두사들(바깥부터). `refs/ref_a/videos/x.md` → refs/, refs/ref_a/, refs/ref_a/videos/.
    폴더 키(`/`로 끝남)는 자기 자신까지."""
    parts = key.split("/")[:-1]
    return ["/".join(parts[: i + 1]) + "/" for i in range(len(parts))]


def not_found(key: str) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), key)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def meta(key: str, size: int, mtime_ns: int) -> Meta:
    return Meta(key=key, size=size, mtime=mtime_ns / 1e9, mtime_ns=mtime_ns)
