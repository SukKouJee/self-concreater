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
