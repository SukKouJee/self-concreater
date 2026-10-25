"""작업 항목의 줄(행)별 진행 상태를 job 항목의 `timing`에 게시한다.

화면은 `timing.steps[row]`의 state/expected/started_at/elapsed/fraction/phase/phase_text와 스냅샷의 `now`로
경과·예상·진행률을 그린다. phase_started_at/phase_fraction은 마지막 가중 구간의 남은 시간을,
prior_elapsed/prior_expected는 같은 줄의 앞 회차(자막 받기 -> 음성 인식)를 화면에 합쳐 보이는 데 쓴다. 전체 `timing` dict를 매번 새 사본으로 `job.update`에 넘겨,
스냅샷이 복사하는 동안 우리가 가진 dict가 바뀌지 않게 한다.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Protocol

from scripts import timings

MIN_INTERVAL = 0.5
# 컷 가중치: 2026-10-25 이 PC 실측(1208초 영상·508장면, 컷 320초 = 받기 약 5초 + 장면 감지 약 92초
# + 카메라 약 225초, 장면당 약 0.44초)으로 정한 기본값. 구간별 기록으로 고치는 것은 나중 일이다.
# 마지막 가중 구간(camera)은 progress-utils.js의 LAST_PHASES와 맞춘다.
PHASE_WEIGHTS: dict[str, dict[str, float]] = {
    "cuts": {"wait": 0.0, "download": 0.03, "scenes": 0.30, "camera": 0.67},
    "fetch": {"wait": 0.0, "meta": 0.0},
    "stt": {"stt": 1.0},
}


class _JobLike(Protocol):
    def update(self, index: int, **fields: Any) -> None: ...


class ItemTiming:
    def __init__(self, job: _JobLike, index: int, rows: list[str], *, engine: str | None = None) -> None:
        self._job = job
        self._index = index
        self._engine = engine
        self._video_sec: float | None = None
        self._keys: dict[str, str] = {row: row for row in rows}
        self._timing: dict[str, Any] = {
            "video_sec": None,
            "steps": {row: {"state": "pending", "expected": self._expected(row)} for row in rows},
        }
        self._current: str | None = None
        self._lock = threading.RLock()
        self._last_publish = float("-inf")
        self._publish()

    # ---- 공개 ------------------------------------------------------------------
    # 모든 공개 메서드는 _lock 아래에서 상태를 바꾸고 게시한다(락 순서: ItemTiming -> Job).

    def set_video_sec(self, sec: float | None) -> None:
        with self._lock:
            self._video_sec = sec
            self._timing["video_sec"] = sec
            for row, data in self._timing["steps"].items():
                if data["state"] in ("pending", "running"):
                    data["expected"] = self._expected(row)
            self._publish()

    def start(self, row: str, sub: str | None = None) -> None:
        with self._lock:
            if self._current is not None:
                self._timing["steps"][self._current].pop("paused_at", None)
            key = sub or row
            prev = self._timing["steps"].get(row, {})
            prior: dict[str, float] = {}
            if key != self._keys.get(row) and prev.get("state") == "done":
                # 같은 줄의 다른 기록(자막 받기 -> 음성 인식): 화면은 합쳐 보이고, 기록은 따로 남는다.
                prior = {
                    "prior_elapsed": (prev.get("prior_elapsed") or 0.0) + (prev.get("elapsed") or 0.0),
                    "prior_expected": (prev.get("prior_expected") or 0.0) + (prev.get("expected") or 0.0),
                }
            self._keys[row] = key
            self._timing["steps"][row] = {
                "state": "running",
                "expected": self._expected(row),
                "started_at": time.time(),
                "elapsed": None,
                "fraction": None,
                "phase": None,
                "phase_text": None,
                "phase_started_at": None,
                "phase_fraction": None,
                **prior,
            }
            self._current = row
            self._publish()

    def phase(self, name: str, fraction: float | None, text: str) -> None:
        with self._lock:
            row = self._current
            if row is None:
                return
            data = self._timing["steps"][row]
            changed = data.get("phase") != name
            weights = PHASE_WEIGHTS.get(self._keys[row], {})
            weight = weights.get(name, 0.0)
            if weight > 0 and fraction is not None:
                before = 0.0
                for key, w in weights.items():
                    if key == name:
                        break
                    before += w
                value = min(1.0, before + weight * min(1.0, max(0.0, fraction)))
                data["fraction"] = max(data["fraction"] or 0.0, value)
            if changed:
                # 멈춘 동안 바뀌면 멈춘 시각에서 시작한 것으로 둔다(재개 때 started_at처럼 밀린다).
                data["phase_started_at"] = data.get("paused_at") or time.time()
                data["phase_fraction"] = None
            if fraction is not None:
                data["phase_fraction"] = min(1.0, max(0.0, fraction))
            data["phase"] = name
            data["phase_text"] = text
            if changed or time.monotonic() - self._last_publish >= MIN_INTERVAL:
                self._publish()

    def pause(self, *, publish: bool = True) -> None:
        """지금 줄의 시계를 멈춘다. publish=False면 바로 이어질 게시(phase 등)에 맡긴다."""
        with self._lock:
            if self._current is None:
                return
            data = self._timing["steps"][self._current]
            if "paused_at" in data:
                return
            data["paused_at"] = time.time()
            if publish:
                self._publish()

    def resume(self, *, publish: bool = True) -> None:
        with self._lock:
            if self._current is None:
                return
            data = self._timing["steps"][self._current]
            paused_at = data.pop("paused_at", None)
            if paused_at is None:
                return
            shift = time.time() - paused_at
            data["started_at"] += shift
            if data.get("phase_started_at") is not None:
                data["phase_started_at"] += shift
            if publish:
                self._publish()

    def done(self, row: str, *, cached: bool = False, **record_kw: Any) -> None:
        with self._lock:
            data = self._timing["steps"][row]
            elapsed = 0.0 if cached else self._elapsed(data)
            data.update(state="done", elapsed=elapsed, fraction=1.0)
            if cached:
                data["expected"] = 0.0
            data.pop("paused_at", None)
            if self._current == row:
                self._current = None
            if not cached:
                timings.record(
                    self._keys[row],
                    elapsed_sec=elapsed,
                    video_sec=self._video_sec,
                    engine=self._engine,
                    **record_kw,
                )
            self._publish()

    def skip(self, row: str) -> None:
        """오류가 아니라 하지 않은 줄(STT 없음·프레임 없음·항목이 먼저 끝남). 기록하지 않고 예상을 0으로 둔다."""
        with self._lock:
            data = self._timing["steps"][row]
            data["state"] = "skipped"
            data["expected"] = 0.0
            data.pop("paused_at", None)
            if self._current == row:
                self._current = None
            self._publish()

    def fail(self, row: str) -> None:
        with self._lock:
            data = self._timing["steps"][row]
            data["state"] = "failed"
            data.pop("paused_at", None)
            if self._current == row:
                self._current = None
            self._publish()

    # ---- 내부 ------------------------------------------------------------------

    def _expected(self, row: str) -> float | None:
        return timings.expected(self._keys[row], video_sec=self._video_sec, engine=self._engine)

    @staticmethod
    def _elapsed(data: dict[str, Any]) -> float:
        started = data.get("started_at")
        if started is None:
            return 0.0
        end = data.get("paused_at") or time.time()
        return max(0.0, end - started)

    def _publish(self) -> None:
        with self._lock:
            self._last_publish = time.monotonic()
            self._job.update(self._index, timing=copy.deepcopy(self._timing))
