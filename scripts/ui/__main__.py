"""UI 서버 실행기: `ytc ui [--port N] [--no-open] [--detach] [--stop]`.

- 작업 폴더(YT_COACH_HOME 또는 현재 폴더)당 서버는 하나다. 살아 있는 `workspace/ui.lock`이 있으면
  새로 띄우지 않고 `{"status": "running", "url"}`를 돌려준다(`--no-open`이 아니면 브라우저도 연다).
- 기본(전경): lock을 쓰고 `{"status": "started", "url"}`를 먼저 출력한 뒤 서버가 끝날 때까지 돈다. 끝나면 lock을 지운다.
- `--detach`: 같은 명령을 분리 프로세스로 띄운다(출력은 `workspace/ui.log`). 자식은 항상 `--no-open`으로 띄우고
  브라우저는 부모가 연다. lock이 생길 때까지 최대 15초 기다린 뒤 `{"status": "started", "url", "pid"}`(pid는 lock의 서버 pid).
  Windows에서는 Claude Code를 닫아도 서버가 살도록 Job 이탈(CREATE_BREAKAWAY_FROM_JOB)을 먼저 시도한다.
- `--stop`: lock의 서버에 `POST /api/shutdown` → `{"status": "stopped"}`, 없으면 `{"status": "not_running"}`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from scripts.common import ArgParser, ScriptError, emit, work_root, workspace_dir
from scripts.ui import server
from scripts.ui.server import read_live_lock

START_TIMEOUT_SEC = 15.0
# 서버가 끝날 때(--stop, Ctrl+C) 진행 중인 작업이 지금 단계를 마칠 때까지 기다리는 최대 시간.
# 단계 안의 try/finally(받은 영상·오디오 삭제)가 돌 시간을 준다. 영상 하나 다운로드+컷 분석이 보통 이 안에 끝난다.
JOB_STOP_TIMEOUT_SEC = 120.0
STOP_TIMEOUT_SEC = 5.0
POLL_SEC = 0.2
LOG = "ui.log"
# Windows CreateProcess 플래그(다른 OS에서도 테스트할 수 있게 숫자로 둔다)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_PLATFORM = sys.platform


def _open(url: str, enabled: bool) -> None:
    if enabled:
        webbrowser.open(url)


def _spawn_attempts() -> list[dict[str, Any]]:
    """분리 실행 옵션을 시도할 순서대로 돌려준다.

    Windows: Claude Code가 자식들을 Job 객체로 묶어 두면 Claude Code를 닫을 때 서버도 같이 죽는다.
    그래서 먼저 CREATE_BREAKAWAY_FROM_JOB로 Job 밖에 띄우고, Job이 이탈을 막아 OSError가 나면 없이 다시 띄운다.
    """
    if _PLATFORM == "win32":
        base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        return [{"creationflags": base | CREATE_BREAKAWAY_FROM_JOB}, {"creationflags": base}]
    return [{"start_new_session": True}]


def _popen_detached(cmd: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    *fallible, last = _spawn_attempts()
    for extra in fallible:
        try:
            return subprocess.Popen(cmd, **kwargs, **extra)
        except OSError:
            continue                      # Job이 이탈을 막음 → 다음 옵션으로
    return subprocess.Popen(cmd, **kwargs, **last)


def _log_tail(path: Path, limit: int = 2000) -> str:
    if not path.exists():
        return ""
    return path.read_bytes()[-limit:].decode("utf-8", errors="replace").strip()


def _detach(root: Path, port: int, open_browser: bool) -> dict:
    cmd = [sys.executable, "-m", "scripts.ui", "--no-open"]
    if port:
        cmd += ["--port", str(port)]
    env = dict(os.environ, YT_COACH_HOME=str(root), PYTHONIOENCODING="utf-8")
    log_path = workspace_dir(LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as log:
        proc = _popen_detached(
            cmd, cwd=str(server.PLUGIN_ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
        )
    deadline = time.monotonic() + START_TIMEOUT_SEC
    while time.monotonic() < deadline:
        info = read_live_lock(root)
        if info:
            _open(info["url"], open_browser)
            # 런처(proc)가 아니라 lock을 쓴 서버 프로세스의 pid를 돌려준다
            return {"status": "started", "url": info["url"], "pid": info["pid"]}
        if proc.poll() is not None:
            raise ScriptError("ui_start_failed", f"UI 서버가 바로 종료되었습니다. {LOG}: {_log_tail(log_path)}")
        time.sleep(POLL_SEC)
    raise ScriptError("ui_start_timeout", f"{int(START_TIMEOUT_SEC)}초 안에 UI 서버가 뜨지 않았습니다. workspace/{LOG}를 확인하세요")


def _stop(root: Path) -> dict:
    info = read_live_lock(root)
    if info is None:
        return {"status": "not_running"}
    try:
        server.request_shutdown(info)
    except OSError as e:
        raise ScriptError("stop_failed", f"UI 서버를 끄지 못했습니다: {e}") from e
    deadline = time.monotonic() + STOP_TIMEOUT_SEC
    while server.lock_path(root).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return {"status": "stopped"}


def _serve(root: Path, port: int, open_browser: bool) -> None:
    srv, app = server.make_app(root, port)
    try:
        info = server.write_lock(app)
        emit({"status": "started", "url": info["url"]})
        _open(info["url"], open_browser)
        srv.serve_forever()
    except KeyboardInterrupt:
        emit({"status": "stopped"})  # 터미널 Ctrl+C: 정상 종료 경로
    finally:
        if app.jobs is not None:
            app.jobs.shutdown(timeout=JOB_STOP_TIMEOUT_SEC)
        server.remove_lock(app.root)
        srv.server_close()


def main(argv: list[str]) -> dict | None:
    parser = ArgParser(prog="ytc ui")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--detach", action="store_true")
    mode.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    root = work_root().resolve()

    if args.stop:
        return _stop(root)
    live = read_live_lock(root)
    if live:
        _open(live["url"], not args.no_open)
        return {"status": "running", "url": live["url"]}
    if args.detach:
        return _detach(root, args.port, not args.no_open)
    _serve(root, args.port, not args.no_open)
    return None


def cli(argv: list[str] | None = None) -> None:
    """`common.run`과 같은 오류 규약. 전경 실행은 main 안에서 직접 출력하므로 None이면 더 출력하지 않는다."""
    args = sys.argv[1:] if argv is None else argv
    try:
        result = main(args)
    except ScriptError as e:
        emit({"error": e.code, "message": e.message, **e.details})
        sys.exit(e.exit_code)
    except Exception as e:  # noqa: BLE001 - 규약상 모든 실패를 JSON으로 돌려준다
        emit({"error": "internal", "message": f"{type(e).__name__}: {e}"})
        sys.exit(2)
    if result is not None:
        emit(result)


if __name__ == "__main__":
    cli()
