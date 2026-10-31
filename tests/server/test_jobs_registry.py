"""사용자별 JobManager(서버 설계 §5.1): 사용자마다 따로, 남의 job_id는 job_not_found, SINGLE_KINDS도 사용자별, 종료."""
from __future__ import annotations

import threading
import uuid

import pytest

from scripts.common import ScriptError
from server.jobs_registry import Registry


def _blocking_target(release: threading.Event):
    def target(job):
        release.wait(5)
    return target


def test_same_user_same_manager_other_user_other_manager():
    reg = Registry()
    a, b = uuid.uuid4(), uuid.uuid4()
    assert reg.for_user(a) is reg.for_user(str(a))
    assert reg.for_user(a) is not reg.for_user(b)


def test_job_ids_are_not_visible_across_users():
    reg = Registry()
    a, b = uuid.uuid4(), uuid.uuid4()
    release = threading.Event()
    job = reg.for_user(a).start("help_chat", _blocking_target(release))
    try:
        assert reg.for_user(a).get(job.id) is job
        with pytest.raises(ScriptError) as e:
            reg.for_user(b).get(job.id)
        assert e.value.code == "job_not_found"
    finally:
        release.set()
        reg.shutdown(timeout=5)


def test_single_kinds_are_per_user():
    reg = Registry()
    a, b = uuid.uuid4(), uuid.uuid4()
    release = threading.Event()
    try:
        reg.for_user(a).start("help_chat", _blocking_target(release))
        reg.for_user(b).start("help_chat", _blocking_target(release))      # B is not blocked by A's job
        with pytest.raises(ScriptError) as e:
            reg.for_user(a).start("help_chat", _blocking_target(release))
        assert e.value.code == "job_running"
    finally:
        release.set()
        reg.shutdown(timeout=5)


def test_bad_user_id_is_refused():
    with pytest.raises(ValueError):
        Registry().for_user("not-a-uuid")


def test_shutdown_cancels_jobs_and_refuses_new_managers():
    reg = Registry()
    a = uuid.uuid4()
    job = reg.for_user(a).start("help_chat", lambda j: j.wait_decision("never"))
    reg.shutdown(timeout=5)
    assert reg.closing
    assert job.snapshot()["status"] == "stopped"
    assert reg.for_user(a) is not None                     # existing manager stays (status reads, cancel)
    with pytest.raises(ScriptError) as e:
        reg.for_user(uuid.uuid4())
    assert e.value.code == "restarting"


def _stubborn(release: threading.Event):
    def target(job):
        release.wait(10)          # ignores cancel until released
    return target


def test_shutdown_uses_one_deadline_across_all_users_and_jobs():
    import time
    reg = Registry()
    release = threading.Event()
    try:
        for _ in range(3):
            mgr = reg.for_user(uuid.uuid4())
            mgr.start("help_chat", _stubborn(release))
            mgr.start("draft", _stubborn(release))
        t0 = time.monotonic()
        reg.shutdown(timeout=0.5)
        elapsed = time.monotonic() - t0
    finally:
        release.set()
    assert 0.4 <= elapsed < 1.2, elapsed


def test_shutdown_cancels_every_users_jobs_before_any_join(monkeypatch):
    from scripts.ui.jobs import Job
    events = []
    real_cancel, real_join = Job.cancel, Job.join
    monkeypatch.setattr(Job, "cancel", lambda self: (events.append("cancel"), real_cancel(self))[1])
    monkeypatch.setattr(Job, "join", lambda self, timeout=None: (events.append("join"), real_join(self, timeout))[1])
    reg = Registry()
    release = threading.Event()
    try:
        for _ in range(3):
            reg.for_user(uuid.uuid4()).start("help_chat", _stubborn(release))
        reg.shutdown(timeout=0.1)
    finally:
        release.set()
    assert events[:3] == ["cancel"] * 3
    assert "cancel" not in events[3:]
