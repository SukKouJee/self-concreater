import json
import subprocess

import pytest

from scripts import claude_cli
from scripts.common import ScriptError


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "C:/bin/claude.exe")
    return tmp_path


class Fake:
    """subprocess.run 대역: 응답을 차례로 돌려주고 호출 기록을 남긴다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def _proc(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(["claude"], returncode, stdout, stderr)


def _ok(obj):
    return _proc(json.dumps({"is_error": False, "structured_output": obj, "result": ""}))


def _install(monkeypatch, fake):
    monkeypatch.setattr(claude_cli.subprocess, "run", fake)
    return fake


def test_find_claude_missing(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    with pytest.raises(ScriptError) as e:
        claude_cli.find_claude()
    assert e.value.code == "claude_missing"


def test_ask_uses_stdin_and_flags(home, monkeypatch):
    rule = home / "rule.md"
    rule.write_text("규칙본문XYZ", encoding="utf-8")
    fake = _install(monkeypatch, Fake(_ok({"a": 1})))

    out = claude_cli.ask([rule], {"k": "값"}, "할일ABC", schema={"type": "object"}, tools=["Read"])

    cmd, kw = fake.calls[0]
    assert out == {"a": 1}
    assert cmd[0] == "C:/bin/claude.exe"
    assert "-p" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert "규칙본문XYZ" in kw["input"] and "할일ABC" in kw["input"] and '"k"' in kw["input"]
    assert kw["cwd"] == home
    assert kw["encoding"] == "utf-8"
    assert kw["errors"] == "replace"


def test_ask_isolates_from_user_context(home, monkeypatch):
    fake = _install(monkeypatch, Fake(_ok({"a": 1})))
    claude_cli.ask([], {}, "t", tools=["Read"])
    cmd, _ = fake.calls[0]
    assert "--strict-mcp-config" in cmd
    assert "--disable-slash-commands" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--system-prompt") + 1] == claude_cli.SYSTEM_PROMPT
    assert "--bare" not in cmd


def test_ask_without_tools_disables_all_and_omits_schema(home, monkeypatch):
    fake = _install(monkeypatch, Fake(_ok({"a": 1})))
    claude_cli.ask([], {}, "t")
    cmd, _ = fake.calls[0]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--json-schema" not in cmd


def test_ask_prefers_structured_output(home, monkeypatch):
    body = {"is_error": False, "structured_output": {"a": 1}, "result": '{"a": 2}'}
    _install(monkeypatch, Fake(_proc(json.dumps(body))))
    assert claude_cli.ask([], {}, "t") == {"a": 1}


def test_ask_parses_fenced_result(home, monkeypatch):
    body = {"is_error": False, "result": '설명\n```json\n{"a": 3}\n```'}
    _install(monkeypatch, Fake(_proc(json.dumps(body))))
    assert claude_cli.ask([], {}, "t") == {"a": 3}


def test_ask_unparseable_result(home, monkeypatch):
    _install(monkeypatch, Fake(_proc(json.dumps({"is_error": False, "result": "JSON 없음"}))))
    with pytest.raises(ScriptError) as e:
        claude_cli.ask([], {}, "t")
    assert e.value.code == "claude_failed"


def test_ask_is_error_login_message(home, monkeypatch):
    _install(monkeypatch, Fake(_proc(json.dumps({"is_error": True, "result": "Please run /login"}))))
    with pytest.raises(ScriptError) as e:
        claude_cli.ask([], {}, "t")
    assert e.value.code == "claude_login"
    assert "로그인" in e.value.message


def test_ask_is_error_other_message(home, monkeypatch):
    _install(monkeypatch, Fake(_proc(json.dumps({"is_error": True, "result": "boom"}))))
    with pytest.raises(ScriptError) as e:
        claude_cli.ask([], {}, "t")
    assert e.value.code == "claude_failed" and "boom" in e.value.message


def test_ask_timeout(home, monkeypatch):
    _install(monkeypatch, Fake(subprocess.TimeoutExpired("claude", 1)))
    with pytest.raises(ScriptError) as e:
        claude_cli.ask([], {}, "t", timeout=1)
    assert e.value.code == "claude_failed" and "시간 초과" in e.value.message


def test_ask_non_json_stdout(home, monkeypatch):
    _install(monkeypatch, Fake(_proc("not json", returncode=1, stderr="crashed")))
    with pytest.raises(ScriptError) as e:
        claude_cli.ask([], {}, "t")
    assert e.value.code == "claude_failed" and "crashed" in e.value.message


@pytest.mark.parametrize("exc", [FileNotFoundError("claude"), PermissionError("denied"), OSError("boom")])
def test_ask_spawn_failure_is_claude_missing(home, monkeypatch, exc):
    _install(monkeypatch, Fake(exc))
    with pytest.raises(ScriptError) as e:
        claude_cli.ask([], {}, "t")
    assert e.value.code == "claude_missing"
