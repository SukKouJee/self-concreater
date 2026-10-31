"""개인판 컨테이너 패키징(서버 설계 §9.1~9.2): Dockerfile·compose·.env.example 정적 검사.
Docker 없이 도는 검사만 둔다. 실제 build/up 스모크는 tests/test_deploy_smoke.py(YTC_TEST_DOCKER=1일 때만)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
COMPOSE = ROOT / "deploy" / "local" / "docker-compose.yml"
ENV_EXAMPLE = ROOT / "deploy" / "local" / ".env.example"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _service() -> dict:
    return _compose()["services"]["ytc"]


def _env_names(path: Path) -> list[str]:
    return [ln.split("=", 1)[0] for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_dockerfile_has_base_and_local_targets_and_syntax_line() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.startswith("# syntax=docker/dockerfile:1")
    assert re.search(r"^FROM .* AS base$", text, re.M)
    assert re.search(r"^FROM base AS local$", text, re.M)


def test_dockerfile_base_image_is_pinned_by_digest_and_not_latest() -> None:
    froms = re.findall(r"^FROM (\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.M)
    external = [f for f in froms if f not in {"base"}]
    assert external and all("@sha256:" in f and ":latest" not in f for f in external)


def test_dockerfile_runs_as_numeric_non_root_with_exec_form_and_tini() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^USER 10001(:10001)?$", text, re.M)
    assert 'ENTRYPOINT ["tini", "--"]' in text
    assert re.search(r'^CMD \[', text, re.M)


def test_dockerfile_local_target_fixes_engine_work_dir_and_disabled_features() -> None:
    local = DOCKERFILE.read_text(encoding="utf-8").split("AS local", 1)[1]
    assert "YT_COACH_HOME=/work" in local
    assert "YTC_LLM_ENGINE=anthropic-api" in local
    assert "YTC_DISABLE=library,caption_video" in local
    for flag in ('"--host", "0.0.0.0"', '"--no-open"', '"--foreground"'):
        assert flag in local


def test_dockerfile_base_sets_container_flag_and_has_no_secrets() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "YTC_IN_CONTAINER=1" in text
    assert "ANTHROPIC_API_KEY" not in text


def test_dockerignore_is_allowlist_that_excludes_env_and_workspace() -> None:
    lines = [ln for ln in (ROOT / "deploy" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines[0].strip() == "*"
    allowed = {ln[1:] for ln in lines if ln.startswith("!")}
    assert {"scripts", "templates", "skills", "requirements.txt"} <= allowed
    assert not any(".env" in a or "workspace" in a for a in allowed)


def test_compose_publishes_only_on_localhost_with_matching_port_numbers() -> None:
    ports = _service()["ports"]
    assert len(ports) == 1
    match = re.fullmatch(r"127\.0\.0\.1:(\$\{YTC_PORT:-8765\}):(\$\{YTC_PORT:-8765\})", ports[0])
    # localhost 바인딩, 그리고 Host 헤더 검사 때문에 게시 포트와 내부 포트는 같은 번호
    assert match is not None and match.group(1) == match.group(2)


def test_compose_mounts_work_dir_and_runs_ui_in_foreground_on_all_interfaces() -> None:
    svc = _service()
    assert "${YTC_WORK_DIR:-./work}:/work" in svc["volumes"]
    cmd = svc["command"]
    assert cmd[:3] == ["python", "-m", "scripts.ui"]
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--port") + 1] == "${YTC_PORT:-8765}"
    assert "--foreground" in cmd and "--no-open" in cmd


def test_compose_has_healthz_healthcheck_restart_and_user() -> None:
    svc = _service()
    check = " ".join(svc["healthcheck"]["test"])
    assert "/healthz" in check
    assert svc["restart"] == "unless-stopped"
    assert svc["user"] == "${YTC_UID:-1000}:${YTC_GID:-1000}"
    assert svc["environment"]["YTC_IN_CONTAINER"] == "1"


def test_compose_takes_api_key_from_env_file_not_from_the_file_itself() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "sk-ant" not in text
    env_file = _service()["env_file"]
    assert any(e["path"] == ".env" and e["required"] is False for e in env_file)


def test_env_example_lists_the_documented_variables_with_empty_values() -> None:
    names = _env_names(ENV_EXAMPLE)
    assert {"ANTHROPIC_API_KEY", "YTC_WORK_DIR", "YT_PROXY", "YTC_LLM_MODEL"} <= set(names)
    for ln in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if ln.startswith("ANTHROPIC_API_KEY="):
            assert ln == "ANTHROPIC_API_KEY="


def test_dockerfile_local_target_has_healthz_healthcheck() -> None:
    local = DOCKERFILE.read_text(encoding="utf-8").split("AS local", 1)[1]
    assert "HEALTHCHECK " in local and 'CMD ["python", "-c"' in local and "/healthz" in local


def test_env_example_documents_existing_work_folder_mount() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for needle in ("새 빈 작업 폴더", "C:/ai/projects/orca/nahon-workspace", "ui.lock", "ytc ui --stop", "id -u", "id -g"):
        assert needle in text


def test_dockerfile_continuations_put_one_instruction_part_per_line() -> None:
    # a collapsed continuation shows up as a run of spaces before `&&`, `CMD` or a next ENV pair mid-line
    for n, line in enumerate(DOCKERFILE.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        assert not re.search(r"\S {3,}\S", line), f"line {n}: {line!r}"


def test_dockerfile_sets_no_unused_edition_flag() -> None:
    # nothing reads YTC_EDITION (server is selected by the uvicorn entry point, design §9.1)
    assert "YTC_EDITION" not in DOCKERFILE.read_text(encoding="utf-8")
