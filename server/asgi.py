"""서버판 ASGI 앱(서버 설계 §4.1·§4.2·§8, ADR-S5): Starlette 위에서 로컬 UI의 라우트 표(`scripts.ui.server.ROUTES`)를 그대로 부른다.

요청 하나의 흐름(`_dispatch`):
1. 경로는 **퍼센트 인코딩 그대로**(`scope["raw_path"]`, 쿼리 제외)를 쓴다. stdlib 서버의 `urlsplit(self.path).path`와 같아서
   기능 가드(`feature_guard`)와 라우팅(`match`, 그룹만 디코딩)이 같은 문자열을 본다(`/api/%6Cibrary`는 가드에도 라우트에도 안 걸려 404).
2. `/healthz`(인증 없음, PG·MinIO 확인) → CSRF(안전하지 않은 메서드: Origin + JSON, `auth.csrf_error`) → `/auth/*`
   (로그인 화면, Google 로그인 시작·콜백, 이메일+비밀번호 로그인, dev 로그인, 로그아웃) → 세션 확인(**요청마다** users의 disabled·session_epoch, Ctx를 만들기 전) →
   서버가 덮어쓰는 기본 라우트(`/`, `/api/ping`, `/api/me`, `/api/shutdown` 404, `/files/<key>`) →
   관리 API(`/api/admin/*`, 관리자만, `server.admin_api`) → `match` → 기능 가드(서버판은 §8 전부 끔)
   → 본문(10MB, `parse_json_body`) → 작업 스레드에서 `edition("server")` + `ctx.use(Ctx(user_id, backend))` 안에서 `call`.
   핸들러가 시작한 작업 스레드는 `copy_context()`로 Ctx·판 문맥을 이어받는다(JobManager.start).
3. `req.app` = `App(root=None, token="", port=0, server=None, jobs=registry.for_user(uid))`. 핸들러가 쓰는 것은 `jobs`뿐.
4. 응답 헤더: 로컬과 같은 표준 헤더(`normalize`) + CSP(앱 화면 `APP_CSP`, `/files`는 스크립트 없는 `FILES_CSP`, SVG는 `SVG_CSP`)
   + https면 HSTS. 접근 로그는 남기지 않는다(쿼리에 비밀이 없더라도 경로만 필요하면 uvicorn 설정으로).

`app`(uvicorn `server.asgi:app`)은 처음 호출(lifespan 시작) 때 env로 설정·저장소를 만든다. 설정이 틀리면 시작하지 않는다.
테스트는 `create_app(settings, backend, users=...)`로 만든다.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import string
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
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
from server import admin_api, auth, files, login_limit, oidc, passwords
from server.backend import ServerBackend
from server.config import DEFAULT_ENV, PRODUCTION, AuthSettings, LlmSettings, ServerSettings
from server.jobs_registry import Registry, install_runtime, uninstall_runtime

EDITION: Final = "server"
SERVER_FEATURES: Final = tuple(ui.FEATURE_PREFIXES)      # §8: server v1 turns every optional feature off
LOGIN_PATH: Final = "/auth/login"
SHUTDOWN_TIMEOUT: Final = 60.0
APP_CSP: Final = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                  "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# User files (conti/report HTML with inline styles, images): no scripts at all; the report page frames them (same origin)
# `sandbox allow-same-origin`: scripts, forms, popups and top navigation stay off; allow-same-origin keeps the document's
# origin so `img-src 'self'` still matches its frame/art images (an opaque origin would not match 'self' in every browser).
FILES_CSP: Final = ("default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; "
                    "frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox allow-same-origin")
HSTS: Final = "max-age=31536000"
RESTARTING: Final = {"error": "restarting", "message": "서버가 다시 시작하는 중입니다. 잠시 뒤 새로고침하세요"}
UNHEALTHY: Final = "unhealthy"
# 500 bodies in the server edition: details (exception text may carry paths or values) stay in the log only
INTERNAL_MESSAGE: Final = "서버 오류가 났습니다. 잠시 뒤 다시 시도해 주세요"
STARTING: Final = {"error": "starting", "message": "서버가 아직 시작하는 중입니다. 잠시 뒤 새로고침하세요"}
PASSWORD_PATH: Final = "/auth/password"
# scrypt needs ~32 MiB and tens of ms per call: at most HASH_SLOTS run at once. The others wait in the event loop
# (anyio.CapacityLimiter), not in a worker thread, so queued logins never take the default thread pool that session
# checks and API calls use. More than MAX_HASH_WAITING already waiting -> 503 (not counted as a failed guess).
HASH_SLOTS: Final = 2
MAX_HASH_WAITING: Final = 16
UNKNOWN_CLIENT: Final = "unknown"
LOGIN_UNAVAILABLE: Final = {"error": "unavailable", "message": "지금은 로그인할 수 없습니다. 잠시 뒤 다시 시도하세요"}
ADMIN_ONLY: Final = {"error": "admin_only", "message": "관리자만 쓸 수 있습니다"}
STORE_UNAVAILABLE: Final = {"error": "unavailable", "message": "저장소에 연결할 수 없습니다. 잠시 뒤 다시 시도하세요"}


def _web(name: str) -> bytes:
    return resources.files("server").joinpath("web", name).read_bytes()


def too_many_attempts(retry_after: int) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
    """429 응답(본문, Retry-After 헤더). 분은 올림."""
    minutes = max(1, math.ceil(retry_after / 60))
    return 429, {"error": "too_many_attempts", "message": f"로그인 시도가 너무 많습니다. {minutes}분 뒤 다시 시도하세요",
                 "retry_after": retry_after}, [("Retry-After", str(retry_after))]


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
    is_admin: bool = False


auth_log = logging.getLogger("ytc.auth")


class _ServerApp:
    """요청 처리기. Starlette 라우트 하나(`/{path:path}`)가 모든 요청을 여기로 보낸다."""

    def __init__(self, settings: AuthSettings, backend: ServerBackend, users: auth.UserDirectory,
                 registry: Registry, google: oidc.GoogleOidc | None = None,
                 login_limits: login_limit.LoginLimiter | None = None,
                 admin: admin_api.AdminStore | None = None,
                 lifecycle: admin_api.UserLifecycle | None = None) -> None:
        self.settings = settings
        self.backend = backend
        self.admin = admin
        self.lifecycle: admin_api.UserLifecycle = lifecycle or ServerLifecycle(backend, registry)
        self.users = users
        self.registry = registry
        self.google = google
        self.login_limits = login_limits or login_limit.LoginLimiter()
        self._hash_limiter: anyio.CapacityLimiter | None = None

    @property
    def admin_info(self) -> admin_api.AdminInfo:
        """관리 화면의 서버 정보(env 값, 화면에서 바꾸지 않는다)."""
        return admin_api.AdminInfo(
            default_daily_usd=self.backend.daily_usd, google=self.google is not None,
            password_login=self.settings.password_login, dev_login=self.settings.dev_login,
            features_off=tuple((name, ui.FEATURE_TEXT.get(name, "")) for name in sorted(SERVER_FEATURES)))

    @property
    def hash_limiter(self) -> anyio.CapacityLimiter:
        """scrypt 동시 실행 한도. 이벤트 루프 안에서 처음 쓸 때 만든다(anyio 객체는 루프 밖에서 만들 수 없다)."""
        if self._hash_limiter is None:
            self._hash_limiter = anyio.CapacityLimiter(HASH_SLOTS)
        return self._hash_limiter

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
            body = norm.body
            if norm.status == 500 and norm.content_type.startswith("application/json"):
                print(f"500 {path}: {body[:2000].decode('utf-8', 'replace')}", file=sys.stderr)
                body = json.dumps({"error": "internal", "message": INTERNAL_MESSAGE}, ensure_ascii=False).encode("utf-8")
            resp = StarletteResponse(body, status_code=norm.status, media_type=norm.content_type)
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
        return _User(row.id, row.email, row.is_admin)

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
        if method == "GET" and path == "/api/me":
            return self._reply({"email": user.email, "is_admin": user.is_admin}, path)
        if path == "/api/shutdown":
            return self._reply(ui.NOT_FOUND, path)
        if path.startswith(admin_api.PREFIX):
            return await self._admin_route(request, method, path, user)
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

    # ---- /api/admin --------------------------------------------------------------

    async def _admin_route(self, request: StarletteRequest, method: str, path: str, user: _User) -> StarletteResponse:
        """관리 API: 관리자가 아니면 본문도 읽지 않고 403(DB 함수도 따로 거부한다). 비밀번호가 오가는 요청은 scrypt 슬롯 안에서."""
        if not user.is_admin:
            auth_log.warning("admin api refused: user %s is not an admin", user.id)
            return self._json(403, dict(ADMIN_ONLY), path)
        if self.admin is None:
            return self._reply(ui.NOT_FOUND, path)
        store = self.admin
        try:
            body = await self._json_body(request) if method in auth.UNSAFE_METHODS else {}
        except ScriptError as e:
            return self._reply(ui.error_result(e), path)
        query = dict(parse_qsl(request.scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))

        def run() -> tuple[int, dict[str, Any]]:
            return admin_api.handle(store, user.id, method, path, query, body, info=self.admin_info,
                                    life=self.lifecycle)

        limiter = self.hash_limiter if admin_api.needs_hash(method, path) else None
        try:
            result = await anyio.to_thread.run_sync(run, limiter=limiter)
        except ScriptError as e:
            return self._reply(ui.error_result(e), path)
        except admin_api.AdminError as e:
            auth_log.warning("admin api refused: %s (user %s, %s %s)", e.code, user.id, method, path)
            return self._json(e.status, e.payload(), path)
        except psycopg.Error as e:
            auth_log.error("admin api unavailable: %s", type(e).__name__)
            return self._json(503, dict(STORE_UNAVAILABLE), path)
        if method in auth.UNSAFE_METHODS and result[0] == 200:
            auth_log.info("admin api: user %s %s %s", user.id, method, path)
        return self._reply(result, path)

    # ---- /auth -------------------------------------------------------------------

    async def _auth_route(self, request: StarletteRequest, method: str, path: str) -> StarletteResponse:
        if method == "GET" and path == LOGIN_PATH:
            if await self._current_user(request) is not None:
                return self._redirect("/", path)
            return self._reply(ui.Response(200, _web("login.html"), "text/html; charset=utf-8"), path)
        if method == "GET" and path == "/auth/login.js":
            return self._reply(ui.Response(200, _web("login.js"), "text/javascript; charset=utf-8"), path)
        if method == "GET" and path == "/auth/dom.js":         # the app's h() helper, public for the login screen
            return self._reply(ui.Response(200, (ui.STATIC_DIR / "dom.js").read_bytes(),
                                           "text/javascript; charset=utf-8"), path)
        if method == "GET" and path == "/auth/methods":
            return self._reply({"dev_login": self.settings.dev_login, "google": self.google is not None,
                                "password": self.settings.password_login}, path)
        if method == "POST" and path == PASSWORD_PATH and self.settings.password_login:
            return await self._password_login(request, path)
        if method == "GET" and path == "/auth/google" and self.google is not None:
            return self._google_start(request, self.google, path)
        if method == "GET" and path == oidc.CALLBACK_PATH and self.google is not None:
            return await self._google_callback(request, self.google, path)
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
        await self._record_login(row.id)
        return self._reply({"ok": True, "email": row.email}, path)

    async def _record_login(self, user_id: uuid.UUID) -> None:
        """마지막 로그인 시각(0004). 편의 정보라서 실패해도 로그인은 그대로 두고 로그만 남긴다."""
        try:
            await anyio.to_thread.run_sync(self.users.record_login, user_id)
        except Exception as e:  # noqa: BLE001 - logged; a missing timestamp must not turn a good login into a 500
            auth_log.error("last login not recorded for user %s: %s", user_id, type(e).__name__)

    # ---- 이메일+비밀번호 -------------------------------------------------------------

    def _password_attempt(self, email: str, password: str) -> tuple[auth.UserRow | None, str]:
        """작업 스레드에서: 행 조회 → (항상) scrypt 한 번 → 판정. 모르는 이메일·비밀번호 없는 계정은 더미 해시로 같은 비용.
        비활성 계정도 검증을 끝낸 뒤 거부한다. 돌려주는 이유 코드는 로그에만 쓴다."""
        found = self.users.password_login(email)
        row, stored = found if found is not None else (None, None)
        try:
            ok = passwords.verify_password(password, stored)
        except passwords.BadHash as e:
            auth_log.error("password login refused: bad_hash (user %s: %s)", row.id if row else "?", e)
            return None, "bad_hash"
        if row is None:
            return None, "not_invited"
        if stored is None:
            return None, "no_password"
        if not ok:
            return None, "wrong_password"
        if row.disabled:
            return None, "disabled"
        return row, "ok"

    async def _password_login(self, request: StarletteRequest, path: str) -> StarletteResponse:
        """`POST /auth/password {email, password}`: 초대·비활성 판정은 Google과 같고, 실패는 모두 `LOGIN_FAILED`.
        이메일별·IP별 실패 횟수가 넘으면 비밀번호를 확인하지 않고 429. 성공하면 그 이메일의 실패 기록을 지운다."""
        try:
            body = await self._json_body(request)
        except ScriptError as e:
            return self._reply(ui.error_result(e), path)
        email, password = body.get("email"), body.get("password")
        if not isinstance(email, str) or not email.strip() or not isinstance(password, str):
            return self._json(400, {"error": "bad_request", "message": "email과 password가 필요합니다"}, path)
        if not passwords.encodable(email):            # a lone surrogate from JSON cannot reach the database driver
            return self._json(400, {"error": "bad_request", "message": "이메일 형식이 아닙니다"}, path)
        ip = request.client.host if request.client is not None else UNKNOWN_CLIENT
        wait = self.login_limits.begin(email, ip)        # counts this attempt while it runs (parallel requests)
        if wait is not None:
            auth_log.warning("password login refused: throttled")
            status, payload, headers = too_many_attempts(wait)
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return self._reply(ui.Response(status, body_bytes, "application/json; charset=utf-8", headers), path)
        outcome = "aborted"                                # an exception (e.g. database down) is not a failed guess
        try:
            limiter = self.hash_limiter
            if limiter.statistics().tasks_waiting >= MAX_HASH_WAITING:
                auth_log.warning("password login refused: hash queue full")
                return self._json(503, dict(LOGIN_UNAVAILABLE), path)
            row, reason = await anyio.to_thread.run_sync(self._password_attempt, email, password, limiter=limiter)
            outcome = "failed" if row is None else "succeeded"
        except psycopg.Error as e:
            auth_log.error("password login unavailable: %s", type(e).__name__)
            return self._json(503, dict(LOGIN_UNAVAILABLE), path)
        finally:
            self.login_limits.end(email, ip, outcome)
        if row is None:
            auth_log.warning("password login refused: %s", reason)
            return self._json(401, dict(auth.LOGIN_FAILED), path)
        auth.start_session(request.session, row)
        await self._record_login(row.id)
        auth_log.info("password login: user %s", row.id)
        return self._reply({"ok": True, "email": row.email}, path)

    # ---- Google OIDC -------------------------------------------------------------

    def _google_start(self, request: StarletteRequest, google: oidc.GoogleOidc, path: str) -> StarletteResponse:
        """대기 로그인 하나(이전 것은 덮어쓴다: 쿠키가 쌓이지 않고 마지막 시도만 유효)를 세션에 두고 Google로.
        요청의 쿼리(next·redirect_uri 등)는 읽지 않는다: 돌아올 주소는 설정값, 로그인 뒤에는 항상 `/`."""
        pending = oidc.new_pending(google.now())
        request.session[oidc.PENDING_KEY] = pending.to_session()
        return self._redirect(google.authorization_url(pending), path)

    async def _google_callback(self, request: StarletteRequest, google: oidc.GoogleOidc,
                               path: str) -> StarletteResponse:
        pending = oidc.PendingLogin.from_session(request.session.pop(oidc.PENDING_KEY, None))   # one use only
        email: str | None = None
        try:
            if pending is None:
                raise oidc.OidcError("state")             # no login started in this browser (or already used)
            code = oidc.check_callback(pending, dict(request.query_params), google.now())
            claims = await google.login(code, pending)
            email = claims.email
            row = await anyio.to_thread.run_sync(auth.admit_google, self.users, claims)
        except oidc.OidcError as e:
            auth_log.warning("google login refused: %s", e.reason)
            return self._refusal(e.reason, None, path)
        except auth.LoginDenied as e:
            auth_log.warning("google login refused: %s", e.reason)
            return self._refusal(e.reason, email, path)
        auth.start_session(request.session, row)         # drops everything else in the session
        await self._record_login(row.id)
        auth_log.info("google login: user %s", row.id)
        return self._redirect("/", path)

    def _refusal(self, reason: str, email: str | None, path: str) -> StarletteResponse:
        status, message = auth.LOGIN_REFUSALS[reason]
        text = message.format(email=email or "")
        page = string.Template(_web("message.html").decode("utf-8")).substitute(
            title=html.escape("로그인하지 못했습니다"), message=html.escape(text))
        return self._reply(ui.Response(status, page.encode("utf-8"), "text/html; charset=utf-8"), path)

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


class ServerLifecycle:
    """관리 화면 지우기·이메일 변경의 DB 밖 정리(`admin_api.UserLifecycle`): 그 사용자의 작업 멈춤, MinIO 객체, 이메일 캐시."""

    def __init__(self, backend: ServerBackend, registry: Registry) -> None:
        self._backend = backend
        self._registry = registry

    def stop_jobs(self, user: uuid.UUID) -> None:
        stopped = self._registry.drop(user)
        if stopped:
            auth_log.info("admin delete: cancelled %d jobs of user %s", stopped, user)

    def resume_jobs(self, user: uuid.UUID) -> None:
        self._registry.undrop(user)

    def purge_storage(self, user: uuid.UUID, s3_keys: list[str]) -> dict[str, Any]:
        return admin_api.purge_user_objects(self._backend.bucket, user, s3_keys)

    def forget(self, user: uuid.UUID) -> None:
        self._backend.forget_user(user)


class _Endpoint:
    """모든 메서드를 `dispatch`로(함수 엔드포인트는 Starlette가 GET만 받는다). 모르는 메서드는 JSON 404·보안 헤더."""

    def __init__(self, handler: _ServerApp) -> None:
        self._handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = await self._handler.dispatch(StarletteRequest(scope, receive))
        await response(scope, receive, send)


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
               registry: Registry | None = None, close_backend: bool = False,
               runtime: LlmSettings | None = None, google: oidc.GoogleOidc | None = None,
               login_limits: login_limit.LoginLimiter | None = None,
               admin: admin_api.AdminStore | None = None,
               lifecycle: admin_api.UserLifecycle | None = None) -> Starlette:
    """설정·저장소로 앱을 만든다. users 기본값은 PG(`auth.PgUsers`). close_backend면 종료 때 저장소를 닫는다.
    runtime을 주면 지금 전역 자원 슬롯·수집 잠금·LLM 고정을 설치하고 종료 때 걷는다(`jobs_registry.install_runtime`).
    google 기본값은 설정에 Google 클라이언트가 있으면 실제 Google(`oidc.GoogleOidc`), 없으면 Google 로그인 없음.
    테스트는 가짜 IdP 전송 계층을 넣은 GoogleOidc를 준다. login_limits 기본값은 이메일 10분에 5번·IP 10분에 20번(`server.login_limit`).
    admin 기본값은 PG 관리 함수(`admin_api.PgAdmin(backend.db)`), lifecycle 기본값은 `ServerLifecycle`(작업·MinIO·캐시)."""
    ui.register_routes()
    if google is None and settings.google is not None:
        google = oidc.GoogleOidc(settings.google)
    handler = _ServerApp(settings, backend, users or auth.PgUsers(backend.db), registry or Registry(), google,
                         login_limits, admin if admin is not None else admin_api.PgAdmin(backend.db), lifecycle)
    if runtime is not None:
        install_runtime(runtime, backend.db)
    if settings.password_login:
        passwords.dummy_hash()                     # pay the one-time dummy hash at start, not on the first login

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            handler.registry.close()
            await anyio.to_thread.run_sync(handler.registry.shutdown, SHUTDOWN_TIMEOUT)
            if runtime is not None:
                uninstall_runtime()
            if close_backend:
                backend.close()

    app = Starlette(
        routes=[Route("/{path:path}", _Endpoint(handler))],      # ASGI endpoint: every method reaches dispatch
        middleware=[Middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie=auth.SESSION_COOKIE,
                               max_age=auth.SESSION_MAX_AGE, path="/", same_site="lax",
                               https_only=settings.secure_cookie)],
        lifespan=lifespan,
    )
    app.state.handler = handler
    return app


log = logging.getLogger("ytc.server")


def log_startup_warnings(env: Mapping[str, str]) -> None:
    """시작은 막지 않지만 운영자가 알아야 할 설정: production에서 ANTHROPIC_API_KEY가 비면 모든 AI 호출이
    `llm_not_configured`로 실패한다(키 없이 스모크·화면 점검은 할 수 있게 거부하지 않는다)."""
    name = (env.get("YTC_ENV") or "").strip().lower() or DEFAULT_ENV
    if name == PRODUCTION and not (env.get("ANTHROPIC_API_KEY") or "").strip():
        log.warning("ANTHROPIC_API_KEY is empty: every AI call will fail with llm_not_configured until it is set")


def from_env() -> Starlette:
    """env → 설정(틀리면 ScriptError로 시작 거부) → 저장소 열기 → 앱."""
    settings = ServerSettings.from_env(os.environ)
    log_startup_warnings(os.environ)
    backend = ServerBackend.open(settings.storage, daily_usd=settings.llm.daily_usd)
    return create_app(settings.auth, backend, close_backend=True, runtime=settings.llm)


class _LazyApp:
    """uvicorn `server.asgi:app`(`--lifespan on`): import 때가 아니라 **lifespan 시작** 때 env로 앱을 한 번 만든다.

    - 만들다 실패하면(설정 오류 `config_missing`·`config_invalid`, DB·MinIO 연결 실패 …) `lifespan.startup.failed`를 보내
      uvicorn이 시작하지 않고 끝나게 한다. 다시 만들지 않는다.
    - 시작 전(또는 실패 뒤)의 http 요청은 503 `starting`, 그 밖의 scope는 거절한다.
    - 앱의 lifespan에는 이미 받은 `lifespan.startup` 메시지를 먼저 다시 넘긴다.
    """

    def __init__(self, factory: Callable[[], ASGIApp]) -> None:
        self._factory = factory
        self._app: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        if self._app is None:
            if scope["type"] != "http":
                raise RuntimeError("app is not started (lifespan startup has not completed)")
            await StarletteResponse(json.dumps(STARTING, ensure_ascii=False).encode("utf-8"), status_code=503,
                                    media_type="application/json; charset=utf-8")(scope, receive, send)
            return
        await self._app(scope, receive, send)

    async def _lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        first = await receive()
        if self._app is None and first.get("type") == "lifespan.startup":
            try:
                self._app = await anyio.to_thread.run_sync(self._factory)
            except Exception as e:  # noqa: BLE001 - every startup failure must stop the server, with its reason
                await send({"type": "lifespan.startup.failed", "message": f"{getattr(e, 'code', type(e).__name__)}: {e}"})
                return
        if self._app is None:
            await send({"type": "lifespan.startup.failed", "message": "app is not available"})
            return
        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return first
            return await receive()

        await self._app(scope, replay, send)


app = _LazyApp(from_env)
