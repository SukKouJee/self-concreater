"""작업 항목 진행 추적(ItemTiming) 테스트: 가짜 Job과 가짜 시계로 검증한다."""
from __future__ import annotations

import copy
import threading
from typing import Any

import pytest

from scripts import timings
from scripts.ui import jobs, progress


class FakeJob:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []

    def update(self, index: int, **fields: Any) -> None:
        self.calls.append((index, copy.deepcopy(fields)))

    @property
    def timing(self) -> dict[str, Any]:
        return [f["timing"] for _, f in self.calls if "timing" in f][-1]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, sec: float) -> None:
        self.now += sec


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(progress.time, "time", lambda: c.now)
    monkeypatch.setattr(progress.time, "monotonic", lambda: c.now)
    return c


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(progress.timings, "record", lambda step, **kw: calls.append((step, kw)))
    monkeypatch.setattr(progress.timings, "expected", lambda step, **kw: 100.0 if kw.get("video_sec") else (
        None if step in ("cuts", "stt") else 5.0))
    return calls


def make(rows: list[str] | None = None, engine: str | None = None) -> tuple[FakeJob, progress.ItemTiming]:
    job = FakeJob()
    return job, progress.ItemTiming(job, 0, rows or ["fetch", "cuts", "analyze"], engine=engine)  # type: ignore[arg-type]


def test_rows_start_pending_with_expected(clock: Clock, recorded: list) -> None:
    job, t = make()
    assert job.timing["video_sec"] is None
    assert job.timing["steps"]["cuts"] == {"state": "pending", "expected": None}
    t.set_video_sec(600)
    assert job.timing["video_sec"] == 600
    assert job.timing["steps"]["cuts"]["expected"] == 100.0


def test_expected_matches_timings_module(clock: Clock) -> None:
    job, t = make()
    t.set_video_sec(600)
    assert job.timing["steps"]["cuts"]["expected"] == timings.expected("cuts", video_sec=600)


