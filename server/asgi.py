"""서버판 ASGI 앱(서버 설계 §4.1·§4.2·§8, ADR-S5): Starlette 위에서 로컬 UI의 라우트 표(`scripts.ui.server.ROUTES`)를 그대로 부른다.

요청 하나의 흐름(`_dispatch`):
1. 경로는 **퍼센트 인코딩 그대로**(`scope["raw_path"]`, 쿼리 제외)를 쓴다. stdlib 서버의 `urlsplit(self.path).path`와 같아서
   기능 가드(`feature_guard`)와 라우팅(`match`, 그룹만 디코딩)이 같은 문자열을 본다(`/api/%6Cibrary`는 가드에도 라우트에도 안 걸려 404).
2. `/healthz`(인증 없음, PG·MinIO 확인) → CSRF(안전하지 않은 메서드: Origin + JSON, `auth.csrf_error`) → `/auth/*`
   (로그인 화면, dev 로그인, 로그아웃) → 세션 확인(**요청마다** users의 disabled·session_epoch, Ctx를 만들기 전) →
   서버가 덮어쓰는 기본 라우트(`/`, `/api/ping`, `/api/shutdown` 404, `/files/<key>`) → `match` → 기능 가드(서버판은 §8 전부 끔)
   → 본문(10MB, `parse_json_body`) → 작업 스레드에서 `edition("server")` + `ctx.use(Ctx(user_id, backend))` 안에서 `call`.
   핸들러가 시작한 작업 스레드는 `copy_context()`로 Ctx·판 문맥을 이어받는다(JobManager.start).
3. `req.app` = `App(root=None, token="", port=0, server=None, jobs=registry.for_user(uid))`. 핸들러가 쓰는 것은 `jobs`뿐.
4. 응답 헤더: 로컬과 같은 표준 헤더(`normalize`) + CSP(앱 화면 `APP_CSP`, `/files`는 스크립트 없는 `FILES_CSP`, SVG는 `SVG_CSP`)
   + https면 HSTS. 접근 로그는 남기지 않는다(쿼리에 비밀이 없더라도 경로만 필요하면 uvicorn 설정으로).

`app`(uvicorn `server.asgi:app`)은 처음 호출(lifespan 시작) 때 env로 설정·저장소를 만든다. 설정이 틀리면 시작하지 않는다.
테스트는 `create_app(settings, backend, users=...)`로 만든다.
"""
from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any, Final
from urllib.parse import parse_qsl, quote, unquote

import anyio
import psycopg
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from scripts import ctx
from scripts.common import ScriptError
from scripts.ui import server as ui
from server import auth, files
from server.backend import ServerBackend
from server.config import AuthSettings, ServerSettings
from server.jobs_registry import Registry

EDITION: Final = "server"
SERVER_FEATURES: Final = tuple(ui.FEATURE_PREFIXES)      # §8: server v1 turns every optional feature off
METHODS: Final = ["GET", "POST", "PUT", "DELETE", "PATCH"]
LOGIN_PATH: Final = "/auth/login"
SHUTDOWN_TIMEOUT: Final = 60.0
APP_CSP: Final = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                  "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# User files (conti/report HTML with inline styles, images): no scripts at all; the report page frames them (same origin)
FILES_CSP: Final = ("default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; "
                    "frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
HSTS: Final = "max-age=31536000"
RESTARTING: Final = {"error": "restarting", "message": "서버가 다시 시작하는 중입니다. 잠시 뒤 새로고침하세요"}
UNHEALTHY: Final = "unhealthy"


def _web(name: str) -> bytes:
    return resources.files("server").joinpath("web", name).read_bytes()


def raw_path(scope: Scope) -> str:
    """퍼센트 인코딩 그대로의 경로(쿼리 제외). stdlib 서버의 `urlsplit(self.path).path`와 같은 문자열."""
    raw = scope.get("raw_path")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).split(b"?", 1)[0].decode("latin-1")
    return quote(str(scope.get("path", "/")), safe="/%:@!$&'()*+,;=-._~")


