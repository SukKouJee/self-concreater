"""촬영 영상 처리: 폴더 스캔 → 파일 지문 → 장면·사진·모음 이미지·받아쓰기 (로컬, 비용 0)

사용법 (작업 폴더에서):
  python -m scripts.footage scan <folder>
  python -m scripts.footage process <file> [--no-stt]

- 원본 파일은 읽기만 한다. 결과는 workspace/footage/<지문>/ 아래(scenes.json, transcript.json,
  frames/, montage/). 경로는 모두 그 폴더 기준 상대 경로다.
- 지문 = sha1(정규화한 절대 경로) 앞 12자 + 크기 + 수정 시각(ns). 파일이 바뀌면 지문도 바뀐다.
- 장면: 짧은 변 360 이하로 줄이고 초당 5프레임만 본다. OpenCV로 열고, 못 열면 PyAV로 푼다.
  둘 다 안 되면 status "unreadable"(이유 포함)로 두고 다음 파일로 넘어간다.
  긴 장면(MAX_SCENE_SEC 초과)은 나눈다: 원테이크 촬영도 맞추기 단위가 너무 길지 않게, 메모리도 묶어 둔다.
- 장면마다 사진 4~6장(긴 변 320px JPEG), 모음 이미지 1장(사진 격자, 긴 변 960px — AI용).
- 받아쓰기: stt.transcribe_file(한국어). faster-whisper가 없으면 transcript None + stt_missing.
- scenes.json은 사진을 다 쓴 뒤 마지막에 원자적으로 쓴다. 없으면 남은 사진을 지우고 처음부터 한다
  (중단된 처리는 파일 단위로 이어서 한다). 받아쓰기는 따로 캐시해 장면을 다시 풀지 않는다.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from scripts import stt
from scripts.common import ArgParser, Progress, ScriptError, run, safe_progress, workspace_dir
from scripts.cuts import DECODE_RETRIES, SCENE_THRESHOLD

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".mkv"})
SCHEMA = 1

SAMPLE_FPS = 5.0          # 장면 탐지는 초당 이만큼만 본다
DECODE_SHORT_SIDE = 360   # 디코딩한 프레임의 짧은 변 상한
DETECT_WIDTH = 160        # 장면 차이는 이 너비로 줄여서 잰다
MIN_SCENE_SEC = 1.0       # 이보다 짧은 장면은 만들지 않는다(플래시·흔들림 오탐 방지)
MAX_SCENE_SEC = 60.0      # 이보다 긴 장면은 나눈다
BUFFER_FRAMES = 48        # 장면 하나에 들고 있는 사진 후보 상한
FRAME_LONG_EDGE = 320
MONTAGE_LONG_EDGE = 960
JPEG_QUALITY = 85
PROGRESS_STEPS = 100      # 장면 확인 진행 보고 횟수
UNKNOWN_DURATION_EVERY = 10.0  # 길이를 모를 때 이 초마다 알린다


# --- 스캔·지문 ------------------------------------------------------------------

def fingerprint(path: str | Path) -> str:
    """sha1(정규화 절대 경로) 앞 12자 + 크기 + mtime_ns. 디렉터리 이름·URL에 그대로 쓸 수 있다."""
    resolved = Path(path).resolve()
    st = resolved.stat()
    digest = hashlib.sha1(os.path.normcase(str(resolved)).encode("utf-8")).hexdigest()[:12]
    return f"{digest}-{st.st_size}-{st.st_mtime_ns}"


def footage_dir(fid: str) -> Path:
    return workspace_dir("footage", fid)


def scan_folder(folder: str | Path) -> list[dict[str, Any]]:
    """폴더 바로 아래 영상 파일(확장자 대소문자 무시, 하위 폴더 제외)을 이름순으로."""
    root = Path(folder)
    if not root.is_dir():
        raise ScriptError("folder_not_found", f"폴더를 찾을 수 없습니다: {root.name or folder}")
    files = []
    for entry in root.iterdir():
        if entry.suffix.lower() not in VIDEO_EXTS or not entry.is_file():
            continue
        resolved = entry.resolve()
        st = resolved.stat()
        files.append({
            "path": str(resolved), "name": entry.name, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "fid": fingerprint(resolved),
        })
    files.sort(key=lambda f: (f["name"].lower(), f["name"]))
    return files


# --- 프레임 읽기 ----------------------------------------------------------------

@dataclass
class Source:
    """디코더 하나: 길이(모르면 None)와 (초, 줄인 BGR 프레임)을 초당 SAMPLE_FPS번 내는 생성기."""

    decoder: str
    duration: float | None
    frames: Iterator[tuple[float, np.ndarray]]


def _fit(frame: np.ndarray, short_side: int) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = short_side / min(h, w)
    if scale >= 1:
        return frame
    return cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)


def _scaled_size(width: int, height: int, short_side: int) -> tuple[int, int]:
    scale = min(1.0, short_side / min(width, height))
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def rotation_code(rotation: float | int) -> int | None:
    """PyAV frame.rotation(디스플레이 행렬, 반시계 각도) → cv2.rotate 코드. ffmpeg autorotate와 같은 방향."""
    angle = round(rotation) % 360
    return {90: cv2.ROTATE_90_COUNTERCLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_CLOCKWISE}.get(angle)


def _read_opencv(path: Path, short_side: int) -> Source | None:
    """OpenCV로 연다. 열지 못하거나 첫 프레임부터 못 읽으면 None(PyAV로 넘긴다)."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0 or not cap.grab():
        cap.release()
        return None

    def frames() -> Iterator[tuple[float, np.ndarray]]:
        try:
            index, next_t = 0, 0.0
            while True:
                t = index / fps
                if t + 1e-6 >= next_t:
                    ok, frame = cap.retrieve()
                    if ok:
                        yield t, _fit(frame, short_side)
                        next_t += 1 / SAMPLE_FPS
                if not any(cap.grab() for _ in range(1 + DECODE_RETRIES)):
                    return
                index += 1
        finally:
            cap.release()

    return Source("opencv", count / fps if count > 0 else None, frames())


