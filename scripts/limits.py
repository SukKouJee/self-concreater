"""전역 자원 제한 이음매(서버 설계 §5.2): `slot(name)`은 로컬판에서 아무것도 하지 않는다.

서버판이 시작할 때 `install(media=Semaphore(n), llm=Semaphore(m))`로 세마포어를 꽂으면, 같은 이름의
`with slot(...)` 블록이 프로세스 전체에서 동시에 n(m)개까지만 실행된다. 호출부는 로컬/서버를 구분하지 않는다.

`collecting(video_id)`: 같은 영상의 수집(raw·컷·프레임·STT)을 한 번에 하나씩(서버 설계 §3.3). 로컬판은 no-op이고,
서버판은 `install(collect=...)`로 PG advisory lock(사용자·프로세스를 가리지 않는 잠금)을 꽂는다.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Literal

SlotName = Literal["media", "llm"]
NAMES: tuple[str, ...] = ("media", "llm")
WAIT_TEXT = "다른 사용자 작업이 끝나기를 기다리는 중"
COLLECT_WAIT_TEXT = "다른 사용자가 같은 영상을 받는 중"

# (video_id, on_wait) -> 잠금을 잡는 컨텍스트. 자리가 없어 기다려야 하면 기다리기 전에 on_wait()를 한 번 부른다.
CollectLock = Callable[[str, Callable[[], None] | None], AbstractContextManager[None]]

_slots: dict[str, threading.Semaphore] = {}
_collect: list[CollectLock] = []


def install(*, media: threading.Semaphore | None = None, llm: threading.Semaphore | None = None,
            collect: CollectLock | None = None) -> None:
    """슬롯·수집 잠금을 통째로 바꾼다. 인자 없이 부르면 모두 no-op(로컬판 기본)으로 되돌린다."""
    _slots.clear()
    _collect.clear()
    if media is not None:
        _slots["media"] = media
    if llm is not None:
        _slots["llm"] = llm
    if collect is not None:
        _collect.append(collect)


def collect_waiter(report: Callable[[str, float | None, str], None] | None) -> Callable[[], None] | None:
    """`collecting(on_wait=...)`용: 진행 콜백이 있으면 기다리는 동안 ("wait", None, COLLECT_WAIT_TEXT)를 한 번 알린다
    (진행 단계 "wait"는 걸린 시간에 넣지 않는다)."""
    if report is None:
        return None
    return lambda: report("wait", None, COLLECT_WAIT_TEXT)


@contextmanager
def collecting(video_id: str, on_wait: Callable[[], None] | None = None) -> Iterator[None]:
    """같은 영상을 다른 작업(다른 사용자 포함)이 받는 중이면 끝날 때까지 기다린 뒤 블록을 실행한다.
    블록 안에서 캐시 확인 → 받기 → 저장을 하면, 뒤에 온 쪽은 앞의 결과를 캐시로 쓴다."""
    lock = _collect[0] if _collect else None
    if lock is None:
        yield
        return
    with lock(video_id, on_wait):
        yield


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