@dataclass(frozen=True)
class _User:
    id: uuid.UUID
    email: str


class _ServerApp:
    """요청 처리기. Starlette 라우트 하나(`/{path:path}`)가 모든 요청을 여기로 보낸다."""

    def __init__(self, settings: AuthSettings, backend: ServerBackend, users: auth.UserDirectory,
                 registry: Registry) -> None:
        self.settings = settings
        self.backend = backend
        self.users = users
        self.registry = registry

    # ---- 응답 --------------------------------------------------------------------

    def _security(self, headers: list[tuple[str, str]], path: str) -> list[tuple[str, str]]:
        out = list(headers)
        if not any(k.lower() == "content-security-policy" for k, _ in out):
            out.append(("Content-Security-Policy", FILES_CSP if path.startswith("/files/") else APP_CSP))
        if self.settings.secure_cookie:
            out.append(("Strict-Transport-Security", HSTS))
        return out

    def _reply(self, result: Any, path: str) -> StarletteResponse:
        """핸들러 밖에서 만든 결과(dict | (status, dict) | ui.Response | files.StreamReply) → 표준 헤더를 붙여 보낸다."""
        return self._send(result if isinstance(result, files.StreamReply) else ui.normalize(result), path)

    def _send(self, norm: ui.Response | files.StreamReply, path: str) -> StarletteResponse:
        """이미 정규화한 응답(`ui.call`·`ui.normalize` 결과, 또는 StreamReply) → Starlette 응답 + 보안 헤더."""
        resp: StarletteResponse
        if isinstance(norm, files.StreamReply):
            resp = StreamingResponse(norm.chunks, status_code=norm.status, media_type=norm.content_type)
            resp.headers["Content-Length"] = str(norm.length)
            headers = [*ui.STD_HEADERS, *norm.headers]
        elif norm.file is not None:
            resp = StreamingResponse(_file_chunks(norm.file), status_code=norm.status, media_type=norm.content_type)
            resp.headers["Content-Length"] = str(norm.file.length)
            headers = norm.headers
        else:
            resp = StarletteResponse(norm.body, status_code=norm.status, media_type=norm.content_type)
            headers = norm.headers
        for key, value in self._security(headers, path):
            resp.headers.append(key, value)
        return resp

    def _json(self, status: int, payload: dict[str, Any], path: str) -> StarletteResponse:
        return self._reply((status, payload), path)

    def _redirect(self, location: str, path: str) -> StarletteResponse:
        return self._reply(ui.Response(302, b"", "text/plain; charset=utf-8", [("Location", location)]), path)

    # ---- 세션 --------------------------------------------------------------------

    async def _current_user(self, request: StarletteRequest) -> _User | None:
        """세션 → 확인된 사용자. users 행을 요청마다 읽어 비활성·epoch·만료를 본다. 아니면 세션을 비우고 None."""
        session = request.session
        claim = auth.session_claim(session)
        if claim is None:
            if session:
                session.clear()
            return None
        row = await anyio.to_thread.run_sync(self.users.by_id, claim.user_id)
        if row is None or not auth.session_valid(claim, row):
            session.clear()
            return None
        return _User(row.id, row.email)

    # ---- 본문 --------------------------------------------------------------------

    @staticmethod
    async def _body(request: StarletteRequest) -> bytes:
        """stdlib `_read_json`과 같은 상한·오류(Content-Length가 틀리거나 10MB를 넘으면 bad_request)."""
        raw_len = request.headers.get("content-length")
        if raw_len is not None:
            try:
                length = int(raw_len)
            except ValueError as e:
                raise ScriptError("bad_request", "Content-Length가 잘못되었습니다") from e
            if length < 0 or length > ui.MAX_BODY:
                raise ScriptError("bad_request", "요청 본문이 너무 큽니다")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > ui.MAX_BODY:
                raise ScriptError("bad_request", "요청 본문이 너무 큽니다")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _json_body(self, request: StarletteRequest) -> dict[str, Any]:
        return ui.parse_json_body(await self._body(request))

    # ---- 처리 --------------------------------------------------------------------

    def _in_user_ctx(self, user: _User, fn: Callable[[], Any]) -> Any:
        """작업 스레드 안에서 그 사용자의 Ctx·서버판 문맥으로 fn을 부른다(ContextVar는 이 스레드에서 직접 건다)."""
        with ui.edition(EDITION, SERVER_FEATURES), ctx.use(ctx.Ctx(user_id=str(user.id), backend=self.backend)):
            return fn()

    async def dispatch(self, request: StarletteRequest) -> StarletteResponse:
        path = raw_path(request.scope)
        method = request.method.upper()
        if path == "/healthz" and method == "GET":
            return await self._healthz(path)
        headers = {k.lower(): v for k, v in request.headers.items()}
        blocked = auth.csrf_error(method, headers, self.settings.public_origin)
        if blocked is not None:
            return self._json(*blocked, path)
        if path.startswith("/auth/"):
            return await self._auth_route(request, method, path)
        user = await self._current_user(request)
        if user is None:
            if path == "/" and method == "GET":
                return self._redirect(LOGIN_PATH, path)
            return self._json(401, dict(auth.LOGIN_REQUIRED), path)
        return await self._user_route(request, method, path, headers, user)

    async def _user_route(self, request: StarletteRequest, method: str, path: str, headers: dict[str, str],
                          user: _User) -> StarletteResponse:
        if method == "GET" and path == "/":
            return self._reply(_index(), path)
        if method == "GET" and path == "/api/ping":
            return self._reply({"ok": True, "edition": EDITION, "user": user.email}, path)
        if path == "/api/shutdown":
            return self._reply(ui.NOT_FOUND, path)
        if method == "GET" and path.startswith("/files/") and len(path) > len("/files/"):
            rel = unquote(path[len("/files/"):])
            result = await anyio.to_thread.run_sync(
                self._in_user_ctx, user, lambda: files.serve(rel, headers.get("range")))
            return self._reply(result, path)
        found = ui.match(method, path)
        if found is None:
            return self._reply(ui.NOT_FOUND, path)
        rt, groups = found
        with ui.edition(EDITION, SERVER_FEATURES):
            guarded = ui.feature_guard(path)
        if guarded is not None:
            return self._reply(guarded, path)
        if self.registry.closing and method in auth.UNSAFE_METHODS:
            return self._json(503, dict(RESTARTING), path)
        try:
            body = await self._json_body(request)
            jobs = self.registry.for_user(user.id)
        except ScriptError as e:
            return self._reply(ui.error_result(e), path)
        req = ui.Request(method=method, path=path,
                         query=dict(parse_qsl(request.scope.get("query_string", b"").decode("latin-1"),
                                              keep_blank_values=True)),
                         json=body, headers={k: v for k, v in headers.items() if k != "cookie"},
                         app=ui.App(root=None, token="", port=0, server=None, jobs=jobs))
        result = await anyio.to_thread.run_sync(self._in_user_ctx, user, lambda: ui.call(rt, req, groups))
        return self._send(result, path)

    # ---- /auth -------------------------------------------------------------------

    async def _auth_route(self, request: StarletteRequest, method: str, path: str) -> StarletteResponse:
        if method == "GET" and path == LOGIN_PATH:
            if await self._current_user(request) is not None:
                return self._redirect("/", path)
            return self._reply(ui.Response(200, _web("login.html"), "text/html; charset=utf-8"), path)
        if method == "GET" and path == "/auth/login.js":
            return self._reply(ui.Response(200, _web("login.js"), "text/javascript; charset=utf-8"), path)
        if method == "GET" and path == "/auth/methods":
            return self._reply({"dev_login": self.settings.dev_login}, path)
        if method == "POST" and path == "/auth/dev" and self.settings.dev_login:
            return await self._dev_login(request, path)
        if method == "POST" and path == "/auth/logout":
            request.session.clear()
            return self._reply({"ok": True}, path)
        return self._reply(ui.NOT_FOUND, path)

    async def _dev_login(self, request: StarletteRequest, path: str) -> StarletteResponse:
        """개발·시험 전용(`YTC_DEV_LOGIN=1`, production이면 설정 단계에서 시작 거부). users에 있는 활성 이메일만."""
        try:
            body = await self._json_body(request)
        except ScriptError as e:
            return self._reply(ui.error_result(e), path)
        email = body.get("email")
        if not isinstance(email, str) or not email.strip():
            return self._json(400, {"error": "bad_request", "message": "email이 필요합니다"}, path)
        row = await anyio.to_thread.run_sync(self.users.by_email, email)
        if row is None or row.disabled:
            return self._json(403, dict(auth.LOGIN_DENIED), path)
        auth.start_session(request.session, row)
        return self._reply({"ok": True, "email": row.email}, path)

    # ---- /healthz ----------------------------------------------------------------

    def _check_stores(self) -> None:
        with self.backend.db.public_tx() as conn:
            conn.execute("SELECT 1")
        self.backend.bucket.ping()

    async def _healthz(self, path: str) -> StarletteResponse:
        try:
            await anyio.to_thread.run_sync(self._check_stores)
        except (psycopg.Error, OSError) as e:
            print(f"healthz: {type(e).__name__}: {e}", file=sys.stderr)
            return self._json(503, {"ok": False, "error": UNHEALTHY, "message": "저장소(PostgreSQL·MinIO)에 연결할 수 없습니다"},
                              path)
        return self._reply({"ok": True}, path)


