import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from scripts.common import ScriptError
from scripts.ui import server
from scripts.ui import __main__ as ui_main


class Client:
    def __init__(self, srv, app):
        self.srv = srv
        self.app = app
        self.base = f"http://127.0.0.1:{app.port}"

    def call(self, method, path, body=None, token=True, headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-YTC-Key", self.app.token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, method, path, body=None, **kw):
        status, _, raw = self.call(method, path, body, **kw)
        return status, json.loads(raw)


def _start(app_root, port=0):
    srv, app = server.make_app(app_root, port)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return srv, app, thread


def _stop(srv, thread):
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


@pytest.fixture
def no_browser(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("browser must not open in tests")
    monkeypatch.setattr("webbrowser.open", fail)


@pytest.fixture
def client(monkeypatch, tmp_path, no_browser):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    srv, app, thread = _start(tmp_path)
    yield Client(srv, app)
    _stop(srv, thread)


@pytest.fixture
def temp_routes():
    saved = list(server.ROUTES)
    yield
    server.ROUTES[:] = saved


def test_binds_loopback_only(client):
    assert client.srv.server_address[0] == "127.0.0.1"
    assert client.app.root == client.app.root.resolve()
    assert client.app.jobs is None


def test_make_app_sets_work_root(client, tmp_path):
    import os
    assert os.environ["YT_COACH_HOME"] == str(tmp_path)


def test_root_sets_cookie_with_key(client):
    status, headers, raw = client.call("GET", f"/?key={client.app.token}", token=False)
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert f"ytc_key={client.app.token}" in cookie
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert headers["Content-Type"].startswith("text/html")
    assert b"yt-coach" in raw


def test_cookie_authorizes_api(client):
    status, body = client.json("GET", "/api/ping", token=False,
                               headers={"Cookie": f"other=1; ytc_key={client.app.token}"})
    assert status == 200 and body["ok"] is True


@pytest.mark.parametrize("query", ["/?key=wrong", "/", "/?key=%ED%95%9C"])
def test_root_rejects_wrong_key(client, query):
    status, body = client.json("GET", query, token=False)
    assert status == 403
    assert body["error"] == "bad_token"


def test_api_403_body_has_code(client):
    status, body = client.json("GET", "/api/status", token=False)
    assert status == 403
    assert body["error"] == "bad_token"
    assert "key" in body["message"]


def test_static_requires_token(client):
    status, body = client.json("GET", "/static/index.html", token=False)
    assert status == 403 and body["error"] == "bad_token"
    status, _, raw = client.call("GET", "/static/index.html")
    assert status == 200 and b"yt-coach" in raw


def test_static_blocks_traversal(client):
    status, _, _ = client.call("GET", "/static/..%2F__init__.py")
    assert status == 404


def test_bad_host_rejected(client):
    status, body = client.json("GET", "/api/ping", headers={"Host": f"evil.example:{client.app.port}"})
    assert status == 403
    assert body["error"] == "bad_host"


def test_localhost_host_accepted(client):
    status, _ = client.json("GET", "/api/ping", headers={"Host": f"localhost:{client.app.port}"})
    assert status == 200


def test_cross_origin_post_rejected(client):
    status, body = client.json("POST", "/api/init", {}, headers={"Origin": "http://evil.example"})
    assert status == 403
    assert body["error"] == "bad_origin"


def test_status_and_init(client):
    status, body = client.json("GET", "/api/status")
    assert status == 200 and body["initialized"] is False
    status, body = client.json("POST", "/api/init", {})
    assert status == 200 and "refs/" in body["created"]
    status, body = client.json("GET", "/api/status")
    assert body["initialized"] is True


def test_files_serves_korean_name(client, tmp_path):
    name = "2026-10-20-맵기 4단계-conti.html"
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / name).write_text("<p>콘티</p>", encoding="utf-8")
    status, headers, raw = client.call("GET", "/files/drafts/" + quote(name))
    assert status == 200
    assert raw.decode("utf-8") == "<p>콘티</p>"
    assert headers["Content-Type"].startswith("text/html")


def test_files_serves_frames(client, tmp_path):
    frame = tmp_path / "workspace" / "frames" / "abc" / "0001.jpg"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"\xff\xd8jpg")
    status, headers, raw = client.call("GET", "/files/workspace/frames/abc/0001.jpg")
    assert status == 200 and raw == b"\xff\xd8jpg"
    assert headers["Content-Type"] == "image/jpeg"


