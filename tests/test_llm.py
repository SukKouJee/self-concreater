"""LLM 엔진 계층: 설정, 점검, 위임, 재시도, anthropic-api 엔진(urllib 가짜, 실제 네트워크·키 없음)."""
import io
import json
import subprocess
import urllib.error

import pytest

from scripts import claude_cli, llm, llm_anthropic
from scripts.common import ScriptError

FAKE_KEY = "sk-ant-TEST-NOT-A-REAL-KEY"


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    monkeypatch.delenv("YTC_LLM_ENGINE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def _write_config(home, data):
    text = data if isinstance(data, str) else json.dumps(data)
    (home / "yt-coach.json").write_text(text, encoding="utf-8")


# ---- config -----------------------------------------------------------------------

def test_config_defaults_to_claude_cli_without_file():
    assert llm.config() == {"engine": "claude-cli"}


def test_config_reads_engine_model_and_max_tokens_from_file(home):
    _write_config(home, {"llm": {"engine": "anthropic-api", "model": "m-1", "max_tokens": 123}})
    assert llm.config() == {"engine": "anthropic-api", "model": "m-1", "max_tokens": 123}


def test_config_env_overrides_file(home, monkeypatch):
    _write_config(home, {"llm": {"engine": "claude-cli"}})
    monkeypatch.setenv("YTC_LLM_ENGINE", "anthropic-api")
    assert llm.config()["engine"] == "anthropic-api"


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    {"llm": "claude-cli"},
    {"llm": {"engine": "gpt-9"}},
    {"llm": {"engine": 3}},
    {"llm": {"model": ""}},
    {"llm": {"max_tokens": 0}},
    {"llm": {"max_tokens": True}},
])
def test_config_invalid_is_llm_not_configured(home, content):
    _write_config(home, content)
    with pytest.raises(ScriptError) as e:
        llm.config()
    assert e.value.code == "llm_not_configured" and e.value.message


def test_config_unknown_env_engine_is_llm_not_configured(monkeypatch):
    monkeypatch.setenv("YTC_LLM_ENGINE", "nope")
    with pytest.raises(ScriptError) as e:
        llm.config()
    assert e.value.code == "llm_not_configured"


# ---- check ------------------------------------------------------------------------

def test_check_claude_cli_missing(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    with pytest.raises(ScriptError) as e:
        llm.check()
    assert e.value.code == "claude_missing"


def test_check_claude_cli_ready(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "C:/bin/claude.exe")
    assert llm.check() == {"engine": "claude-cli", "ready": True}


def test_check_anthropic_requires_key(monkeypatch):
    monkeypatch.setenv("YTC_LLM_ENGINE", "anthropic-api")
    with pytest.raises(ScriptError) as e:
        llm.check()
    assert e.value.code == "llm_not_configured"
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    assert llm.check() == {"engine": "anthropic-api", "ready": True}


# ---- ask 위임 ---------------------------------------------------------------------

def _cli_ok(obj):
    return subprocess.CompletedProcess(["claude"], 0, json.dumps({"is_error": False, "structured_output": obj, "result": ""}), "")


def test_ask_delegates_to_claude_cli_and_files_enable_read_tool(home, monkeypatch):
    rule = home / "rule.md"
    rule.write_text("규칙", encoding="utf-8")
    img = home / "workspace" / "frames" / "a.jpg"
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _cli_ok({"ok": 1})

    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "C:/bin/claude.exe")
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)

    assert llm.ask([rule], {"k": 1}, "할일", files=[img]) == {"ok": 1}

    cmd, kw = calls[0]
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert "workspace/frames/a.jpg" in kw["input"]


def test_ask_without_files_keeps_tools_off_and_prompt_unchanged(home, monkeypatch):
    rule = home / "rule.md"
    rule.write_text("규칙", encoding="utf-8")
    calls = []
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "C:/bin/claude.exe")
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or _cli_ok({"ok": 1}))

    llm.ask([rule], {"k": 1}, "할일")

    cmd, kw = calls[0]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "읽을 파일" not in kw["input"]
    assert kw["input"] == claude_cli._build_prompt([rule], {"k": 1}, "할일")


# ---- ask_validated ----------------------------------------------------------------

def _scripted_ask(monkeypatch, replies):
    seen = []

    def fake(rules, payload, task, **kw):
        seen.append(task)
        return replies.pop(0)

    monkeypatch.setattr(llm, "ask", fake)
    return seen


