"""실행 문맥(Ctx): 지금 실행이 어느 작업 폴더(와 사용자)를 위한 것인지, 그리고 그 저장소.

`here()`는 문맥이 있으면 그것을, 없으면 지금처럼 `YT_COACH_HOME` 또는 현재 폴더로 **호출마다** 임시 Ctx를 만든다
(ADR-7: 저장소를 프로세스 전역으로 캐시하지 않는다. 테스트가 `YT_COACH_HOME`만 바꿔도 따라간다).
`common.work_root()`는 `here().root`다. 모듈은 `ctx.here().store.read(keys.index_md(cat))`처럼 쓴다.

- `use(c)`: 블록 동안만 문맥을 바꾸고 끝나면(예외 포함) 이전 값으로 되돌린다. 로컬판 시작 지점
  (`common.run`, UI 요청 스레드)은 이것을 쓴다. 같은 프로세스에서 여러 번 불려도(테스트) 뒤 호출의
  환경 변수 변경을 막지 않는다.
- `set_default(c)`: 문맥이 없을 때 쓰는 프로세스 전역 기본값. 모든 스레드가 본다. 프로세스가 끝날 때까지
  한 작업 폴더만 쓰는 시작 지점(서버판 워커 등)용이고, 로컬판은 쓰지 않는다.
- ContextVar는 새 스레드로 저절로 넘어가지 않는다. 스레드를 만들 때 `contextvars.copy_context().run`으로 넘긴다
  (`ui.jobs.JobManager.start`).
- 저장소(`store`·`objects`·`shared`·`scratch`·`trash`)는 처음 쓸 때 만든다. `backend`가 있으면(서버판) 그것이,
  없으면 `scripts.filestore`의 파일 구현이 `root`로 만든다. frozen이어도 `cached_property`는 `__dict__`에 써서 동작한다.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.stores import Backend, DocStore, ObjectStore, Scratch, SharedCache, Trash, UsageMeter


@dataclass(frozen=True)
class Ctx:
    root: Path | None = None              # 로컬판만. 서버판 None
    user_id: str | None = None
    backend: Backend | None = field(default=None, compare=False, repr=False)   # 서버판이 주입

    def _root(self) -> Path:
        if self.root is None:
            raise RuntimeError("작업 폴더(root)도 저장소 backend도 없는 실행 문맥입니다")
        return self.root

    @cached_property
    def store(self) -> DocStore:
        if self.backend is not None:
            return self.backend.store(self.user_id)
        from scripts import filestore
        return filestore.FileDocStore(self._root())

    @cached_property
    def objects(self) -> ObjectStore:
        if self.backend is not None:
            return self.backend.objects(self.user_id)
        from scripts import filestore
        return filestore.FileObjectStore(self._root())

    @cached_property
    def shared(self) -> SharedCache:
        if self.backend is not None:
            return self.backend.shared(self.user_id)
        from scripts import filestore
        return filestore.FileSharedCache(self._root())

    @cached_property
    def scratch(self) -> Scratch:
        if self.backend is not None:
            return self.backend.scratch(self.user_id)
        from scripts import filestore
        return filestore.FileScratch(self._root())

    @cached_property
    def trash(self) -> Trash:
        if self.backend is not None:
            return self.backend.trash(self.user_id)
        from scripts import filestore
        return filestore.FileTrash(self._root())

    @cached_property
    def usage(self) -> UsageMeter:
        if self.backend is not None:
            return self.backend.usage(self.user_id)
        from scripts import stores
        return stores.NullMeter()

    def for_key(self, key: str) -> DocStore | ObjectStore:
        """키를 다루는 저장소(`keys.kind`): md는 store, 영상 캐시는 shared, 나머지는 objects.
        임시 키(`workspace/tmp/`)는 저장소가 아니라 scratch라서 ValueError(`local_file`은 scratch로 푼다)."""
        from scripts import keys
        kind = keys.kind(key)
        if kind == "scratch":
            raise ValueError(f"임시 파일 키는 저장소에 없습니다: {key!r}")
        if kind == "doc":
            return self.store
        return self.shared if kind == "shared" else self.objects

    def local_file(self, key: str) -> AbstractContextManager[Path]:
        """키의 실제 파일 경로(읽기용). 임시 키는 `scratch.tmp_file`, 나머지는 `for_key(key).local_path`."""
        from scripts import keys
        if keys.kind(key) == "scratch":
            return self.scratch.tmp_file(key)
        return self.for_key(key).local_path(key)


_current: ContextVar[Ctx | None] = ContextVar("yt_coach_ctx", default=None)
_default: Ctx | None = None


def current() -> Ctx | None:
    """지금 문맥. `use()` 안이면 그 값, 아니면 `set_default()` 값, 둘 다 없으면 None."""
    return _current.get() or _default


def here() -> Ctx:
    """지금 문맥, 없으면 `YT_COACH_HOME`(없으면 현재 폴더)로 만든 임시 문맥. 문맥을 설정하지는 않는다."""
    c = current()
    if c is not None:
        return c
    home = os.environ.get("YT_COACH_HOME")
    return Ctx(root=Path(home) if home else Path.cwd())


@contextmanager
def use(c: Ctx) -> Iterator[Ctx]:
    token = _current.set(c)
    try:
        yield c
    finally:
        _current.reset(token)


def set_default(c: Ctx | None) -> None:
    global _default
    _default = c