@pytest.mark.parametrize("path", [
    "/files/../x",
    "/files/%2E%2E/x",
    "/files/drafts/..%2F..%2Fx",
    "/files/refs/ref_a/v.md",
    "/files/workspace/raw/x.json",
    "/files/workspace/ui.lock",
    "/files/drafts",
    "/files/drafts/missing.html",
])
def test_files_blocks_traversal_and_other_dirs(client, tmp_path, path):
    (tmp_path.parent / "x").write_text("secret", encoding="utf-8")
    for rel in ["refs/ref_a/v.md", "workspace/raw/x.json", "drafts/ok.html"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("no", encoding="utf-8")
    status, _, raw = client.call("GET", path)
    assert status == 404
    assert b"secret" not in raw


def test_unknown_route_404(client):
    status, body = client.json("GET", "/api/nope")
    assert status == 404 and body["error"] == "not_found"


def test_script_error_maps_to_400(client, temp_routes):
    @server.route("GET", "/api/test-error")
    def boom(req):
        raise ScriptError("x", "y", extra=1)

    status, body = client.json("GET", "/api/test-error")
    assert status == 400
    assert body == {"error": "x", "message": "y", "extra": 1}


def test_unexpected_error_maps_to_500(client, temp_routes):
    @server.route("GET", "/api/test-crash")
    def crash(req):
        raise RuntimeError("bad")

    status, body = client.json("GET", "/api/test-crash")
    assert status == 500
    assert body["error"] == "internal" and "bad" in body["message"]


def test_route_groups_query_and_json(client, temp_routes):
    @server.route("POST", r"/api/echo/(?P<name>[^/]+)")
    def echo(req, name):
        return 201, {"name": name, "query": req.query, "json": req.json, "root": str(req.app.root)}

    status, body = client.json("POST", "/api/echo/" + quote("맵기 4") + "?a=1&b=%ED%95%9C", {"k": "v"})
    assert status == 201
    assert body == {"name": "맵기 4", "query": {"a": "1", "b": "한"}, "json": {"k": "v"},
                    "root": str(client.app.root)}


def test_bad_json_is_400(client, temp_routes):
    @server.route("POST", "/api/echo")
    def echo(req):
        return {"json": req.json}

    req = urllib.request.Request(client.base + "/api/echo", data=b"{nope", method="POST")
    req.add_header("X-YTC-Key", client.app.token)
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 400
    assert json.loads(err.value.read())["error"] == "bad_json"
    status, body = client.json("POST", "/api/echo")
    assert status == 200 and body == {"json": {}}


def test_lock_written_and_live_lock_detected(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    srv, app, thread = _start(tmp_path)
    try:
        written = server.write_lock(app)
        lock_path = tmp_path / "workspace" / server.LOCK
        assert lock_path.exists()
        assert written["url"] == f"http://127.0.0.1:{app.port}/?key={app.token}"
        live = server.read_live_lock(tmp_path)
        assert live is not None
        assert live["port"] == app.port and live["token"] == app.token
    finally:
        _stop(srv, thread)
    assert server.read_live_lock(tmp_path) is None
    assert not lock_path.exists()


def test_read_live_lock_handles_missing_and_corrupt(tmp_path):
    assert server.read_live_lock(tmp_path) is None
    lock = tmp_path / "workspace" / server.LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text("{not json", encoding="utf-8")
    assert server.read_live_lock(tmp_path) is None
    assert not lock.exists()


def test_remove_lock_keeps_other_servers_lock(tmp_path):
    lock = tmp_path / "workspace" / server.LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": -1, "port": 1, "token": "t", "url": "u"}), encoding="utf-8")
    server.remove_lock(tmp_path)
    assert lock.exists()


def test_main_reuses_running_server(client, monkeypatch, tmp_path):
    server.write_lock(client.app)

    def no_new_server(*a, **k):
        raise AssertionError("must not start a new server")
    monkeypatch.setattr(server, "make_app", no_new_server)
    monkeypatch.setattr(ui_main.subprocess, "Popen", no_new_server)

    out = ui_main.main(["--no-open"])
    assert out == {"status": "running", "url": f"http://127.0.0.1:{client.app.port}/?key={client.app.token}"}


def test_main_opens_browser_for_running_server(client, monkeypatch):
    server.write_lock(client.app)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    out = ui_main.main([])
    assert out["status"] == "running" and opened == [out["url"]]


def test_shutdown_endpoint_stops_server(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    srv, app, thread = _start(tmp_path)
    try:
        c = Client(srv, app)
        status, body = c.json("POST", "/api/shutdown", {})
        assert status == 200 and body["status"] == "stopping"
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        _stop(srv, thread)


def test_foreground_main_serves_until_stop(monkeypatch, tmp_path, capsys, no_browser):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    result = {}
    fg = threading.Thread(target=lambda: result.update(out=ui_main.main(["--no-open"])), daemon=True)
    fg.start()
    lock = tmp_path / "workspace" / server.LOCK
    deadline = time.monotonic() + 5
    while not lock.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.read_live_lock(tmp_path) is not None

    assert ui_main.main(["--stop"]) == {"status": "stopped"}
    fg.join(timeout=5)
    assert not fg.is_alive()
    assert not lock.exists()
    first = json.loads(capsys.readouterr().out.splitlines()[0])
    assert first["status"] == "started" and first["url"].startswith("http://127.0.0.1:")
    assert result["out"] is None


def test_stop_without_server(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    assert ui_main.main(["--stop"]) == {"status": "not_running"}


def test_detach_spawns_child_and_waits_for_lock(monkeypatch, tmp_path, no_browser):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    calls = []

    class FakeProc:
        pid = 4321

        def __init__(self, cmd, **kwargs):
            calls.append((cmd, kwargs))

        def poll(self):
            return None

    live = iter([None, None, {"pid": 4321, "port": 5555, "token": "t", "url": "http://127.0.0.1:5555/?key=t"}])
    monkeypatch.setattr(ui_main, "read_live_lock", lambda root: next(live))
    monkeypatch.setattr(ui_main.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(ui_main, "POLL_SEC", 0.01)

    out = ui_main.main(["--detach", "--no-open", "--port", "5555"])

    assert out == {"status": "started", "url": "http://127.0.0.1:5555/?key=t", "pid": 4321}
    cmd, kwargs = calls[0]
    assert cmd[1:] == ["-m", "scripts.ui", "--no-open", "--port", "5555"]
    assert "--detach" not in cmd
    assert kwargs["env"]["YT_COACH_HOME"] == str(tmp_path)
    assert Path(kwargs["cwd"]) == server.PLUGIN_ROOT
    import sys
    if sys.platform == "win32":
        import subprocess
        assert kwargs["creationflags"] & subprocess.DETACHED_PROCESS
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True
    assert (tmp_path / "workspace" / "ui.log").exists()


def test_detach_reports_child_exit(monkeypatch, tmp_path, no_browser):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))

    class DeadProc:
        pid = 1

        def __init__(self, cmd, **kwargs):
            kwargs["stdout"].write(b"boom\n")

        def poll(self):
            return 1

    monkeypatch.setattr(ui_main, "read_live_lock", lambda root: None)
    monkeypatch.setattr(ui_main.subprocess, "Popen", DeadProc)
    with pytest.raises(ScriptError) as err:
        ui_main.main(["--detach", "--no-open"])
    assert err.value.code == "ui_start_failed"
    assert "boom" in err.value.message