def _read_pyav(path: Path, short_side: int) -> Source:
    """PyAV로 푼다(HEVC 등 OpenCV가 못 여는 파일). 못 열면 ScriptError("unreadable")."""
    import av

    try:
        container = av.open(str(path), mode="r")
    except (av.error.FFmpegError, OSError) as e:
        raise ScriptError("unreadable", f"영상을 열 수 없습니다: {stt.av_detail(e)}") from e
    if not container.streams.video:
        container.close()
        raise ScriptError("unreadable", "영상 트랙이 없습니다")
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    duration = None
    if stream.duration is not None and stream.time_base is not None:
        duration = float(stream.duration * stream.time_base)
    elif container.duration is not None:
        duration = container.duration / av.time_base

    def frames() -> Iterator[tuple[float, np.ndarray]]:
        try:
            first: float | None = None
            next_t = 0.0
            size: tuple[int, int] | None = None
            for frame in container.decode(stream):
                if frame.time is None:
                    continue
                first = frame.time if first is None else first
                t = frame.time - first
                if t + 1e-6 < next_t:
                    continue
                size = size or _scaled_size(frame.width, frame.height, short_side)
                image = frame.to_ndarray(format="bgr24", width=size[0], height=size[1])
                code = rotation_code(getattr(frame, "rotation", 0) or 0)
                yield t, (cv2.rotate(image, code) if code is not None else image)
                next_t = t + 1 / SAMPLE_FPS
        except av.error.FFmpegError:
            return  # 뒤가 깨진 파일: 읽은 데까지만 쓴다(하나도 못 읽으면 호출한 쪽이 unreadable로 둔다)
        finally:
            container.close()

    return Source("pyav", duration, frames())


def open_source(path: Path) -> Source:
    source = _read_opencv(path, DECODE_SHORT_SIDE)
    return source if source is not None else _read_pyav(path, DECODE_SHORT_SIDE)


# --- 장면 -----------------------------------------------------------------------

@dataclass
class SceneBuffer:
    """장면 하나의 사진 후보. limit을 넘으면 하나 건너 하나를 버리고 간격을 두 배로 늘린다(고르게, 메모리 고정)."""

    limit: int = BUFFER_FRAMES
    items: list[tuple[float, np.ndarray]] = field(default_factory=list)
    stride: int = 1
    seen: int = 0

    def add(self, t: float, frame: np.ndarray) -> None:
        if self.seen % self.stride == 0:
            self.items.append((t, frame))
            if len(self.items) > self.limit:
                self.items = self.items[::2]
                self.stride *= 2
        self.seen += 1

    def pick(self, n: int) -> list[tuple[float, np.ndarray]]:
        total = len(self.items)
        if total <= n:
            return list(self.items)
        return [self.items[min(total - 1, int((k + 0.5) * total / n))] for k in range(n)]


