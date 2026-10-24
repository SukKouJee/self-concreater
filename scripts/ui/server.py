"""로컬 전용 UI 서버: 127.0.0.1 바인딩, 토큰 인증, 라우트 표, 작업 폴더 lock.

보안
- `127.0.0.1`에만 바인딩한다. `Host` 헤더가 `127.0.0.1:<port>`/`localhost:<port>`가 아니면 403 `bad_host`
  (DNS rebinding 차단). `Origin`이 있는데 이 서버가 아니면 403 `bad_origin`.
- `/`는 `?key=<token>`(또는 이미 받은 쿠키)로 열고, 성공하면 `ytc_key_<port>` 쿠키(HttpOnly, SameSite=Strict)를 준다.
  쿠키 이름에 포트를 넣어 같은 127.0.0.1의 다른 포트 서버(이전 실행 등)와 쿠키가 섞이지 않게 한다.
  그 밖의 모든 경로(`/static/` 포함)는 쿠키 `ytc_key_<port>` 또는 헤더 `X-YTC-Key`가 필요하다. 실패는 403 `bad_token`.
- `/files/<path>`는 작업 폴더 안의 `workspace/frames`, `drafts`, `reports`만 내준다. 그 밖은 모두 404.

라우트 등록 규약 (다음 Task들이 이 방식으로 붙인다)
- `@route(method, pattern)`으로 모듈 전역 표 `ROUTES`에 등록한다. pattern은 경로(쿼리 제외, 퍼센트 인코딩 그대로)에
  대한 `re.fullmatch` 정규식이고, 이름 붙은 그룹은 URL 디코딩되어 키워드 인자로 넘어간다.
- 핸들러: `handler(req: Request, **groups) -> dict | tuple[int, dict] | Response`.
  `ScriptError` → 400 `{"error", "message", **details}`, 그 밖의 예외 → 500 `{"error": "internal", "message"}`.
- 라우트 모듈(예: `scripts.ui.api_add`, `scripts.ui.api_conti`, `scripts.ui.api_refs`)은 `ROUTE_MODULES`에 이름을 더한다.
  `make_app()`이 `register_routes()`로 그 모듈들을 import하고, import 때 데코레이터가 같은 `ROUTES`에 등록한다.
  (라우트 모듈은 `from scripts.ui.server import route`만 하면 되고, 순환 import가 생기지 않는다.)
- `App.jobs`는 `make_app()`이 붙이는 `jobs.JobManager`다. `/api/shutdown`은 진행 중인 작업을 먼저 멈춘다.
"""
from __future__ import annotations

import importlib
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlsplit

from scripts import refstore, status
from scripts.common import PLUGIN_ROOT, ScriptError
from scripts.ui.jobs import JobManager

HOST = "127.0.0.1"
LOCK = "ui.lock"
COOKIE_PREFIX = "ytc_key"
HEADER = "X-YTC-Key"
STATIC_DIR = Path(__file__).resolve().parent / "static"
FILE_DIRS: tuple[tuple[str, ...], ...] = (("workspace", "frames"), ("drafts",), ("reports",))
MAX_BODY = 10 * 1024 * 1024
PING_TIMEOUT = 1.0

# 라우트 모듈: Task 5/6이 여기에 이름을 더한다(예: "scripts.ui.api_add").
ROUTE_MODULES: list[str] = ["scripts.ui.api_add", "scripts.ui.api_conti", "scripts.ui.api_refs", "scripts.ui.api_typify",
                           "scripts.ui.api_motif", "scripts.ui.api_draft", "scripts.ui.api_polish",
                           "scripts.ui.api_diagnose"]

BAD_TOKEN = {"error": "bad_token", "message": "주소의 key가 맞지 않습니다. 터미널에 나온 새 주소로 여세요"}

# Windows 레지스트리의 mimetypes 값이 틀릴 수 있어(.js → text/plain 등) 자주 쓰는 것은 고정한다.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".srt": "application/x-subrip; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
}


@dataclass
class App:
    root: Path
    token: str
    port: int
    server: ThreadingHTTPServer | None = None
    jobs: JobManager | None = None


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    json: dict[str, Any]
    headers: dict[str, str]
    app: App


@dataclass
class Response:
    """JSON이 아닌 응답(HTML, 파일)."""

    status: int
    body: bytes
    content_type: str
    headers: list[tuple[str, str]] = field(default_factory=list)


Handler = Callable[..., Any]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    auth: bool


ROUTES: list[Route] = []


def route(method: str, pattern: str, *, auth: bool = True) -> Callable[[Handler], Handler]:
    """핸들러를 `ROUTES`에 등록한다. `auth=False`는 핸들러가 직접 인증하는 경우(`GET /`)만 쓴다."""

    def deco(fn: Handler) -> Handler:
        ROUTES.append(Route(method.upper(), re.compile(pattern), fn, auth))
        return fn

    return deco


