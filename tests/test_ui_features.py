"""서버 이음매(T2): match/call 분리, /healthz, --host, /api/features + YTC_DISABLE 가드, data-feature 숨김."""
import json
import re
import shutil
import subprocess
import threading
import time

import pytest

from scripts.common import ScriptError
from scripts.ui import __main__ as ui_main
from scripts.ui import server
from scripts.ui.server import STATIC_DIR
from tests.test_ui_server import client, no_browser, temp_routes  # noqa: F401  (fixture 재사용)


# ---- match / call -------------------------------------------------------------

def test_match_returns_route_and_decoded_groups(temp_routes):
    @server.route("GET", r"/api/m/(?P<name>[^/]+)")
    def handler(req, name):
        return {"name": name}

    found = server.match("GET", "/api/m/%EB%A7%B5")
    assert found is not None
    rt, groups = found
    assert rt.handler is handler and groups == {"name": "맵"}
    assert server.match("POST", "/api/m/x") is None
    assert server.match("GET", "/api/nope-at-all") is None


def _req():
    return server.Request(method="GET", path="/x", query={}, json={}, headers={}, app=None)


def _route(handler):
    return server.Route("GET", re.compile("/x"), handler, True)


def test_call_maps_dict_tuple_and_errors_to_json_responses():
    ok = server.call(_route(lambda req: {"a": "한"}), _req(), {})
    assert ok.status == 200 and json.loads(ok.body) == {"a": "한"}
    assert ok.content_type == "application/json; charset=utf-8"
    assert ("Cache-Control", "no-store") in ok.headers

    created = server.call(_route(lambda req: (201, {"k": 1})), _req(), {})
    assert created.status == 201

    def bad(req):
        raise ScriptError("x", "y", extra=1)

    err = server.call(_route(bad), _req(), {})
    assert err.status == 400 and json.loads(err.body) == {"error": "x", "message": "y", "extra": 1}

    def crash(req):
        raise RuntimeError("bad")

    boom = server.call(_route(crash), _req(), {})
    body = json.loads(boom.body)
    assert boom.status == 500 and body["error"] == "internal" and "bad" in body["message"]


def test_call_passes_response_through_with_standard_headers():
    resp = server.Response(200, b"hi", "text/plain", [("X-A", "1")])
    out = server.call(_route(lambda req: resp), _req(), {})
    assert out.body == b"hi" and ("X-A", "1") in out.headers
    assert ("X-Content-Type-Options", "nosniff") in out.headers
    assert resp.headers == [("X-A", "1")]          # the handler's own object is not mutated


def test_call_unserializable_payload_is_500():
    out = server.call(_route(lambda req: {"x": float("nan")}), _req(), {})
    assert out.status == 500 and json.loads(out.body)["error"] == "internal"


# ---- /healthz ------------------------------------------------------------------

def test_healthz_needs_no_token(client):
    status, body = client.json("GET", "/healthz", token=False)
    assert status == 200 and body == {"ok": True}


def test_healthz_still_checks_host(client):
    status, body = client.json("GET", "/healthz", token=False, headers={"Host": "evil.example:80"})
    assert status == 403 and body["error"] == "bad_host"


# ---- features ------------------------------------------------------------------

def test_features_default_is_all_enabled(client, monkeypatch):
    monkeypatch.delenv("YTC_DISABLE", raising=False)
    status, body = client.json("GET", "/api/features")
    assert status == 200 and body == {"edition": "local", "disabled": {}}


def test_features_lists_disabled_with_korean_text(client, monkeypatch):
    monkeypatch.setenv("YTC_DISABLE", " library , caption_video,unknown ,")
    status, body = client.json("GET", "/api/features")
    assert status == 200 and body["edition"] == "local"
    assert set(body["disabled"]) == {"library", "caption_video"}      # unknown names are ignored
    assert "공용 라이브러리" in body["disabled"]["library"]
    assert "내 PC 판" in body["disabled"]["caption_video"]


@pytest.mark.parametrize("method,path,feature", [
    ("GET", "/api/library/status", "library"),
    ("POST", "/api/library/connect", "library"),
    ("POST", "/api/conti/caption-video", "caption_video"),
    ("GET", "/api/captions/check", "caption_video"),
    ("POST", "/api/conti/srt-fit", "srtfit"),
    ("GET", "/api/footage", "footage"),
    ("POST", "/api/footage/process", "footage"),
    ("GET", "/api/footage/media/abc", "footage"),
])
def test_disabled_feature_routes_answer_403(client, monkeypatch, method, path, feature):
    monkeypatch.setenv("YTC_DISABLE", feature)
    status, body = client.json(method, path, {} if method == "POST" else None)
    assert status == 403
    assert body["error"] == "feature_disabled" and body["feature"] == feature
    assert body["message"] == server.FEATURE_TEXT[feature]


