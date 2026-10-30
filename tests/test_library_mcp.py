"""공용 라이브러리 MCP 서버: 하위 프로세스로 띄워 JSON-RPC(stdio)로 구동한다. 원격은 로컬 bare 저장소."""
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from scripts import library
from tests.test_library import CAT, make_draft, make_home, remote  # noqa: F401  (remote는 fixture)

SERVER = Path(__file__).resolve().parent.parent / "scripts" / "library_mcp.py"
TIMEOUT = 60
PLUGIN_VERSION = json.loads((SERVER.parent.parent / ".claude-plugin" / "plugin.json").read_text("utf-8"))["version"]
VENV = SERVER.parent.parent / ".venv"


class McpClient:
    def __init__(self, home: Path, *, set_home_env: bool = True, argv: list[str] | None = None,
                 extra_env: dict[str, str] | None = None) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(extra_env or {})}
        env.pop("YT_COACH_HOME", None)
        if set_home_env:
            env["YT_COACH_HOME"] = str(home)
        self.home = home
        self.proc = subprocess.Popen(argv or [sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=str(home), env=env)
        self.raw_stdout: list[bytes] = []
        self._lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self.proc.stderr.read, daemon=True).start()
        self._next_id = 0

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.raw_stdout.append(line)
            self._lines.put(line)
        self._lines.put(b"")

    def send_raw(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def notify(self, method: str) -> None:
        self.send_raw(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")

    def recv(self) -> dict[str, Any]:
        line = self._lines.get(timeout=TIMEOUT)
        assert line, "서버가 응답 없이 종료됨"
        return json.loads(line.decode("utf-8"))

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        reply = self.recv()
        assert reply["id"] == self._next_id
        return reply

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[bool, Any]:
        reply = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        result = reply["result"]
        assert result["content"][0]["type"] == "text"
        return result["isError"], json.loads(result["content"][0]["text"])

    def close(self) -> int:
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()      # 내가 띄운 프로세스만 끈다
            raise


@pytest.fixture
def client(tmp_path, monkeypatch, remote):  # noqa: F811
    home = make_home(tmp_path, monkeypatch, "home_mcp")
    library.connect(str(remote), "Alice")
    c = McpClient(home)
    yield c
    if c.proc.poll() is None:
        c.proc.kill()
        c.proc.wait()


def test_initialize_echoes_client_protocol_and_reports_server_info(client):
    reply = client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "1"}})
    result = reply["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"] == {"name": "yt-coach-library", "version": PLUGIN_VERSION}


