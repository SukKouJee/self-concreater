"""전역 자원 제한 이음매(서버 설계 §5.2): `slot(name)`은 로컬판에서 아무것도 하지 않는다.

서버판이 시작할 때 `install(media=Semaphore(n), llm=Semaphore(m))`로 세마포어를 꽂으면, 같은 이름의
`with slot(...)` 블록이 프로세스 전체에서 동시에 n(m)개까지만 실행된다. 호출부는 로컬/서버를 구분하지 않는다.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal

SlotName = Literal["media", "llm"]
NAMES: tuple[str, ...] = ("media", "llm")
WAIT_TEXT = "다른 사용자 작업이 끝나기를 기다리는 중"

_slots: dict[str, threading.Semaphore] = {}


def install(*, media: threading.Semaphore | None = None, llm: threading.Semaphore | None = None) -> None:
    """슬롯을 통째로 바꾼다. 인자 없이 부르면 모두 no-op(로컬판 기본)으로 되돌린다."""
    _slots.clear()
    if media is not None:
        _slots["media"] = media
    if llm is not None:
        _slots["llm"] = llm


@contextmanager
def slot(name: SlotName, on_wait: Callable[[], None] | None = None) -> Iterator[None]:
    """자리를 잡고 블록을 실행한다. 자리가 없어 기다려야 하면 기다리기 전에 `on_wait()`를 한 번 부른다."""
    if name not in NAMES:
        raise ValueError(f"알 수 없는 슬롯입니다: {name!r}")
    sem = _slots.get(name)
    if sem is None:
        yield
        return
    if not sem.acquire(blocking=False):
        if on_wait is not None:
            on_wait()
        sem.acquire()
    try:
        yield
    finally:
        sem.release()
