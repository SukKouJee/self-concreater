"""컨테이너 스택 스모크: build -> up --wait -> /healthz -> /api/status -> down -v.

사용법:  python deploy/smoke.py local [--project ytc-t3] [--image ytc:local-t3] [--with-youtube]
         python deploy/smoke.py server [--project ytc-t13] [--image ytc:server-t13] [--no-browser]

- server: deploy/server/docker-compose.yml 전체(postgres·minio·버킷·migrate·app)를 YTC_ENV=test + dev 로그인 + 비밀번호 로그인으로 띄우고
  admin add-user 2명 -> dev 로그인 -> /healthz·/api/status -> 기획안 저장·읽기 -> 두 사용자 격리 1건 ->
  add-user --password-stdin -> POST /auth/password(틀림 401·맞음 200) ->
  admin make-admin -> 관리 API(/api/me, 사용자 목록에 해시 없음, 일반 사용자 403, 다른 사람 모든 기기 로그아웃, 자기 비활성 409, 감사 기록) ->
  (Chrome이 있으면) 로그인 화면의 비밀번호 폼(틀림 문구·맞으면 /로 이동)과 /files의 HTML이 FILES_CSP(sandbox allow-same-origin) 아래서
  이미지를 그리는지 -> 게시 포트로 보낸 X-Forwarded-For를 믿지 않는지(IP별 실패 제한 429) -> down -v.
  Chrome은 YTC_CHROME 또는 흔한 설치 경로에서 찾는다. 없으면 그 점검만 건너뛴다고 출력한다(--no-browser로 끌 수도 있다).

- 임시 작업 폴더와 가짜 API 키만 쓴다(실제 Claude 호출 없음). 표준 라이브러리만 쓴다.
- `--with-youtube`: tests/fixtures/links.txt의 첫 링크 1개로 `scripts.fetch`와 `scripts.cuts`(360p 다운로드·장면 분석)를
  컨테이너 안에서 돌려 yt-dlp가 JS 런타임 없이 되는지 본다(LLM 호출 없음, 네트워크 사용). 기본은 끔.
- 종료 코드 0이면 통과. 끝나면 항상 `docker compose down -v`로 정리한다(이미지는 남긴다).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIR = ROOT / "deploy" / "local"
SERVER_DIR = ROOT / "deploy" / "server"
FAKE_KEY = "sk-ant-SMOKE-NOT-A-REAL-KEY"
UP_TIMEOUT_SEC = 180
LINKS = ROOT / "tests" / "fixtures" / "links.txt"


class SmokeFailure(Exception):
    pass


def check(cond: bool, message: str) -> None:
    if not cond:
        raise SmokeFailure(message)
    print(f"ok  {message}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run(cmd: list[str], env: dict[str, str], timeout: int = 900, ok_codes: tuple[int, ...] = (0,),
        stdin_text: str | None = None) -> str:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, input=stdin_text)
    if proc.returncode not in ok_codes:
        raise SmokeFailure(f"{' '.join(cmd[:6])} ... 종료 코드 {proc.returncode}\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")
    return proc.stdout + proc.stderr


def http(url: str, key: str | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, headers={"X-YTC-Key": key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, body = r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


def parse_ps(text: str) -> list[dict]:
    """`docker compose ps --format json`은 버전에 따라 JSON 배열이거나 줄마다 객체(NDJSON)다."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return list(json.loads(text))
    return [json.loads(ln) for ln in text.splitlines() if ln.strip().startswith("{")]


def first_link() -> str:
    for line in LINKS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return line
    raise SmokeFailure("tests/fixtures/links.txt에 링크가 없습니다")


