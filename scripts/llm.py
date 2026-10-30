"""LLM 엔진 계층: 화면·라이브러리가 Claude를 부르는 모든 호출의 단일 입구.

엔진은 작업 폴더 `yt-coach.json`의 `{"llm": {"engine": ..., "model": ..., "max_tokens": ...}}`로 고른다.
파일이 없으면 `claude-cli`(기존 동작 그대로). 환경 변수 `YTC_LLM_ENGINE`이 있으면 우선한다.
새 엔진은 `check()`와 `ask(...)`를 가진 모듈을 만들어 ENGINES에 등록하면 끝난다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from scripts import claude_cli, llm_anthropic
from scripts.common import ScriptError, work_root

CONFIG_FILE = "yt-coach.json"
DEFAULT_ENGINE = "claude-cli"
ENV_ENGINE = "YTC_LLM_ENGINE"

ENGINES: dict[str, ModuleType] = {
    "claude-cli": claude_cli,
    "anthropic-api": llm_anthropic,
}


def _not_configured(message: str) -> ScriptError:
    return ScriptError("llm_not_configured", message)


def _read_llm_section() -> dict[str, Any]:
    path = work_root() / CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise _not_configured(f"{CONFIG_FILE}을 읽지 못했습니다(올바른 JSON이어야 합니다): {e}") from e
    if not isinstance(data, dict):
        raise _not_configured(f"{CONFIG_FILE}의 최상위는 JSON 객체여야 합니다")
    section = data.get("llm", {})
    if not isinstance(section, dict):
        raise _not_configured(f'{CONFIG_FILE}의 "llm"은 JSON 객체여야 합니다')
    return section


def config() -> dict[str, Any]:
    """{"engine": str, "model"?: str, "max_tokens"?: int}. 잘못된 설정은 llm_not_configured."""
    section = _read_llm_section()
    engine = os.environ.get(ENV_ENGINE) or section.get("engine") or DEFAULT_ENGINE
    if not isinstance(engine, str) or engine not in ENGINES:
        names = ", ".join(ENGINES)
        raise _not_configured(f"알 수 없는 LLM 엔진입니다: {engine!r} (사용 가능: {names})")
    result: dict[str, Any] = {"engine": engine}
    model = section.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise _not_configured('llm.model은 비어 있지 않은 문자열이어야 합니다')
        result["model"] = model.strip()
    max_tokens = section.get("max_tokens")
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise _not_configured("llm.max_tokens는 양의 정수여야 합니다")
        result["max_tokens"] = max_tokens
    return result


def _engine() -> tuple[ModuleType, dict[str, Any]]:
    cfg = config()
    return ENGINES[cfg["engine"]], cfg


def check() -> dict[str, Any]:
    """엔진이 준비됐는지 확인한다. 실패하면 ScriptError(claude_missing / llm_not_configured 등)."""
    engine, cfg = _engine()
    engine.check()
    return {"engine": cfg["engine"], "ready": True}


def ask(
    rules: list[Path],
    payload: dict,
    task: str,
    *,
    schema: dict | None = None,
    files: list[Path] | None = None,
    timeout: int = 600,
    max_tokens: int | None = None,
) -> dict:
    engine, cfg = _engine()
    options = {k: v for k, v in cfg.items() if k != "engine"}
    if max_tokens is not None:              # 호출 단위 덮어쓰기(config의 max_tokens보다 우선)
        options["max_tokens"] = max_tokens
    return engine.ask(rules, payload, task, schema=schema, files=files, timeout=timeout, options=options)


def ask_validated(
    rules: list[Path],
    payload: dict,
    task: str,
    validate: Callable[[dict], Any],
    **kw: Any,
) -> tuple[dict, Any]:
    """검증이 `invalid_*` 오류를 내면 이유를 붙여 한 번만 다시 묻는다."""
    data = ask(rules, payload, task, **kw)
    try:
        return data, validate(data)
    except ScriptError as first:
        if not first.code.startswith("invalid_"):
            raise
        retry_task = f"{task}\n\n이전 출력이 거절됐습니다: {first.message}\n고쳐서 다시 출력하세요."
    data = ask(rules, payload, retry_task, **kw)
    return data, validate(data)
