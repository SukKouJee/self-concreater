"""실행 문맥(Ctx): 지금 실행이 어느 작업 폴더(와 사용자)를 위한 것인지.

`common.work_root`는 문맥이 있으면 그 root를, 없으면 지금처럼 `YT_COACH_HOME` 또는 현재 폴더를 쓴다.

- `use(c)`: 블록 동안만 문맥을 바꾸고 끝나면(예외 포함) 이전 값으로 되돌린다. 로컬판 시작 지점
  (`common.run`, UI 요청 스레드)은 이것을 쓴다. 같은 프로세스에서 여러 번 불려도(테스트) 뒤 호출의
  환경 변수 변경을 막지 않는다.
- `set_default(c)`: 문맥이 없을 때 쓰는 프로세스 전역 기본값. 모든 스레드가 본다. 프로세스가 끝날 때까지
  한 작업 폴더만 쓰는 시작 지점(서버판 워커 등)용이고, 로컬판은 쓰지 않는다.
- ContextVar는 새 스레드로 저절로 넘어가지 않는다. 스레드를 만들 때 `contextvars.copy_context().run`으로 넘긴다
  (`ui.jobs.JobManager.start`).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Ctx:
    root: Path
    user_id: str | None = None


_current: ContextVar[Ctx | None] = ContextVar("yt_coach_ctx", default=None)
_default: Ctx | None = None


def current() -> Ctx | None:
    """지금 문맥. `use()` 안이면 그 값, 아니면 `set_default()` 값, 둘 다 없으면 None."""
    return _current.get() or _default


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