def smoke_local(project: str, image: str, with_youtube: bool) -> None:
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="ytc-smoke-") as tmp:
        tmp_path = Path(tmp)
        work = tmp_path / "work"
        work.mkdir()
        env_file = tmp_path / "smoke.env"
        env_file.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        override = tmp_path / "override.yml"
        override.write_text(f'services:\n  ytc:\n    env_file:\n      - "{env_file.as_posix()}"\n', encoding="utf-8")
        env = dict(os.environ, YTC_WORK_DIR=str(work), YTC_PORT=str(port), YTC_IMAGE=image)
        if hasattr(os, "getuid"):                      # Linux: 임시 폴더 소유자와 컨테이너 uid를 맞춘다
            env.update(YTC_UID=str(os.getuid()), YTC_GID=str(os.getgid()))
        compose = ["docker", "compose", "-p", project, "-f", str(LOCAL_DIR / "docker-compose.yml"), "-f", str(override)]
        try:
            run(compose + ["up", "-d", "--build", "--wait", "--wait-timeout", str(UP_TIMEOUT_SEC)], env, timeout=1500)
            check(True, "build + up --wait (healthcheck healthy)")

            logs = run(compose + ["logs", "--no-log-prefix", "ytc"], env)
            m = re.search(r'"url":\s*"(http://127\.0\.0\.1:%d/\?key=([^"&]+))"' % port, logs)
            check(m is not None, "로그에 ?key= 주소가 나온다")
            assert m is not None
            base, key = f"http://127.0.0.1:{port}", m.group(2)

            ps = json.loads("[" + ",".join(run(compose + ["ps", "--format", "json"], env).split("\n")[:-1] or ["{}"]) + "]")
            publishers = [p for row in ps for p in row.get("Publishers") or []]
            check(bool(publishers) and all(p["URL"] == "127.0.0.1" for p in publishers), "게시 포트는 127.0.0.1에만 열린다")

            status, body = http(f"{base}/healthz")
            check(status == 200 and body == {"ok": True}, "/healthz 200 {ok: true} (인증 없음)")
            status, _ = http(f"{base}/api/status")
            check(status == 403, "키 없이 /api/status는 403")
            status, body = http(f"{base}/api/status", key)
            check(status == 200 and isinstance(body, dict), f"/api/status 200 (키 있음) keys={sorted(body)[:6] if isinstance(body, dict) else body}")
            status, body = http(f"{base}/api/features", key)
            disabled = body.get("disabled", {}) if isinstance(body, dict) else {}
            check(status == 200 and {"library", "caption_video"} <= set(disabled), "/api/features: library·caption_video 비활성")

            uid = run(compose + ["exec", "-T", "ytc", "id", "-u"], env).strip()
            check(uid != "0", f"비루트로 실행(uid={uid})")
            engine = run(compose + ["exec", "-T", "ytc", "python", "-c",
                                     "from scripts import llm; print(llm.config())"], env).strip()
            check("anthropic-api" in engine, f"엔진 env 고정 (yt-coach.json 없이): {engine}")
            check((work / "workspace" / "ui.lock").is_file(), "작업 폴더 볼륨에 workspace/ui.lock 생성")

            if with_youtube:
                url = first_link()
                # 자막 없는 영상은 no_transcript(종료 코드 1)지만 yt-dlp 메타데이터 수집은 끝난 상태(raw_status: fetched)다
                out = run(compose + ["exec", "-T", "ytc", "python", "-m", "scripts.fetch", url], env, timeout=300,
                          ok_codes=(0, 1))
                fetched = json.loads(out.strip().splitlines()[-1])
                check(fetched.get("error") in (None, "no_transcript") and bool(fetched.get("title")),
                      f"yt-dlp 메타데이터 수집 통과: {fetched.get('title')} ({fetched.get('error', 'ok')})")
                out = run(compose + ["exec", "-T", "ytc", "python", "-m", "scripts.cuts", url], env, timeout=600)
                check('"error"' not in out, f"360p 다운로드·장면 분석 통과: {out.strip()[:160]}")
        finally:
            try:                                           # 정리 실패가 원래 실패를 가리지 않게 한다
                run(compose + ["down", "-v", "--remove-orphans"], env, timeout=300)
                print("down -v 완료", flush=True)
            except (SmokeFailure, subprocess.TimeoutExpired) as e:
                print(f"WARN down -v 실패(수동 정리 필요: docker compose -p {project} down -v): {e}", file=sys.stderr)


# ---- server edition ------------------------------------------------------------------------------------------

