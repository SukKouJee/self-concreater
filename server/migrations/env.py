"""Alembic 실행 환경(서버 설계 §3.1). 원시 SQL 마이그레이션만 쓴다(ADR-S8, ORM 메타데이터 없음).

접속 정보: `config.attributes["owner_dsn"|"app_dsn"]`(테스트·코드에서 부를 때) 또는 env `YTC_DB_OWNER_DSN`·`YTC_DB_DSN`.
둘 다 필수다. owner로 접속해 DDL을 실행하고, 앱 롤 이름·비밀번호는 앱 DSN에서 읽어 마이그레이션에 넘긴다.
"""
from __future__ import annotations

import os

import psycopg
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from scripts.common import ScriptError
from server.db import app_role_from_dsn

config = context.config


def _setting(attr: str, env: str) -> str:
    value = config.attributes.get(attr) or os.environ.get(env)
    if not value:
        raise ScriptError("config_missing", f"마이그레이션에는 {env}가 필요합니다.")
    return str(value)


def run() -> None:
    if context.is_offline_mode():
        raise ScriptError("offline_unsupported",
                          "오프라인(--sql) 마이그레이션은 지원하지 않습니다(앱 롤 비밀번호가 SQL에 남습니다). DB에 직접 적용하세요.")
    owner_dsn = _setting("owner_dsn", "YTC_DB_OWNER_DSN")
    app_dsn = _setting("app_dsn", "YTC_DB_DSN")
    config.attributes["app_role"], config.attributes["app_password"] = app_role_from_dsn(app_dsn)
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(owner_dsn), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None, transactional_ddl=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run()
