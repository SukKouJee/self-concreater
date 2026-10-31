"""사용자별 작업 관리자(서버 설계 §5.1, ADR-S6): `Registry.for_user(uid)`가 그 사용자의 `JobManager`를 준다.

- 앱 프로세스 하나(uvicorn `--workers 1`) 안에서 사용자마다 JobManager 하나. `SINGLE_KINDS`·`EXCLUSIVE`는 사용자별로 적용되고,
  남의 job_id는 그 사용자의 매니저에 없으므로 `job_not_found`다(격리).
- 종료: `close()` 뒤에는 새 매니저를 만들지 않고(`closing`) 어댑터가 상태를 바꾸는 요청을 503 `restarting`으로 거절한다.
  `shutdown(timeout)`은 모든 사용자의 작업을 멈추고 기다린다.
- 전역 자원 슬롯(media·llm) 설치는 T11.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable

from scripts.common import ScriptError
from scripts.ui.jobs import JobManager


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
        """close() 뒤 모든 사용자의 작업을 멈추고 각 작업 스레드를 timeout초까지 기다린다."""
        self.close()
        with self._lock:
            managers = list(self._managers.values())
        for manager in managers:
            manager.shutdown(timeout)
