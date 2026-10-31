"""`anthropic-api` 엔진: Anthropic Messages API를 표준 라이브러리 HTTP(urllib)로 부른다.

키는 환경 변수 ANTHROPIC_API_KEY에서만 읽고, 파일·로그·오류 메시지에 남기지 않는다.
"""
from __future__ import annotations

import base64
import json
import os
import posixpath
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from scripts import ctx
from scripts.claude_cli import parse_result_text
from scripts.common import ScriptError, scrub

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 8000
TOOL_NAME = "respond"
MAX_IMAGES = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
RESPOND_INSTRUCTION = "반드시 respond 도구 하나로만 답하세요."
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
TEXT_SUFFIXES = {".md", ".txt"}


def _api_key() -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise ScriptError("llm_not_configured", f"{KEY_ENV} 환경 변수가 없습니다. API 키를 환경 변수로 설정하세요")
    return key


def check() -> None:
    _api_key()


def _skipped(name: str) -> dict[str, Any]:
    return {"type": "text", "text": f"[건너뜀: {name} — 크기/개수 한도 초과, 내용을 추측하지 마세요]"}


def _file_blocks(files: list[str] | None) -> list[dict[str, Any]]:
    """files는 저장소 키. 실제 파일은 `ctx.local_file`로 열어 읽는다(서버판은 임시 다운로드)."""
    blocks: list[dict[str, Any]] = []
    images = 0
    here = ctx.here()
    for key in files or []:
        name = posixpath.basename(key)
        suffix = posixpath.splitext(name)[1].lower()
        if suffix in IMAGE_TYPES:
            if images >= MAX_IMAGES:
                blocks.append(_skipped(name))
                continue
            try:
                with here.local_file(key) as path:
                    if path.stat().st_size > MAX_IMAGE_BYTES:
                        blocks.append(_skipped(name))
                        continue
                    data = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as e:
                raise ScriptError("llm_failed", f"파일을 읽지 못했습니다: {name} ({e.__class__.__name__})") from e
            images += 1
            blocks.append({"type": "text", "text": f"[이미지: {name}]"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[suffix], "data": data}})
        elif suffix in TEXT_SUFFIXES:
            try:
                with here.local_file(key) as path:
                    text = path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_CHARS]
            except OSError as e:
                raise ScriptError("llm_failed", f"파일을 읽지 못했습니다: {name} ({e.__class__.__name__})") from e
            blocks.append({"type": "text", "text": f"[파일: {name}]\n{text}"})
    return blocks


def _build_body(
    rules: list[Path], payload: dict, task: str, schema: dict | None, files: list[str] | None, options: dict,
) -> dict[str, Any]:
    system = "\n\n".join(Path(p).read_text(encoding="utf-8") for p in rules)
    text = f"## 입력\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n## 할 일\n{task}"
    text += f"\n\n{RESPOND_INSTRUCTION}" if schema is not None else "\n\n출력은 JSON 하나만."
    body: dict[str, Any] = {
        "model": options.get("model") or DEFAULT_MODEL,
        "max_tokens": options.get("max_tokens") or DEFAULT_MAX_TOKENS,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}, *_file_blocks(files)]}],
    }
    if system:
        body["system"] = system
    if schema is not None:
        body["tools"] = [{"name": TOOL_NAME, "description": "Return the result as structured JSON.", "input_schema": schema}]
        body["tool_choice"] = {"type": "auto"}
    return body


def _scrub(text: str, key: str) -> str:
    return scrub(text.replace(key, "***") if key else text)


def _http_error(e: urllib.error.HTTPError, key: str) -> ScriptError:
    reason = ""
    try:
        err = json.loads(e.read().decode("utf-8", errors="replace")).get("error")
        if isinstance(err, dict) and isinstance(err.get("type"), str):
            reason = err["type"]
    except (ValueError, OSError, AttributeError):
        reason = ""
    detail = f"HTTP {e.code}" + (f", {_scrub(reason, key)}" if reason else "")
    if e.code in (401, 403):
        return ScriptError("llm_auth", f"Anthropic API 인증에 실패했습니다({detail}). ANTHROPIC_API_KEY를 확인하세요")
    return ScriptError("llm_failed", f"Anthropic API 호출이 실패했습니다({detail})")


USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _report_usage(response: Any, options: dict) -> None:
    """응답의 usage를 `options["on_usage"]`로 넘긴다(반환 dict 형식과 무관). 거절된 응답도 넘긴다."""
    on_usage = options.get("on_usage")
    usage = response.get("usage") if isinstance(response, dict) else None
    if on_usage is None or not isinstance(usage, dict):
        return
    on_usage({f: v if isinstance(v := usage.get(f), int) and not isinstance(v, bool) else 0 for f in USAGE_FIELDS})


def _extract(response: Any, schema: dict | None) -> dict:
    content = response.get("content") if isinstance(response, dict) else None
    if not isinstance(content, list):
        raise ScriptError("llm_failed", "Anthropic API 응답 형식이 올바르지 않습니다")
    stop = response.get("stop_reason")
    if stop == "max_tokens":
        raise ScriptError("llm_failed", "응답이 max_tokens에서 잘렸습니다. yt-coach.json의 llm.max_tokens를 늘리세요")
    if stop == "refusal":
        raise ScriptError("llm_failed", "모델이 요청을 거절했습니다")
    if schema is not None:
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                return block["input"]
        # tool_choice가 auto라 모델이 텍스트로 답할 수 있다: 텍스트 JSON으로 대체
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    try:
        return parse_result_text(text)
    except ScriptError as e:
        raise ScriptError("llm_bad_json" if e.code == "llm_bad_json" else "llm_failed", e.message) from e


def ask(
    rules: list[Path],
    payload: dict,
    task: str,
    *,
    schema: dict | None = None,
    files: list[str] | None = None,
    timeout: int = 600,
    options: dict | None = None,
) -> dict:
    key = _api_key()
    body = _build_body(rules, payload, task, schema, files, options or {})
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise _http_error(e, key) from None
    except TimeoutError as e:
        raise ScriptError("llm_failed", f"Anthropic API 응답 시간 초과({timeout}초)") from e
    except (urllib.error.URLError, OSError) as e:
        raise ScriptError("llm_failed", _scrub(f"Anthropic API에 연결하지 못했습니다: {e}", key)) from None
    try:
        response = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise ScriptError("llm_failed", "Anthropic API 응답을 읽지 못했습니다") from e
    _report_usage(response, options or {})
    return _extract(response, schema)
