"""UI 백그라운드 작업(job): 스레드 하나에서 영상을 차례로 처리하고, 확인이 필요하면 결정을 기다린다.

상태
- job.status: running → (awaiting ↔ running)* → done | stopped | failed
  - awaiting: 어떤 항목이 사용자 결정을 기다리는 중
  - stopped: 차단으로 남은 항목을 건너뛰었거나 `cancel()`/`shutdown()`으로 멈춤
  - failed: 작업 함수가 예상치 못한 오류로 끝남(진행 중 항목은 failed, 남은 항목은 skipped)
- item.state: waiting → running → (awaiting → running)* → saved | done | skipped | failed (done: 확인이 필요 없는 작업의 정상 끝, 예: srtfit, caption_video)

동시성
- 상태는 모두 `Job._lock` 아래에서 바뀐다. 바깥에는 `snapshot()`(깊은 복사)만 내준다.
- 결정 대기: 작업 스레드가 `update(i, state="awaiting")` 뒤 `wait_decision(item_id)`로 Event를 기다리고,
  HTTP 스레드가 `decide(item_id, decision)`로 결정을 넣고 깨운다. Event는 먼저 오는 쪽이 만든다(경합 없음).
- collect는 "작업 줄" 모델이다: 워커 스레드 하나가 `next_task()`로 줄에서 할 일을 꺼내 처리하고, 새 링크·저장 요청은
  HTTP 스레드가 `append_queued()`/`push_task()`로 붙인다. 줄 대기는 `_lock`에 묶인 Condition이라 상태 확인과 대기가 원자적이고,
  `cancel()`이 기다리는 `next_task()`도 깨운다(JobCancelled).
  저장 확인을 기다리는 항목(`state=="awaiting"`, `ask=="review"`)은 분석 워커가 멈춰도 그대로 남고(`_close`가 건드리지 않음),
  `take_review()`가 잠금 안에서 `running`으로 바꾸는 쪽만 저장을 진행한다(저장 버튼 중복 클릭 방지).
  줄은 열림/닫힘이 있다: `append_queued()`/`requeue()`는 줄이 열려 있고 작업이 진행 중일 때만 받는다(멈춘 작업에 넣은 일이
  조용히 사라지지 않게). `close_if_idle()`은 줄이 비고 모든 항목이 끝났을 때만 done으로 닫고, `close_queue()`는 멈출 때
  남은 할 일을 돌려준다. `sync_status()`는 확인 대기 유무로 running/awaiting을 올리고 내린다.
  `release_reviews()`는 멈춘 작업의 확인 대기를 새 작업으로 옮길 때 쓴다.
- 같은 kind 중 `SINGLE_KINDS`(collect, typify, motif, draft, polish, srtfit, caption_video, diagnose, mission, mission_feedback, review, library_summary, help_chat)는 진행 중(running/awaiting)인 작업이 하나뿐이다.
"""
from __future__ import annotations

