"""서버판 설정(서버 설계 §4.4): env → frozen 설정. 필수 값이 없으면 시작하지 않는다(`config_missing`).

지금은 저장소 설정(`StorageSettings`: PG·MinIO·임시/상태 폴더·연결 풀 크기)만 있다.
인증·LLM·한도 설정은 T10~T12가 더한다.
연결 풀: 앱 요청 스레드(anyio 기본 40)와 작업 스레드가 같은 풀을 나눠 쓴다. 풀이 모자라면 `YTC_DB_POOL_MAX`·
`YTC_S3_MAX_POOL_CONNECTIONS`를 올린다(boto3 기본 10, 넘으면 "Connection pool is full" 경고 후 연결을 버린다).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from scripts.common import ScriptError

DEFAULT_SCRATCH_DIR: Final = "/tmp/ytc"
DEFAULT_STATE_DIR: Final = "/var/lib/ytc/state"
DEFAULT_BUCKET: Final = "ytc"
DEFAULT_DB_POOL_MIN: Final = 1
DEFAULT_DB_POOL_MAX: Final = 10
DEFAULT_S3_MAX_POOL: Final = 10


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ScriptError("config_missing", f"서버 설정 {name}이(가) 없습니다. deploy/server/.env에 넣으세요.")
    return value


def _int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    text = (env.get(name) or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError as e:
        raise ScriptError("config_invalid", f"{name}은(는) 정수여야 합니다: {text!r}") from e
    if value < minimum:
        raise ScriptError("config_invalid", f"{name}은(는) {minimum} 이상이어야 합니다: {value}")
    return value


@dataclass(frozen=True)
class StorageSettings:
    db_dsn: str = field(repr=False)                     # app role (NOBYPASSRLS); contains the password
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str = field(repr=False)
    s3_bucket: str = DEFAULT_BUCKET
    db_owner_dsn: str | None = field(default=None, repr=False)   # migrate·admin only
    db_pool_min: int = DEFAULT_DB_POOL_MIN
    db_pool_max: int = DEFAULT_DB_POOL_MAX
    s3_max_pool_connections: int = DEFAULT_S3_MAX_POOL
    scratch_dir: Path = Path(DEFAULT_SCRATCH_DIR)
    state_dir: Path = Path(DEFAULT_STATE_DIR)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StorageSettings:
        pool_min = _int(env, "YTC_DB_POOL_MIN", DEFAULT_DB_POOL_MIN, 0)
        pool_max = _int(env, "YTC_DB_POOL_MAX", DEFAULT_DB_POOL_MAX, 1)
        if pool_min > pool_max:
            raise ScriptError("config_invalid",
                              f"YTC_DB_POOL_MIN({pool_min})이 YTC_DB_POOL_MAX({pool_max})보다 큽니다")
        return cls(
            db_dsn=_required(env, "YTC_DB_DSN"),
            s3_endpoint=_required(env, "YTC_S3_ENDPOINT"),
            s3_access_key=_required(env, "YTC_S3_ACCESS_KEY"),
            s3_secret_key=_required(env, "YTC_S3_SECRET_KEY"),
            s3_bucket=(env.get("YTC_S3_BUCKET") or "").strip() or DEFAULT_BUCKET,
            db_owner_dsn=(env.get("YTC_DB_OWNER_DSN") or "").strip() or None,
            db_pool_min=pool_min,
            db_pool_max=pool_max,
            s3_max_pool_connections=_int(env, "YTC_S3_MAX_POOL_CONNECTIONS", DEFAULT_S3_MAX_POOL, 1),
            scratch_dir=Path((env.get("YTC_SCRATCH_DIR") or "").strip() or DEFAULT_SCRATCH_DIR),
            state_dir=Path((env.get("YTC_STATE_DIR") or "").strip() or DEFAULT_STATE_DIR),
        )
