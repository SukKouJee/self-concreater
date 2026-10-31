"""서버판 테스트 공용 fixture: PostgreSQL 16 + MinIO(서버 설계 §10).

`tests/conftest.py`가 `pytest_plugins`로 한 번만 등록한다(세션 fixture `pg_stack`이 tests/server·tests/contract에서
같은 compose 스택 하나를 쓰게). 다른 conftest에서 fixture 함수를 다시 import하지 않는다(스택이 둘 뜬다).

환경 고르기(먼저 맞는 것):
1. `YTC_TEST_DB_DSN`(테스트 클러스터의 owner DSN, 슈퍼유저) — 이미 띄운 PG를 쓴다. MinIO는 `YTC_TEST_S3_ENDPOINT`.
2. `YTC_TEST_DOCKER=1` — `deploy/server/docker-compose.test.yml`을 프로젝트 이름
   `YTC_TEST_COMPOSE_PROJECT`(기본 `ytc-test-<pid>`)로 띄우고, 세션이 끝나면 `down -v`로 지운다.
3. 둘 다 없거나 Docker를 못 쓰면 이 fixture를 쓰는 테스트는 이유와 함께 skip.

테스트마다 새 데이터베이스와 새 앱 롤(`ytc_app_<hex>`)을 만들어 서로 섞이지 않게 한다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PLUGIN_ROOT / "deploy" / "server" / "docker-compose.test.yml"
ALEMBIC_INI = PLUGIN_ROOT / "server" / "alembic.ini"


@dataclass(frozen=True)
class Stack:
    """테스트 클러스터 접속 정보. `owner_dsn`은 기본 DB(`ytc`)에 owner(슈퍼유저)로 붙는다."""

    host: str
    port: int
    owner_user: str
    owner_password: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str

    def dsn(self, dbname: str, user: str | None = None, password: str | None = None) -> str:
        from psycopg.conninfo import make_conninfo

        return make_conninfo(host=self.host, port=self.port, dbname=dbname, user=user or self.owner_user,
                             password=password if user else self.owner_password, connect_timeout=10)


@dataclass(frozen=True)
class TestDb:
    """테스트 하나의 데이터베이스. `app_dsn`의 사용자 = 마이그레이션이 만드는 앱 롤."""

    __test__ = False

    name: str
    owner_dsn: str
    app_dsn: str
    app_role: str


def _compose(project: str, *args: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), *args],
                          capture_output=True, text=True, timeout=timeout, check=False)


def _host_port(project: str, service: str, port: int) -> tuple[str, int]:
    out = _compose(project, "port", service, str(port))
    if out.returncode != 0 or ":" not in out.stdout:
        raise RuntimeError(f"docker compose port {service} {port} failed: {out.stderr.strip()}")
    host, _, num = out.stdout.strip().rpartition(":")
    return host, int(num)


def _docker_ready() -> str | None:
    """Docker를 쓸 수 없으면 그 이유, 쓸 수 있으면 None."""
    if shutil.which("docker") is None:
        return "docker 명령이 없다"
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"docker info 실패: {e}"
    if info.returncode != 0:
        return f"Docker 데몬에 연결할 수 없다: {info.stderr.strip()[:200]}"
    return None


@pytest.fixture(scope="session")
def pg_stack() -> Iterator[Stack]:
    external = os.environ.get("YTC_TEST_DB_DSN")
    if external:
        from psycopg.conninfo import conninfo_to_dict

        p = conninfo_to_dict(external)
        yield Stack(str(p.get("host", "127.0.0.1")), int(p.get("port", 5432)), str(p.get("user", "ytc_owner")),
                    str(p.get("password", "")), os.environ.get("YTC_TEST_S3_ENDPOINT", ""),
                    os.environ.get("YTC_TEST_S3_ACCESS_KEY", "ytc-test"),
                    os.environ.get("YTC_TEST_S3_SECRET_KEY", "ytc-test-secret"))
        return
    if os.environ.get("YTC_TEST_DOCKER") != "1":
        pytest.skip("PG·MinIO 테스트: YTC_TEST_DOCKER=1(Docker로 띄움) 또는 YTC_TEST_DB_DSN(이미 띄운 PG)이 필요하다")
    reason = _docker_ready()
    if reason:
        pytest.skip(f"PG·MinIO 테스트: {reason}")
    project = os.environ.get("YTC_TEST_COMPOSE_PROJECT") or f"ytc-test-{os.getpid()}"
    try:
        up = _compose(project, "up", "-d", "--wait", timeout=300)
        if up.returncode != 0:
            raise RuntimeError(f"docker compose up failed: {up.stderr.strip()[-2000:]}")
        pg_host, pg_port = _host_port(project, "postgres", 5432)
        s3_host, s3_port = _host_port(project, "minio", 9000)
        yield Stack(pg_host, pg_port, "ytc_owner", os.environ.get("YTC_TEST_PG_PASSWORD", "ytc-test-owner"),
                    f"http://{s3_host}:{s3_port}", os.environ.get("YTC_TEST_S3_ACCESS_KEY", "ytc-test"),
                    os.environ.get("YTC_TEST_S3_SECRET_KEY", "ytc-test-secret"))
    finally:
        _compose(project, "down", "-v", "--remove-orphans", timeout=180)


@pytest.fixture
def test_db(pg_stack: Stack) -> Iterator[TestDb]:
    """빈 데이터베이스 하나(마이그레이션 전). 끝나면 데이터베이스와 앱 롤을 지운다."""
    import psycopg
    from psycopg import sql

    suffix = uuid.uuid4().hex[:10]
    name, role, password = f"t_{suffix}", f"ytc_app_{suffix}", f"pw-{uuid.uuid4().hex}"
    admin = pg_stack.dsn("ytc")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield TestDb(name, pg_stack.dsn(name), pg_stack.dsn(name, role, password), role)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def run_alembic(db: TestDb, action: str, revision: str) -> None:
    """`alembic -c server/alembic.ini <upgrade|downgrade> <revision>`을 이 테스트 DB에 대해 실행한다."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["owner_dsn"] = db.owner_dsn
    cfg.attributes["app_dsn"] = db.app_dsn
    getattr(command, action)(cfg, revision)


@pytest.fixture
def migrated_db(test_db: TestDb) -> TestDb:
    run_alembic(test_db, "upgrade", "head")
    return test_db


def pg_user(owner_dsn: str, email: str) -> uuid.UUID:
    """owner로 `users` 행 하나를 넣고 id를 돌려준다."""
    import psycopg

    with psycopg.connect(owner_dsn) as conn:
        row = conn.execute("INSERT INTO users (email) VALUES (%s) RETURNING id", (email,)).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


@pytest.fixture
def pg_db(request: pytest.FixtureRequest) -> Any:
    """마이그레이션한 테스트 DB의 앱 롤 풀(`server.db.Db`). 동시성 테스트용으로 연결 8개."""
    pytest.importorskip("psycopg")
    migrated = request.getfixturevalue("migrated_db")      # skips here when there is no PG environment
    from server.db import Db

    db = Db(migrated.app_dsn, migrated.owner_dsn, max_size=8)
    db.open()
    request.addfinalizer(db.close)
    return db


def pg_doc_store(request: pytest.FixtureRequest, email: str = "a@example.com") -> Any:
    """새 사용자(email)의 PgDocStore. 같은 테스트에서 여러 번 부르면 같은 DB의 다른 사용자들."""
    db = request.getfixturevalue("pg_db")
    from server.pgstore import PgDocStore

    return PgDocStore(db, pg_user(request.getfixturevalue("migrated_db").owner_dsn, email), email)
