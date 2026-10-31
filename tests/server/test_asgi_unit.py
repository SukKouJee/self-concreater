"""서버판 ASGI 어댑터 단위 테스트(서버 설계 §4.1·§4.2·§8): 가짜 사용자 목록·가짜 저장소, PG·MinIO 없이.

실제 PG+MinIO 위의 격리·라우트 전수·/files는 test_isolation.py · test_route_smoke.py · test_files_proxy.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from scripts import ctx  # noqa: E402
from scripts.ui import server as ui  # noqa: E402
from scripts.ui.server import STATIC_DIR  # noqa: E402
from server import asgi, auth  # noqa: E402
from server.config import AuthSettings  # noqa: E402
from server.jobs_registry import Registry  # noqa: E402

ORIGIN = "https://testserver"
SECRET = "t" * 40


class FakeUsers:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, auth.UserRow] = {}

    def add(self, email: str, **over: Any) -> auth.UserRow:
        row = auth.UserRow(uuid.uuid4(), email, over.get("disabled", False), over.get("session_epoch", 0))
        self.rows[row.id] = row
        return row

    def update(self, uid: uuid.UUID, **over: Any) -> None:
        self.rows[uid] = replace(self.rows[uid], **over)

    def by_id(self, user_id: uuid.UUID) -> auth.UserRow | None:
        return self.rows.get(user_id)

    def by_email(self, email: str) -> auth.UserRow | None:
        return next((r for r in self.rows.values() if r.email == email.strip().lower()), None)


class _Conn:
    def execute(self, sql: str) -> None:
        return None


class FakeDb:
    def __init__(self) -> None:
        self.fail = False

    @contextmanager
    def public_tx(self):
        if self.fail:
            import psycopg
            raise psycopg.OperationalError("db down")
        yield _Conn()


class FakeBucket:
    def __init__(self) -> None:
        self.fail = False

    def ping(self) -> None:
        if self.fail:
            raise OSError("s3 down")


class FakeBackend:
    def __init__(self) -> None:
        self.db = FakeDb()
        self.bucket = FakeBucket()


def _settings(**over: str) -> AuthSettings:
    env = {"YTC_PUBLIC_URL": ORIGIN, "YTC_SESSION_SECRET": SECRET, "YTC_ENV": "test", "YTC_DEV_LOGIN": "1"}
    env.update(over)
    return AuthSettings.from_env(env)


@pytest.fixture
def users() -> FakeUsers:
    return FakeUsers()


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def registry() -> Registry:
    return Registry()


@pytest.fixture
def client(users, backend, registry, monkeypatch):
    monkeypatch.delenv(ui.DISABLE_ENV, raising=False)
    app = asgi.create_app(_settings(), backend, users=users, registry=registry)   # type: ignore[arg-type]
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c


def _login(client: TestClient, email: str) -> Any:
    return client.post("/auth/dev", json={"email": email})


@pytest.fixture
def alice(client, users):
    row = users.add("a@example.com")
    assert _login(client, "a@example.com").status_code == 200
    return row


# ---- no session ---------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [("GET", "/api/status"), ("GET", "/static/app.js"), ("GET", "/api/features"),
                                         ("POST", "/api/init"), ("GET", "/files/drafts/a.html"),
                                         ("GET", "/api/nope")])
def test_no_session_is_401_login_required(client, method, path):
    r = client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 401 and r.json()["error"] == "login_required"


def test_root_without_session_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/auth/login"


def test_login_page_and_script_need_no_session(client):
    page = client.get("/auth/login")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert b"/auth/login.js" in page.content and b"<script>" not in page.content   # no inline script (CSP)
    assert "script-src" not in page.headers["content-security-policy"]              # default-src 'self'
    js = client.get("/auth/login.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert "innerHTML" not in js.text
    assert client.get("/auth/methods").json() == {"dev_login": True}


def test_healthz_needs_no_session(client, backend):
    assert client.get("/healthz").json() == {"ok": True}
    backend.bucket.fail = True
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["error"] == "unhealthy"
    backend.bucket.fail = False
    backend.db.fail = True
    assert client.get("/healthz").status_code == 503


# ---- dev login · session cookie --------------------------------------------------------

def test_dev_login_sets_secure_httponly_lax_cookie(client, users):
    users.add("a@example.com")
    r = _login(client, "A@Example.com ")
    assert r.status_code == 200 and r.json() == {"ok": True, "email": "a@example.com"}
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(f"{auth.SESSION_COOKIE}=")
    for flag in ("httponly", "secure", "samesite=lax", "path=/", f"max-age={auth.SESSION_MAX_AGE}"):
        assert flag in cookie


@pytest.mark.parametrize("disabled", [False, True])
def test_dev_login_refuses_unknown_or_disabled(client, users, disabled):
    if disabled:
        users.add("a@example.com", disabled=True)
    r = _login(client, "a@example.com")
    assert r.status_code == 403 and r.json()["error"] == "login_denied"
    assert "set-cookie" not in r.headers


def test_dev_login_needs_an_email(client):
    assert client.post("/auth/dev", json={}).status_code == 400


def test_dev_login_route_absent_when_not_enabled(users, backend):
    app = asgi.create_app(_settings(YTC_DEV_LOGIN="0"), backend, users=users)   # type: ignore[arg-type]
    users.add("a@example.com")
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        assert c.get("/auth/methods").json() == {"dev_login": False}
        r = _login(c, "a@example.com")
        assert r.status_code == 404 and "set-cookie" not in r.headers


def test_production_refuses_dev_login_at_startup():
    with pytest.raises(Exception) as e:
        _settings(YTC_ENV="production")
    assert getattr(e.value, "code", None) == "config_invalid"


def test_session_survives_and_logout_clears(client, alice):
    assert client.get("/api/ping").json() == {"ok": True, "edition": "server", "user": "a@example.com"}
    r = client.post("/auth/logout", json={})
    assert r.status_code == 200 and "ytc_session=null" in r.headers["set-cookie"]
    assert client.get("/api/ping").status_code == 401


@pytest.mark.parametrize("change", [{"disabled": True}, {"session_epoch": 1}])
def test_disabled_user_or_epoch_bump_kills_the_session(client, users, alice, change):
    assert client.get("/api/ping").status_code == 200
    users.update(alice.id, **change)
    r = client.get("/api/ping")
    assert r.status_code == 401 and r.json()["error"] == "login_required"
    assert "ytc_session=null" in r.headers["set-cookie"]
    users.update(alice.id, disabled=False, session_epoch=alice.session_epoch)
    assert client.get("/api/ping").status_code == 401      # the cookie was cleared, not just refused once


def test_deleted_user_kills_the_session(client, users, alice):
    del users.rows[alice.id]
    assert client.get("/api/ping").status_code == 401


def test_forged_cookie_is_ignored(client, users):
    users.add("a@example.com")
    client.cookies.set(auth.SESSION_COOKIE, "eyJ1aWQiOiAieCJ9.forged.sig", domain="testserver")
    assert client.get("/api/ping").status_code == 401


def test_session_signed_with_another_secret_is_ignored(users, backend):
    row = users.add("a@example.com")
    other = asgi.create_app(_settings(YTC_SESSION_SECRET="o" * 40), backend, users=users)   # type: ignore[arg-type]
    with TestClient(other, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        assert _login(c, row.email).status_code == 200
        stolen = c.cookies.get(auth.SESSION_COOKIE)
    mine = asgi.create_app(_settings(), backend, users=users)   # type: ignore[arg-type]
    with TestClient(mine, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        c.cookies.set(auth.SESSION_COOKIE, stolen, domain="testserver")
        assert c.get("/api/ping").status_code == 401


# ---- CSRF ---------------------------------------------------------------------------------

@pytest.mark.parametrize("headers,error", [
    ({"Origin": "https://evil.example"}, "bad_origin"),
    ({"Origin": "null"}, "bad_origin"),
    ({"Origin": "http://testserver"}, "bad_origin"),
])
def test_cross_origin_post_is_403(client, alice, headers, error):
    r = client.post("/api/init", json={}, headers=headers)
    assert r.status_code == 403 and r.json()["error"] == error


def test_post_without_origin_needs_same_origin_fetch_site(client, alice):
    client.headers.pop("Origin")
    r = client.post("/api/init", json={})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    r = client.post("/api/init", json={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/api/shutdown", json={}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 404                           # passed CSRF; server edition has no shutdown


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"])
def test_form_content_types_are_403(client, alice, ctype):
    r = client.post("/api/init", content=b"{}", headers={"Content-Type": ctype})
    assert r.status_code == 403 and r.json()["error"] == "bad_content_type"


def test_cross_origin_login_and_logout_are_refused(client, users, alice):
    assert client.post("/auth/logout", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/ping").status_code == 200
    users.add("b@example.com")
    assert client.post("/auth/dev", json={"email": "b@example.com"},
                       headers={"Origin": "https://evil.example"}).status_code == 403


# ---- routing · guard · body ---------------------------------------------------------------

def test_features_report_server_edition_with_everything_off(client, alice):
    body = client.get("/api/features").json()
    assert body["edition"] == "server" and set(body["disabled"]) == set(ui.FEATURE_PREFIXES)


@pytest.mark.parametrize("method,path,feature", [
    ("GET", "/api/library/status", "library"), ("POST", "/api/library/pull", "library"),
    ("POST", "/api/conti/caption-video", "caption_video"), ("GET", "/api/captions/check", "caption_video"),
    ("POST", "/api/conti/srt-fit", "srtfit"), ("GET", "/api/footage", "footage"),
    ("POST", "/api/footage/process", "footage"), ("GET", "/api/footage/media/x", "footage"),
])
def test_disabled_features_answer_403(client, alice, method, path, feature):
    r = client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 403 and r.json() == {"error": "feature_disabled", "feature": feature,
                                                 "message": ui.FEATURE_TEXT[feature]}


@pytest.mark.parametrize("path", ["/api/%6Cibrary/status", "/api/footage%2Fprocess", "/api/Library/status"])
def test_guard_and_routing_see_the_same_raw_path(client, alice, path):
    # stdlib server: guard and match both see urlsplit(self.path).path (still percent-encoded)
    assert ui.match("GET", path) is None and ui.feature_guard(path) is None
    assert client.get(path).status_code == 404


def test_raw_path_keeps_percent_encoding():
    assert asgi.raw_path({"raw_path": b"/files/drafts/%ED%95%9C.html?x=1", "path": "/files/drafts/한.html"}) \
        == "/files/drafts/%ED%95%9C.html"
    assert asgi.raw_path({"path": "/files/drafts/한.html"}) == "/files/drafts/%ED%95%9C.html"


def test_unknown_route_is_404(client, alice):
    assert client.get("/api/nope").status_code == 404


def test_body_limits_and_bad_json(client, alice):
    r = client.post("/api/init", content=b"[1]", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_json"
    big = b'{"a": "' + b"x" * (ui.MAX_BODY + 1) + b'"}'
    r = client.post("/api/init", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_restarting_refuses_state_changes(client, alice, registry):
    registry.close()
    r = client.post("/api/init", json={})
    assert r.status_code == 503 and r.json()["error"] == "restarting"


def test_security_headers(client, alice):
    r = client.get("/api/ping")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == asgi.APP_CSP
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_root_serves_the_shell_when_logged_in(client, alice):
    r = client.get("/")
    assert r.status_code == 200 and b"/static/app.js" in r.content
    assert "set-cookie" not in r.headers or "ytc_key" not in r.headers["set-cookie"]
    assert client.get("/static/app.js").status_code == 200


# ---- request context in the worker thread ---------------------------------------------------

@pytest.fixture
def temp_routes():
    saved = list(ui.ROUTES)
    yield
    ui.ROUTES[:] = saved


def test_handler_runs_with_the_users_ctx_in_a_worker_thread(client, alice, temp_routes, registry):
    seen: dict[str, Any] = {}
    done = threading.Event()

    def in_job(job):
        c = ctx.here()
        seen["job"] = (c.user_id, ui.current_edition(), set(ui.disabled_features()))
        done.set()

    @ui.route("GET", "/api/t10/ctx")
    def handler(req):
        c = ctx.here()
        seen["handler"] = (c.user_id, c.root, ui.current_edition(), threading.current_thread().name)
        job = req.app.jobs.start("help_chat", in_job)
        return {"job": job.id, "root": req.app.root, "token": req.app.token}

    r = client.get("/api/t10/ctx")
    assert r.status_code == 200, r.text
    assert done.wait(5)
    uid, root, edition, thread = seen["handler"]
    assert (uid, root, edition) == (str(alice.id), None, "server")
    assert thread != threading.main_thread().name
    assert seen["job"] == (str(alice.id), "server", set(ui.FEATURE_PREFIXES))
    assert registry.for_user(alice.id).get(r.json()["job"]) is not None
    assert r.json()["root"] is None and r.json()["token"] == ""
    assert ctx.current() is None and ui.current_edition() == "local"     # nothing leaked into this thread


def test_handler_exception_is_500_json(client, alice, temp_routes):
    @ui.route("GET", "/api/t10/boom")
    def handler(req):
        raise RuntimeError("boom")

    r = client.get("/api/t10/boom")
    assert r.status_code == 500 and r.json()["error"] == "internal"


# ---- static UI: 401 → login, disabled screens -------------------------------------------------

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("status,error,expected", [(401, "login_required", ["/auth/login"]), (403, "bad_token", []),
                                                   (401, "other", [])])
def test_api_js_sends_login_required_to_the_login_screen(status, error, expected):
    code = """
