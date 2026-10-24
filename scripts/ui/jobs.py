"""UI 백그라운드 작업(job): 스레드 하나에서 영상을 차례로 처리하고, 확인이 필요하면 결정을 기다린다.

상태
- job.status: running → (awaiting ↔ running)* → done | stopped | failed
  - awaiting: 어떤 항목이 사용자 결정을 기다리는 중
  - stopped: 차단으로 남은 항목을 건너뛰었거나 `cancel()`/`shutdown()`으로 멈춤
  - failed: 작업 함수가 예상치 못한 오류로 끝남(진행 중 항목은 failed, 남은 항목은 skipped)
- item.state: waiting → running → (awaiting → running)* → saved | done | skipped | failed (done: 확인이 필요 없는 작업의 정상 끝, 예: srtfit)

동시성
- 상태는 모두 `Job._lock` 아래에서 바뀐다. 바깥에는 `snapshot()`(깊은 복사)만 내준다.
- 결정 대기: 작업 스레드가 `update(i, state="awaiting")` 뒤 `wait_decision(item_id)`로 Event를 기다리고,
  HTTP 스레드가 `decide(item_id, decision)`로 결정을 넣고 깨운다. Event는 먼저 오는 쪽이 만든다(경합 없음).
- 같은 kind 중 `SINGLE_KINDS`(collect, typify, motif, draft, polish, srtfit, diagnose)는 진행 중(running/awaiting)인 작업이 하나뿐이다.
"""
from __future__ import annotations

import copy
import secrets
import sys
import threading
import traceback
from collections.abc import Iterable, Mapping
from typing import Any, Callable

from scripts.common import ScriptError

ACTIVE = frozenset({"running", "awaiting"})
ITEM_DONE = frozenset({"saved", "skipped", "failed"})
SINGLE_KINDS = {
    "collect": "이미 수집 중인 작업이 있습니다",
    "typify": "이미 유형화가 진행 중입니다",
    "motif": "이미 카드를 뽑는 중입니다",
    "draft": "이미 기획안을 만드는 중입니다",
    "polish": "이미 다듬는 중입니다",
    "srtfit": "이미 자막 시간을 맞추는 중입니다",
    "diagnose": "이미 진단 설명을 받는 중입니다",
}
STOP_MESSAGE = "중단됨"


class JobCancelled(Exception):
    """cancel()/shutdown()으로 작업을 멈출 때 작업 스레드 안에서 올라온다."""


def new_item(item_id: str, url: str | None = None, title: str | None = None, step: str = "fetch") -> dict:
    return {"id": item_id, "url": url, "title": title, "step": step, "state": "waiting",
            "message": None, "preview": None, "ask": None, "frames": []}


