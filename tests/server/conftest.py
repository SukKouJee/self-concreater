"""서버판 테스트: fixture는 `tests/helpers/pg.py`(루트 conftest가 등록). 여기서는 기존 import 경로를 이어 주고,
ASGI 앱 fixture(`asgi_stack`: 실제 PG+MinIO 위의 서버 앱 + dev 로그인한 사용자별 TestClient)를 둔다."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.helpers.pg import ALEMBIC_INI, COMPOSE_FILE, PLUGIN_ROOT, Stack, TestDb, pg_user, run_alembic

__all__ = ["ALEMBIC_INI", "COMPOSE_FILE", "PLUGIN_ROOT", "Stack", "TestDb", "run_alembic"]

ORIGIN = "https://testserver"


@dataclass
class AsgiStack:
    """서버 앱 하나와 그 위의 사용자들. `login(email)` = 새 사용자 행 + dev 로그인한 TestClient(쿠키 따로)."""

    app: Any
    backend: Any
    owner_dsn: str
    users: dict[str, Any] = field(default_factory=dict)       # email -> uuid

    def client(self) -> Any:
        from starlette.testclient import TestClient

        # no `with`: lifespan (registry shutdown) is driven by the fixture, so several clients share one app
        return TestClient(self.app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def login(self, email: str) -> Any:
        if email not in self.users:
            self.users[email] = pg_user(self.owner_dsn, email)
        c = self.client()
        r = c.post("/auth/dev", json={"email": email})
        assert r.status_code == 200, r.text
        return c

    def owner_sql(self, sql: str, *params: Any) -> None:
        import psycopg

        with psycopg.connect(self.owner_dsn) as conn:
            conn.execute(sql, params)

    @property
    def registry(self) -> Any:
        return self.app.state.handler.registry


@pytest.fixture
def asgi_stack(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[AsgiStack]:
    """실제 PG 16 + MinIO 위의 서버 앱(dev 로그인, https://testserver). 환경이 없으면 skip."""
    pytest.importorskip("starlette")
    backend = request.getfixturevalue("pg_backend")
    migrated = request.getfixturevalue("migrated_db")
    from server.asgi import create_app
    from server.config import AuthSettings

    monkeypatch.delenv("YTC_DISABLE", raising=False)
    settings = AuthSettings.from_env({"YTC_PUBLIC_URL": ORIGIN, "YTC_SESSION_SECRET": "x" * 48,
                                      "YTC_ENV": "test", "YTC_DEV_LOGIN": "1"})
    stack = AsgiStack(create_app(settings, backend), backend, migrated.owner_dsn)
    try:
        yield stack
    finally:
        stack.registry.shutdown(timeout=10)