def test_guard_does_not_touch_neighbouring_paths(monkeypatch):
    monkeypatch.setenv("YTC_DISABLE", "library,caption_video,srtfit,footage")
    for path in ("/api/status", "/api/conti/srt", "/api/librarian", "/api/features", "/healthz", "/static/app.js"):
        assert server.feature_guard(path) is None, path


def test_every_guarded_prefix_belongs_to_a_registered_route():
    server.register_routes()
    for feature, prefixes in server.FEATURE_PREFIXES.items():
        for prefix in prefixes:
            assert any(rt.pattern.pattern.startswith(prefix) for rt in server.ROUTES), (feature, prefix)


def test_enabled_feature_is_not_blocked(client, monkeypatch):
    monkeypatch.setenv("YTC_DISABLE", "footage")
    status, _ = client.json("GET", "/api/status")
    assert status == 200
    _, body = client.json("GET", "/api/library/status")
    assert body.get("error") != "feature_disabled"


def test_token_is_checked_before_the_guard(client, monkeypatch):
    monkeypatch.setenv("YTC_DISABLE", "library")
    status, body = client.json("GET", "/api/library/status", token=False)
    assert status == 403 and body["error"] == "bad_token"


# ---- --host / --foreground -----------------------------------------------------

def test_default_host_is_loopback(client):
    assert client.srv.server_address[0] == "127.0.0.1"


def test_non_loopback_host_refused_outside_container(tmp_path, monkeypatch):
    monkeypatch.delenv("YTC_IN_CONTAINER", raising=False)
    with pytest.raises(ScriptError) as err:
        server.make_app(tmp_path, 0, host="0.0.0.0")
    assert err.value.code == "host_not_allowed"


def test_non_loopback_host_allowed_in_container(tmp_path, monkeypatch):
    monkeypatch.setenv("YTC_IN_CONTAINER", "1")
    srv, app = server.make_app(tmp_path, 0, host="0.0.0.0")
    try:
        assert srv.server_address[0] == "0.0.0.0"
        assert app.port == srv.server_address[1]
    finally:
        srv.server_close()


def test_host_option_reaches_make_app(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    seen = {}

    def fake(root, port, host=server.HOST):
        seen["host"] = host
        raise ScriptError("stop_here", "x")

    monkeypatch.setattr(server, "make_app", fake)
    with pytest.raises(ScriptError):
        ui_main.main(["--no-open", "--host", "0.0.0.0", "--foreground"])
    assert seen["host"] == "0.0.0.0"


def test_foreground_conflicts_with_detach_and_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    for other in ("--detach", "--stop"):
        with pytest.raises(ScriptError):
            ui_main.main(["--foreground", other])


def test_foreground_serves_and_stops(monkeypatch, tmp_path, no_browser):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    fg = threading.Thread(target=lambda: ui_main.main(["--no-open", "--foreground"]), daemon=True)
    fg.start()
    deadline = time.monotonic() + 5
    while server.read_live_lock(tmp_path) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.read_live_lock(tmp_path) is not None
    assert ui_main.main(["--stop"]) == {"status": "stopped"}
    fg.join(timeout=5)
    assert not fg.is_alive()


# ---- static: data-feature ------------------------------------------------------

def _text(rel):
    return (STATIC_DIR / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel,marker", [
    ("pages/conti.js", '"data-feature": "srtfit"'),
    ("pages/conti.js", '"data-feature": "caption_video"'),
    ("footage-area.js", '"data-feature": "footage"'),
    ("app.js", '"data-feature": featureOfLink(it)'),
])
def test_feature_markers_are_in_the_static_js(rel, marker):
    assert marker in _text(rel)


def test_app_applies_features_at_start():
    text = _text("app.js")
    assert 'from "/static/features.js"' in text and "watchFeatures" in text


def test_features_module_has_no_innerhtml():
    assert "innerHTML" not in _text("features.js")


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_apply_features_hides_marked_elements_and_adds_note_once():
    code = """
import(%s).then(m => {
  const mk = (feature) => ({ dataset: { feature }, hidden: false, next: null,
    after(n) { this.next = n; }, get nextElementSibling() { return this.next; } });
  const a = mk("library"), b = mk("footage"), c = mk("library");
  const root = { querySelectorAll: () => [a, b, c] };
  const note = (text, feature) => ({ text, feature, dataset: { featureNote: feature } });
  m.applyFeatures(root, { library: "막힘" }, note);
  m.applyFeatures(root, { library: "막힘" }, note);
  console.log(JSON.stringify([a.hidden, b.hidden, c.hidden, a.next && a.next.text, b.next]));
})""" % json.dumps((STATIC_DIR / "features.js").as_uri())
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, False, True, "막힘", None]