globalThis.document = { getElementById: () => null };
const went = [];
globalThis.location = { pathname: "/", assign: (u) => went.push(u) };
globalThis.fetch = async () => ({ ok: false, status: %d, json: async () => ({ error: %s, message: "m" }) });
import(%s).then(async (m) => {
  let err = null;
  try { await m.api("GET", "/api/status"); } catch (e) { err = e; }
  console.log(JSON.stringify({ went, status: err && err.status, error: err && err.error }));
});""" % (status, json.dumps(error), json.dumps((STATIC_DIR / "api.js").as_uri()))
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"went": expected, "status": status, "error": error}


def test_app_shows_the_notice_for_disabled_screens():
    text = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "const PAGE_FEATURES = { library: \"library\" };" in text
    assert "disabledNote(page)" in text and "disabledFeatures = await loadFeatures(api);" in text
    assert "innerHTML" not in text


# ---- uvicorn entry point ----------------------------------------------------------------------

def test_module_app_is_lazy_and_refuses_to_start_without_config(monkeypatch):
    for name in ("YTC_DB_DSN", "YTC_S3_ENDPOINT", "YTC_PUBLIC_URL", "YTC_SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    import anyio as _anyio

    async def lifespan_start():
        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            raise AssertionError(f"must not start: {message}")
        await asgi._LazyApp(asgi.from_env)({"type": "lifespan"}, receive, send)

    with pytest.raises(Exception) as e:
        _anyio.run(lifespan_start)
    assert getattr(e.value, "code", None) == "config_missing"


def test_module_app_builds_once(monkeypatch):
    built = []

    async def inner(scope, receive, send):
        built.append(scope["type"])

    lazy = asgi._LazyApp(lambda: (built.append("factory"), inner)[1])
    import anyio as _anyio
    _anyio.run(lazy, {"type": "http"}, None, None)
    _anyio.run(lazy, {"type": "http"}, None, None)
    assert built == ["factory", "http", "http"]
