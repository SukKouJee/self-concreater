"""server.config 저장소 설정(서버 설계 §4.4): env → Settings. PG 연결 풀·boto 연결 풀 크기를 env로 정한다(T7 m-004)."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.common import ScriptError
from server.config import StorageSettings

BASE = {
    "YTC_DB_DSN": "postgresql://ytc_app:pw@postgres/ytc",
    "YTC_S3_ENDPOINT": "http://minio:9000",
    "YTC_S3_ACCESS_KEY": "ak",
    "YTC_S3_SECRET_KEY": "sk",
}


def test_defaults() -> None:
    s = StorageSettings.from_env(BASE)
    assert s.db_dsn == BASE["YTC_DB_DSN"] and s.db_owner_dsn is None
    assert (s.db_pool_min, s.db_pool_max) == (1, 10)
    assert s.s3_bucket == "ytc" and s.s3_max_pool_connections == 10
    assert s.scratch_dir == Path("/tmp/ytc") and s.state_dir == Path("/var/lib/ytc/state")


def test_pool_sizes_and_paths_from_env() -> None:
    s = StorageSettings.from_env({**BASE, "YTC_DB_POOL_MIN": "2", "YTC_DB_POOL_MAX": "20",
                                  "YTC_S3_MAX_POOL_CONNECTIONS": "32", "YTC_S3_BUCKET": "b",
                                  "YTC_DB_OWNER_DSN": "postgresql://ytc_owner@postgres/ytc",
                                  "YTC_SCRATCH_DIR": "/x/tmp", "YTC_STATE_DIR": "/x/state"})
    assert (s.db_pool_min, s.db_pool_max, s.s3_max_pool_connections) == (2, 20, 32)
    assert s.s3_bucket == "b" and s.db_owner_dsn == "postgresql://ytc_owner@postgres/ytc"
    assert s.scratch_dir == Path("/x/tmp") and s.state_dir == Path("/x/state")


@pytest.mark.parametrize("name", sorted(BASE))
def test_missing_required_value_is_config_missing(name: str) -> None:
    env = {k: v for k, v in BASE.items() if k != name}
    with pytest.raises(ScriptError) as e:
        StorageSettings.from_env(env)
    assert e.value.code == "config_missing" and name in e.value.message


@pytest.mark.parametrize("patch", [
    {"YTC_DB_POOL_MAX": "zero"}, {"YTC_DB_POOL_MAX": "0"}, {"YTC_DB_POOL_MIN": "-1"},
    {"YTC_DB_POOL_MIN": "5", "YTC_DB_POOL_MAX": "4"}, {"YTC_S3_MAX_POOL_CONNECTIONS": "0"},
])
def test_bad_pool_sizes_are_config_invalid(patch: dict[str, str]) -> None:
    with pytest.raises(ScriptError) as e:
        StorageSettings.from_env({**BASE, **patch})
    assert e.value.code == "config_invalid"


def test_repr_hides_secrets() -> None:
    text = repr(StorageSettings.from_env(BASE))
    assert "pw" not in text and "sk" not in text


def test_s3_bucket_connect_uses_max_pool_connections() -> None:
    pytest.importorskip("boto3")
    from server.s3store import S3Bucket

    bucket = S3Bucket.connect("http://127.0.0.1:9", "ak", "sk", "b", max_pool_connections=33)
    assert bucket.client.meta.config.max_pool_connections == 33
    assert S3Bucket.connect("http://127.0.0.1:9", "ak", "sk", "b").client.meta.config.max_pool_connections == 10
