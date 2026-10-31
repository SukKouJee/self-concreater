"""저장소 계약 테스트 fixture(서버 설계 §10-1): 같은 단언을 FileStore(tmp_path)와 서버 저장소(PG 16 + MinIO)에서 돌린다.

`pg`·`s3` 매개변수는 PG·MinIO 환경(`YTC_TEST_DOCKER=1` 또는 `YTC_TEST_DB_DSN`, `tests/helpers/pg.py`)이 없으면 skip.
- `doc_store`: FileDocStore · PgDocStore
- `object_store`(+`object_store_pair`: 두 사용자): FileObjectStore · S3ObjectStore
- `shared_cache`(+`shared_cache_pair`: 두 사용자가 같은 캐시를 본다): FileSharedCache · S3SharedCache
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.filestore import FileDocStore, FileObjectStore, FileSharedCache
from scripts.stores import DocStore, ObjectStore, SharedCache
from tests.helpers.pg import pg_doc_store, pg_object_store, pg_shared_cache

BACKENDS = ["file", "pg"]
OBJECT_BACKENDS = ["file", "s3"]


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(params=OBJECT_BACKENDS)
def obj_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


@pytest.fixture
def doc_store(backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> DocStore:
    if backend == "file":
        return FileDocStore(_root(tmp_path, "work"))
    store: DocStore = pg_doc_store(request)
    return store


@pytest.fixture
def object_store(obj_backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStore:
    if obj_backend == "file":
        return FileObjectStore(_root(tmp_path, "work"))
    store: ObjectStore = pg_object_store(request)
    return store


@pytest.fixture
def object_store_pair(obj_backend: str, request: pytest.FixtureRequest,
                      tmp_path: Path) -> tuple[ObjectStore, ObjectStore]:
    """사용자 A·B의 객체 저장소(File = 작업 폴더 둘)."""
    if obj_backend == "file":
        return FileObjectStore(_root(tmp_path, "a")), FileObjectStore(_root(tmp_path, "b"))
    return pg_object_store(request, "a@example.com"), pg_object_store(request, "b@example.com")


@pytest.fixture
def shared_cache(obj_backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> SharedCache:
    if obj_backend == "file":
        return FileSharedCache(_root(tmp_path, "work"))
    cache: SharedCache = pg_shared_cache(request)
    return cache


@pytest.fixture
def shared_cache_pair(obj_backend: str, request: pytest.FixtureRequest,
                      tmp_path: Path) -> tuple[SharedCache, SharedCache]:
    """사용자 A·B가 쓰는 공유 캐시(File = 같은 작업 폴더 하나, 로컬판은 사용자가 하나)."""
    if obj_backend == "file":
        root = _root(tmp_path, "work")
        return FileSharedCache(root), FileSharedCache(root)
    return pg_shared_cache(request, "a@example.com"), pg_shared_cache(request, "b@example.com")