def photos_per_scene(seconds: float) -> int:
    if seconds < 10:
        return 4
    if seconds < 30:
        return 5
    return 6


def _hsv_small(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (DETECT_WIDTH, max(1, round(h * DETECT_WIDTH / w))), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2HSV).astype(np.int16)


def _content_score(a: np.ndarray, b: np.ndarray) -> float:
    """scenedetect ContentDetector 기본값과 같은 점수: H·S·V 평균 차이의 평균."""
    return float(np.abs(a - b).reshape(-1, 3).mean(axis=0).mean())


def make_montage(tiles: list[np.ndarray]) -> np.ndarray:
    """사진들을 격자(5장 이상 3열, 아니면 2열)로 붙이고 긴 변을 MONTAGE_LONG_EDGE로 맞춘다."""
    h, w = tiles[0].shape[:2]
    cols = 3 if len(tiles) >= 5 else min(2, len(tiles))
    rows = math.ceil(len(tiles) / cols)
    canvas = np.zeros((rows * h, cols * w, 3), dtype=np.uint8)
    for k, tile in enumerate(tiles):
        r, c = divmod(k, cols)
        canvas[r * h:(r + 1) * h, c * w:(c + 1) * w] = cv2.resize(tile, (w, h))
    return _long_edge(canvas, MONTAGE_LONG_EDGE)


def _long_edge(image: np.ndarray, edge: int) -> np.ndarray:
    h, w = image.shape[:2]
    if w >= h:
        size = (edge, max(1, round(h * edge / w)))
    else:
        size = (max(1, round(w * edge / h)), edge)
    interpolation = cv2.INTER_AREA if max(h, w) > edge else cv2.INTER_LINEAR
    return cv2.resize(image, size, interpolation=interpolation)


def _write_jpeg(path: Path, image: np.ndarray) -> None:
    # cv2.imwrite는 윈도의 한글 경로에 쓰지 못한다
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ScriptError("write_failed", f"사진을 만들지 못했습니다: {path.name}")
    path.write_bytes(data.tobytes())


def _clock(sec: float) -> str:
    total = int(sec)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def scene_progress_text(done: float, duration: float | None) -> str:
    if not duration:
        return f"장면 확인 중 {_clock(done)}"
    return f"장면 확인 중 {_clock(min(done, duration))} / {_clock(duration)}"


def extract_scenes(source: Source, out_dir: Path, report: Progress | None = None) -> tuple[float, list[dict]]:
    """한 번 읽으며 장면을 나누고, 장면이 끝날 때마다 사진·모음 이미지를 쓴다. -> (길이, 장면 목록)"""
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    (out_dir / "montage").mkdir(parents=True, exist_ok=True)
    scenes: list[dict] = []
    buffer = SceneBuffer()
    start = 0.0
    prev: np.ndarray | None = None
    last_t: float | None = None
    every = source.duration / PROGRESS_STEPS if source.duration else UNKNOWN_DURATION_EVERY
    next_report = every

    def close(end: float) -> None:
        i = len(scenes)
        picked = buffer.pick(photos_per_scene(end - start))
        names = []
        for k, (_, frame) in enumerate(picked):
            rel = f"frames/s{i:03d}_{k}.jpg"
            _write_jpeg(out_dir / rel, _long_edge(frame, FRAME_LONG_EDGE))
            names.append(rel)
        montage = f"montage/s{i:03d}.jpg"
        _write_jpeg(out_dir / montage, make_montage([frame for _, frame in picked]))
        scenes.append({"i": i, "start": round(start, 2), "end": round(end, 2), "frames": names, "montage": montage})

    with contextlib.closing(source.frames):
        for t, frame in source.frames:
            hsv = _hsv_small(frame)
            if prev is not None and t - start >= MIN_SCENE_SEC and (
                _content_score(prev, hsv) >= SCENE_THRESHOLD or t - start >= MAX_SCENE_SEC
            ):
                close(t)
                buffer, start = SceneBuffer(), t
            buffer.add(t, frame)
            prev, last_t = hsv, t
            if report is not None and t >= next_report:
                fraction = min(0.99, t / source.duration) if source.duration else None
                report("scenes", fraction, scene_progress_text(t, source.duration))
                next_report = t + every

    if last_t is None:
        raise ScriptError("unreadable", "영상 프레임을 하나도 읽지 못했습니다")
    duration = max(source.duration or 0.0, last_t + 1 / SAMPLE_FPS)
    close(duration)
    if report is not None:
        report("scenes", 1.0, scene_progress_text(duration, duration))
    return round(duration, 2), scenes


