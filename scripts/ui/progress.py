"""작업 항목의 줄(행)별 진행 상태를 job 항목의 `timing`에 게시한다.

화면은 `timing.steps[row]`의 state/expected/started_at/elapsed/fraction/phase/phase_text와 스냅샷의 `now`로
경과·예상·진행률을 그린다. 전체 `timing` dict를 매번 새 사본으로 `job.update`에 넘겨,
스냅샷이 복사하는 동안 우리가 가진 dict가 바뀌지 않게 한다.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Protocol

from scripts import timings

MIN_INTERVAL = 0.5
PHASE_WEIGHTS: dict[str, dict[str, float]] = {
    "cuts": {"wait": 0.0, "download": 0.2, "scenes": 0.6, "camera": 0.2},
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
            self._keys[row] = sub or row
            self._timing["steps"][row] = {
                "state": "running",
                "expected": self._expected(row),
                "started_at": time.time(),
                "elapsed": None,
                "fraction": None,
                "phase": None,
                "phase_text": None,
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
            data["phase"] = name
            data["phase_text"] = text
            if changed or time.monotonic() - self._last_publish >= MIN_INTERVAL:
                self._publish()

    def pause(self) -> None:
        with self._lock:
            if self._current is None:
                return
            data = self._timing["steps"][self._current]
            if "paused_at" in data:
                return
            data["paused_at"] = time.time()
            self._publish()

    def resume(self) -> None:
        with self._lock:
            if self._current is None:
                return
            data = self._timing["steps"][self._current]
            paused_at = data.pop("paused_at", None)
            if paused_at is None:
                return
            data["started_at"] += time.time() - paused_at
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
