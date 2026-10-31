"""사용자별 작업 관리자(서버 설계 §5.1, ADR-S6): `Registry.for_user(uid)`가 그 사용자의 `JobManager`를 준다.

- 앱 프로세스 하나(uvicorn `--workers 1`) 안에서 사용자마다 JobManager 하나. `SINGLE_KINDS`·`EXCLUSIVE`는 사용자별로 적용되고,
  남의 job_id는 그 사용자의 매니저에 없으므로 `job_not_found`다(격리).
- 종료: `close()` 뒤에는 새 매니저를 만들지 않고(`closing`) 어댑터가 상태를 바꾸는 요청을 503 `restarting`으로 거절한다.
  `shutdown(timeout)`은 모든 사용자의 작업에 먼저 취소를 알리고 마감 하나(timeout)까지만 기다린다.
- 전역 자원(T11, 설계 §5.2): `install_runtime(llm_settings, db)`가 시작 때 한 번 media·llm 세마포어(`YTC_MEDIA_SLOTS`·`YTC_LLM_SLOTS`),
  같은 영상 수집 잠금(`Db.collect_lock`, PG advisory lock), LLM 고정(`llm.pin`: 엔진·모델·호출당 max_tokens 상한)을 꽂는다.
  YouTube 간격은 `fetch.wait_turn`의 프로세스 잠금 + `.last_fetch`(서버 상태 폴더)라 따로 설치할 것이 없다(프로세스 하나 = 서버 전체 1개).
  `uninstall_runtime()`은 로컬판 기본(no-op)으로 되돌린다.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable

from scripts import limits, llm
from scripts.common import ScriptError
from scripts.ui.jobs import JobManager, join_until
from server.config import LlmSettings
from server.db import Db


def install_runtime(settings: LlmSettings, db: Db) -> None:
    """프로세스 전역 자원 제한과 LLM 정책을 설치한다(서버 시작 때 한 번)."""
    llm.pin(engine=settings.engine, model=settings.model, max_tokens=settings.max_tokens)
    limits.install(media=threading.Semaphore(settings.media_slots), llm=threading.Semaphore(settings.llm_slots),
                   collect=db.collect_lock)


def uninstall_runtime() -> None:
    limits.install()
    llm.unpin()


class Registry:
    def __init__(self, factory: Callable[[], JobManager] = JobManager) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._managers: dict[uuid.UUID, JobManager] = {}
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def for_user(self, user_id: uuid.UUID | str) -> JobManager:
        """그 사용자의 JobManager(없으면 만든다). user_id는 인증된 값만 넘긴다(UUID가 아니면 ValueError)."""
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
        with self._lock:
            manager = self._managers.get(uid)
            if manager is None:
                if self._closing:
                    raise ScriptError("restarting", "서버가 다시 시작하는 중입니다. 잠시 뒤 새로고침하세요")
                manager = self._factory()
                self._managers[uid] = manager
            return manager

    def close(self) -> None:
        """새 작업을 받지 않는다(이미 있는 매니저는 조회·취소용으로 남는다)."""
        with self._lock:
            self._closing = True

    def shutdown(self, timeout: float | None = None) -> None:
        """close() 뒤 모든 사용자의 모든 작업에 먼저 취소를 알리고, timeout초 뒤의 마감 하나까지만 기다린다.
        사용자·작업 수와 상관없이 전체 대기가 timeout을 넘지 않는다(compose 종료 유예 안에 끝나게)."""
        self.close()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            managers = list(self._managers.values())
        jobs = [job for manager in managers for job in manager.cancel_all()]
        join_until(jobs, deadline)