def test_initialize_defaults_protocol_when_client_gives_none(client):
    assert client.request("initialize", {})["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_has_all_tools_with_schemas_and_confirm_warning(client):
    client.request("initialize", {})
    client.notify("notifications/initialized")
    tools = {t["name"]: t for t in client.request("tools/list")["result"]["tools"]}
    assert set(tools) == {"library_status", "library_pull", "library_search", "library_get", "library_my_techniques",
                          "library_share_preview", "library_summarize", "library_share_confirm"}
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
    assert tools["library_get"]["inputSchema"]["required"] == ["id"]
    assert tools["library_share_preview"]["inputSchema"]["required"] == ["kind"]
    assert tools["library_share_confirm"]["inputSchema"]["required"] == ["preview_id"]
    desc = tools["library_share_confirm"]["description"]
    assert "명시적으로 확인받은 뒤에만" in desc and "explicitly confirmed" in desc


def test_notification_gets_no_reply_and_ping_still_answers(client):
    client.notify("notifications/initialized")
    assert client.request("ping")["result"] == {}


def test_share_technique_end_to_end_then_search_and_get(client):
    err, status = client.call("library_status")
    assert not err and status["connected"] is True
    err, mine = client.call("library_my_techniques", {"category": CAT})
    assert not err and "결과 선공개" in {t["name"] for t in mine["techniques"]}

    args = {"kind": "technique", "category": CAT, "technique_kind": "훅", "name": "결과 선공개"}
    err, preview = client.call("library_share_preview", args)
    assert not err and preview["preview_id"]
    err, shared = client.call("library_share_confirm", {"preview_id": preview["preview_id"], "note": "잘 먹힘",
                                                        "worked": True})
    assert not err, shared
    item_id = shared["id"]

    err, pulled = client.call("library_pull")
    assert not err, pulled
    err, found = client.call("library_search", {"query": "결과", "kind": "technique"})
    assert not err and [i["id"] for i in found["items"]] == [item_id]
    err, item = client.call("library_get", {"id": item_id})
    assert not err and item["name"] == "결과 선공개" and item["worked"] is True

    err, again_preview = client.call("library_share_preview", args)
    err, again = client.call("library_share_confirm", {"preview_id": again_preview["preview_id"]})
    assert err is True and again["error"] == "duplicate" and again["shared_id"] == item_id


def test_share_template_preview_from_draft(client):
    path = make_draft(client.home)
    err, preview = client.call("library_share_preview", {"kind": "template", "draft": str(path)})
    assert not err and preview["preview_id"]
    assert "비밀대사ABC" not in json.dumps(preview, ensure_ascii=False)


def test_tool_error_is_iserror_json_without_traceback(client):
    err, payload = client.call("library_get", {"id": "nobody-20260101-abcdef"})
    assert err is True
    assert payload["error"] == "item_not_found" and payload["message"]
    assert "Traceback" not in json.dumps(payload)


@pytest.mark.parametrize("name,args,fragment", [
    ("library_get", {}, "id"),
    ("library_get", {"id": 5}, "id"),
    ("library_search", {"kind": "bogus"}, "kind"),
    ("library_status", {"x": 1}, "x"),
    ("library_share_preview", {"kind": "technique", "category": CAT}, "technique_kind"),
])
def test_invalid_arguments_are_tool_errors(client, name, args, fragment):
    err, payload = client.call(name, args)
    assert err is True and payload["error"] == "invalid_arguments" and fragment in payload["message"]


def test_unknown_tool_is_invalid_params_and_unknown_method_is_not_found(client):
    assert client.request("tools/call", {"name": "library_nope", "arguments": {}})["error"]["code"] == -32602
    assert client.request("resources/list")["error"]["code"] == -32601


def test_malformed_json_gets_parse_error_and_server_survives(client):
    client.send_raw(b"{not json\n")
    reply = client.recv()
    assert reply["error"]["code"] == -32700 and reply["id"] is None
    client.send_raw(b"\xff\xfe\n")
    assert client.recv()["error"]["code"] == -32700
    client.send_raw(b"[1]\n")
    assert client.recv()["error"]["code"] == -32600
    assert client.request("ping")["result"] == {}
    assert client.proc.poll() is None


def test_stdout_carries_only_json_rpc_lines_and_clean_exit(client):
    client.request("initialize", {})
    client.notify("notifications/initialized")
    client.call("library_status")
    client.send_raw(b"garbage\n")
    client.recv()
    assert client.close() == 0
    assert client.raw_stdout
    for line in client.raw_stdout:
        assert line.endswith(b"\n") and not line.endswith(b"\r\n")
        assert json.loads(line.decode("utf-8"))["jsonrpc"] == "2.0"


def test_confirm_tool_has_destructive_annotations_and_force_new_wording(client):
    tools = {t["name"]: t for t in client.request("tools/list")["result"]["tools"]}
    confirm = tools["library_share_confirm"]
    assert confirm["annotations"]["destructiveHint"] is True and confirm["annotations"]["openWorldHint"] is True
    assert confirm["annotations"]["title"]
    assert "re-confirms" in confirm["inputSchema"]["properties"]["force_new"]["description"]
    assert "may take minutes" in tools["library_summarize"]["description"]


def test_duplicate_error_tells_claude_to_ask_the_user_not_to_retry(client):
    args = {"kind": "technique", "category": CAT, "technique_kind": "훅", "name": "결과 선공개"}
    for _ in range(2):
        _, preview = client.call("library_share_preview", args)
        err, payload = client.call("library_share_confirm", {"preview_id": preview["preview_id"]})
    assert err is True and payload["error"] == "duplicate" and payload["shared_id"]
    assert "사용자에게 알리고 다시 올릴지 물어보세요" in payload["message"]
    assert "force_new" not in payload["message"] and "새 파일로 또" not in payload["message"]


@pytest.mark.parametrize("bad_id", [None, True, {"a": 1}, [1]])
def test_invalid_request_id_gets_invalid_request(client, bad_id):
    client.send_raw(json.dumps({"jsonrpc": "2.0", "id": bad_id, "method": "ping"}).encode() + b"\n")
    reply = client.recv()
    assert reply["error"]["code"] == -32600 and reply["id"] is None
    assert client.request("ping")["result"] == {}


def test_lone_surrogate_method_is_method_not_found_and_server_survives(client):
    client.send_raw(b'{"jsonrpc":"2.0","id":7,"method":"\\ud800"}\n')
    reply = client.recv()
    assert reply["id"] == 7 and reply["error"]["code"] == -32601
    assert client.request("ping")["result"] == {}


def test_work_root_falls_back_to_cwd_without_env(tmp_path, monkeypatch, remote):  # noqa: F811
    home = make_home(tmp_path, monkeypatch, "home_cwd")
    library.connect(str(remote), "Alice")
    c = McpClient(home, set_home_env=False)
    try:
        err, status = c.call("library_status")
        assert not err and status["connected"] is True
    finally:
        c.proc.kill()
        c.proc.wait()


STRAY_RUNNER = """
import os, sys
sys.path.insert(0, {root!r})
from scripts import library, library_mcp
def noisy():
    print("STRAY PRINT")
    os.write(1, b"STRAY FD WRITE\\n")
    return {{"connected": True}}
library.status = noisy
sys.exit(library_mcp.main())
"""


def test_stray_prints_in_a_handler_never_reach_stdout(tmp_path, monkeypatch, remote):  # noqa: F811
    home = make_home(tmp_path, monkeypatch, "home_stray")
    library.connect(str(remote), "Alice")
    runner = STRAY_RUNNER.format(root=str(SERVER.parent.parent))
    c = McpClient(home, argv=[sys.executable, "-c", runner])
    try:
        err, status = c.call("library_status")
        assert not err and status == {"connected": True}
        assert c.close() == 0
    finally:
        if c.proc.poll() is None:
            c.proc.kill()
    assert all(json.loads(line)["jsonrpc"] == "2.0" for line in c.raw_stdout)
    assert b"STRAY" not in b"".join(c.raw_stdout)


def _base_python() -> str | None:
    base = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
    return str(base) if base.is_file() else None


@pytest.mark.skipif(not (VENV.is_dir() and _base_python()), reason="플러그인 .venv와 venv 밖 Python이 필요합니다")
def test_server_reexecs_into_plugin_venv_when_interpreter_lacks_pyyaml(tmp_path):
    # -I -S: site-packages 없이 시작해 pyyaml이 없는 시스템 Python을 흉내 낸다
    probe = subprocess.run([_base_python(), "-I", "-S", "-c", "import yaml"], capture_output=True)
    if probe.returncode == 0:
        pytest.skip("-S로도 pyyaml이 보입니다")
    c = McpClient(tmp_path, argv=[_base_python(), "-I", "-S", str(SERVER)])
    try:
        assert c.request("ping")["result"] == {}
    finally:
        c.proc.kill()
        c.proc.wait()


@pytest.mark.skipif(not _base_python(), reason="venv 밖 Python이 필요합니다")
def test_reexec_guard_stops_the_loop_with_a_clear_exit(tmp_path):
    probe = subprocess.run([_base_python(), "-I", "-S", "-c", "import yaml"], capture_output=True)
    if probe.returncode == 0:
        pytest.skip("-S로도 pyyaml이 보입니다")
    env = {**os.environ, "YTC_MCP_REEXEC": "1"}
    done = subprocess.run([_base_python(), "-I", "-S", str(SERVER)], input=b"", capture_output=True, env=env,
                          timeout=30)
    assert done.returncode == 1 and done.stdout == b""


@pytest.mark.parametrize("set_home_env", [True, False])
def test_startup_log_names_the_work_folder_from_env_or_cwd(tmp_path, set_home_env):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("YT_COACH_HOME", None)
    if set_home_env:
        env["YT_COACH_HOME"] = str(tmp_path)
    done = subprocess.run([sys.executable, str(SERVER)], input=b"", capture_output=True, env=env, cwd=str(tmp_path),
                          timeout=30)
    assert done.returncode == 0
    assert f"시작: 작업 폴더 {tmp_path}" in done.stderr.decode("utf-8")
