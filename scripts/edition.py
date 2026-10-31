"""판(edition) 문맥 하나: 로컬판(`ytc ui`·플러그인)과 서버판(server/asgi.py)이 같은 코드를 돌며 문구만 고른다.

- 문맥은 `ContextVar` 하나(판 이름, 항상 끄는 기능). 기본은 로컬판. 서버판 어댑터가 요청마다 `use("server", …)`로
  바꾸고(`scripts.ui.server.edition`이 기능 이름을 검사한 뒤 이것을 부른다), 작업 스레드는 `copy_context()`로 이어받는다.
- `ed(local, server)`: 사용자에게 보일 문구를 판에 맞게 고른다. 로컬판은 언제나 `local` 그대로다(문구를 바꾸지 않는다).
  서버판 문구에는 작업 폴더·터미널·명령어·슬래시 커맨드 같은 내 PC 전용 말을 쓰지 않는다(tests/test_edition_copy.py가 검사).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

LOCAL = "local"
SERVER = "server"

_EDITION: ContextVar[tuple[str, frozenset[str]]] = ContextVar("ytc_edition", default=(LOCAL, frozenset()))


@contextmanager
def use(name: str, disabled: frozenset[str] = frozenset()) -> Iterator[None]:
    """블록 동안(이 스레드 문맥에서) 판 이름을 name으로, disabled를 항상 끄는 기능으로 둔다."""
    token = _EDITION.set((name, frozenset(disabled)))
    try:
        yield
    finally:
        _EDITION.reset(token)


def current() -> str:
    return _EDITION.get()[0]


def forced() -> frozenset[str]:
    """판 문맥이 항상 끄는 기능 이름."""
    return _EDITION.get()[1]


def is_server() -> bool:
    return current() == SERVER


def ed(local: str, server: str) -> str:
    """판에 맞는 문구: 서버판이면 server, 그 밖(로컬판)은 local 그대로."""
    return server if is_server() else local
