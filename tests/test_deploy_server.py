"""서버판 패키징(서버 설계 §9.1·§9.3) 정적 검사: Dockerfile server 타깃, compose, Caddyfile, .env.example.
Docker 없이 도는 검사만 둔다. 실제 build/up 스모크는 tests/test_deploy_smoke.py(YTC_TEST_DOCKER=1일 때만)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from server.config import AuthSettings, LlmSettings, StorageSettings

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
SERVER_DIR = ROOT / "deploy" / "server"
COMPOSE = SERVER_DIR / "docker-compose.yml"
ENV_EXAMPLE = SERVER_DIR / ".env.example"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _services() -> dict[str, dict[str, Any]]:
    return dict(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])


def _server_stage() -> str:
    return DOCKERFILE.read_text(encoding="utf-8").split("FROM base AS server", 1)[1]


def _env_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.lstrip("# ").strip() if line.startswith("# YTC_") else line
        if re.match(r"^[A-Z][A-Z0-9_]*=", text):
            names.add(text.split("=", 1)[0])
    return names


# ---- Dockerfile ---------------------------------------------------------------------------------------------

def test_dockerfile_server_target_runs_uvicorn_per_design_9_1() -> None:
    stage = _server_stage()
    cmd = re.search(r"^CMD (\[.*\])$", stage, re.M)
    assert cmd is not None
    args = yaml.safe_load(cmd.group(1))
    assert args[:2] == ["uvicorn", "server.asgi:app"]
    for flag, value in (("--host", "0.0.0.0"), ("--port", "8080"), ("--workers", "1"), ("--lifespan", "on")):
        assert args[args.index(flag) + 1] == value
    assert "--no-access-log" in args and "--proxy-headers" in args


def test_dockerfile_server_target_is_non_root_with_healthz_and_server_files() -> None:
    stage = _server_stage()
    assert re.findall(r"^USER (\S+)$", stage, re.M)[-1] == "10001"           # root only for pip, then back
    assert "COPY server ./server" in stage and "requirements-server.txt" in stage
    assert 'HEALTHCHECK' in stage and "/healthz" in stage and "8080" in stage
    assert "ANTHROPIC_API_KEY" not in stage and "YTC_SESSION_SECRET" not in stage
    assert "pytest" not in stage


def test_dockerfile_server_target_skips_the_nested_requirements_include() -> None:
    # `-r requirements.txt` again would let scenedetect pull the GUI opencv back in
    assert "grep -v '^-r '" in _server_stage()


def test_dockerignore_lets_the_server_target_files_into_the_build_context() -> None:
    lines = (ROOT / "deploy" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert {"!server", "!requirements-server.txt"} <= set(lines)
    assert not any(".env" in ln for ln in lines if ln.startswith("!"))


# ---- compose --------------------------------------------------------------------------------------------------

def test_compose_has_the_services_of_design_9_3() -> None:
    assert set(_services()) == {"postgres", "minio", "minio-init", "migrate", "app", "admin", "caddy"}


def test_compose_third_party_images_are_pinned_by_digest() -> None:
    for name in ("postgres", "minio", "minio-init", "caddy"):
        assert DIGEST.search(_services()[name]["image"]), name
    assert _services()["minio"]["image"] == _services()["minio-init"]["image"]


def test_compose_minio_image_matches_the_test_compose() -> None:
    test = yaml.safe_load((SERVER_DIR / "docker-compose.test.yml").read_text(encoding="utf-8"))["services"]
    assert _services()["minio"]["image"] == test["minio"]["image"]
    assert _services()["postgres"]["image"] == test["postgres"]["image"]


def test_compose_postgres_and_minio_publish_no_ports_and_have_healthchecks() -> None:
    services = _services()
    for name in ("postgres", "minio"):
        assert "ports" not in services[name]
        assert "healthcheck" in services[name]
    assert "pg_isready" in services["postgres"]["healthcheck"]["test"]
    assert services["minio"]["healthcheck"]["test"] == ["CMD", "mc", "ready", "local"]
    assert services["postgres"]["environment"]["POSTGRES_USER"] == "ytc_owner"
    assert "pgdata:/var/lib/postgresql/data" in services["postgres"]["volumes"]


def test_compose_minio_init_and_migrate_run_once() -> None:
    services = _services()
    assert services["minio-init"]["restart"] == "no" and services["migrate"]["restart"] == "no"
    assert services["minio-init"]["entrypoint"] == ["mc"]
    assert services["minio-init"]["command"][:2] == ["mb", "--ignore-existing"]
    assert services["migrate"]["command"][:3] == ["python", "-m", "alembic"]
    assert services["migrate"]["command"][-2:] == ["upgrade", "head"]
    assert set(services["migrate"]["environment"]) == {"YTC_DB_OWNER_DSN", "YTC_DB_DSN"}


def test_compose_app_waits_for_healthy_stores_and_completed_setup() -> None:
    depends = _services()["app"]["depends_on"]
    assert depends["postgres"]["condition"] == "service_healthy"
    assert depends["minio"]["condition"] == "service_healthy"
    assert depends["migrate"]["condition"] == "service_completed_successfully"
    assert depends["minio-init"]["condition"] == "service_completed_successfully"


def test_compose_app_is_localhost_only_with_state_volume_tmpfs_and_grace_period() -> None:
    app = _services()["app"]
    assert app["ports"] == ["127.0.0.1:${YTC_PORT:-8080}:8080"]
    assert "state:/var/lib/ytc/state" in app["volumes"]
    assert any(t.startswith("/tmp/ytc:") for t in app["tmpfs"])
    assert app["stop_grace_period"] == "90s"
    assert "/healthz" in " ".join(app["healthcheck"]["test"])
    assert app["cap_drop"] == ["ALL"]


def test_compose_app_gets_the_app_role_only_not_the_owner_dsn() -> None:
    env = _services()["app"]["environment"]
    assert "YTC_DB_OWNER_DSN" not in env
    assert "ytc_app:" in env["YTC_DB_DSN"] and "ytc_owner" not in env["YTC_DB_DSN"]
    assert "YTC_DB_OWNER_DSN" in _services()["admin"]["environment"]


def test_compose_secrets_come_from_variables_never_literals() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "sk-ant" not in text
    env = _services()["app"]["environment"]
    for name in ("YTC_SESSION_SECRET", "YTC_PUBLIC_URL"):
        assert env[name].startswith("${") and ":?" in env[name]          # required, no default
    assert ":?" in _services()["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert ":?" in _services()["minio"]["environment"]["MINIO_ROOT_PASSWORD"]
    assert env["YTC_ENV"] == "${YTC_ENV:-production}"                    # production unless the operator says otherwise
    assert "YTC_DEV_LOGIN" in env and env["YTC_DEV_LOGIN"] == "${YTC_DEV_LOGIN:-}"


def test_compose_caddy_and_admin_are_behind_profiles() -> None:
    services = _services()
    assert services["caddy"]["profiles"] == ["tls"]
    assert services["admin"]["profiles"] == ["admin"]
    assert services["caddy"]["ports"] == ["80:80", "443:443", "443:443/udp"]
    assert "caddy_data:/data" in services["caddy"]["volumes"]
    assert services["caddy"]["depends_on"]["app"]["condition"] == "service_healthy"
    for name in ("postgres", "minio", "minio-init", "migrate", "app"):
        assert "profiles" not in services[name]


def test_caddyfile_proxies_the_domain_to_the_app() -> None:
    text = (SERVER_DIR / "Caddyfile").read_text(encoding="utf-8")
    assert "{$YTC_DOMAIN}" in text and "reverse_proxy app:8080" in text


def test_compose_only_passes_variables_the_app_reads_and_env_example_lists_all() -> None:
    names = _env_names(ENV_EXAMPLE)
    interpolated = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", COMPOSE.read_text(encoding="utf-8")))
    assert interpolated <= names, sorted(interpolated - names)
    # every variable of design 4.4 that an operator sets is documented
    assert {"YTC_ENV", "YTC_PUBLIC_URL", "YTC_SESSION_SECRET", "YTC_GOOGLE_CLIENT_ID", "YTC_GOOGLE_CLIENT_SECRET",
            "YTC_DEV_LOGIN", "YTC_DB_OWNER_PASSWORD", "YTC_DB_APP_PASSWORD", "YTC_S3_ACCESS_KEY", "YTC_S3_SECRET_KEY",
            "ANTHROPIC_API_KEY", "YTC_LLM_MODEL", "YTC_LLM_MAX_TOKENS", "YTC_DAILY_USD", "YTC_MEDIA_SLOTS", "YTC_LLM_SLOTS",
            "YT_PROXY", "YT_FETCH_INTERVAL", "YTC_DOMAIN", "YTC_S3_BUCKET", "YTC_DB_POOL_MIN", "YTC_DB_POOL_MAX",
            "YTC_S3_MAX_POOL_CONNECTIONS"} <= names


def test_env_example_has_no_values_and_korean_comments() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if re.match(r"^[A-Z][A-Z0-9_]*=", line):
            name, value = line.split("=", 1)
            # secrets and passwords are always empty; only harmless defaults carry a value
            assert value == "" or (name, value) in {("YTC_ENV", "production"), ("YTC_PASSWORD_LOGIN", "1")}, line
    assert re.search(r"[가-힣]", text) and "secrets.token_urlsafe" in text
    assert "sk-ant" not in text


def test_env_example_values_boot_the_config_when_filled_with_the_documented_defaults() -> None:
    """compose가 앱에 넘기는 빈 값은 설정 단계에서 기본값으로 읽힌다(빈 문자열이 config_invalid를 내지 않는다)."""
    env = {"YTC_DB_DSN": "postgresql://ytc_app:x@postgres/ytc", "YTC_S3_ENDPOINT": "http://minio:9000",
           "YTC_S3_ACCESS_KEY": "k", "YTC_S3_SECRET_KEY": "s", "YTC_PUBLIC_URL": "https://ytc.example.com",
           "YTC_SESSION_SECRET": "x" * 48, "YTC_GOOGLE_CLIENT_ID": "id", "YTC_GOOGLE_CLIENT_SECRET": "secret",
           **{name: "" for name in ("YTC_ENV", "YTC_DEV_LOGIN", "YTC_DB_POOL_MIN", "YTC_DB_POOL_MAX", "YTC_S3_BUCKET",
                                    "YTC_S3_MAX_POOL_CONNECTIONS", "YTC_LLM_MODEL", "YTC_LLM_MAX_TOKENS", "YTC_DAILY_USD",
                                    "YTC_MEDIA_SLOTS", "YTC_LLM_SLOTS")}}
    assert StorageSettings.from_env(env).s3_bucket == "ytc"
    assert AuthSettings.from_env(env).production
    assert LlmSettings.from_env(env).llm_slots == 4


def test_smoke_script_accepts_the_server_edition() -> None:
    text = (ROOT / "deploy" / "smoke.py").read_text(encoding="utf-8")
    assert 'choices=["local", "server"]' in text and "def smoke_server" in text
    assert "down" in text and "-v" in text


# ---- fix round 1 -------------------------------------------------------------------------------------------------

def test_compose_every_service_rotates_json_logs() -> None:
    for name, svc in _services().items():
        assert svc["logging"] == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}, name


def test_compose_app_has_a_configurable_memory_limit() -> None:
    assert _services()["app"]["mem_limit"] == "${YTC_APP_MEM_LIMIT:-3g}"
    assert "YTC_APP_MEM_LIMIT" in _env_names(ENV_EXAMPLE)


def test_compose_scratch_tmpfs_defaults_to_1g_inside_the_memory_limit() -> None:
    # tmpfs pages count against the container memory limit: 1g scratch inside a 3g limit
    tmpfs = [t for t in _services()["app"]["tmpfs"] if t.startswith("/tmp/ytc:")]
    assert tmpfs and "size=${YTC_SCRATCH_SIZE:-1g}" in tmpfs[0]
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "# YTC_SCRATCH_SIZE=1g" in text and "# YTC_APP_MEM_LIMIT=3g" in text
    assert "메모리 상한에 포함" in text
    ops = (ROOT / "deploy" / "server" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "YTC_SCRATCH_SIZE" in ops and "메모리 상한에 포함" in ops


def test_compose_stores_are_only_on_the_back_network_and_caddy_only_on_front() -> None:
    services = _services()
    for name in ("postgres", "minio", "minio-init", "migrate", "admin"):
        assert services[name]["networks"] == ["back"], name
    assert sorted(services["app"]["networks"]) == ["back", "front"]
    assert list(services["caddy"]["networks"]) == ["front"]    # caddy cannot reach postgres or minio


def test_compose_caddy_fails_fast_with_a_clear_message_when_the_domain_is_empty() -> None:
    caddy = _services()["caddy"]
    script = caddy["entrypoint"][-1]
    assert caddy["entrypoint"][:2] == ["sh", "-c"]
    assert '[ -z "$$YTC_DOMAIN" ]' in script and "YTC_DOMAIN이 비어 있습니다" in script and "exit 64" in script
    assert "exec caddy run" in script
    assert caddy["restart"] == "on-failure:5"                  # no endless crash loop
    assert "wget" in caddy["healthcheck"]["test"]
    # only the tls profile needs the domain: the variable has a default, so the rest of the stack starts without it
    assert _services()["caddy"]["environment"]["YTC_DOMAIN"] == "${YTC_DOMAIN:-}"
    assert "YTC_DOMAIN:?" not in COMPOSE.read_text(encoding="utf-8")


def test_env_example_says_s3_keys_must_be_url_safe() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "영문·숫자·-_" in text.split("YTC_S3_ACCESS_KEY=", 1)[0].rsplit("[필수] MinIO", 1)[1]


def test_operations_notes_cover_backup_restore_passwords_digest_and_users() -> None:
    text = (SERVER_DIR / "OPERATIONS.md").read_text(encoding="utf-8")
    for needle in ("pg_dump", "mc", "mirror", "pg_restore", "복원 순서", "처음 초기화될 때만", "imagetools", "add-user", "import"):
        assert needle in text, needle


def test_docs_explain_that_logout_is_per_browser_and_how_to_sign_out_everywhere() -> None:
    for path in (ROOT / "deploy" / "server" / "OPERATIONS.md", ROOT / "README.md"):
        text = path.read_text(encoding="utf-8")
        assert "logout-all" in text and "이 브라우저" in text, path


# ---- 이메일+비밀번호 로그인(T15) ------------------------------------------------------------------

def _front_net_default() -> str:
    m = re.search(r"\$\{YTC_FRONT_NET:-([0-9.]+)\}", COMPOSE.read_text(encoding="utf-8"))
    assert m is not None
    return m.group(1)


def _expand(value: str) -> str:
    return value.replace("${YTC_FRONT_NET:-" + _front_net_default() + "}", _front_net_default())


def test_compose_passes_password_login_and_env_example_turns_it_on() -> None:
    env = _services()["app"]["environment"]
    assert env["YTC_PASSWORD_LOGIN"] == "${YTC_PASSWORD_LOGIN:-}"
    assert "YTC_PASSWORD_LOGIN=1" in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def test_compose_trusts_forwarded_headers_only_from_caddys_fixed_ip() -> None:
    import ipaddress
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    (pool,) = doc["networks"]["front"]["ipam"]["config"]
    subnet, dynamic = ipaddress.ip_network(_expand(pool["subnet"])), ipaddress.ip_network(_expand(pool["ip_range"]))
    caddy_ip = ipaddress.ip_address(_expand(doc["services"]["caddy"]["networks"]["front"]["ipv4_address"]))
    trusted = _expand(doc["services"]["app"]["environment"]["FORWARDED_ALLOW_IPS"])
    assert ipaddress.ip_address(trusted) == caddy_ip                      # one exact address, no CIDR, no '*'
    assert caddy_ip in subnet and caddy_ip not in dynamic                 # app and others cannot take caddy's IP
    assert caddy_ip != next(subnet.hosts())                               # not the gateway (port-published traffic)
    assert dynamic.subnet_of(subnet)  # type: ignore[arg-type]
    assert "--proxy-headers" in _server_stage()


def test_caddy_does_not_trust_incoming_forwarded_headers() -> None:
    text = (SERVER_DIR / "Caddyfile").read_text(encoding="utf-8")
    directives = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("trusted_proxies" in ln for ln in directives)


def test_env_example_boots_production_with_password_login_only() -> None:
    env = {"YTC_PUBLIC_URL": "https://ytc.example.com", "YTC_SESSION_SECRET": "x" * 48, "YTC_PASSWORD_LOGIN": "1",
           "YTC_ENV": "", "YTC_DEV_LOGIN": "", "YTC_GOOGLE_CLIENT_ID": "", "YTC_GOOGLE_CLIENT_SECRET": ""}
    s = AuthSettings.from_env(env)
    assert s.production and s.password_login and s.google is None


def test_docs_explain_password_users_and_resets() -> None:
    for path in (SERVER_DIR / "OPERATIONS.md", ROOT / "README.md"):
        text = path.read_text(encoding="utf-8")
        for needle in ("--password", "set-password", "clear-password", "--password-stdin", "YTC_PASSWORD_LOGIN"):
            assert needle in text, (path.name, needle)
