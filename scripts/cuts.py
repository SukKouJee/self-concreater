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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

import cv2
import numpy as np

from scripts import ctx, keys, limits, refstore
from scripts.common import (
    TEMPLATES_DIR,
    ArgParser,
    Progress,
    ScriptError,
    run,
    safe_progress,
)
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
SCENE_REPORTS = 500       # 장면 감지 중 진행 보고 횟수(전체 프레임을 이만큼 나눠 알린다)
UNKNOWN_TOTAL_EVERY = 30  # 전체 프레임 수를 모를 때 이 프레임마다 알린다
DECODE_RETRIES = 5        # 프레임 읽기 실패 시 다시 시도하는 횟수 (scenedetect max_decode_attempts 기본값과 같다)

if TYPE_CHECKING:
    from scenedetect import ContentDetector, FrameTimecode


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


def download_progress_hook(progress: Progress) -> Callable[[dict[str, Any]], None]:
    """yt-dlp progress_hooks용 함수. 받은 바이트/전체(없으면 추정치)로 ("download", 0~1, 글)을 알린다.

    전체를 모르면 진행률은 None. 추정치가 커져도 진행률·글은 뒤로 가지 않는다.
    yt-dlp는 hook 예외로 다운로드를 멈추므로 콜백 오류는 safe_progress가 삼킨다.
    """
    report = safe_progress(progress)
    assert report is not None
    best = 0.0

    def hook(d: dict[str, Any]) -> None:
        nonlocal best
        done = d.get("downloaded_bytes")
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "finished":
            best = 1.0
        elif done is not None and total:
            best = max(best, min(1.0, done / total))
        elif best == 0.0:
            mb = f" ({done / 1_000_000:.1f}MB)" if done else ""
            report("download", None, f"영상 받는 중{mb}")
            return
        report("download", best, f"영상 받는 중 {int(best * 100)}%")

    return hook


def download_video(video_id: str, out_dir: Path, proxy: str | None, progress: Progress | None = None) -> Path:
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

    extra: dict[str, Any] = {"progress_hooks": [download_progress_hook(progress)]} if progress else {}
    opts = ytdlp_options(proxy, format=selector, outtmpl=str(out_dir / f"{video_id}.%(ext)s"), **extra)
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


def _gray(frame: np.ndarray) -> np.ndarray:
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


def flow_pairs(start: int, end: int, fps: float) -> list[tuple[int, int]]:
    """장면 [start, end)에서 optical flow를 잴 프레임 쌍(앞, 0.1초 뒤). 장면이 너무 짧으면 빈 목록."""
    step = max(1, round(fps / 10))
    first, last = start + 2, end - 2 - step
    if last < first:
        return []
    positions = sorted({round(first + i * (last - first) / max(1, FLOW_SAMPLES - 1))
                        for i in range(FLOW_SAMPLES)})
    return [(pos, pos + step) for pos in positions]


def classify_camera(grays: dict[int, np.ndarray], pairs: list[tuple[int, int]]) -> str:
    labels = []
    for a, b in pairs:
        if a not in grays or b not in grays:
            continue
        flow = cv2.calcOpticalFlowFarneback(grays[a], grays[b], None, 0.5, 3, 15, 3, 5, 1.2, 0)
        labels.append(_classify_flow(flow))
    # 카메라 움직임은 장면 내내 이어진다. 손·음식처럼 잠깐 움직이는 피사체를 걸러내려고
    # 모든 지점이 같은 판정일 때만 pan/zoom으로 본다 (다수결은 고정 카메라 먹방을 pan 29%로 오판)
    if len(labels) >= 2 and len(set(labels)) == 1:
        return labels[0]
    return "static"


def read_scene_frames(
    cap: cv2.VideoCapture, plans: list[tuple[list[tuple[int, int]], int | None]],
) -> Iterator[tuple[dict[int, np.ndarray], np.ndarray | None]]:
    """장면 순서대로 (flow용 흑백 프레임 {번호: 프레임}, 대표 프레임 또는 None)을 하나씩 낸다.

    plans는 장면마다 (flow_pairs, 대표 프레임 번호 또는 None)이고 장면 구간은 겹치지 않고 순서대로다.
    번호로 찾아가면(CAP_PROP_POS_FRAMES) 매번 앞 키프레임부터 다시 풀어서 느리다(8분 영상 장면 65개:
    25초). 그래서 처음부터 한 번만 읽고(grab) 필요한 번호에서만 꺼낸다(retrieve, 같은 영상 6.6초).
    한 장면 프레임만 들고 있다가 내보낸다. 깨진 프레임은 장면 감지처럼 DECODE_RETRIES번 다시 시도하고,
    그래도 안 되면(영상이 컨테이너 프레임 수보다 일찍 끝남) 못 읽은 프레임은 빠진다.
    """
    index = -1  # 마지막으로 읽은 프레임 번호
    ended = False
    for pairs, picture_at in plans:
        wanted = {n for pair in pairs for n in pair}
        targets = sorted(n for n in wanted | ({picture_at} if picture_at is not None else set()) if n >= 0)
        grays: dict[int, np.ndarray] = {}
        picture = None
        for target in targets:
            while not ended and index < target:
                if not any(cap.grab() for _ in range(1 + DECODE_RETRIES)):
                    ended = True
                    break
                index += 1
            if ended or index != target:
                continue
            ok, frame = cap.retrieve()
            if not ok:
                continue
            if target in wanted:
                grays[target] = _gray(frame)
            if target == picture_at:
                picture = frame
        yield grays, picture


