"""컷 통계: 저화질 다운로드 → 장면 감지 → 장면별 카메라 움직임·대표 프레임 → 원본 삭제

사용법 (작업 폴더에서):
  python -m scripts.cuts <url|video_id> [--force]

- 다운로드: 영상 트랙만, 짧은 변 360 이하(thresholds.json download.max_height), https·h264 우선
- 장면 감지: PySceneDetect ContentDetector (threshold·최소 장면 길이는 thresholds.json cuts)
- 대표 프레임: 장면 가운데 1장, 영상당 최대 40장(thresholds.json frames.max_per_video)
  → workspace/frames/{id}/
- 카메라: 장면 안 몇 군데에서 optical flow(Farneback)로 static / pan / zoom 추정 (참고값)
  static은 '카메라가 고정'이라는 뜻으로, 화면 속 사람·손이 움직여도 static일 수 있다
- 결과: workspace/cuts/{id}.json. 있으면 다시 받지 않는다(--force만 예외)
- 받은 영상은 성공·실패와 관계없이 바로 삭제한다
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from scripts.common import TEMPLATES_DIR, ArgParser, ScriptError, run, work_root, workspace_dir
from scripts.fetch import wait_turn, parse_video_id, ytdlp_error, ytdlp_options

_LIMITS = json.loads((TEMPLATES_DIR / "thresholds.json").read_text("utf-8"))
MAX_SIDE = _LIMITS["download"]["max_height"]
MAX_FRAMES = _LIMITS["frames"]["max_per_video"]

# 같은 구도 점프컷·짧은 디졸브까지 잡으려면 기본값(27)보다 낮아야 한다 (먹방 15개 링크 육안 대조, 2026-10-20)
SCENE_THRESHOLD = _LIMITS["cuts"]["threshold"]
MIN_SCENE_FRAMES = _LIMITS["cuts"]["min_scene_frames"]
FLOW_WIDTH = 160          # optical flow는 이 너비로 줄여서 계산
FLOW_SAMPLES = 4          # 장면당 flow를 재는 지점 수
STATIC_PX = 0.5           # 평균 움직임이 이보다 작으면 static (FLOW_WIDTH 기준, 픽셀/샘플 간격)
PAN_RATIO = 0.6           # 전체 움직임 중 한 방향 평행이동 비중
ZOOM_RATIO = 0.5          # 전체 움직임 중 중심에서 퍼지거나 모이는 비중


# --- 다운로드 -----------------------------------------------------------------

def select_format(formats: list[dict]) -> dict:
    """짧은 변이 MAX_SIDE 이하인 영상 트랙 중 https > h264 > 해상도 순으로 고른다."""
    candidates = [
        f for f in formats
        if f.get("vcodec") not in (None, "none") and f.get("width") and f.get("height")
        and min(f["width"], f["height"]) <= MAX_SIDE
    ]
    if not candidates:
        raise ScriptError("no_low_res_format", f"{MAX_SIDE}p 이하 화질이 없어 받지 않았습니다.")

    def key(f):
        return (
            f.get("protocol") in ("https", "http"),
            str(f.get("vcodec")).startswith("avc1"),
            min(f["width"], f["height"]),
            f.get("ext") == "mp4",
        )

    return max(candidates, key=key)


def download_video(video_id: str, out_dir: Path, proxy: str | None) -> Path:
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    chosen: dict[str, Any] = {}

    def selector(ctx):
        # 추출과 다운로드를 한 인스턴스에서 해야 한다(인스턴스를 나누면 다운로드 URL이 403)
        try:
            chosen["format"] = select_format(ctx["formats"])
        except ScriptError as e:
            chosen["error"] = e
            return
        yield chosen["format"]

    opts = ytdlp_options(proxy, format=selector, outtmpl=str(out_dir / f"{video_id}.%(ext)s"))
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        if "error" in chosen:
            raise chosen["error"] from e
        raise ytdlp_error(e, "영상 다운로드") from e
    if "error" in chosen:
        raise chosen["error"]
    path = out_dir / f"{video_id}.{chosen['format']['ext']}"
    if not path.exists():
        raise ScriptError("download_failed", "영상 파일을 받지 못했습니다.")
    return path


# --- 분석 ---------------------------------------------------------------------

def pick_frame_scenes(n: int, cap: int) -> list[int]:
    """대표 프레임을 저장할 장면 번호. 장면이 cap보다 많으면 고르게 뽑는다."""
    if n <= cap:
        return list(range(n))
    if cap == 1:
        return [0]
    return sorted({round(i * (n - 1) / (cap - 1)) for i in range(cap)})


def _read_gray(cap: cv2.VideoCapture, index: int) -> np.ndarray | None:
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    if not ok:
        return None
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (FLOW_WIDTH, max(1, round(h * FLOW_WIDTH / w))))
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def _classify_flow(flow: np.ndarray) -> str:
    magnitude = float(np.linalg.norm(flow, axis=2).mean())
    if magnitude < STATIC_PX:
        return "static"
    translation = float(np.linalg.norm(flow.reshape(-1, 2).mean(axis=0)))

    h, w = flow.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    rx, ry = xs - (w - 1) / 2, ys - (h - 1) / 2
    norm = np.sqrt(rx ** 2 + ry ** 2) + 1e-6
    radial = float(((flow[..., 0] * rx + flow[..., 1] * ry) / norm).mean())

    if abs(radial) / magnitude > ZOOM_RATIO:
        return "zoom"
    if translation / magnitude > PAN_RATIO:
        return "pan"
    return "static"  # 방향이 제각각인 움직임 = 카메라는 고정, 피사체가 움직임


def classify_camera(cap: cv2.VideoCapture, start: int, end: int, fps: float) -> str:
    step = max(1, round(fps / 10))
    first, last = start + 2, end - 2 - step
    if last < first:
        return "static"
    positions = sorted({round(first + i * (last - first) / max(1, FLOW_SAMPLES - 1))
                        for i in range(FLOW_SAMPLES)})
    labels = []
    for pos in positions:
        a, b = _read_gray(cap, pos), _read_gray(cap, pos + step)
        if a is None or b is None:
            continue
        flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        labels.append(_classify_flow(flow))
    # 카메라 움직임은 장면 내내 이어진다. 손·음식처럼 잠깐 움직이는 피사체를 걸러내려고
    # 모든 지점이 같은 판정일 때만 pan/zoom으로 본다 (다수결은 고정 카메라 먹방을 pan 29%로 오판)
    if len(labels) >= 2 and len(set(labels)) == 1:
        return labels[0]
    return "static"


def analyze_video(path: Path, frames_dir: Path) -> dict:
    from scenedetect import ContentDetector, detect

    detected = detect(
        str(path),
        ContentDetector(threshold=SCENE_THRESHOLD, min_scene_len=MIN_SCENE_FRAMES),
        start_in_scene=True,
    )
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        bounds = [(s.frame_num, e.frame_num) for s, e in detected] or [(0, total)]

        frames_dir.mkdir(parents=True, exist_ok=True)
        with_frame = set(pick_frame_scenes(len(bounds), MAX_FRAMES))
        scenes = []
        for i, (start, end) in enumerate(bounds):
            frame_name = None
            if i in with_frame:
                mid = (start + end) // 2
                cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
                ok, frame = cap.read()
                if ok:
                    frame_name = f"scene{i + 1:03d}_{mid / fps:.1f}s.jpg"
                    cv2.imwrite(str(frames_dir / frame_name), frame)
            scenes.append({
                "start": round(start / fps, 2),
                "end": round(end / fps, 2),
                "camera": classify_camera(cap, start, end, fps),
                "frame": frame_name,
            })
    finally:
        cap.release()
    return {"duration_sec": round(bounds[-1][1] / fps, 2), "fps": round(fps, 2), "scenes": scenes}


def summarize_camera(scenes: list[dict]) -> dict:
    total = sum(s["end"] - s["start"] for s in scenes) or 1
    return {
        label: round(sum(s["end"] - s["start"] for s in scenes if s["camera"] == label) / total, 2)
        for label in ("static", "pan", "zoom")
    }


# --- 흐름 ---------------------------------------------------------------------

def cut_stats(video_id: str, force: bool = False) -> dict:
    out_path = workspace_dir("cuts", f"{video_id}.json")
    if out_path.exists() and not force:
        return {**json.loads(out_path.read_text("utf-8")), "status": "cached"}

    frames_dir = workspace_dir("frames", video_id)
    shutil.rmtree(frames_dir, ignore_errors=True)
    tmp_dir = workspace_dir("tmp")
    try:
        wait_turn()
        video = download_video(video_id, tmp_dir, os.environ.get("YT_PROXY") or None)
        analysis = analyze_video(video, frames_dir)
    finally:
        # 원본 영상은 어떤 경우에도 남기지 않는다
        for leftover in tmp_dir.glob(f"{video_id}.*") if tmp_dir.exists() else []:
            leftover.unlink(missing_ok=True)

    rel_frames = frames_dir.relative_to(work_root()).as_posix()
    scenes = [
        {**s, "frame": f"{rel_frames}/{s['frame']}" if s["frame"] else None}
        for s in analysis["scenes"]
    ]
    result = {
        "id": video_id,
        "duration_sec": analysis["duration_sec"],
        "fps": analysis["fps"],
        "scene_count": len(scenes),
        "avg_cut_sec": round(analysis["duration_sec"] / len(scenes), 2),
        "cuts": [s["start"] for s in scenes[1:]],
        "camera": summarize_camera(scenes),
        "frames": [s["frame"] for s in scenes if s["frame"]],
        "scenes": scenes,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**result, "status": "analyzed"}


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.cuts")
    parser.add_argument("video")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    return cut_stats(parse_video_id(args.video), args.force)


if __name__ == "__main__":
    run(main)