def _validator(data):
    if data.get("v") != "good":
        raise ScriptError("invalid_thing", "고쳐야 함")
    return data["v"]


def test_ask_validated_retries_once_on_invalid(monkeypatch):
    seen = _scripted_ask(monkeypatch, [{"v": "bad"}, {"v": "good"}])
    data, value = llm.ask_validated([], {}, "T", _validator)
    assert value == "good" and len(seen) == 2
    assert "고쳐야 함" in seen[1] and seen[1].startswith("T")


def test_ask_validated_second_failure_raises(monkeypatch):
    _scripted_ask(monkeypatch, [{"v": "bad"}, {"v": "bad"}])
    with pytest.raises(ScriptError) as e:
        llm.ask_validated([], {}, "T", _validator)
    assert e.value.code == "invalid_thing"


def test_ask_validated_does_not_retry_other_errors(monkeypatch):
    seen = _scripted_ask(monkeypatch, [{"v": 1}])

    def boom(data):
        raise ScriptError("other", "x")

    with pytest.raises(ScriptError) as e:
        llm.ask_validated([], {}, "T", boom)
    assert e.value.code == "other" and len(seen) == 1


# ---- anthropic-api 엔진 -------------------------------------------------------------

class FakeResponse:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    @property
    def body(self):
        return json.loads(self.requests[0].data.decode("utf-8"))


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("YTC_LLM_ENGINE", "anthropic-api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

    def install(outcome):
        fake = FakeUrlopen(outcome)
        monkeypatch.setattr(llm_anthropic.urllib.request, "urlopen", fake)
        return fake

    return install


def _tool_reply(data):
    return FakeResponse({"content": [{"type": "tool_use", "name": "respond", "input": data}]})


def _text_reply(text):
    return FakeResponse({"content": [{"type": "text", "text": text}]})


def _http_error(status, body):
    return urllib.error.HTTPError(llm_anthropic.API_URL, status, "x", {}, io.BytesIO(json.dumps(body).encode("utf-8")))


def test_anthropic_request_shape_with_schema_and_files(home, api):
    rule = home / "rule.md"
    rule.write_text("규칙본문", encoding="utf-8")
    img = home / "f.png"
    img.write_bytes(b"\x89PNG-bytes")
    note = home / "ex.md"
    note.write_text("예시 본문", encoding="utf-8")
    fake = api(_tool_reply({"answer": 7}))
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}

    out = llm.ask([rule], {"k": "값"}, "할일ABC", schema=schema, files=[img, note], timeout=33)

    assert out == {"answer": 7}
    req = fake.requests[0]
    headers = {k.lower(): v for k, v in req.header_items()}
    assert req.full_url == "https://api.anthropic.com/v1/messages" and req.get_method() == "POST"
    assert headers["x-api-key"] == FAKE_KEY
    assert headers["anthropic-version"] == "2023-06-01" and headers["content-type"] == "application/json"
    assert fake.timeouts == [33]
    body = fake.body
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 8000
    assert body["system"] == "규칙본문"
    assert body["tools"] == [{"name": "respond", "description": body["tools"][0]["description"], "input_schema": schema}]
    assert body["tool_choice"] == {"type": "tool", "name": "respond"}
    content = body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "user"
    assert "할일ABC" in content[0]["text"] and '"k"' in content[0]["text"]
    image = next(b for b in content if b["type"] == "image")
    assert image["source"]["type"] == "base64" and image["source"]["media_type"] == "image/png"
    assert image["source"]["data"] == "iVBORy1ieXRlcw=="
    assert any(b["type"] == "text" and "예시 본문" in b["text"] for b in content)
    assert FAKE_KEY not in json.dumps(body)


def test_anthropic_uses_config_model_and_max_tokens(home, api):
    _write_config(home, {"llm": {"engine": "anthropic-api", "model": "my-model", "max_tokens": 321}})
    fake = api(_text_reply('{"a": 1}'))
    llm.ask([], {}, "t")
    assert fake.body["model"] == "my-model" and fake.body["max_tokens"] == 321
    assert "tools" not in fake.body and "system" not in fake.body


def test_anthropic_parses_json_from_text_when_no_schema(api):
    api(_text_reply('설명입니다\n```json\n{"a": 1}\n```'))
    assert llm.ask([], {}, "t") == {"a": 1}