class Job:
    def __init__(self, kind: str, items: list[dict] | None = None, category: str | None = None) -> None:
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.category = category
        self._lock = threading.Lock()
        self._items: list[dict] = [dict(i) for i in items or []]
        self._status = "running"
        self._summary: dict | None = None
        self._message: str | None = None
        self._waits: dict[str, threading.Event] = {}
        self._decisions: dict[str, dict] = {}
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 읽기 -------------------------------------------------------------------

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def items(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._items)

    @property
    def summary(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._summary)

    def item(self, index: int) -> dict:
        with self._lock:
            return copy.deepcopy(self._items[index])

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "id": self.id, "kind": self.kind, "status": self._status, "category": self.category,
                "items": self._items, "summary": self._summary, "message": self._message,
            })

    def is_active(self) -> bool:
        return self.status in ACTIVE

    # ---- 작업 스레드 쪽 ------------------------------------------------------------

    def update(self, index: int, **fields: Any) -> None:
        with self._lock:
            self._items[index].update(fields)
            if fields.get("state") == "awaiting" and self._status in ACTIVE:
                self._status = "awaiting"

    def skip_rest(self, start: int, message: str) -> None:
        with self._lock:
            for item in self._items[start:]:
                if item["state"] not in ITEM_DONE:
                    item.update(state="skipped", message=message)

    def finish(self, status: str, summary: dict | None = None, message: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._summary = summary
            self._message = message

    def check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise JobCancelled

    def wait_decision(self, item_id: str) -> dict:
        """item_id에 결정이 들어올 때까지 막는다. 취소되면 JobCancelled."""
        with self._lock:
            event = self._waits.setdefault(item_id, threading.Event())
            # cancel()이 이 Event를 만들기 전에 지나갔으면 깨워 줄 쪽이 없으므로 여기서 확인한다
            if self._cancelled.is_set() and item_id not in self._decisions:
                self._waits.pop(item_id, None)
                raise JobCancelled
        event.wait()
        with self._lock:
            self._waits.pop(item_id, None)
            decision = self._decisions.pop(item_id, None)
            if decision is None:
                raise JobCancelled
            if self._status == "awaiting":
                self._status = "running"
            return decision

    # ---- HTTP 스레드 쪽 ------------------------------------------------------------

    def find(self, item_id: str) -> dict:
        """결정 대기 중인 항목을 먼저, 없으면 첫 항목을 돌려준다(복사본)."""
        with self._lock:
            return copy.deepcopy(self._items[self._index_of(item_id)])

    def _index_of(self, item_id: str) -> int:
        matches = [i for i, item in enumerate(self._items) if item["id"] == item_id]
        if not matches:
            raise ScriptError("item_not_found", f"작업에 없는 항목입니다: {item_id}")
        awaiting = [i for i in matches if self._items[i]["state"] == "awaiting"]
        return (awaiting or matches)[0]

    def decide(self, item_id: str, decision: dict, allowed: Mapping[str | None, Iterable[str]] | None = None) -> None:
        """결정을 넣고 대기 중인 작업 스레드를 깨운다.

        allowed: 항목의 `ask` 값 → 허용 action 목록. 주면 lock 안에서 확인하고, 밖이면 `bad_action`.
        """
        with self._lock:
            index = self._index_of(item_id)
            item = self._items[index]
            if item["state"] != "awaiting" or item_id in self._decisions:
                raise ScriptError("not_awaiting", "이 항목은 결정을 기다리고 있지 않습니다")
            if allowed is not None:
                choices = sorted(allowed.get(item.get("ask"), ()))
                if decision.get("action") not in choices:
                    raise ScriptError(
                        "bad_action",
                        f"이 항목에서 할 수 없는 결정입니다: {decision.get('action')!r} (가능: {', '.join(choices)})",
                    )
            item["state"] = "running"
            if self._status == "awaiting":
                self._status = "running"
            self._decisions[item_id] = dict(decision)
            self._waits.setdefault(item_id, threading.Event()).set()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            for event in self._waits.values():
                event.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- 끝 처리 -------------------------------------------------------------------

    def _close(self, status: str, message: str, item_message: str) -> None:
        with self._lock:
            self._status = status
            self._message = message
            for item in self._items:
                if item["state"] in ITEM_DONE:
                    continue
                if status == "failed" and item["state"] in ACTIVE:
                    item.update(state="failed", message=message)
                else:
                    item.update(state="skipped", message=item_message)

    def _run(self, target: Callable[[Job], None]) -> None:
        try:
            target(self)
        except JobCancelled:
            self._close("stopped", STOP_MESSAGE, STOP_MESSAGE)
        except ScriptError as e:
            self._close("failed", e.message, "오류로 중단")
        except Exception as e:  # noqa: BLE001 - 작업 스레드가 조용히 죽지 않도록 failed로 남긴다
            traceback.print_exc(file=sys.stderr)
            self._close("failed", f"{type(e).__name__}: {e}", "오류로 중단")
        else:
            with self._lock:
                if self._status in ACTIVE:
                    self._status = "done"


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._latest: dict[str, Job] = {}

    def start(
        self,
        kind: str,
        target: Callable[[Job], None],
        *,
        items: list[dict] | None = None,
        category: str | None = None,
    ) -> Job:
        with self._lock:
            current = self._latest.get(kind)
            if kind in SINGLE_KINDS and current is not None and current.is_active():
                raise ScriptError("job_running", SINGLE_KINDS[kind])
            job = Job(kind, items, category)
            self._jobs[job.id] = job
            self._latest[kind] = job
            job._thread = threading.Thread(target=job._run, args=(target,), name=f"job-{kind}-{job.id}",
                                           daemon=True)
            job._thread.start()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ScriptError("job_not_found", f"작업을 찾을 수 없습니다: {job_id}")
        return job

    def current(self, kind: str) -> Job | None:
        """그 kind의 가장 최근 작업(끝난 작업 포함). 브라우저를 다시 열어도 결과·확인 카드가 보이게."""
        with self._lock:
            return self._latest.get(kind)

    def shutdown(self, timeout: float | None = None) -> None:
        """진행 중인 작업을 모두 멈추고 스레드가 끝나기를 기다린다."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        for job in jobs:
            job.join(timeout)