CATEGORY = "travel"
DRAFT: dict[str, Any] = {
    "title": "편의점 라면 3단계 맵기 도전!",
    "topic": "편의점 라면",
    "source": "M-mukbang-001",
    "archetype": "A1",
    "hook_type": "result_first",
    "narrative_type": "journey",
    "sections": [
        {"label": "hook", "start": 0, "end": 3, "script": "자막: 3단계 다 먹을 수 있을까?",
         "shots": [{"size": "탑뷰", "angle": "탑뷰", "sec": 3, "action": "완성된 라면 3개"}]},
        {"label": "body", "sub": "story", "start": 3, "end": 52, "script": "단계별로 먹으며 반응 자막",
         "shots": [{"size": "클로즈업", "sec": 2, "action": "면 들기"}] * 20 + [{"size": "인서트", "sec": 9, "action": "물컵"}]},
        {"label": "cta", "start": 52, "end": 60, "script": "다음 단계 추천 댓글",
         "shots": [{"size": "탑뷰", "sec": 8, "action": "빈 컵 3개"}]},
    ],
    "prep": ["컵라면 3종", "삼각대"],
}
FRAME_VIDEO = "smokevid001"
SESSION_COOKIE = "ytc_session"
PW_EMAIL = "carol@example.com"
LOGIN_FAILED = "이메일 또는 비밀번호가 맞지 않습니다"
IP_FAILURES = 20                                   # server.login_limit.IP_FAILURES
FILES_CSP_PARTS = ("default-src 'none'", "img-src 'self' data:", "sandbox allow-same-origin")

