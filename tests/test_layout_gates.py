"""서버판 배치 게이트(서버 설계 §2): 의존 방향, 골격, `.env` 미추적, 서버 의존성 파일."""
from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"


def server_imports(source: str) -> list[int]:
    """`server` 패키지를 가리키는 import·동적 import 호출의 줄 번호."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hits += [node.lineno for a in node.names if a.name.split(".")[0] == "server"]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == "server":
                hits.append(node.lineno)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) and node.args[0].value.split(".")[0] == "server":
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in {"import_module", "__import__"}:
                hits.append(node.lineno)
    return hits


@pytest.mark.parametrize("path", sorted(SCRIPTS.rglob("*.py")), ids=lambda p: p.relative_to(SCRIPTS).as_posix())
def test_scripts_module_never_imports_server(path: Path) -> None:
    assert server_imports(path.read_text(encoding="utf-8")) == []


def test_server_import_detector_flags_all_forms() -> None:
    assert server_imports("import server\n") == [1]
    assert server_imports("import server.config as c\n") == [1]
    assert server_imports("from server import db\n") == [1]
    assert server_imports("from server.asgi import app\n") == [1]
    assert server_imports("import importlib\nm = importlib.import_module('server.db')\n") == [2]
    assert server_imports("m = __import__('server')\n") == [1]
    assert server_imports("from scripts import server_like\nimport servers\nfrom . import server\n") == []


def test_server_package_and_deploy_skeleton_exist() -> None:
    assert (PLUGIN_ROOT / "server" / "__init__.py").is_file()
    assert (PLUGIN_ROOT / "deploy").is_dir()
    assert (PLUGIN_ROOT / "tests" / "server" / "__init__.py").is_file()


def test_requirements_server_extends_base_and_lists_server_deps() -> None:
    lines = [ln.strip() for ln in (PLUGIN_ROOT / "requirements-server.txt").read_text(encoding="utf-8").splitlines()]
    active = [ln for ln in lines if ln and not ln.startswith("#")]
    assert active[0] == "-r requirements.txt"
    names = {ln.split("[")[0].split(">")[0].split("=")[0].split("<")[0].split("~")[0].strip().lower() for ln in active[1:]}
    assert names == {"starlette", "uvicorn", "authlib", "itsdangerous", "httpx", "psycopg", "psycopg_pool",
                     "alembic", "sqlalchemy", "boto3"}
    assert all(any(op in ln for op in (">=", "~=", "==")) for ln in active[1:]), "버전 범위 없는 항목"


def test_base_requirements_do_not_pull_server_deps() -> None:
    text = (PLUGIN_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for name in ("starlette", "uvicorn", "psycopg", "boto3", "sqlalchemy"):
        assert name not in text


def _tracked_env_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=PLUGIN_ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines()
            if Path(p).name == ".env" or (Path(p).name.startswith(".env.") and Path(p).name != ".env.example")]


@pytest.mark.skipif(shutil.which("git") is None, reason="git 없음")
def test_no_env_file_is_tracked() -> None:
    assert _tracked_env_files() == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git 없음")
@pytest.mark.parametrize("name", [".env", "deploy/server/.env", "deploy/local/.env"])
def test_env_files_are_gitignored(name: str) -> None:
    result = subprocess.run(["git", "check-ignore", "-q", name], cwd=PLUGIN_ROOT)
    assert result.returncode == 0, f"{name} 가 .gitignore 에 걸리지 않는다"


@pytest.mark.skipif(shutil.which("git") is None, reason="git 없음")
def test_env_example_is_not_ignored() -> None:
    result = subprocess.run(["git", "check-ignore", "-q", "deploy/server/.env.example"], cwd=PLUGIN_ROOT)
    assert result.returncode == 1
