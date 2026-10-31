"""LLM 엔진 계층: 화면·라이브러리가 Claude를 부르는 모든 호출의 단일 입구.

엔진은 작업 폴더 `yt-coach.json`의 `{"llm": {"engine": ..., "model": ..., "max_tokens": ...}}`로 고른다.
파일이 없으면 `claude-cli`(기존 동작 그대로). 환경 변수 `YTC_LLM_ENGINE`·`YTC_LLM_MODEL`·`YTC_LLM_MAX_TOKENS`가
비어 있지 않게 있으면 파일보다 우선한다(컨테이너가 yt-coach.json 없이 엔진을 고정한다).
새 엔진은 `check()`와 `ask(...)`를 가진 모듈을 만들어 ENGINES에 등록하면 끝난다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from scripts import claude_cli, ctx, keys, limits, llm_anthropic
from scripts.common import ScriptError

CONFIG_FILE = keys.CONFIG
DEFAULT_ENGINE = "claude-cli"
ENV_ENGINE = "YTC_LLM_ENGINE"
ENV_MODEL = "YTC_LLM_MODEL"
ENV_MAX_TOKENS = "YTC_LLM_MAX_TOKENS"

ENGINES: dict[str, ModuleType] = {
    "claude-cli": claude_cli,
    "anthropic-api": llm_anthropic,
}


def _not_configured(message: str) -> ScriptError:
    return ScriptError("llm_not_configured", message)


def _read_llm_section() -> dict[str, Any]:
    objects = ctx.here().objects
    if not objects.exists(CONFIG_FILE):
        return {}
    try:
        data = json.loads(objects.get(CONFIG_FILE).decode("utf-8"))
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
    model = (os.environ.get(ENV_MODEL) or "").strip() or section.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise _not_configured('llm.model은 비어 있지 않은 문자열이어야 합니다')
        result["model"] = model.strip()
    max_tokens = section.get("max_tokens")
    env_tokens = (os.environ.get(ENV_MAX_TOKENS) or "").strip()
    if env_tokens:
        try:
            max_tokens = int(env_tokens)
        except ValueError:
            max_tokens = 0
        if max_tokens <= 0:
            raise _not_configured(f"{ENV_MAX_TOKENS}는 양의 정수여야 합니다: {env_tokens!r}")
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


def _file_keys(files: list[str] | None) -> list[str] | None:
    """첨부 목록(저장소 키)을 검증한다(keys.check: 상대 POSIX 경로, `..`·역슬래시·드라이브 안 됨 → bad_path).
    키가 아닌 값(Path 등)은 프로그램 오류(TypeError)."""
    if files is None:
        return None
    for f in files:
        if not isinstance(f, str):
            raise TypeError(f"첨부 파일은 저장소 키(str)여야 합니다: {f!r}")
    return [keys.check(f) for f in files]


def ask(
    rules: list[Path],
    payload: dict,
    task: str,
    *,
    schema: dict | None = None,
    files: list[str] | None = None,
    timeout: int = 600,
    max_tokens: int | None = None,
) -> dict:
    """files: 첨부할 파일의 저장소 키(작업 폴더 기준 상대 경로)."""
    engine, cfg = _engine()
    options = {k: v for k, v in cfg.items() if k != "engine"}
    if max_tokens is not None:              # 호출 단위 덮어쓰기(config의 max_tokens보다 우선)
        options["max_tokens"] = max_tokens
    file_keys = _file_keys(files)
    meter = ctx.here().usage
    meter.check()                           # 한도 초과면 엔진을 부르기 전에 거절(로컬판 NullMeter는 no-op)
    seen: list[dict[str, int]] = []
    options["on_usage"] = seen.append
    model = str(options.get("model") or getattr(engine, "DEFAULT_MODEL", ""))
    try:
        with limits.slot("llm"):
            result = engine.ask(rules, payload, task, schema=schema, files=file_keys, timeout=timeout, options=options)
    except BaseException:
        for usage in seen:                  # 실패 응답도 토큰은 썼다
            meter.record(model, usage, False)
        raise
    for usage in seen:
        meter.record(model, usage, True)
    return result


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
