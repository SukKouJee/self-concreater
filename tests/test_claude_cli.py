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
    assert e.value.code == "claude_failed"
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


def _validator(data):
    if "y" not in data:
        raise ScriptError("invalid_x", "필드 y 없음")
    return data["y"]


def test_ask_validated_retries_once_with_message(home, monkeypatch):
    fake = _install(monkeypatch, Fake(_ok({"z": 1}), _ok({"y": 5})))
    data, checked = claude_cli.ask_validated([], {}, "할일", _validator)
    assert (data, checked) == ({"y": 5}, 5)
    assert "이전 출력이 거절됐습니다: 필드 y 없음" in fake.calls[1][1]["input"]
    assert "이전 출력이 거절됐습니다" not in fake.calls[0][1]["input"]


def test_ask_validated_fails_twice_raises_original(home, monkeypatch):
    fake = _install(monkeypatch, Fake(_ok({"z": 1}), _ok({"z": 2})))
    with pytest.raises(ScriptError) as e:
        claude_cli.ask_validated([], {}, "t", _validator)
    assert e.value.code == "invalid_x"
    assert len(fake.calls) == 2


def test_ask_validated_does_not_retry_other_errors(home, monkeypatch):
    def validate(data):
        raise ScriptError("conti_changed", "바뀜")

    fake = _install(monkeypatch, Fake(_ok({"y": 1}), _ok({"y": 1})))
    with pytest.raises(ScriptError) as e:
        claude_cli.ask_validated([], {}, "t", validate)
    assert e.value.code == "conti_changed"
    assert len(fake.calls) == 1
