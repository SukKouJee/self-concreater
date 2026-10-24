"""움직이는 자막 영상(크로마 그린 MP4) 렌더: 콘티 자막 → Remotion(`remotion/`) → `drafts/<기획안>-captions.mp4`.

Remotion은 선택 설치다. 사용자가 `ytc captions setup`을 직접 실행해야 하고, 자동으로 설치하지 않는다.
렌더는 하위 프로세스(`npx remotion render`)이며 중간 파일은 `workspace/tmp/captions_*`에 두고 끝나면 지운다.
취소·시간 초과 시에는 그 프로세스(트리)만 PID로 종료한다. 이미지 이름으로 프로세스를 끄지 않는다.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import conti
from scripts.common import ArgParser, ScriptError, run, work_root, workspace_dir

REMOTION_DIR = Path(__file__).resolve().parent.parent / "remotion"
FPS = 30
BACKGROUND = "#00FF00"
SHORT_SIDE = 1080
SIZES = {"shorts": (1080, 1920), "long": (1920, 1080)}
DEFAULT_TIMEOUT_SEC = 900
TERMINATE_GRACE_SEC = 5.0
POLL_SEC = 0.1
MISSING_MESSAGE = "움직이는 자막을 만들려면 처음 한 번 설치가 필요합니다: ytc captions setup (Node 18 이상)"
_PROGRESS_RE = re.compile(r"Rendered\s+(\d+)\s*/\s*(\d+)")
_WIN = sys.platform == "win32"


class RenderCancelled(ScriptError):
    """취소 콜백이 참이라 렌더를 멈췄다."""

    def __init__(self) -> None:
        super().__init__("cancelled", "자막 영상 만들기를 취소했습니다")


def _tool(name: str) -> str | None:
    """Windows는 .cmd 래퍼(npx.cmd, npm.cmd)를 쓴다."""
    return shutil.which(f"{name}.cmd") if _WIN else shutil.which(name)


def _missing() -> ScriptError:
    return ScriptError("remotion_missing", MISSING_MESSAGE)


def check_setup() -> dict:
    """node·npx와 `remotion/node_modules/@remotion/cli`가 있는지 확인한다. 없으면 remotion_missing."""
    node, npx = shutil.which("node"), _tool("npx") or shutil.which("npx")
    if not node or not npx:
        raise _missing()
    if not (REMOTION_DIR / "node_modules" / "@remotion" / "cli").is_dir():
        raise _missing()
    return {"ready": True, "node": node, "npx": npx}


def _tail(text: str, n: int = 5) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def setup() -> dict:
    """`remotion/`에서 npm ci(lock 파일이 없으면 npm install)를 실행한다. 사용자가 직접 부르는 명령이다."""
    npm = _tool("npm") or shutil.which("npm")
    if not shutil.which("node") or not npm:
        raise _missing()
    sub = "ci" if (REMOTION_DIR / "package-lock.json").is_file() else "install"
    try:
        done = subprocess.run([npm, sub], cwd=REMOTION_DIR, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScriptError("setup_failed", f"설치하지 못했습니다: {e}") from e
    if done.returncode != 0:
        raise ScriptError("setup_failed", f"설치하지 못했습니다: {_tail(done.stderr or done.stdout)}")
    return {"ok": True, "command": f"npm {sub}", "summary": _tail(done.stdout)}


def _video_size(path: Path) -> tuple[int, int]:
    import cv2        # 편집본 크기를 읽을 때만 불러온다

    cap = cv2.VideoCapture(str(path))
    try:
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    if width <= 0 or height <= 0:
        raise ScriptError("bad_video", f"영상 크기를 읽지 못했습니다: {path.name}")
    return width, height


def _fit_size(width: int, height: int) -> tuple[int, int]:
    """짧은 변이 1080이 되도록 비율을 유지해 맞춘다(H.264를 위해 짝수로)."""
    scale = SHORT_SIDE / min(width, height)
    return tuple(max(2, round(v * scale / 2) * 2) if v != min(width, height) else SHORT_SIDE
                 for v in (width, height))  # type: ignore[return-value]


def _build(conti_path: str | Path, video: str | Path | None, track: str,
           progress: Callable[[str], None] | None = None) -> tuple[dict, dict]:
    calc = conti.caption_blocks(conti_path, track, video, progress)
    blocks = calc["blocks"]
    if not blocks:
        raise ScriptError("no_captions", "내보낼 자막이 없습니다")
    if calc["video"] is not None:
        width, height = _fit_size(*_video_size(calc["video"]))
    else:
        width, height = SIZES["shorts" if calc["fm"].get("format") == "shorts" else "long"]
    duration_ms = calc["duration_ms"]
    props = {
        "fps": FPS, "width": width, "height": height,
        "durationInFrames": -(-duration_ms * FPS // 1000),        # 올림(정수 연산)
        "background": BACKGROUND,
        "captions": [{"start": a / 1000, "end": b / 1000, "text": t} for a, b, t in blocks],
    }
    return props, calc


def build_props(conti_path: str | Path, video: str | Path | None = None, track: str = "caption") -> dict:
    """Remotion 입력 props. 기본은 콘티 계획 시간, 편집본이면 실제 컷에 맞춘 시간과 그 영상의 비율."""
    return _build(conti_path, video, track)[0]


def _stop(proc: Any) -> None:
    """그 프로세스(트리)만 종료한다: terminate → 대기 → kill. Windows는 PID로 자식까지 끈다."""
    if _WIN and isinstance(getattr(proc, "pid", None), int):
        # npx.cmd 아래의 node·Chrome까지 정리한다. 부모를 먼저 죽이면 트리를 못 찾으므로 terminate()보다 먼저.
        # PID 기준(/T)이며 이미지 이름(/IM)은 쓰지 않는다
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    proc.terminate()
    try:
        proc.wait(TERMINATE_GRACE_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _pump(stream: Any, lines: queue.Queue) -> None:
    for line in iter(stream.readline, ""):
        lines.put(line)
    lines.put(None)


def render(conti_path: str | Path, video: str | Path | None = None, track: str = "caption",
           progress: Callable[[int, int], None] | None = None, cancel: Callable[[], bool] | None = None,
           timeout: float = DEFAULT_TIMEOUT_SEC) -> dict:
    """콘티 자막을 크로마 그린 MP4로 렌더해 `drafts/<기획안>-captions.mp4`에 둔다(같은 이름이면 교체)."""
    check_setup()
    npx = _tool("npx") or shutil.which("npx") or "npx"
    props, calc = _build(conti_path, video, track)
    path: Path = calc["path"]
    out = path.with_name(f"{path.stem[: -len(conti.CONTI_SUFFIX)]}-captions.mp4")
    tmp_dir = workspace_dir("tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    props_file, tmp_mp4 = tmp_dir / f"captions_{token}.json", tmp_dir / f"captions_{token}.mp4"
    tail: deque[str] = deque(maxlen=5)
    try:
        props_file.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
        cmd = [npx, "remotion", "render", "src/index.jsx", "Captions", str(tmp_mp4),
               f"--props={props_file}"]
        try:
            proc = subprocess.Popen(cmd, cwd=REMOTION_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            raise ScriptError("render_failed", f"자막 영상을 만들지 못했습니다: {e}") from e
        lines: queue.Queue = queue.Queue()
        reader = threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        eof = False
        try:
            while True:
                try:
                    line = lines.get(timeout=POLL_SEC)
                except queue.Empty:
                    line = ""
                else:
                    if line is None:
                        eof = True
                    else:
                        m = _PROGRESS_RE.search(line)
                        if m:
                            if progress:
                                progress(int(m.group(1)), int(m.group(2)))
                        elif line.strip():
                            tail.append(line.strip())
                if cancel is not None and cancel():
                    raise RenderCancelled()
                if time.monotonic() > deadline:
                    raise ScriptError("render_timeout", f"자막 영상 만들기가 {int(timeout)}초 안에 끝나지 않아 멈췄습니다")
                if eof:
                    break
            proc.wait(TERMINATE_GRACE_SEC + 25)
        except BaseException:
            _stop(proc)
            raise
        finally:
            reader.join(2)
        if proc.returncode != 0 or not tmp_mp4.is_file():
            raise ScriptError("render_failed", "자막 영상을 만들지 못했습니다: " + "\n".join(tail))
        try:
            os.replace(tmp_mp4, out)
        except OSError as e:
            raise ScriptError("render_failed", f"자막 영상을 저장하지 못했습니다(열려 있는 파일인지 확인하세요): {e}") from e
    finally:
        props_file.unlink(missing_ok=True)
        tmp_mp4.unlink(missing_ok=True)
    result: dict[str, Any] = {"path": conti._rel(out), "count": len(props["captions"]),
                              "duration_sec": calc["duration_ms"] / 1000,
                              "width": props["width"], "height": props["height"]}
    if calc["retime"] is not None:
        result["retime"] = calc["retime"]
    return result


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.captions")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("check")
    r = sub.add_parser("render")
    r.add_argument("conti")
    r.add_argument("--video", default=None)
    r.add_argument("--track", choices=sorted(conti.SRT_TRACKS), default="caption")
    args = parser.parse_args(argv)
    if args.cmd == "setup":
        return setup()
    if args.cmd == "check":
        return check_setup()
    return render(args.conti, args.video, args.track)


if __name__ == "__main__":
    run(main)
