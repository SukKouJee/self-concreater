"""비밀번호 로그인 실패 횟수 제한(서버 설계 §4.2, T15). 프로세스 메모리 안(uvicorn worker 1개)이라 재시작하면 비워진다.

- `FailureWindow`: 키마다 최근 실패 시각(미끄러지는 창). 창 안에 `max_failures`번 실패했으면 막히고, `retry_after`는
  가장 오래된 실패가 창을 벗어날 때까지 남은 초(올림). 키 수는 `max_keys`로 묶는다(넘으면 지난 기록부터, 그래도 많으면 오래된 키부터 버림).
- `LoginLimiter`: 이메일별(기본 10분에 5번)·클라이언트 IP별(기본 10분에 20번: 공유기·사설망 프록시 뒤 여러 사람이 한 IP일 수 있다).
  성공하면 그 이메일의 기록만 지운다(IP 기록은 남긴다: 한 IP가 여러 계정을 돌며 맞히는 것을 늦춘다).
  이메일 키는 소문자·앞뒤 공백 제거(로그인과 같은 정규화). 모르는 이메일도 똑같이 센다(계정 유무를 드러내지 않는다).
- 진행 중인 시도도 센다(`begin` → 해시 → `end`): 동시에 여러 요청을 보내 확인과 기록 사이의 틈으로 한도를 넘지 못하게,
  `begin`은 (기록된 실패 + 진행 중) 이 한도에 닿으면 막는다(진행 중만으로 막히면 1초 뒤). `end`는 결과(failed·succeeded·aborted)를 남긴다.
  aborted(DB 오류 등)는 세지 않는다.
- 이벤트 루프에서만 부르지만 잠금으로 감싸 스레드에서 불러도 안전하다.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Final

EMAIL_FAILURES: Final = 5
IP_FAILURES: Final = 20
WINDOW_SEC: Final = 600
MAX_KEYS: Final = 10_000
MAX_EMAIL_KEY: Final = 320
OUTCOMES: Final = ("failed", "succeeded", "aborted")


class FailureWindow:
    def __init__(self, *, max_failures: int, window: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = MAX_KEYS) -> None:
        self._max = max_failures
        self._window = window
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._pending: dict[str, int] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._hits)

    def _recent(self, key: str, now: float) -> deque[float] | None:
        hits = self._hits.get(key)
        if hits is None:
            return None
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def _bound(self, now: float) -> None:
        if len(self._hits) < self._max_keys:
            return
        for key in list(self._hits):
            self._recent(key, now)
        while len(self._hits) >= self._max_keys:
            del self._hits[next(iter(self._hits))]          # dicts keep insertion order: the oldest key goes

    def fail(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            hits = self._recent(key, now)
            if hits is None:
                self._bound(now)
                hits = self._hits[key] = deque()
            else:
                self._hits[key] = self._hits.pop(key)       # move to the newest end
            hits.append(now)
            while len(hits) > self._max:
                hits.popleft()

    def retry_after(self, key: str) -> int | None:
        """막혀 있으면 남은 초(1 이상), 아니면 None. 진행 중인 시도도 실패처럼 센다(그것만으로 막히면 1초)."""
        with self._lock:
            now = self._clock()
            hits = self._recent(key, now)
            recorded = 0 if hits is None else len(hits)
            if recorded + self._pending.get(key, 0) < self._max:
                return None
            if hits is None or recorded < self._max:
                return 1
            return max(1, math.ceil(hits[0] + self._window - now))

    def reserve(self, key: str) -> None:
        with self._lock:
            self._pending[key] = self._pending.get(key, 0) + 1

    def release(self, key: str) -> None:
        with self._lock:
            left = self._pending.get(key, 0) - 1
            if left > 0:
                self._pending[key] = left
            else:
                self._pending.pop(key, None)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


def email_key(email: str) -> str:
    return email.strip().lower()[:MAX_EMAIL_KEY]


class LoginLimiter:
    def __init__(self, *, email_failures: int = EMAIL_FAILURES, ip_failures: int = IP_FAILURES,
                 window: float = WINDOW_SEC, clock: Callable[[], float] = time.monotonic) -> None:
        self._email = FailureWindow(max_failures=email_failures, window=window, clock=clock)
        self._ip = FailureWindow(max_failures=ip_failures, window=window, clock=clock)
        self._lock = threading.Lock()

    def blocked(self, email: str, ip: str) -> int | None:
        """이메일이나 IP가 막혀 있으면 기다릴 초(둘 중 긴 쪽), 아니면 None."""
        waits = [w for w in (self._email.retry_after(email_key(email)), self._ip.retry_after(ip)) if w is not None]
        return max(waits) if waits else None

    def begin(self, email: str, ip: str) -> int | None:
        """시도를 시작한다: 막혀 있으면 기다릴 초(시작하지 않음), 아니면 None(진행 중으로 세고, 반드시 `end`를 부른다)."""
        with self._lock:
            wait = self.blocked(email, ip)
            if wait is None:
                self._email.reserve(email_key(email))
                self._ip.reserve(ip)
            return wait

    def end(self, email: str, ip: str, outcome: str) -> None:
        """`begin`으로 시작한 시도를 끝낸다. outcome: failed(실패로 기록) · succeeded(이메일 기록 지움) · aborted(세지 않음)."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome!r}")
        with self._lock:
            self._email.release(email_key(email))
            self._ip.release(ip)
            if outcome == "failed":
                self.failed(email, ip)
            elif outcome == "succeeded":
                self.succeeded(email)

    def failed(self, email: str, ip: str) -> None:
        self._email.fail(email_key(email))
        self._ip.fail(ip)

    def succeeded(self, email: str) -> None:
        self._email.reset(email_key(email))