def test_anthropic_text_without_json_is_llm_failed(api):
    api(_text_reply("JSON 없음"))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t")
    assert e.value.code == "llm_failed"


def test_anthropic_schema_without_tool_use_is_llm_failed(api):
    api(_text_reply("그냥 글"))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t", schema={"type": "object"})
    assert e.value.code == "llm_failed"


def test_anthropic_limits_images_to_20_and_skips_over_5mb(home, api, monkeypatch):
    files = []
    for i in range(23):
        p = home / f"{i:02d}.jpg"
        p.write_bytes(b"x")
        files.append(p)
    big = home / "big.jpg"
    big.write_bytes(b"x")
    monkeypatch.setattr(llm_anthropic, "MAX_IMAGE_BYTES", 0)   # 모든 이미지가 한도 초과 -> 건너뜀
    fake = api(_text_reply("{}"))
    llm.ask([], {}, "t", files=[big])
    assert not [b for b in fake.body["messages"][0]["content"] if b["type"] == "image"]

    monkeypatch.setattr(llm_anthropic, "MAX_IMAGE_BYTES", 5 * 1024 * 1024)
    fake = api(_text_reply("{}"))
    llm.ask([], {}, "t", files=files)
    images = [b for b in fake.body["messages"][0]["content"] if b["type"] == "image"]
    names = [b["text"] for b in fake.body["messages"][0]["content"] if b["type"] == "text"][1:]
    assert len(images) == 20 and names[0] == "[이미지: 00.jpg]" and names[-1] == "[이미지: 19.jpg]"


@pytest.mark.parametrize("status", [401, 403])
def test_anthropic_auth_error_maps_to_llm_auth(api, status):
    api(_http_error(status, {"error": {"type": "authentication_error", "message": f"bad {FAKE_KEY}"}}))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t")
    assert e.value.code == "llm_auth"
    assert FAKE_KEY not in e.value.message and str(status) in e.value.message


@pytest.mark.parametrize("status,kind", [(500, "api_error"), (429, "rate_limit_error"), (529, "overloaded_error")])
def test_anthropic_other_http_errors_map_to_llm_failed(api, status, kind):
    api(_http_error(status, {"error": {"type": kind, "message": f"secret {FAKE_KEY}"}}))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t")
    assert e.value.code == "llm_failed"
    assert str(status) in e.value.message and kind in e.value.message and FAKE_KEY not in e.value.message


def test_anthropic_http_error_with_unreadable_body_still_maps(api):
    err = urllib.error.HTTPError(llm_anthropic.API_URL, 500, "x", {}, io.BytesIO(b"<html>"))
    api(err)
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t")
    assert e.value.code == "llm_failed" and "500" in e.value.message


def test_anthropic_timeout_is_llm_failed(api):
    api(TimeoutError("timed out"))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t", timeout=5)
    assert e.value.code == "llm_failed" and "5" in e.value.message


def test_anthropic_network_error_is_llm_failed_without_key(api):
    api(urllib.error.URLError(f"dns failure for {FAKE_KEY}"))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t")
    assert e.value.code == "llm_failed" and FAKE_KEY not in e.value.message


def test_anthropic_garbage_response_is_llm_failed(api):
    api(FakeResponse(b"not json"))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t")
    assert e.value.code == "llm_failed"


def test_anthropic_missing_key_is_llm_not_configured(monkeypatch):
    monkeypatch.setenv("YTC_LLM_ENGINE", "anthropic-api")
    called = []
    monkeypatch.setattr(llm_anthropic.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    with pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t")
    assert e.value.code == "llm_not_configured" and not called


def test_anthropic_ask_validated_retry_path_runs_through_engine(api, monkeypatch):
    replies = [_tool_reply({"v": "bad"}), _tool_reply({"v": "good"})]
    calls = []

    def fake(request, timeout=None):
        calls.append(json.loads(request.data.decode("utf-8")))
        return replies.pop(0)

    monkeypatch.setattr(llm_anthropic.urllib.request, "urlopen", fake)
    _, value = llm.ask_validated([], {}, "T", _validator, schema={"type": "object"})
    assert value == "good" and len(calls) == 2
    assert "고쳐야 함" in calls[1]["messages"][0]["content"][0]["text"]
