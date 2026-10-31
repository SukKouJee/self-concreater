"""server.db·마이그레이션 설정 검사 — DB 없이 도는 부분(T5)."""
from __future__ import annotations

import uuid

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("alembic")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from scripts.common import ScriptError  # noqa: E402
from server.db import Db, app_role_from_dsn  # noqa: E402
from tests.server.conftest import ALEMBIC_INI  # noqa: E402


@pytest.mark.parametrize(("dsn", "expected"), [
    ("postgresql://ytc_app:s3cret@db:5432/ytc", ("ytc_app", "s3cret")),
    ("host=db dbname=ytc user=ytc_app password='a b'", ("ytc_app", "a b")),
    ("postgresql://ytc_app@db/ytc", ("ytc_app", None)),
])
def test_app_role_from_dsn_reads_user_and_password(dsn: str, expected: tuple[str, str | None]) -> None:
    assert app_role_from_dsn(dsn) == expected


def test_app_role_from_dsn_requires_user() -> None:
    with pytest.raises(ScriptError) as e:
        app_role_from_dsn("postgresql://db/ytc")
    assert e.value.code == "config_missing"


@pytest.mark.parametrize("user", ["YtcApp", 'ytc"app', "ytc-app", "1ytc", "a" * 64])
def test_app_role_from_dsn_rejects_names_that_need_quoting(user: str) -> None:
    with pytest.raises(ScriptError) as e:
        app_role_from_dsn(f"host=db user='{user}'")
    assert e.value.code == "config_invalid"


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "00000000-0000-0000-0000-00000000000g", "1; DROP TABLE docs"])
def test_user_tx_rejects_non_uuid_before_touching_the_db(bad: str) -> None:
    d = Db("postgresql://ytc_app@127.0.0.1:1/none")  # never opened, never connects
    with pytest.raises(ValueError), d.user_tx(bad):
        pass


@pytest.mark.parametrize("bad", [123, None, b"0" * 16, uuid.uuid4().int])
def test_user_tx_rejects_ids_that_are_not_uuid_or_str(bad: object) -> None:
    d = Db("postgresql://ytc_app@127.0.0.1:1/none")
    with pytest.raises(TypeError), d.user_tx(bad):  # type: ignore[arg-type]
        pass


def test_admin_tx_without_owner_dsn_is_config_missing() -> None:
    d = Db("postgresql://ytc_app@127.0.0.1:1/none")
    with pytest.raises(ScriptError) as e, d.admin_tx():
        pass
    assert e.value.code == "config_missing"
    assert "YTC_DB_OWNER_DSN" in e.value.message


@pytest.mark.parametrize(("owner", "app", "missing"), [
    (None, "postgresql://ytc_app@127.0.0.1:1/none", "YTC_DB_OWNER_DSN"),
    ("postgresql://ytc_owner@127.0.0.1:1/none", None, "YTC_DB_DSN"),
])
def test_migration_requires_both_dsns(monkeypatch: pytest.MonkeyPatch, owner: str | None, app: str | None,
                                      missing: str) -> None:
    monkeypatch.delenv("YTC_DB_OWNER_DSN", raising=False)
    monkeypatch.delenv("YTC_DB_DSN", raising=False)
    cfg = Config(str(ALEMBIC_INI))
    if owner:
        cfg.attributes["owner_dsn"] = owner
    if app:
        cfg.attributes["app_dsn"] = app
    with pytest.raises(ScriptError) as e:
        command.upgrade(cfg, "head")
    assert e.value.code == "config_missing"
    assert missing in e.value.message


def test_migration_refuses_offline_sql_mode() -> None:
    """오프라인(--sql)은 앱 롤 비밀번호를 SQL 텍스트로 내보내게 되므로 막는다."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["owner_dsn"] = "postgresql://ytc_owner@127.0.0.1:1/none"
    cfg.attributes["app_dsn"] = "postgresql://ytc_app:pw@127.0.0.1:1/none"
    with pytest.raises(ScriptError) as e:
        command.upgrade(cfg, "head", sql=True)
    assert e.value.code == "offline_unsupported"
