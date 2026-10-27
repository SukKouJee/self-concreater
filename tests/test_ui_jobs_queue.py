"""Job 작업 줄, 항목 덧붙이기, 중단 후에도 남는 확인 대기 항목."""
from __future__ import annotations

import threading

import pytest

from scripts.common import ScriptError
from scripts.ui.jobs import Job, JobCancelled, new_item


def _review_item(item_id: str) -> dict:
    return dict(new_item(item_id), state="awaiting", ask="review")


def test_append_items_returns_new_indexes() -> None:
    job = Job("collect", [new_item("a")])

    indexes = job.append_items([new_item("b"), new_item("c")])

    assert indexes == [1, 2]
    assert [i["id"] for i in job.items] == ["a", "b", "c"]


def test_append_items_copies_input() -> None:
    job = Job("collect")
    source = new_item("a")

    job.append_items([source])
    source["state"] = "failed"

    assert job.items[0]["state"] == "waiting"


def test_task_queue_is_fifo_and_zero_timeout_does_not_block() -> None:
    job = Job("collect")
    job.push_task({"kind": "x", "n": 1})
    job.push_task({"kind": "x", "n": 2})

    first = job.next_task(0)
    second = job.next_task(0)
    third = job.next_task(0)

    assert first is not None and first["n"] == 1
    assert second is not None and second["n"] == 2
    assert third is None


def test_next_task_times_out_with_none() -> None:
    job = Job("collect")

    assert job.next_task(0.01) is None


def test_push_task_wakes_blocked_next_task() -> None:
    job = Job("collect")
    got: list[dict | None] = []
    thread = threading.Thread(target=lambda: got.append(job.next_task(None)), daemon=True)
    thread.start()

    job.push_task({"kind": "x", "n": 7})
    thread.join(5)

    assert not thread.is_alive()
    assert got and got[0] is not None and got[0]["n"] == 7


def test_cancel_wakes_blocked_next_task() -> None:
    job = Job("collect")
    raised: list[BaseException] = []

    def worker() -> None:
        try:
            job.next_task(None)
        except JobCancelled as e:
            raised.append(e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    job.cancel()
    thread.join(5)

    assert not thread.is_alive()
    assert len(raised) == 1


def test_next_task_raises_when_already_cancelled() -> None:
    job = Job("collect")
    job.cancel()

    with pytest.raises(JobCancelled):
        job.next_task(0)


def test_take_review_is_atomic() -> None:
    job = Job("collect", [_review_item("a")])

    index = job.take_review("a")

    assert index == 0
    assert job.items[0]["state"] == "running"
    assert job.items[0]["step"] == "confirm"
    with pytest.raises(ScriptError) as e:
        job.take_review("a")
    assert e.value.code == "not_awaiting"


def test_take_review_rejects_non_review_ask() -> None:
    job = Job("collect", [dict(new_item("a"), state="awaiting", ask="save")])

    with pytest.raises(ScriptError) as e:
        job.take_review("a")

    assert e.value.code == "not_awaiting"


def test_take_review_unknown_item() -> None:
    job = Job("collect")

    with pytest.raises(ScriptError) as e:
        job.take_review("nope")

    assert e.value.code == "item_not_found"


def test_take_review_concurrent_only_one_wins() -> None:
    job = Job("collect", [_review_item("a")])
    results: list[str] = []
    barrier = threading.Barrier(8)

    def click() -> None:
        barrier.wait()
        try:
            job.take_review("a")
            results.append("ok")
        except ScriptError:
            results.append("err")

    threads = [threading.Thread(target=click, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert results.count("ok") == 1


def test_close_keeps_review_items() -> None:
    job = Job("collect", [_review_item("a"), dict(new_item("b"), state="running")])

    job._close("stopped", "중단됨", "중단됨")

    assert job.items[0]["state"] == "awaiting"
    assert job.items[1]["state"] == "skipped"


def test_close_failed_still_keeps_review_items() -> None:
    job = Job("collect", [_review_item("a"), dict(new_item("b"), state="running")])

    job._close("failed", "오류", "오류로 중단")

    assert job.items[0]["state"] == "awaiting"
    assert job.items[1]["state"] == "failed"


def test_review_done_and_idle_status() -> None:
    job = Job("collect", [_review_item("a")])

    job.set_idle_status()

    assert job.status == "awaiting"
    assert not job.review_done()


def test_review_done_true_when_no_awaiting_review() -> None:
    job = Job("collect", [dict(new_item("a"), state="saved")])

    job.set_idle_status()

    assert job.review_done()
    assert job.status == "running"
