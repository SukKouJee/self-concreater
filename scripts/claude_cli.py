# Step0 확인: 구조화 출력은 JSON의 "structured_output" 키(result는 문자열), 도구 전부 끄기는 `--tools ""`, Read만 허용은 `--tools Read`.
"""`claude -p` 호출기: 스킬 규칙 파일 + 입력 JSON + 할 일을 stdin으로 넘기고 JSON 답을 받는다."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from scripts.common import PLUGIN_ROOT, ScriptError, work_root

SKILLS_DIR = PLUGIN_ROOT / "skills"

_LOGIN_HINTS = ("login", "/login", "authenticate")
_LOGIN_MESSAGE = "Claude에 로그인되어 있지 않습니다. 터미널에서 claude를 실행해 로그인하세요"
# 사용자 전역 설정(플러그인·훅·스킬 목록·MCP 서버)을 싣지 않도록 고정한다. 규칙은 stdin 프롬프트로 간다.
# 측정(2026-10-24, 작은 프로브): 기본 37.1k 입력 토큰 $0.297 → 아래 플래그 1.8k 토큰 $0.014. --bare는 OAuth를 못 써서 쓰지 않는다.
SYSTEM_PROMPT = "You are a JSON-only analysis helper for a YouTube creator coaching tool. Follow the rules in the user message."
_ISOLATION_FLAGS = [
    "--strict-mcp-config",
    "--setting-sources", "project,local",
    "--disable-slash-commands",
    "--system-prompt", SYSTEM_PROMPT,
]
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def find_claude() -> str:
    path = shutil.which("claude")
    if not path:
        raise ScriptError(
            "claude_missing",
            "Claude Code CLI(claude)를 찾을 수 없습니다. 설치 후 터미널에서 claude를 한 번 실행해 로그인하세요",
        )
    return path


def check() -> None:
    """엔진 준비 확인(llm.check에서 부른다): claude가 없으면 claude_missing."""
    find_claude()


def _display_path(path: Path) -> str:
    try:
        return Path(path).relative_to(work_root()).as_posix()
    except ValueError:
        return str(path)


def _build_prompt(rules: list[Path], payload: dict, task: str, files: list[Path] | None = None) -> str:
    rule_texts = [Path(p).read_text(encoding="utf-8") for p in rules]
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    if not files:
        parts = [*rule_texts, f"## 입력\n```json\n{body}\n```\n\n## 할 일\n{task}\n\n출력은 JSON 하나만."]
        return "\n\n".join(parts)
    listing = "\n".join(f"- {_display_path(f)}" for f in files)
    parts = [
        *rule_texts,
        f"## 입력\n```json\n{body}\n```",
        f"## 읽을 파일\n규칙이 요구할 때만 Read 도구로 여세요.\n{listing}",
        f"## 할 일\n{task}\n\n출력은 JSON 하나만.",
    ]
    return "\n\n".join(parts)


def _parse_result_text(text: str) -> dict:
    text = _FENCE_RE.sub(lambda m: m.group(1), text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ScriptError("claude_failed", f"Claude 응답에서 JSON을 찾지 못했습니다: {text[:200]}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ScriptError("claude_failed", f"Claude 응답 JSON을 읽지 못했습니다: {e}") from e
    if not isinstance(data, dict):
        raise ScriptError("claude_failed", "Claude 응답이 JSON 객체가 아닙니다")
    return data


def _interpret(proc: subprocess.CompletedProcess[str]) -> dict:
    try:
        envelope = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as e:
        detail = (proc.stderr or proc.stdout or "")[:200]
        raise ScriptError("claude_failed", f"Claude 호출이 실패했습니다(종료 코드 {proc.returncode}): {detail}") from e
    if not isinstance(envelope, dict):
        raise ScriptError("claude_failed", f"예상치 못한 Claude 출력: {str(proc.stdout)[:200]}")
    result = str(envelope.get("result") or "")
    if envelope.get("is_error"):
        if any(h in result.lower() for h in _LOGIN_HINTS):
            raise ScriptError("claude_login", _LOGIN_MESSAGE)
        raise ScriptError("claude_failed", result[:200] or "Claude가 오류를 돌려줬습니다")
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    return _parse_result_text(result)


def ask(
    rules: list[Path],
    payload: dict,
    task: str,
    *,
    schema: dict | None = None,
    tools: list[str] | None = None,
    files: list[Path] | None = None,
    timeout: int = 600,
    options: dict | None = None,  # 엔진 공통 인터페이스용(claude-cli는 model 등을 쓰지 않는다)
) -> dict:
    if files and not tools:
        tools = ["Read"]
    cmd = [find_claude(), "-p", "--output-format", "json", "--no-session-persistence", *_ISOLATION_FLAGS]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    # 빈 문자열이면 도구 전부 끔
    cmd += ["--tools", ",".join(tools) if tools else ""]
    try:
        proc = subprocess.run(
            cmd,
            input=_build_prompt(rules, payload, task, files),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_root(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise ScriptError("claude_failed", f"Claude 응답 시간 초과({timeout}초)") from e
    except OSError as e:
        raise ScriptError("claude_missing", f"Claude Code CLI(claude)를 실행하지 못했습니다: {e}") from e
    return _interpret(proc)


def ask_validated(
    rules: list[Path],
    payload: dict,
    task: str,
    validate: Callable[[dict], Any],
    **kw: Any,
) -> tuple[dict, Any]:
    data = ask(rules, payload, task, **kw)
    try:
        return data, validate(data)
    except ScriptError as first:
        if not first.code.startswith("invalid_"):
            raise
        retry_task = f"{task}\n\n이전 출력이 거절됐습니다: {first.message}\n고쳐서 다시 출력하세요."
    data = ask(rules, payload, retry_task, **kw)
    return data, validate(data)
