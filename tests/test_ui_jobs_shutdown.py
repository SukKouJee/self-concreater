"""JobManager.shutdown: 모든 작업에 먼저 취소를 알리고, 하나의 마감 시각까지만 기다린다(작업 수 × timeout이 아니다)."""
from __future__ import annotations

import threading
import time

import pytest

from scripts.ui.jobs import Job, JobManager


def _stubborn(release: threading.Event):
    def target(job: Job) -> None:
        release.wait(10)          # ignores cancel until the test releases it
    return target


def test_shutdown_waits_one_shared_deadline_not_per_job() -> None:
    mgr = JobManager()
    release = threading.Event()
    try:
        for n in range(4):
            mgr.start(f"stubborn{n}", _stubborn(release))
        t0 = time.monotonic()
        mgr.shutdown(timeout=0.5)
        elapsed = time.monotonic() - t0
    finally:
        release.set()
    assert 0.4 <= elapsed < 1.2, elapsed


def test_shutdown_cancels_every_job_before_any_join(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    real_cancel, real_join = Job.cancel, Job.join
    monkeypatch.setattr(Job, "cancel", lambda self: (events.append("cancel"), real_cancel(self))[1])
    monkeypatch.setattr(Job, "join", lambda self, timeout=None: (events.append("join"), real_join(self, timeout))[1])
    mgr = JobManager()
    release = threading.Event()
    try:
        for n in range(3):
            mgr.start(f"stubborn{n}", _stubborn(release))
        mgr.shutdown(timeout=0.1)
    finally:
        release.set()
    assert events[:3] == ["cancel"] * 3
    assert "cancel" not in events[3:]


def test_shutdown_returns_early_when_jobs_stop_on_cancel() -> None:
    mgr = JobManager()
    jobs = [mgr.start(f"polite{n}", lambda j: j.wait_decision("never")) for n in range(3)]
    t0 = time.monotonic()
    mgr.shutdown(timeout=5)
    assert time.monotonic() - t0 < 2
    assert all(job.snapshot()["status"] == "stopped" for job in jobs)


def test_shutdown_none_timeout_waits_for_all() -> None:
    mgr = JobManager()
    release = threading.Event()
    jobs = [mgr.start(f"stubborn{n}", _stubborn(release)) for n in range(2)]
    threading.Timer(0.3, release.set).start()
    mgr.shutdown(timeout=None)
    assert all(not job._thread.is_alive() for job in jobs)  # type: ignore[union-attr]