# 컨테이너 안에서 앱과 같은 설정으로 저장소를 열어 카테고리·HTML·이미지를 넣는다(앱 임시 폴더는 건드리지 않게 따로 지정).
SEED_SCRIPT = r"""
import os, sys
import cv2, numpy as np
from scripts import ctx, keys, refstore
from scripts.ctx import Ctx
from server.backend import ServerBackend
from server.config import StorageSettings

uid, category, vid, with_html = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
backend = ServerBackend.open(StorageSettings.from_env(os.environ))
try:
    with ctx.use(Ctx(user_id=uid, backend=backend)):
        refstore.init_category(category)
        if with_html:
            c = ctx.here()
            ok, jpg = cv2.imencode(".jpg", np.full((32, 48, 3), (0, 128, 255), np.uint8))
            assert ok
            frame = keys.frames_dir(vid) + "0001.jpg"
            art = keys.art("drafts/smoke-conti.md", "c1", "12345678", "abcdef")
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30"><rect width="40" height="30" fill="#333"/></svg>'
            page = ('<!doctype html><meta charset="utf-8"><title>smoke</title><body><h1>smoke</h1>'
                    '<img id="frame" src="../' + frame + '"><img id="art" src="../' + art + '"></body>').encode("utf-8")
            c.shared.put(frame, jpg.tobytes())
            c.objects.put(art, svg)
            c.objects.put("drafts/smoke-conti.html", page)
            c.objects.put(keys.report_html(category, "2026-10-31"), page)
finally:
    backend.close()
"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Client:
    """쿠키를 모으고 Origin 헤더를 붙이는 얇은 HTTP 클라이언트(리디렉션은 따르지 않는다)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())

    def request(self, method: str, path: str, body: Any = None, origin: bool = True,
                extra: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
        headers = {"Origin": self.base} if origin and method != "GET" else {}
        headers.update(extra or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + quote(path, safe="/?&=:%"), data=data, method=method, headers=headers)
        try:
            resp: Any = self._opener.open(req, timeout=20)
        except urllib.error.HTTPError as e:
            resp = e
        raw = resp.read()
        code = int(resp.code)
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        try:
            return code, json.loads(raw), hdrs
        except ValueError:
            return code, raw, hdrs

    def session_cookie(self) -> tuple[str, str]:
        for c in self.jar:
            if c.name == SESSION_COOKIE and c.value is not None:
                return c.name, c.value
        raise SmokeFailure("로그인 뒤 세션 쿠키가 없습니다")


class Cdp:
    """Chrome DevTools Protocol 최소 클라이언트(표준 라이브러리만으로 websocket 구현). 브라우저 점검 전용."""

    def __init__(self, ws_url: str) -> None:
        parts = urlsplit(ws_url)
        self.sock = socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SmokeFailure("DevTools websocket 핸드셰이크 실패")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise SmokeFailure(f"DevTools websocket 거부: {head[:120]!r}")
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        self._id = 0
        self.events: list[dict[str, Any]] = []

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SmokeFailure("DevTools 연결이 끊겼습니다")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv(self) -> dict[str, Any]:
        message = b""
        while True:
            b1, b2 = self._read(2)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            message += self._read(length)
            if b1 & 0x80:                      # FIN
                break
        return dict(json.loads(message))

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._id += 1
        payload = json.dumps({"id": self._id, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        if len(payload) < 126:
            header += bytes([0x80 | len(payload)])
        elif len(payload) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(payload))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)
        while True:
            msg = self._recv()
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise SmokeFailure(f"CDP {method}: {msg['error']}")
                return dict(msg.get("result") or {})
            self.events.append(msg)

    def wait_event(self, name: str, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, ev in enumerate(self.events):
                if ev.get("method") == name:
                    return dict(self.events.pop(i))
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                self.events.append(self._recv())
            except socket.timeout:
                break
        raise SmokeFailure(f"CDP 이벤트 {name}을(를) {timeout:.0f}초 안에 받지 못했습니다")

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise SmokeFailure(f"브라우저 스크립트 오류: {result['exceptionDetails']}")
        return result["result"].get("value")

    def close(self) -> None:
        self.sock.close()


def find_chrome() -> str | None:
    candidates = [os.environ.get("YTC_CHROME", ""), shutil.which("google-chrome") or "", shutil.which("chromium") or "",
                  shutil.which("chromium-browser") or "", shutil.which("chrome") or "",
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    return next((c for c in candidates if c and Path(c).is_file()), None)


IMAGES_JS = ("JSON.stringify(Array.from(document.images).map(i => ({id: i.id, ok: i.complete && i.naturalWidth > 0, "
             "w: i.naturalWidth})))")
FRAME_JS = """
new Promise((resolve, reject) => {
  const f = document.createElement('iframe');
  f.src = '%s';
  f.onload = () => resolve(JSON.stringify({
    images: Array.from(f.contentDocument.images).map(i => ({id: i.id, ok: i.complete && i.naturalWidth > 0, w: i.naturalWidth})),
    sameOrigin: f.contentDocument.location.origin === location.origin}));
  document.body.appendChild(f);
  setTimeout(() => reject(new Error('iframe timeout')), 15000);
})
"""


LOGIN_FORM_JS = """
new Promise((resolve, reject) => {
  const t0 = Date.now();
  (function poll() {
    const form = document.getElementById('password-form');
    if (form) {
      document.getElementById('pw-email').value = %s;
      document.getElementById('pw-password').value = %s;
      form.requestSubmit();
      resolve(true);
      return;
    }
    if (Date.now() - t0 > 10000) { reject(new Error('password form did not appear')); return; }
    setTimeout(poll, 100);
  })();
})
"""
LOGIN_ERROR_JS = """
new Promise((resolve, reject) => {
  const t0 = Date.now();
  (function poll() {
    const error = document.getElementById('pw-error');
    if (error && !error.hidden) { resolve(error.textContent); return; }
    if (Date.now() - t0 > 10000) { reject(new Error('no error message')); return; }
    setTimeout(poll, 100);
  })();
})
"""


class BrowserUnavailable(Exception):
    """Chrome이 있지만 이 환경에서 뜨지 않는다(샌드박스·디스플레이 문제 등): 점검을 건너뛴다(실패 아님)."""


def collect_csp_violations(page: Any) -> list[str]:
    """지금까지 받은 로그 중 CSP 위반 문구. wait_event가 꺼낸 이벤트도 되돌려 놓고 훑는다(꺼낸 것을 버리면 위반 1건을 놓친다)."""
    try:
        page.events.append(page.wait_event("Log.entryAdded", timeout=1.0))   # 이미 와 있으면 바로, 없으면 시간 초과
    except SmokeFailure:
        pass
    texts = [str(e["params"]["entry"].get("text")) for e in page.events if e.get("method") == "Log.entryAdded"]
    return [text for text in texts if "Content Security Policy" in text]


def browser_check(chrome: str, base: str, cookie: tuple[str, str], files_path: str,
                  login: tuple[str, str] | None = None) -> None:
    """실제 브라우저로 /files의 HTML(콘티·리포트)이 FILES_CSP 아래서 이미지를 그리는지 본다:
    (1) 최상위로 열 때 (2) 앱 화면 안의 iframe으로 열 때(같은 출처 접근 포함). CSP 위반 로그가 있으면 실패.
    login(이메일, 비밀번호)을 주면 먼저 로그인 화면의 비밀번호 폼을 쓴다: 틀린 비밀번호 문구 -> 맞으면 /로 이동."""
    with tempfile.TemporaryDirectory(prefix="ytc-chrome-") as profile:
        port = free_port()
        args = [chrome, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--no-first-run",
                "--disable-gpu", "--disable-extensions", "about:blank"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise BrowserUnavailable(f"Chrome을 실행하지 못했습니다: {e}") from e
        cdp: Cdp | None = None
        try:
            ws_url = ""
            for _ in range(60):
                if proc.poll() is not None:
                    raise BrowserUnavailable(f"Chrome이 바로 종료했습니다(종료 코드 {proc.returncode})")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                        pages = [p for p in json.load(r) if p.get("type") == "page"]
                    if pages:
                        ws_url = pages[0]["webSocketDebuggerUrl"]
                        break
                except (urllib.error.URLError, OSError, ValueError):
                    pass
                time.sleep(0.5)
            if not ws_url:
                raise BrowserUnavailable("headless Chrome의 DevTools가 30초 안에 열리지 않았습니다")
            page = Cdp(ws_url)
            cdp = page
            for domain in ("Page", "Network", "Log"):
                page.call(f"{domain}.enable")

            def navigate(path: str) -> None:
                page.events.clear()
                page.call("Page.navigate", url=base + path)
                page.wait_event("Page.loadEventFired")
                time.sleep(0.5)

            def csp_violations() -> list[str]:
                return collect_csp_violations(page)

            if login is not None:
                email, password = login
                navigate("/auth/login")
                page.evaluate(LOGIN_FORM_JS % (json.dumps(email), json.dumps("not the password!")))
                shown = page.evaluate(LOGIN_ERROR_JS)
                check(shown == LOGIN_FAILED, f"브라우저: 로그인 화면 비밀번호 폼, 틀리면 문구가 보인다: {shown}")
                page.events.clear()
                page.evaluate(LOGIN_FORM_JS % (json.dumps(email), json.dumps(password)))
                page.wait_event("Page.loadEventFired")
                time.sleep(0.5)
                where = page.evaluate("location.pathname")
                check(where == "/", f"브라우저: 맞는 비밀번호로 로그인하면 앱 화면(/)으로 간다: {where}")
                check(not csp_violations(), "브라우저: 로그인 화면·앱 화면에 CSP 위반 로그 없음")

            page.call("Network.setCookie", name=cookie[0], value=cookie[1], url=base)
            navigate(files_path)
            top = json.loads(page.evaluate(IMAGES_JS))
            check(len(top) == 2 and all(i["ok"] for i in top), f"브라우저: {files_path} 를 최상위로 열어도 이미지 2개(JPEG·SVG)가 그려진다 {top}")
            check(not csp_violations(), "브라우저: 최상위 문서에 CSP 위반 로그 없음")
            navigate("/")
            framed = json.loads(page.evaluate(FRAME_JS % (base + files_path)))
            check(framed["sameOrigin"] and len(framed["images"]) == 2 and all(i["ok"] for i in framed["images"]),
                  f"브라우저: 앱 화면의 iframe 안에서도 같은 출처로 접근되고 이미지가 그려진다 {framed}")
            check(not csp_violations(), "브라우저: iframe 문서에 CSP 위반 로그 없음")
        finally:
            if cdp is not None:
                cdp.close()
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()                    # 이 스모크가 띄운 Chrome 프로세스 핸들만 끈다


def teardown(compose: list[str], env: dict[str, str], project: str) -> str | None:
    """down -v 후 이 프로젝트의 컨테이너·볼륨이 남지 않았는지 확인한다. 문제가 있으면 설명 문자열(예외를 던지지 않는다)."""
    problems: list[str] = []
    try:
        run(compose + ["--profile", "admin", "--profile", "tls", "down", "-v", "--remove-orphans"], env, timeout=300)
        print("down -v 완료", flush=True)
    except (SmokeFailure, subprocess.TimeoutExpired, OSError) as e:
        problems.append(f"down -v 실패: {e}")
    label = f"label=com.docker.compose.project={project}"
    for what, cmd in (("컨테이너", ["docker", "ps", "-a", "-q", "--filter", label]),
                      ("볼륨", ["docker", "volume", "ls", "-q", "--filter", label])):
        try:
            left = run(cmd, env).split()
        except (SmokeFailure, subprocess.TimeoutExpired, OSError) as e:
            problems.append(f"남은 {what} 확인 실패: {e}")
            continue
        if left:
            problems.append(f"남은 {what}: {left} (수동 정리: docker compose -p {project} down -v)")
    return "; ".join(problems) or None


def last_json(text: str) -> dict[str, Any]:
    lines = [ln for ln in text.strip().splitlines() if ln.strip().startswith("{")]
    if not lines:
        raise SmokeFailure(f"JSON 출력이 없습니다: {text[-400:]}")
    return dict(json.loads(lines[-1]))


def smoke_server(project: str, image: str, with_browser: bool) -> None:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="ytc-smoke-server-") as tmp:
        env_file = Path(tmp) / "smoke.env"
        env_file.write_text("\n".join([
            "YTC_ENV=test", "YTC_DEV_LOGIN=1", "YTC_PASSWORD_LOGIN=1", f"YTC_PUBLIC_URL={base}", f"YTC_PORT={port}", f"YTC_IMAGE={image}",
            f"YTC_SESSION_SECRET={secrets.token_urlsafe(48)}",
            f"YTC_DB_OWNER_PASSWORD={secrets.token_urlsafe(24)}", f"YTC_DB_APP_PASSWORD={secrets.token_urlsafe(24)}",
            f"YTC_S3_ACCESS_KEY=smoke{secrets.token_hex(4)}", f"YTC_S3_SECRET_KEY={secrets.token_urlsafe(24)}",
            f"ANTHROPIC_API_KEY={FAKE_KEY}", "YTC_DAILY_USD=1.00", "YTC_MEDIA_SLOTS=1", "YTC_LLM_SLOTS=2", ""]), encoding="utf-8")
        # 셸 환경 변수가 --env-file보다 우선하므로, 이 스모크가 정하는 이름은 상속하지 않는다
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith(("YTC_", "YT_")) or k == "ANTHROPIC_API_KEY" or k.startswith("POSTGRES_"))}
        compose = ["docker", "compose", "-p", project, "--env-file", str(env_file), "-f", str(SERVER_DIR / "docker-compose.yml")]
        failure: BaseException | None = None
        try:
            run(compose + ["up", "-d", "--build", "--wait", "--wait-timeout", str(UP_TIMEOUT_SEC)], env, timeout=1800)
            check(True, "build + up --wait (postgres·minio healthy, minio-init·migrate 정상 종료, app healthy)")

            rows = parse_ps(run(compose + ["ps", "-a", "--format", "json"], env))
            by_service = {r["Service"]: r for r in rows}
            check(by_service["migrate"]["ExitCode"] == 0 and by_service["minio-init"]["ExitCode"] == 0,
                  "migrate·minio-init 종료 코드 0 (한 번 하고 끝남)")
            check(set(by_service) == {"postgres", "minio", "minio-init", "migrate", "app"},
                  f"서비스 구성 {sorted(by_service)} (caddy·admin은 프로필)")
            published = {r["Service"]: [p for p in r.get("Publishers") or [] if p.get("PublishedPort")] for r in rows}
            check(not published["postgres"] and not published["minio"], "postgres·minio는 포트를 게시하지 않는다")
            check(bool(published["app"]) and all(p["URL"] == "127.0.0.1" for p in published["app"]), "app은 127.0.0.1에만 게시한다")
            uid = run(compose + ["exec", "-T", "app", "id", "-u"], env).strip()
            check(uid == "10001", f"app은 비루트(uid={uid})")

            admin = compose + ["--profile", "admin", "run", "--rm", "-T", "admin"]
            users: dict[str, str] = {}
            for email in ("alice@example.com", "bob@example.com"):
                added = last_json(run(admin + ["add-user", email], env))
                check(added.get("email") == email and bool(added.get("id")), f"admin add-user {email}")
                users[email] = str(added["id"])
            dup = run(admin + ["add-user", "alice@example.com"], env, ok_codes=(1,))
            check("user_exists" in dup, "같은 이메일 add-user는 user_exists로 실패한다")

            plain = Client(base)
            status, body, headers = plain.request("GET", "/healthz")
            check(status == 200 and body == {"ok": True}, "/healthz 200 {ok: true} (인증 없음, 저장소 점검 포함)")
            check("set-cookie" not in headers, "/healthz는 쿠키를 만들지 않는다")
            check(plain.request("GET", "/api/status")[0] == 401, "로그인 없이 /api/status는 401")
            check(plain.request("POST", "/auth/dev", {"email": "mallory@example.com"})[0] == 403, "초대되지 않은 이메일은 dev 로그인 403")

            alice, bob = Client(base), Client(base)
            for client, email in ((alice, "alice@example.com"), (bob, "bob@example.com")):
                status, body, _ = client.request("POST", "/auth/dev", {"email": email})
                check(status == 200 and body.get("ok") is True, f"dev 로그인 {email}")
            check(alice.request("POST", "/api/init", {}, origin=False)[0] == 403, "Origin 없는 POST는 CSRF로 403")
            check(alice.request("POST", "/api/init", {})[0] == 200, "POST /api/init 200")
            status, body, _ = alice.request("GET", "/api/status")
            check(status == 200 and body["work_root"] == "ytc:alice@example.com", f"/api/status 200 (alice) work_root={body.get('work_root')}")

            for email, uid_ in users.items():            # 카테고리(+ alice는 HTML·이미지)를 앱과 같은 설정으로 넣는다
                run(compose + ["exec", "-T", "-e", "YTC_SCRATCH_DIR=/tmp/ytc-seed", "app", "python", "-c", SEED_SCRIPT,
                               uid_, CATEGORY, FRAME_VIDEO, "1" if email.startswith("alice") else "0"], env)
            check(True, "시드(카테고리·콘티 HTML·리포트·프레임·SVG) 저장")

            status, saved, _ = alice.request("POST", "/api/draft/save", {"category": CATEGORY, "draft": DRAFT})
            check(status == 200 and isinstance(saved, dict) and str(saved.get("path", "")).startswith("drafts/"),
                  f"API로 기획안 저장: {saved if status != 200 else saved['path']}")
            key = saved["path"]
            status, listed, _ = alice.request("GET", f"/api/draft/list?category={CATEGORY}")
            check(status == 200 and [d["path"] for d in listed["drafts"]] == [key], "alice의 기획안 목록에 방금 저장한 것이 있다")
            status, raw, _ = alice.request("GET", f"/files/{key}")
            check(status == 200 and DRAFT["title"].encode("utf-8") in raw, "alice가 /files로 저장한 기획안 md를 읽는다")

            status, listed, _ = bob.request("GET", f"/api/draft/list?category={CATEGORY}")
            check(status == 200 and listed["drafts"] == [], "격리: bob의 기획안 목록은 비어 있다")
            check(bob.request("GET", f"/files/{key}")[0] == 404, "격리: bob이 alice의 기획안 /files를 열면 404")
            html = "/files/drafts/smoke-conti.html"
            status, _, headers = alice.request("GET", html)
            csp = headers.get("content-security-policy", "")
            check(status == 200 and all(part in csp for part in FILES_CSP_PARTS), f"/files HTML 응답에 FILES_CSP: {csp}")
            check(bob.request("GET", html)[0] == 404, "격리: bob이 alice의 콘티 HTML을 열면 404")
            frame = f"/files/workspace/frames/{FRAME_VIDEO}/0001.jpg"
            check(alice.request("GET", frame)[0] == 200 and bob.request("GET", frame)[0] == 200, "공유 프레임은 두 사용자 모두 읽는다")

            password = secrets.token_urlsafe(18)           # never printed; goes to the admin CLI on stdin only
            added = last_json(run(admin + ["add-user", PW_EMAIL, "--password-stdin"], env, stdin_text=password + "\n"))
            check(added.get("email") == PW_EMAIL and added.get("password") is True and password not in json.dumps(added),
                  f"admin add-user {PW_EMAIL} --password-stdin (출력에 비밀번호·해시 없음)")
            carol = Client(base)
            check(carol.request("GET", "/auth/methods")[1].get("password") is True, "/auth/methods: password true")
            status, body, headers = carol.request("POST", "/auth/password", {"email": PW_EMAIL, "password": "wrong-password!"})
            check(status == 401 and body.get("message") == LOGIN_FAILED and "set-cookie" not in headers,
                  "틀린 비밀번호는 401 '이메일 또는 비밀번호가 맞지 않습니다'")
            status, body, _ = carol.request("POST", "/auth/password", {"email": PW_EMAIL, "password": password})
            check(status == 200 and body.get("email") == PW_EMAIL, "POST /auth/password 맞는 비밀번호로 로그인 200")
            status, body, _ = carol.request("GET", "/api/ping")
            check(status == 200 and body.get("user") == PW_EMAIL, "비밀번호로 만든 세션으로 /api/ping")

            made = last_json(run(admin + ["make-admin", "alice@example.com"], env))
            check(made.get("is_admin") is True and made.get("admins") == 1, "admin make-admin alice (첫 관리자, owner 경로)")
            check(alice.request("GET", "/api/me")[1] == {"email": "alice@example.com", "is_admin": True},
                  "/api/me: alice는 관리자(같은 세션, 다음 요청부터)")
            status, listed, _ = alice.request("GET", "/api/admin/users")
            emails = sorted(u["email"] for u in listed.get("users", [])) if status == 200 else []
            check(status == 200 and emails == sorted([*users, PW_EMAIL]) and "scrypt" not in json.dumps(listed),
                  f"GET /api/admin/users: 사용자 {len(emails)}명, 해시 없음")
            check(bob.request("GET", "/api/admin/users")[0] == 403, "관리자가 아닌 bob의 관리 API는 403")
            status, _, _ = alice.request("POST", f"/api/admin/users/{users['bob@example.com']}/logout-all", {})
            check(status == 200 and bob.request("GET", "/api/ping")[0] == 401, "관리 API로 bob을 모든 기기에서 로그아웃 → bob 401")
            status, body, _ = alice.request("POST", f"/api/admin/users/{users['alice@example.com']}/disable", {})
            check(status == 409 and body.get("error") == "self_protected", "자기 계정 비활성은 409 self_protected")
            status, audit, _ = alice.request("GET", "/api/admin/audit?limit=10")
            actions = [e["action"] for e in audit.get("entries", [])] if status == 200 else []
            check(actions == ["logout_all", "set_admin"], f"감사 기록(최신순): {actions}")

            chrome = find_chrome() if with_browser else None
            if not with_browser:
                print("SKIP 브라우저 점검(--no-browser)", flush=True)
            elif chrome is None:
                print("SKIP 브라우저 점검: Chrome을 찾지 못했습니다(YTC_CHROME으로 경로를 주면 한다)", flush=True)
            else:
                try:
                    browser_check(chrome, base, alice.session_cookie(), html, login=(PW_EMAIL, password))
                except BrowserUnavailable as e:
                    print(f"SKIP 브라우저 점검: {e}", flush=True)

            # last: this blocks the published-port client IP for password login for 10 minutes
            # every attempt uses a new email and a new X-Forwarded-For: only a per-IP count on the real peer can reach 429
            # (the earlier wrong passwords from this host already count, so 429 may come before IP_FAILURES attempts)
            spoof = Client(base)
            status, body, headers, tries = 401, {}, {}, 0
            while status == 401 and tries <= IP_FAILURES:
                tries += 1
                status, body, headers = spoof.request("POST", "/auth/password",
                                                      {"email": f"nobody{tries}@example.com", "password": "x" * 12},
                                                      extra={"X-Forwarded-For": f"203.0.113.{tries}"})
            check(status == 429 and "retry-after" in headers and "로그인 시도가 너무 많습니다" in str(body.get("message")),
                  f"게시 포트로 보낸 X-Forwarded-For는 믿지 않는다(값을 바꿔도 같은 IP로 세어 {tries}번째에 429, Retry-After)")
        except BaseException as e:
            failure = e
            raise
        finally:
            problem = teardown(compose, env, project)
            if problem:
                print(f"WARN 정리 문제: {problem}", file=sys.stderr)
                if failure is None:
                    raise SmokeFailure(f"정리 문제: {problem}")     # 통과로 보고하지 않는다(다른 실패가 있으면 그쪽이 먼저 보인다)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="deploy/smoke.py")
    parser.add_argument("edition", choices=["local", "server"])
    parser.add_argument("--project", default=f"ytc-smoke-{os.getpid()}")
    parser.add_argument("--image", default=f"ytc:smoke-{os.getpid()}")
    parser.add_argument("--with-youtube", action="store_true")
    parser.add_argument("--no-browser", action="store_true", help="server: headless Chrome 점검을 건너뛴다")
    args = parser.parse_args(argv)
    try:
        if args.edition == "server":
            smoke_server(args.project, args.image, not args.no_browser)
        else:
            smoke_local(args.project, args.image, args.with_youtube)
    except SmokeFailure as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
