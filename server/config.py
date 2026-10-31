"""서버판 설정(서버 설계 §4.4): env → frozen 설정. 필수 값이 없으면 시작하지 않는다(`config_missing`).

- `StorageSettings`: PG·MinIO·임시/상태 폴더·연결 풀 크기(T8).
- `AuthSettings`(T10): `YTC_ENV`(production|dev|test, 기본 production), `YTC_PUBLIC_URL`(출처 = CSRF Origin 비교 기준), `YTC_SESSION_SECRET`(32바이트 이상),
  `YTC_DEV_LOGIN`(1이면 dev 로그인. production에서는 시작 거부). production은 https 공개 주소만(쿠키 Secure).
- `ServerSettings`: 앱이 쓰는 설정 묶음(storage + auth). Google OIDC(T12)·LLM·한도(T11)는 그 Task가 더한다.
연결 풀: 앱 요청 스레드(anyio 기본 40)와 작업 스레드가 같은 풀을 나눠 쓴다. 풀이 모자라면 `YTC_DB_POOL_MAX`·
`YTC_S3_MAX_POOL_CONNECTIONS`를 올린다(boto3 기본 10, 넘으면 "Connection pool is full" 경고 후 연결을 버린다).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit

from scripts.common import ScriptError

DEFAULT_SCRATCH_DIR: Final = "/tmp/ytc"
DEFAULT_STATE_DIR: Final = "/var/lib/ytc/state"
DEFAULT_BUCKET: Final = "ytc"
DEFAULT_DB_POOL_MIN: Final = 1
DEFAULT_DB_POOL_MAX: Final = 10
DEFAULT_S3_MAX_POOL: Final = 10
DEFAULT_ENV: Final = "production"
PRODUCTION: Final = "production"
ENV_NAMES: Final = ("production", "dev", "test")
MIN_SECRET_BYTES: Final = 32
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


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


def _abs_dir(env: Mapping[str, str], name: str, default: str) -> Path:
    """절대 경로만(상대 경로는 프로세스 cwd에 따라 달라진다). 서버는 POSIX 경로, 테스트는 그 OS의 절대 경로도 받는다."""
    text = (env.get(name) or "").strip() or default
    if not (PurePosixPath(text).is_absolute() or Path(text).is_absolute()):
        raise ScriptError("config_invalid", f"{name}은(는) 절대 경로여야 합니다: {text!r}")
    return Path(text)


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
            scratch_dir=_abs_dir(env, "YTC_SCRATCH_DIR", DEFAULT_SCRATCH_DIR),
            state_dir=_abs_dir(env, "YTC_STATE_DIR", DEFAULT_STATE_DIR),
        )


def _origin(url: str) -> str:
    """`YTC_PUBLIC_URL` → 출처(`scheme://host[:port]`, 소문자, 기본 포트 생략). 경로·쿼리·사용자 정보가 있으면 config_invalid."""
    bad = ScriptError("config_invalid", f"YTC_PUBLIC_URL은 https://도메인[:포트] 형식이어야 합니다(경로 없이): {url!r}")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise bad from e
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname or parts.username is not None             or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise bad
    host = parts.hostname.lower()
    if ":" in host:                                  # IPv6 literal
        host = f"[{host}]"
    suffix = "" if port is None or port == _DEFAULT_PORTS[parts.scheme] else f":{port}"
    return f"{parts.scheme}://{host}{suffix}"


def _flag(env: Mapping[str, str], name: str) -> bool:
    text = (env.get(name) or "").strip()
    if text not in ("", "0", "1"):
        raise ScriptError("config_invalid", f"{name}은(는) 0 또는 1이어야 합니다: {text!r}")
    return text == "1"


@dataclass(frozen=True)
class AuthSettings:
    env: str
    public_origin: str                                  # CSRF Origin 비교 기준
    session_secret: str = field(repr=False)
    dev_login: bool = False

    @property
    def production(self) -> bool:
        return self.env == PRODUCTION

    @property
    def secure_cookie(self) -> bool:
        """공개 주소가 https면 쿠키에 Secure·HSTS(production은 https만 받는다)."""
        return self.public_origin.startswith("https://")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AuthSettings:
        name = (env.get("YTC_ENV") or "").strip().lower() or DEFAULT_ENV
        if name not in ENV_NAMES:
            raise ScriptError("config_invalid", f"YTC_ENV는 {', '.join(ENV_NAMES)} 중 하나여야 합니다: {name!r}")
        origin = _origin(_required(env, "YTC_PUBLIC_URL"))
        secret = _required(env, "YTC_SESSION_SECRET")
        if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
            raise ScriptError("config_invalid", f"YTC_SESSION_SECRET은(는) {MIN_SECRET_BYTES}바이트 이상이어야 합니다. "
                                                "python -c \"import secrets;print(secrets.token_urlsafe(48))\"로 만드세요.")
        dev = _flag(env, "YTC_DEV_LOGIN")
        if dev and name == PRODUCTION:
            raise ScriptError("config_invalid", "YTC_DEV_LOGIN=1은 YTC_ENV=production(기본값)에서 쓸 수 없습니다. "
                                                "개발·시험에서만 YTC_ENV=dev와 함께 켜세요.")
        if name == PRODUCTION and not origin.startswith("https://"):
            raise ScriptError("config_invalid", "YTC_ENV=production에서는 YTC_PUBLIC_URL이 https여야 합니다(세션 쿠키 Secure).")
        return cls(env=name, public_origin=origin, session_secret=secret, dev_login=dev)


@dataclass(frozen=True)
class ServerSettings:
    storage: StorageSettings
    auth: AuthSettings

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ServerSettings:
        return cls(storage=StorageSettings.from_env(env), auth=AuthSettings.from_env(env))