def scene_progress_text(done: int, total: int | None) -> str:
    if not total:
        return f"장면 확인 중 ({done:,} 프레임)"
    return f"장면 확인 중 ({done:,} / {total:,} 프레임)"


def camera_progress_text(done: int, total: int) -> str:
    return f"카메라 움직임 확인 중 ({done:,} / {total:,} 장면)"


def reporting_detector(report: Progress, total: int) -> ContentDetector:
    """process_frame에서 읽은 프레임 수를 ("scenes", 읽은/전체, 글)로 알리는 ContentDetector.

    scenedetect 0.6은 프레임 번호를 int로, 0.7은 FrameTimecode로 넘긴다. 둘 다 int()가 된다.
    전체 프레임 수(컨테이너 값)를 모르거나 실제보다 작아 읽은 수가 넘어서면 100%에 멈추지 않도록
    진행률을 None으로 보내고 글에는 실제 읽은 수만 쓴다(화면은 앞 값을 유지해 뒤로 가지 않는다).
    """
    from scenedetect import ContentDetector

    every = max(1, total // SCENE_REPORTS) if total > 0 else UNKNOWN_TOTAL_EVERY

    class _Reporting(ContentDetector):
        def process_frame(self, timecode: FrameTimecode | int, frame_img: np.ndarray) -> list[FrameTimecode]:
            found: list[FrameTimecode] = super().process_frame(timecode, frame_img)
            read = int(timecode) + 1
            if read % every == 0:
                if 0 < total and read < total:
                    report("scenes", read / total, scene_progress_text(read, total))
                else:
                    report("scenes", None, scene_progress_text(read, None))
            return found

    return _Reporting(threshold=SCENE_THRESHOLD, min_scene_len=MIN_SCENE_FRAMES)


def detect_scenes(path: Path, progress: Progress | None = None) -> list[tuple[FrameTimecode, FrameTimecode]]:
    """scenedetect.detect()와 같은 장면 목록. progress가 있으면 ("scenes", 읽은 프레임/전체, 글)을 알린다.

    진행은 별도 스레드 없이, 감지기가 프레임을 처리하는 자리에서 프레임 번호를 읽어
    전체의 1/SCENE_REPORTS마다 한 번만 알린다(프레임마다 정수 비교 한 번).
    """
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(path))
    report = safe_progress(progress)
    detector: ContentDetector
    if report is None:
        detector = ContentDetector(threshold=SCENE_THRESHOLD, min_scene_len=MIN_SCENE_FRAMES)
    else:
        total = video.duration.frame_num if video.duration is not None else 0
        detector = reporting_detector(report, total)

    manager = SceneManager()
    manager.add_detector(detector)
    manager.detect_scenes(video=video)
    scenes: list[tuple[FrameTimecode, FrameTimecode]] = manager.get_scene_list(start_in_scene=True)
    if report is not None:
        end = video.frame_number
        report("scenes", 1.0, scene_progress_text(end, end))
    return scenes


