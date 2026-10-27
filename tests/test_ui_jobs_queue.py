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


# ---- Task 3: queue open/close, status sync, adopting reviews ----------------------------

def _same_url(a: dict, b: dict) -> bool:
    return a["url"] == b["url"]


def test_append_queued_adds_items_and_tasks() -> None:
    job = Job("collect", [dict(new_item("a", url="u1"), state="running")])

    indexes = job.append_queued([new_item("b", url="u2")], "collect", _same_url)

    assert indexes == [1]
    assert job.next_task(0) == {"kind": "collect", "index": 1}


def test_append_queued_skips_items_matching_unfinished_or_new_ones() -> None:
    job = Job("collect", [dict(new_item("a", url="u1"), state="running"),
                          dict(new_item("s", url="u3"), state="saved")])

    indexes = job.append_queued(
        [new_item("x", url="u1"), new_item("y", url="u2"), new_item("z", url="u2"), new_item("w", url="u3")],
        "collect", _same_url,
    )

    assert [job.items[i]["id"] for i in indexes or []] == ["y", "w"]


def test_append_queued_refuses_closed_queue() -> None:
    job = Job("collect")
    job.close_queue()

    assert job.append_queued([new_item("b", url="u2")], "collect", _same_url) is None
    assert job.items == []


def test_append_queued_refuses_cancelled_job() -> None:
    job = Job("collect")
    job.cancel()

    assert job.append_queued([new_item("b", url="u2")], "collect", _same_url) is None


def test_requeue_updates_item_and_pushes_when_open() -> None:
    job = Job("collect", [dict(new_item("a"), state="running")])

    ok = job.requeue(0, {"kind": "reanalyze", "index": 0}, state="waiting", step="analyze")

    assert ok is True
    assert job.items[0]["state"] == "waiting" and job.items[0]["step"] == "analyze"
    assert job.next_task(0) == {"kind": "reanalyze", "index": 0}


@pytest.mark.parametrize("close", ["queue", "cancel", "finish"])
def test_requeue_refuses_when_job_cannot_run_it(close: str) -> None:
    job = Job("collect", [dict(new_item("a"), state="running")])
    if close == "queue":
        job.close_queue()
    elif close == "cancel":
        job.cancel()
    else:
        job.finish("stopped")

    ok = job.requeue(0, {"kind": "reanalyze", "index": 0}, state="waiting")

    assert ok is False
    assert job.items[0]["state"] == "running"


def test_close_queue_returns_leftover_tasks_and_closes() -> None:
    job = Job("collect", [new_item("a")])
    job.push_task({"kind": "collect", "index": 0})

    leftovers = job.close_queue()

    assert leftovers == [{"kind": "collect", "index": 0}]
    assert job.next_task(0) is None
    assert job.requeue(0, {"kind": "x"}) is False


def test_close_if_idle_finishes_done_when_all_items_finished() -> None:
    job = Job("collect", [dict(new_item("a"), state="saved"), dict(new_item("b"), state="skipped")])

    closed = job.close_if_idle(lambda items: {"n": len(items)})

    assert closed is True
    assert job.status == "done" and job.summary == {"n": 2}
    assert job.requeue(0, {"kind": "x"}) is False


@pytest.mark.parametrize("state", ["awaiting", "running", "waiting"])
def test_close_if_idle_waits_for_unfinished_items(state: str) -> None:
    job = Job("collect", [dict(new_item("a"), state=state, ask="review")])

    assert job.close_if_idle(lambda items: {}) is False
    assert job.status == "running"


def test_close_if_idle_waits_for_queued_tasks() -> None:
    job = Job("collect", [dict(new_item("a"), state="saved")])
    job.push_task({"kind": "x"})

    assert job.close_if_idle(lambda items: {}) is False


def test_sync_status_lowers_awaiting_when_no_review_left() -> None:
    job = Job("collect", [_review_item("a")])
    job.sync_status()
    assert job.status == "awaiting"

    job.update(0, state="saved", ask=None)
    job.sync_status()

    assert job.status == "running"


def test_sync_status_leaves_finished_job_alone() -> None:
    job = Job("collect", [_review_item("a")])
    job.finish("stopped")

    job.sync_status()

    assert job.status == "stopped"


def test_release_reviews_marks_moved_and_returns_copies() -> None:
    job = Job("collect", [dict(_review_item("a"), preview={"x": 1}), dict(new_item("b"), state="saved")])
    job.finish("stopped")

    released = job.release_reviews("새 작업으로 옮김")

    assert [(i, item["id"], item["state"], item["preview"]) for i, item in released] == [(0, "a", "awaiting", {"x": 1})]
    assert job.items[0]["state"] == "skipped" and job.items[0]["message"] == "새 작업으로 옮김"
    assert job.items[1]["state"] == "saved"


def test_set_summary_keeps_status_and_message() -> None:
    job = Job("collect")
    job.finish("stopped", {"saved": 0}, "중단됨")

    job.set_summary({"saved": 1})

    snap = job.snapshot()
    assert (snap["status"], snap["message"], snap["summary"]) == ("stopped", "중단됨", {"saved": 1})


def test_manager_keeps_target_on_job() -> None:
    from scripts.ui.jobs import JobManager

    def target(job: Job) -> None:
        return None

    mgr = JobManager()
    job = mgr.start("collect", target)
    job.join(5)

    assert job.target is target


def test_manager_queues_start_tasks_before_the_worker_runs() -> None:
    from scripts.ui.jobs import JobManager

    seen: list[dict | None] = []
    mgr = JobManager()
    job = mgr.start("collect", lambda j: seen.append(j.next_task(0)), items=[new_item("a")],
                    tasks=[{"kind": "collect", "index": 0}])
    job.join(5)

    assert seen == [{"kind": "collect", "index": 0}]


def test_manager_latest_where_scans_newest_first() -> None:
    from scripts.ui.jobs import JobManager

    mgr = JobManager()
    first = mgr.start("collect", lambda j: None, items=[new_item("a")])
    first.join(5)
    second = mgr.start("collect", lambda j: None, items=[new_item("b")])
    second.join(5)
    other = mgr.start("conti", lambda j: None)
    other.join(5)

    assert mgr.latest_where("collect", lambda j: True) is second
    assert mgr.latest_where("collect", lambda j: j.items[0]["id"] == "a") is first
    assert mgr.latest_where("collect", lambda j: False) is None