import copy
import secrets
import sys
import threading
import time
import traceback
from collections import deque
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
    "caption_video": "이미 자막 영상을 만드는 중입니다",
    "diagnose": "이미 진단 설명을 받는 중입니다",
    "mission": "이미 미션을 만드는 중입니다",
    "mission_feedback": "이미 피드백을 받는 중입니다",
    "review": "이미 회고를 쓰는 중입니다",
    "library_summary": "이미 섹션 요약을 받는 중입니다",
    "help_chat": "이미 답을 만드는 중입니다",
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
        self._tasks_cond = threading.Condition(self._lock)
        self._tasks: deque[dict] = deque()
        self._items: list[dict] = [dict(i) for i in items or []]
        self._status = "running"
        self._summary: dict | None = None
        self._message: str | None = None
        self._waits: dict[str, threading.Event] = {}
        self._decisions: dict[str, dict] = {}
        self._cancelled = threading.Event()
        self._queue_open = True
        self._thread: threading.Thread | None = None
        self.target: Callable[[Job], None] | None = None   # JobManager.start가 넣는다(작업 함수 객체)

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
                "items": self._items, "summary": self._summary, "message": self._message, "now": time.time(),
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

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise JobCancelled

    def push_task(self, task: dict) -> None:
        """워커 줄 끝에 할 일을 넣고 기다리는 `next_task()`를 깨운다."""
        with self._tasks_cond:
            self._tasks.append(dict(task))
            self._tasks_cond.notify()

    def next_task(self, timeout: float | None) -> dict | None:
        """줄 맨 앞 할 일을 꺼낸다. timeout=0이면 안 기다리고, None이면 할 일이나 취소가 올 때까지 기다린다.

        시간 안에 없으면 None. 취소됐으면 JobCancelled.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._tasks_cond:
            while True:
                if self._cancelled.is_set():
                    raise JobCancelled
                if self._tasks:
                    return self._tasks.popleft()
                if deadline is None:
                    self._tasks_cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._tasks_cond.wait(remaining)

    def _can_queue(self) -> bool:
        """잠금 안에서 부른다: 워커가 아직 줄을 받을 수 있으면 True."""
        return self._queue_open and not self._cancelled.is_set() and self._status in ACTIVE

    def append_queued(
        self, items: list[dict], kind: str, same: Callable[[dict, dict], bool],
    ) -> list[int] | None:
        """줄을 받을 수 있으면 항목을 끝에 붙이고 항목마다 `{"kind": kind, "index": i}`를 줄에 넣는다.

        아직 끝나지 않은 항목이나 앞서 붙인 새 항목과 `same(old, new)`인 항목은 붙이지 않는다.
        줄이 닫혔으면(끝남·멈춤·취소) 아무것도 바꾸지 않고 None.
        """
        with self._tasks_cond:
            if not self._can_queue():
                return None
            kept: list[dict] = []
            pending = [i for i in self._items if i["state"] not in ITEM_DONE]
            for new in items:
                if any(same(old, new) for old in [*pending, *kept]):
                    continue
                kept.append(dict(new))
            start = len(self._items)
            self._items.extend(kept)
            indexes = list(range(start, len(self._items)))
            for index in indexes:
                self._tasks.append({"kind": kind, "index": index})
            if indexes:
                self._tasks_cond.notify()
            return indexes

    def requeue(self, index: int, task: dict, **fields: Any) -> bool:
        """줄을 받을 수 있으면 항목을 fields로 바꾸고 task를 줄에 넣는다(한 잠금 안에서). 아니면 그대로 두고 False."""
        with self._tasks_cond:
            if not self._can_queue():
                return False
            self._items[index].update(fields)
            self._tasks.append(dict(task))
            self._tasks_cond.notify()
            return True

    def close_queue(self) -> list[dict]:
        """줄을 닫고(이후 requeue·append_queued는 거절) 남은 할 일을 꺼내 돌려준다. status는 바꾸지 않는다."""
        with self._tasks_cond:
            self._queue_open = False
            leftovers = list(self._tasks)
            self._tasks.clear()
            return leftovers

    def close_if_idle(self, summarize: Callable[[list[dict]], dict]) -> bool:
        """줄이 비고 모든 항목이 끝났으면(확인 대기·저장 중 없음) 줄을 닫고 done으로 끝낸다.

        summarize는 잠금 안에서 항목 목록(사본)으로 요약을 만든다(이 Job의 메서드를 부르면 안 된다).
        """
        with self._tasks_cond:
            if not self._can_queue() or self._tasks:
                return False
            if any(i["state"] not in ITEM_DONE for i in self._items):
                return False
            self._queue_open = False
            self._status = "done"
            self._summary = summarize(copy.deepcopy(self._items))
            return True

    def sync_status(self) -> None:
        """진행 중이면 확인 대기 항목이 있을 때 awaiting, 없으면 running으로 맞춘다(올리고 내린다)."""
        with self._lock:
            if self._status in ACTIVE:
                waiting = any(i["state"] == "awaiting" for i in self._items)
                self._status = "awaiting" if waiting else "running"

    def release_reviews(self, message: str) -> list[tuple[int, dict]]:
        """확인 대기(`ask=="review"`) 항목을 넘겨준다: 사본(그대로 awaiting)을 돌려주고 여기서는 skipped/message로 둔다."""
        with self._lock:
            released: list[tuple[int, dict]] = []
            for index, item in enumerate(self._items):
                if item["state"] == "awaiting" and item.get("ask") == "review":
                    released.append((index, copy.deepcopy(item)))
                    item.update(state="skipped", message=message)
            return released

    def set_summary(self, summary: dict | None) -> None:
        with self._lock:
            self._summary = summary

    def review_done(self) -> bool:
        """확인 대기(`ask=="review"`) 항목이 하나도 남지 않았으면 True."""
        with self._lock:
            return not any(i["state"] == "awaiting" and i.get("ask") == "review" for i in self._items)

    def take_review(self, item_id: str) -> int:
        """확인 대기 항목을 잠금 안에서 `running`/`confirm`으로 바꾸고 인덱스를 돌려준다. 아니면 not_awaiting."""
        with self._lock:
            index = self._index_of(item_id)
            item = self._items[index]
            if item["state"] != "awaiting" or item.get("ask") != "review":
                raise ScriptError("not_awaiting", "이 항목은 확인을 기다리고 있지 않습니다")
            item.update(state="running", step="confirm")
            return index

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
            self._tasks_cond.notify_all()

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
                if item["state"] == "awaiting" and item.get("ask") == "review":
                    continue  # 저장 확인 대기는 작업이 멈춰도 사용자가 이어서 처리한다
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


def mark_saved(app: Any, job_id: Any, kind: str, item_id: str) -> None:
    """미리보기 항목(id == item_id)이 아직 저장 대기면 저장됨으로 바꾼다. 없는 작업·다른 종류·다른 항목은 무시."""
    if not isinstance(job_id, str):
        return
    try:
        job = app.jobs.get(job_id)
    except ScriptError:
        return
    if job.kind != kind:
        return
    item = job.item(0)
    if item["id"] == item_id and item["state"] == "awaiting":
        job.update(0, state="saved", ask=None)


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
        tasks: list[dict] | None = None,
    ) -> Job:
        """tasks: 작업 스레드가 돌기 전에 줄에 넣어 둘 할 일(collect 작업 줄 모델)."""
        with self._lock:
            current = self._latest.get(kind)
            if kind in SINGLE_KINDS and current is not None and current.is_active():
                raise ScriptError("job_running", SINGLE_KINDS[kind])
            job = Job(kind, items, category)
            job.target = target
            for task in tasks or []:
                job.push_task(task)
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

    def latest_where(self, kind: str, pred: Callable[[Job], bool]) -> Job | None:
        """그 kind 중 pred를 만족하는 가장 최근 작업(최신부터 본다). pred는 매니저 잠금 아래에서 불린다."""
        with self._lock:
            for job in reversed(list(self._jobs.values())):
                if job.kind == kind and pred(job):
                    return job
        return None

    def active(self, kind: str) -> list[Job]:
        """그 kind의 아직 도는 작업 전부(콘티 만들기처럼 동시에 여러 개가 돌 수 있는 kind도 빠짐없이)."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.kind == kind]
        return [j for j in jobs if j.is_active()]

    def shutdown(self, timeout: float | None = None) -> None:
        """진행 중인 작업을 모두 멈추고 스레드가 끝나기를 기다린다."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        for job in jobs:
            job.join(timeout)