def test_fraction_never_decreases(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("cuts")
    t.phase("scenes", 0.5, "장면 찾는 중")
    clock.advance(1)
    t.phase("scenes", 0.3, "장면 찾는 중")
    clock.advance(1)
    t.phase("scenes", 0.31, "장면 찾는 중")
    w = progress.PHASE_WEIGHTS["cuts"]
    assert t._timing["steps"]["cuts"]["fraction"] == pytest.approx(w["download"] + w["scenes"] * 0.5)


def test_zero_weight_phase_keeps_fraction(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("fetch")
    t.phase("meta", 0.9, "메타")
    row = t._timing["steps"]["fetch"]
    assert row["fraction"] is None
    assert row["phase_text"] == "메타"


def test_updates_are_throttled(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("cuts")
    base = len(job.calls)
    t.phase("download", 0.0, "받는 중")  # name change -> immediate
    for i in range(10):
        clock.advance(0.1)
        t.phase("download", i / 10, "받는 중")
    assert len(job.calls) - base <= 1 + 2
    t.phase("scenes", 0.0, "장면")  # name change -> immediate
    assert len(job.calls) - base <= 1 + 2 + 1
    assert job.timing["steps"]["cuts"]["phase"] == "scenes"


def test_throttled_value_not_lost(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("cuts")
    t.phase("scenes", 0.1, "a")
    clock.advance(0.1)
    t.phase("scenes", 0.8, "b")  # skipped
    w = progress.PHASE_WEIGHTS["cuts"]
    assert job.timing["steps"]["cuts"]["fraction"] == pytest.approx(w["download"] + w["scenes"] * 0.1)
    t.done("cuts")
    assert job.timing["steps"]["cuts"]["fraction"] == 1.0


def test_pause_excludes_waiting_time(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("analyze")
    clock.advance(10)
    t.pause()
    clock.advance(100)
    t.resume()
    clock.advance(5)
    t.done("analyze")
    assert job.timing["steps"]["analyze"]["elapsed"] == pytest.approx(15)
    assert recorded[0][1]["elapsed_sec"] == pytest.approx(15)


def test_restart_resets_row(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("analyze")
    clock.advance(3)
    t.done("analyze")
    clock.advance(50)
    t.start("analyze")
    row = job.timing["steps"]["analyze"]
    assert row["state"] == "running"
    assert row.get("elapsed") is None
    assert row["started_at"] == clock.now
    assert row["fraction"] is None


def test_cached_done_not_recorded(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.done("cuts", cached=True)
    assert recorded == []
    row = job.timing["steps"]["cuts"]
    assert row["state"] == "done"
    assert row["elapsed"] == 0
    assert row["expected"] == 0.0


def test_fail_not_recorded(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("cuts")
    t.fail("cuts")
    assert recorded == []
    assert job.timing["steps"]["cuts"]["state"] == "failed"


def test_sub_key_used_for_expected_and_record(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.set_video_sec(60)
    t.start("fetch", sub="stt")
    assert job.timing["steps"]["fetch"]["expected"] == 100.0
    clock.advance(4)
    t.done("fetch", text_chars=500)
    step, kw = recorded[0]
    assert step == "stt"
    assert kw["text_chars"] == 500 and kw["video_sec"] == 60


def test_record_passes_engine(clock: Clock, recorded: list) -> None:
    job, t = make(engine="claude")
    t.start("analyze")
    clock.advance(2)
    t.done("analyze")
    assert recorded[0][0] == "analyze"
    assert recorded[0][1]["engine"] == "claude"


def test_snapshot_has_now() -> None:
    assert isinstance(jobs.Job("collect", []).snapshot()["now"], float)


def test_pause_on_one_row_does_not_leak_to_next(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("fetch")
    t.pause()
    t.start("cuts")
    assert "paused_at" not in job.timing["steps"]["fetch"]
    clock.advance(4)
    t.done("cuts")
    assert job.timing["steps"]["cuts"]["elapsed"] == pytest.approx(4)


def test_concurrent_phase_and_start_done_do_not_raise(recorded: list) -> None:
    real = jobs.Job("collect", [jobs.new_item("a")])
    t = progress.ItemTiming(real, 0, ["fetch", "cuts"])
    errors: list[BaseException] = []

    def hammer_phase() -> None:
        try:
            for i in range(3000):
                t.phase("scenes" if i % 2 else "camera", (i % 10) / 10, "x")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def hammer_rows() -> None:
        try:
            for _ in range(3000):
                t.start("cuts")
                t.done("cuts")
                real.snapshot()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hammer_phase), threading.Thread(target=hammer_rows)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    assert real.item(0)["timing"]["steps"]["cuts"]["state"] == "done"


def test_skip_sets_skipped_without_record(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("fetch")
    t.pause()
    t.skip("fetch")
    row = job.timing["steps"]["fetch"]
    assert row["state"] == "skipped" and row["expected"] == 0.0 and "paused_at" not in row
    assert recorded == []
    t.phase("stt", 0.5, "음성 인식 중")              # 지금 달리는 줄이 없다
    assert job.timing["steps"]["fetch"].get("phase") is None


def test_skip_pending_row(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.skip("analyze")
    assert job.timing["steps"]["analyze"] == {"state": "skipped", "expected": 0.0}


def test_pause_resume_without_publish(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("cuts")
    before = len(job.calls)
    t.pause(publish=False)
    clock.advance(30)
    t.resume(publish=False)
    assert len(job.calls) == before
    clock.advance(2)
    t.done("cuts")
    assert job.timing["steps"]["cuts"]["elapsed"] == pytest.approx(2)


def test_cuts_weights_are_measured_defaults() -> None:
    # 2026-10-25 순차 읽기로 바꾼 뒤 실측 두 건(받기/장면/카메라 비중):
    # 1208초·508장면 0.08/0.65/0.28, 1113초·142장면 0.05/0.79/0.16
    w = progress.PHASE_WEIGHTS["cuts"]
    assert w == {"wait": 0.0, "download": 0.06, "scenes": 0.70, "camera": 0.24}
    assert sum(w.values()) == pytest.approx(1.0)
    assert [k for k, v in w.items() if v > 0][-1] == "camera"   # progress-utils.js LAST_PHASES와 맞춘다


def test_phase_records_start_and_own_fraction(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("cuts")
    row = job.timing["steps"]["cuts"]
    assert row["phase_started_at"] is None and row["phase_fraction"] is None
    t.phase("scenes", 0.5, "장면")
    scenes_start = clock.now
    clock.advance(10)
    t.phase("camera", 0.0, "카메라")
    cam_start = clock.now
    clock.advance(1)
    t.phase("camera", 0.25, "카메라")
    row = t._timing["steps"]["cuts"]
    assert row["phase_started_at"] == cam_start != scenes_start
    assert row["phase_fraction"] == pytest.approx(0.25)
    t.phase("camera", None, "카메라")                 # 측정 불가 값은 그 구간 진행률을 지우지 않는다
    assert t._timing["steps"]["cuts"]["phase_fraction"] == pytest.approx(0.25)


def test_resume_shifts_phase_started_at(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("cuts")
    t.phase("camera", 0.1, "카메라")
    cam_start = clock.now
    clock.advance(5)
    t.pause()
    clock.advance(100)
    t.resume()
    row = job.timing["steps"]["cuts"]
    assert row["phase_started_at"] == pytest.approx(cam_start + 100)
    assert row["started_at"] == pytest.approx(cam_start + 100)


def test_phase_change_while_paused_starts_at_pause(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("cuts")
    clock.advance(3)
    t.pause(publish=False)
    clock.advance(20)
    t.phase("wait", None, "차단 방지 대기")
    clock.advance(10)
    t.resume()
    # 활성 시간 기준으로 이 구간은 재개 시각에 막 시작한 것과 같다.
    assert job.timing["steps"]["cuts"]["phase_started_at"] == pytest.approx(clock.now)


def test_restart_with_other_sub_keeps_prior_elapsed(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("fetch")
    clock.advance(6)
    t.done("fetch")
    t.set_video_sec(60)
    t.start("fetch", sub="stt")
    row = job.timing["steps"]["fetch"]
    assert row["prior_elapsed"] == pytest.approx(6)
    assert row["prior_expected"] == 5.0
    clock.advance(30)
    t.done("fetch")
    row = job.timing["steps"]["fetch"]
    assert row["elapsed"] == pytest.approx(30) and row["prior_elapsed"] == pytest.approx(6)
    assert [s for s, _ in recorded] == ["fetch", "stt"]
    assert [kw["elapsed_sec"] for _, kw in recorded] == [pytest.approx(6), pytest.approx(30)]


def test_restart_same_sub_has_no_prior_elapsed(clock: Clock, recorded: list) -> None:
    job, t = make()
    t.start("analyze")
    clock.advance(3)
    t.done("analyze")
    t.start("analyze")
    assert "prior_elapsed" not in job.timing["steps"]["analyze"]


def test_rebind_publishes_same_timing_to_new_job_and_index(clock: Clock, recorded: list) -> None:
    old, t = make()
    t.start("fetch")
    clock.advance(2)
    t.done("fetch")
    new = FakeJob()

    t.rebind(new, 3)  # type: ignore[arg-type]

    assert new.calls[-1][0] == 3
    assert new.timing["steps"]["fetch"]["state"] == "done"
    before = len(old.calls)
    t.skip("cuts")
    assert len(old.calls) == before
    assert new.timing["steps"]["cuts"]["state"] == "skipped"
