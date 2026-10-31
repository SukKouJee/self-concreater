"""scripts.limits: 기본 no-op, 서버가 install한 세마포어로 동시 실행을 제한한다."""
from __future__ import annotations

import threading

import pytest

from scripts import limits


@pytest.fixture(autouse=True)
def reset_limits():
    limits.install()
    yield
    limits.install()


def test_slot_is_noop_by_default_and_reentrant() -> None:
    with limits.slot("media"), limits.slot("media"), limits.slot("llm"):
        pass


def test_slot_rejects_unknown_name() -> None:
    with pytest.raises(ValueError):
        with limits.slot("gpu"):  # type: ignore[arg-type]
            pass


def test_install_limits_concurrency_and_releases_on_error() -> None:
    limits.install(media=threading.Semaphore(1))
    with limits.slot("media"):
        got = threading.Semaphore(0)

        def other() -> None:
            with limits.slot("media"):
                got.release()

        t = threading.Thread(target=other)
        t.start()
        assert not got.acquire(timeout=0.2)     # 자리가 없어 기다린다
    assert got.acquire(timeout=2)               # 반납되면 들어간다
    t.join()

    with pytest.raises(RuntimeError):
        with limits.slot("media"):
            raise RuntimeError("boom")
    with limits.slot("media"):                  # 예외 뒤에도 반납됨
        pass


def test_on_wait_called_only_when_blocked() -> None:
    limits.install(llm=threading.Semaphore(1))
    waited: list[int] = []
    with limits.slot("llm", on_wait=lambda: waited.append(1)):
        assert waited == []
        t = threading.Thread(target=lambda: limits.slot("llm", on_wait=lambda: waited.append(2)).__enter__())
        t.daemon = True
        t.start()
        t.join(0.3)
        assert waited == [2]


# --- collecting(video_id): 같은 영상 수집 직렬화 이음매(서버 설계 §3.3, 서버는 PG advisory lock) ---

def test_collecting_is_noop_by_default() -> None:
    with limits.collecting("AAAAAAAAAAA"), limits.collecting("AAAAAAAAAAA"):
        pass


def test_installed_collect_lock_receives_video_and_on_wait() -> None:
    from contextlib import contextmanager

    seen: list[tuple[str, object]] = []

    @contextmanager
    def lock(video_id, on_wait):
        seen.append(("enter", video_id))
        if on_wait is not None:
            on_wait()
        yield
        seen.append(("exit", video_id))

    limits.install(collect=lock)
    waited: list[int] = []
    with limits.collecting("BBBBBBBBBBB", on_wait=lambda: waited.append(1)):
        assert seen == [("enter", "BBBBBBBBBBB")]
    assert seen[-1] == ("exit", "BBBBBBBBBBB") and waited == [1]
    limits.install()
    with limits.collecting("CCCCCCCCCCC"):
        pass
    assert len(seen) == 2                       # install() 뒤에는 다시 no-op


def test_collect_waiter_reports_a_wait_step_or_nothing() -> None:
    calls: list[tuple] = []
    assert limits.collect_waiter(None) is None
    waiter = limits.collect_waiter(lambda *a: calls.append(a))
    assert waiter is not None
    waiter()
    assert calls == [("wait", None, limits.COLLECT_WAIT_TEXT)]