def _index() -> ui.Response:
    return ui.Response(200, (ui.STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")


def _file_chunks(part: ui.FileSlice) -> Iterator[bytes]:
    with part.path.open("rb") as f:
        f.seek(part.start)
        left = part.length
        while left > 0:
            data = f.read(min(part.chunk, left))
            if not data:
                raise OSError("파일이 보내는 도중에 짧아졌습니다")
            yield data
            left -= len(data)


def create_app(settings: AuthSettings, backend: ServerBackend, *, users: auth.UserDirectory | None = None,
               registry: Registry | None = None, close_backend: bool = False) -> Starlette:
    """설정·저장소로 앱을 만든다. users 기본값은 PG(`auth.PgUsers`). close_backend면 종료 때 저장소를 닫는다."""
    ui.register_routes()
    handler = _ServerApp(settings, backend, users or auth.PgUsers(backend.db), registry or Registry())

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            handler.registry.close()
            await anyio.to_thread.run_sync(handler.registry.shutdown, SHUTDOWN_TIMEOUT)
            if close_backend:
                backend.close()

    app = Starlette(
        routes=[Route("/{path:path}", handler.dispatch, methods=METHODS)],
        middleware=[Middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie=auth.SESSION_COOKIE,
                               max_age=auth.SESSION_MAX_AGE, path="/", same_site="lax",
                               https_only=settings.secure_cookie)],
        lifespan=lifespan,
    )
    app.state.handler = handler
    return app


def from_env() -> Starlette:
    """env → 설정(틀리면 ScriptError로 시작 거부) → 저장소 열기 → 앱."""
    settings = ServerSettings.from_env(os.environ)
    backend = ServerBackend.open(settings.storage)
    return create_app(settings.auth, backend, close_backend=True)


class _LazyApp:
    """uvicorn `server.asgi:app`: import 때가 아니라 첫 호출(lifespan 시작) 때 env로 앱을 만든다(테스트 import가 env를 요구하지 않게)."""

    def __init__(self, factory: Callable[[], ASGIApp]) -> None:
        self._factory = factory
        self._app: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            self._app = self._factory()
        await self._app(scope, receive, send)


app = _LazyApp(from_env)