def analyze_video(path: Path, frames_dir: Path, progress: Progress | None = None) -> dict:
    report = safe_progress(progress)
    detected = detect_scenes(path, report)
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        bounds = [(s.frame_num, e.frame_num) for s, e in detected] or [(0, total)]

        frames_dir.mkdir(parents=True, exist_ok=True)
        with_frame = set(pick_frame_scenes(len(bounds), MAX_FRAMES))
        plans = [(flow_pairs(start, end, fps), (start + end) // 2 if i in with_frame else None)
                 for i, (start, end) in enumerate(bounds)]
        scenes = []
        frames = read_scene_frames(cap, plans)
        for i, ((start, end), (pairs, mid), (grays, picture)) in enumerate(zip(bounds, plans, frames, strict=True)):
            frame_name = None
            if picture is not None and mid is not None:
                frame_name = f"scene{i + 1:03d}_{mid / fps:.1f}s.jpg"
                cv2.imwrite(str(frames_dir / frame_name), picture)
            scenes.append({
                "start": round(start / fps, 2),
                "end": round(end / fps, 2),
                "camera": classify_camera(grays, pairs),
                "frame": frame_name,
            })
            if report is not None:
                report("camera", (i + 1) / len(bounds), camera_progress_text(i + 1, len(bounds)))
    finally:
        cap.release()
    return {"duration_sec": round(bounds[-1][1] / fps, 2), "fps": round(fps, 2), "scenes": scenes}


def detect_cuts(path: Path) -> dict:
    """영상의 내부 컷 시각만 가볍게 찾는다(프레임 저장·카메라 분석 없음). 영상 파일은 읽기만 한다."""
    from scenedetect import ContentDetector, detect

    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ScriptError("bad_video", f"영상을 열 수 없습니다: {Path(path).name}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()
    if fps <= 0:
        raise ScriptError("bad_video", f"영상 길이를 읽을 수 없습니다: {Path(path).name}")
    try:
        scenes = detect(
            str(path),
            ContentDetector(threshold=SCENE_THRESHOLD, min_scene_len=MIN_SCENE_FRAMES),
            start_in_scene=True,
        )
    except (OSError, ValueError, RuntimeError, cv2.error) as e:
        raise ScriptError("bad_video", f"영상에서 컷을 찾지 못했습니다: {Path(path).name} ({e})") from e
    if total > 0:
        duration = total / fps
    elif scenes:
        duration = scenes[-1][1].frame_num / fps        # 컨테이너에 프레임 수가 없으면 장면 감지가 읽은 끝을 쓴다
    else:
        raise ScriptError("bad_video", f"영상 길이를 읽을 수 없습니다: {Path(path).name}")
    return {
        "duration_sec": round(duration, 3),
        "fps": round(fps, 2),
        "cuts": [round(start.frame_num / fps, 3) for start, _ in scenes[1:]],
    }


def summarize_camera(scenes: list[dict]) -> dict:
    total = sum(s["end"] - s["start"] for s in scenes) or 1
    return {
        label: round(sum(s["end"] - s["start"] for s in scenes if s["camera"] == label) / total, 2)
        for label in ("static", "pan", "zoom")
    }


# --- 흐름 ---------------------------------------------------------------------

DOWNLOAD_START_TEXT = "영상 받는 중"


def _wait_reporter(report: Progress | None) -> Callable[[], None] | None:
    """media 슬롯을 기다리는 동안 보일 문구(진행 단계 "wait": 걸린 시간에 넣지 않는다)."""
    if report is None:
        return None
    return lambda: report("wait", None, limits.WAIT_TEXT)


def cut_stats(video_id: str, force: bool = False, progress: Progress | None = None) -> dict:
    """progress가 있으면 wait → download → scenes → camera 순서로 하위 구간 진행을 알린다.

    캐시 확인부터 저장까지 같은 영상의 수집 잠금(`limits.collecting`) 안에서 한다: 프레임 폴더를 비우고 다시 채우는
    동안 다른 사용자의 같은 영상 수집이 끼어들지 않고, 뒤에 온 쪽은 앞의 결과를 캐시로 쓴다.
    """
    report = safe_progress(progress)
    with limits.collecting(video_id, on_wait=limits.collect_waiter(report)):
        return _cut_stats(video_id, force, report)


def _cut_stats(video_id: str, force: bool, report: Progress | None) -> dict:
    c = ctx.here()
    cuts_key = keys.cuts(video_id)
    if c.shared.exists(cuts_key) and not force:
        return {**json.loads(c.shared.get(cuts_key).decode("utf-8")), "status": "cached"}

    frames_key = keys.frames_dir(video_id)
    c.shared.delete_prefix(frames_key)
    tmp_dir = c.scratch.local_path("tmp")
    try:
        wait_turn(report)
        with limits.slot("media", on_wait=_wait_reporter(report)):
            if report is not None:
                # 첫 progress hook은 extract_info가 끝난 뒤에야 온다: 그 사이를 대기로 남기지 않는다
                report("download", None, DOWNLOAD_START_TEXT)
            proxy = os.environ.get("YT_PROXY") or None
            video = download_video(video_id, tmp_dir, proxy, progress=report)
            try:
                with c.shared.write_path(frames_key) as frames_dir:
                    analysis = analyze_video(video, frames_dir, report)
            except BaseException:
                # write_path가 미리 만든 빈 폴더는 남기지 않는다(프레임이 일부 있으면 예전처럼 둔다)
                if not c.shared.list(frames_key):
                    c.shared.delete_prefix(frames_key)
                raise
    finally:
        # 원본 영상은 어떤 경우에도 남기지 않는다
        for leftover in tmp_dir.glob(f"{video_id}.*") if tmp_dir.exists() else []:
            leftover.unlink(missing_ok=True)

    rel_frames = frames_key.rstrip("/")
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
    # write_text 기본 줄바꿈(Windows CRLF)과 같은 바이트
    c.shared.put(cuts_key, refstore.text_bytes(json.dumps(result, ensure_ascii=False, indent=2)))
    return {**result, "status": "analyzed"}


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.cuts")
    parser.add_argument("video")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    return cut_stats(parse_video_id(args.video), args.force)


if __name__ == "__main__":
    run(main)