def register_routes() -> None:
    for name in ROUTE_MODULES:
        importlib.import_module(name)


def content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _CONTENT_TYPES:
        return _CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def token_ok(given: str | None, token: str) -> bool:
    if not given:
        return False
    # compare_digest는 ASCII가 아닌 str에 TypeError를 내므로 bytes로 비교한다
    return secrets.compare_digest(given.encode("utf-8"), token.encode("utf-8"))


def cookie_name(port: int) -> str:
    """쿠키는 포트를 가리지 않으므로 이름에 포트를 넣는다."""
    return f"{COOKIE_PREFIX}_{port}"


def _cookie(header: str | None, name: str) -> str | None:
    for part in (header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return None


def _contained(base: Path, rel: str) -> Path | None:
    """rel(디코딩된 상대 경로)을 base 안으로 resolve한다. 벗어나거나 파일이 아니면 None."""
    if "\x00" in rel:
        return None
    base = base.resolve()
    try:
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return None
    except OSError:  # 잘못된 경로 문자 등: 없는 파일과 같게 404
        return None
    return target


def _file_response(path: Path) -> Response:
    return Response(200, path.read_bytes(), content_type(path))


NOT_FOUND = (404, {"error": "not_found", "message": "없는 경로입니다"})


# ---- 기본 라우트 -------------------------------------------------------------

@route("GET", "/", auth=False)
def _index(req: Request) -> Response | tuple[int, dict]:
    key = req.query.get("key")
    name = cookie_name(req.app.port)
    cookie = _cookie(req.headers.get("cookie"), name)
    if not (token_ok(key, req.app.token) or token_ok(cookie, req.app.token)):
        return 403, BAD_TOKEN
    resp = _file_response(STATIC_DIR / "index.html")
    resp.headers.append(("Set-Cookie", f"{name}={req.app.token}; Path=/; HttpOnly; SameSite=Strict"))
    return resp


@route("GET", r"/static/(?P<rel>.+)")
def _static(req: Request, rel: str) -> Response | tuple[int, dict]:
    path = _contained(STATIC_DIR, rel)
    return _file_response(path) if path else NOT_FOUND


@route("GET", r"/files/(?P<rel>.+)")
def _files(req: Request, rel: str) -> Response | tuple[int, dict]:
    path = _contained(req.app.root, rel)
    if path is None:
        return NOT_FOUND
    parts = path.relative_to(req.app.root.resolve()).parts
    if not any(parts[: len(prefix)] == prefix and len(parts) > len(prefix) for prefix in FILE_DIRS):
        return NOT_FOUND
    return _file_response(path)


@route("GET", "/api/ping")
def _api_ping(req: Request) -> dict:
    return {"ok": True, "pid": os.getpid(), "root": str(req.app.root)}


@route("GET", "/api/status")
def _status(req: Request) -> dict:
    return status.overview(req.query.get("category") or None)


@route("POST", "/api/init")
def _init(req: Request) -> dict:
    return refstore.scaffold()


@route("POST", "/api/shutdown")
def _shutdown(req: Request) -> dict:
    srv = req.app.server
    if req.app.jobs is not None:
        req.app.jobs.shutdown(timeout=0)   # 결정 대기 중인 작업 스레드를 깨워 멈춘다
    if srv is not None:
        # 응답을 먼저 보내도록 다른 스레드에서 끈다
        threading.Thread(target=srv.shutdown, daemon=True).start()
    return {"status": "stopping"}


# ---- HTTP 처리 ---------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "yt-coach-ui"
    server: _Server
    _body_read: bool = False

    def do_GET(self) -> None:  # noqa: N802 - http.server 규약
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # 요청 줄에 ?key=가 들어 있으므로 접근 로그는 남기지 않는다
        return

    def _dispatch(self, method: str) -> None:
        app = self.server.app
        try:
            result = self._handle(method, app)
        except ScriptError as e:
            result = (400, {"error": e.code, "message": e.message, **e.details})
        except Exception as e:  # noqa: BLE001 - 모든 실패를 JSON 500으로 돌려준다
            traceback.print_exc(file=sys.stderr)
            result = (500, {"error": "internal", "message": f"{type(e).__name__}: {e}"})
        self._drain_body()
        self._send(result)

    def _drain_body(self) -> None:
        # 읽지 않은 본문을 남긴 채 응답하면 Windows가 연결을 RST로 끊으므로 비운다(거부 응답 포함)
        if self._body_read:
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY:
            self.rfile.read(length)
        elif length > MAX_BODY:
            self.close_connection = True

    def _handle(self, method: str, app: App) -> Any:
        host = (self.headers.get("Host") or "").lower()
        if host not in {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}:
            return 403, {"error": "bad_host", "message": "이 서버는 127.0.0.1 주소로만 열 수 있습니다"}
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {f"http://{host}"}:
            return 403, {"error": "bad_origin", "message": "다른 사이트에서 온 요청은 받지 않습니다"}

        parts = urlsplit(self.path)
        found = self._match(method, parts.path)
        if found is None:
            return NOT_FOUND
        rt, groups = found
        if rt.auth:
            given = self.headers.get(HEADER) or _cookie(self.headers.get("Cookie"), cookie_name(app.port))
            if not token_ok(given, app.token):
                return 403, BAD_TOKEN
        req = Request(
            method=method,
            path=parts.path,
            query=dict(parse_qsl(parts.query, keep_blank_values=True)),
            json=self._read_json(),
            headers={k.lower(): v for k, v in self.headers.items()},
            app=app,
        )
        return rt.handler(req, **groups)

    @staticmethod
    def _match(method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        for rt in ROUTES:
            if rt.method != method:
                continue
            m = rt.pattern.fullmatch(path)
            if m:
                return rt, {k: unquote(v) for k, v in m.groupdict().items() if v is not None}
        return None

    def _read_json(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_len)
        except ValueError as e:
            raise ScriptError("bad_request", "Content-Length가 잘못되었습니다") from e
        if length < 0 or length > MAX_BODY:
            raise ScriptError("bad_request", "요청 본문이 너무 큽니다")
        raw = self.rfile.read(length) if length else b""
        self._body_read = True
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ScriptError("bad_json", f"요청 본문이 JSON이 아닙니다: {e}") from e
        if not isinstance(data, dict):
            raise ScriptError("bad_json", "요청 본문은 JSON 객체여야 합니다")
        return data

    def _send(self, result: Any) -> None:
        if isinstance(result, Response):
            resp = result
        else:
            code, payload = result if isinstance(result, tuple) else (200, result)
            try:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
            except (ValueError, TypeError) as e:
                code = 500
                body = json.dumps({"error": "internal", "message": f"응답을 만들지 못했습니다: {type(e).__name__}"},
                                  ensure_ascii=False).encode("utf-8")
            resp = Response(code, body, "application/json; charset=utf-8")
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in resp.headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp.body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket bind a port that is already listening
    # (double-bind / port hijack), so "port_unavailable" would never fire. POSIX keeps it
    # so a restart can rebind a port still in TIME_WAIT.
    allow_reuse_address = sys.platform != "win32"
    app: App


def make_app(root: Path, port: int = 0) -> tuple[ThreadingHTTPServer, App]:
    """127.0.0.1에 바인딩한 서버와 App을 만든다(아직 serve하지 않음). YT_COACH_HOME을 root로 맞춘다."""
    root = Path(root).resolve()
    os.environ["YT_COACH_HOME"] = str(root)
    register_routes()
    try:
        srv = _Server((HOST, port), _Handler)
    except OSError as e:
        raise ScriptError("port_unavailable", f"{HOST}:{port}에 서버를 열 수 없습니다: {e}") from e
    app = App(root=root, token=secrets.token_urlsafe(24), port=srv.server_address[1], server=srv,
              jobs=JobManager())
    srv.app = app
    return srv, app


# ---- lock --------------------------------------------------------------------

def lock_path(root: Path) -> Path:
    return Path(root) / "workspace" / LOCK


def app_url(port: int, token: str) -> str:
    return f"http://{HOST}:{port}/?key={token}"


def write_lock(app: App) -> dict:
    info = {"pid": os.getpid(), "port": app.port, "token": app.token, "url": app_url(app.port, app.token)}
    path = lock_path(app.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{LOCK}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    os.replace(tmp, path)
    return info


def _read_lock(root: Path) -> dict | None:
    path = lock_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def remove_lock(root: Path) -> None:
    """이 프로세스가 쓴 lock만 지운다."""
    data = _read_lock(root)
    if data and data.get("pid") == os.getpid():
        lock_path(root).unlink(missing_ok=True)


def _ping(info: dict) -> bool:
    req = urllib.request.Request(f"http://{HOST}:{int(info['port'])}/api/ping")
    req.add_header(HEADER, str(info["token"]))
    try:
        with urllib.request.urlopen(req, timeout=PING_TIMEOUT) as resp:
            return resp.status == 200 and json.loads(resp.read()).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def read_live_lock(root: Path) -> dict | None:
    """살아 있는 서버의 lock 정보. 응답이 없으면(죽은 서버, 깨진 파일) lock을 지우고 None."""
    data = _read_lock(root)
    if data is None:
        return None
    if {"pid", "port", "token", "url"} <= data.keys() and _ping(data):
        return data
    lock_path(root).unlink(missing_ok=True)
    return None


def request_shutdown(info: dict) -> None:
    req = urllib.request.Request(f"http://{HOST}:{int(info['port'])}/api/shutdown", data=b"", method="POST")
    req.add_header(HEADER, str(info["token"]))
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()