# --- 캐시·흐름 ------------------------------------------------------------------

def _write_json(path: Path, data: dict[str, Any]) -> None:
    """임시 파일에 쓴 뒤 바꿔 끼운다: 중단돼도 반쯤 쓴 json이 남지 않는다."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return data


def _scene_record(path: Path, fid: str, base: Path, report: Progress | None) -> dict[str, Any]:
    for leftover in ("frames", "montage"):
        shutil.rmtree(base / leftover, ignore_errors=True)
    record: dict[str, Any] = {"schema": SCHEMA, "fid": fid, "name": path.name}
    try:
        source = open_source(path)
        duration, scenes = extract_scenes(source, base, report)
    except ScriptError as e:
        if e.code != "unreadable":
            raise
        for leftover in ("frames", "montage"):
            shutil.rmtree(base / leftover, ignore_errors=True)
        record.update(status="unreadable", reason=e.message, duration_sec=None, scenes=[])
    else:
        record.update(status="done", decoder=source.decoder, duration_sec=duration, scenes=scenes)
    _write_json(base / "scenes.json", record)
    return record


def _transcript(path: Path, base: Path, report: Progress | None) -> dict[str, Any]:
    """-> transcript.json 내용 {"segments": [...] | None, "error"?}. faster-whisper가 없으면 캐시하지 않는다."""
    cached = _read_json(base / "transcript.json")
    if cached is not None:
        return cached
    try:
        data: dict[str, Any] = {"segments": stt.transcribe_file(path, lang="ko", progress=report)}
    except ScriptError as e:
        if e.code == "stt_unavailable":
            return {"segments": None, "stt_missing": True}
        if e.code != "stt_failed":
            raise
        data = {"segments": None, "error": e.message}
    _write_json(base / "transcript.json", data)
    return data


def process_file(path: str | Path, progress: Progress | None = None, stt: bool = True) -> dict[str, Any]:
    """파일 하나를 처리한다(캐시가 있으면 그대로). 원본은 읽기만 한다.

    -> {"fid", "name", "status": "done"|"unreadable", "reason"?, "decoder"?, "duration_sec",
        "scenes": [{"i", "start", "end", "frames": [rel...], "montage": rel}],
        "transcript": [{"start", "end", "text"}] | None, "stt_missing"?, "transcript_error"?}
    rel은 footage_dir(fid) 기준 상대 경로. progress: ("scenes", 비율, 글) → ("stt", 비율, 글).
    """
    path = Path(path)
    if not path.is_file():
        raise ScriptError("file_not_found", f"파일을 찾을 수 없습니다: {path.name}")
    fid = fingerprint(path)
    base = footage_dir(fid)
    base.mkdir(parents=True, exist_ok=True)
    report = safe_progress(progress)

    record = _read_json(base / "scenes.json") or _scene_record(path, fid, base, report)
    result = {k: v for k, v in record.items() if k != "schema"}
    result["transcript"] = None
    if record["status"] != "done":
        return result
    transcript = _transcript(path, base, report) if stt else _read_json(base / "transcript.json")
    if transcript is not None:
        result["transcript"] = transcript["segments"]
        if transcript.get("stt_missing"):
            result["stt_missing"] = True
        if transcript.get("error"):
            result["transcript_error"] = transcript["error"]
    return result


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.footage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("folder")
    proc = sub.add_parser("process")
    proc.add_argument("file")
    proc.add_argument("--no-stt", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        return {"folder": str(Path(args.folder).resolve()), "files": scan_folder(args.folder)}
    return process_file(args.file, stt=not args.no_stt)


if __name__ == "__main__":
    run(main)
