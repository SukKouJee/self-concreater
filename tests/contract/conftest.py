"""저장소 계약 테스트 fixture(서버 설계 §10-1): 같은 단언을 FileStore(tmp_path)와 PgStore(PG 16)에서 돌린다.

`pg` 매개변수는 PG 환경(`YTC_TEST_DOCKER=1` 또는 `YTC_TEST_DB_DSN`, `tests/helpers/pg.py`)이 없으면 skip.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.filestore import FileDocStore
from scripts.stores import DocStore
from tests.helpers.pg import pg_doc_store

BACKENDS = ["file", "pg"]


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def doc_store(backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> DocStore:
    if backend == "file":
        root = tmp_path / "work"
        root.mkdir()
        return FileDocStore(root)
    store: DocStore = pg_doc_store(request)
    return store
