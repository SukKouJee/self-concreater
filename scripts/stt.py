"""자막이 없는 영상의 음성을 받아 STT로 자막을 만든다 (선택 기능).

사용법 (작업 폴더에서, fetch 이후):
  python -m scripts.stt <video_id> [--force]

- 선택 의존성: pip install faster-whisper
- 모델: YT_STT_MODEL 환경변수 (기본 small). CPU int8로 실행
- 오디오는 workspace/tmp/에 받았다가 성공·실패와 관계없이 삭제한다
- 로컬 파일(촬영 영상)은 transcribe_file()로 받아쓴다(언어 기본 ko, 원본은 읽기만)
- 디코딩은 PyAV로 직접 한다(16kHz mono float32). faster-whisper 1.2.1 내장 디코더는
  av 19에서 사라진 metadata_errors 인자를 넘겨 실패한다
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, NamedTuple

from scripts import ctx, limits
from scripts.common import ArgParser, Progress, ScriptError, run, safe_progress
from scripts.edition import ed
from scripts.fetch import load_raw, save_raw, ytdlp_error, ytdlp_options

DEFAULT_MODEL = "small"
SAMPLE_RATE = 16000
WINDOW_SEC = 30 * 60      # 로컬 파일은 이 길이씩 끊어 받아쓴다(메모리 상한: 창 하나 ≈ 115MB)

if TYPE_CHECKING:
    import numpy as np


def _load_whisper():
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ScriptError(
            "stt_unavailable",
            ed("음성 인식 기능이 설치돼 있지 않습니다. 플러그인 폴더에서 "
               "`pip install faster-whisper`를 실행한 뒤 다시 시도하세요.",
               "이 서버에는 음성 인식 기능이 없습니다."),
        ) from e
    return WhisperModel


def download_audio(video_id: str, out_dir: Path, proxy: str | None) -> Path:
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = ytdlp_options(
        proxy,
        format="bestaudio[ext=m4a]/bestaudio",
        outtmpl=str(out_dir / f"{video_id}.%(ext)s"),
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            return Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        raise ytdlp_error(e, "오디오 다운로드") from e


def av_detail(e: Exception) -> str:
    """PyAV 오류의 설명만(문구에 붙는 파일 경로는 빼고) — 화면에 보일 이유로 쓴다."""
    return getattr(e, "strerror", None) or type(e).__name__


def _audio_chunks(path: Path) -> Iterator[np.ndarray]:
    """PyAV(faster-whisper 의존성)로 16kHz mono float32 조각을 차례로 낸다. 리샘플러가 바로 float32를 낸다."""
    import av

    try:
        with av.open(str(path), mode="r") as container:
            resampler = av.audio.resampler.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):
                    yield out.to_ndarray().reshape(-1)
            for out in resampler.resample(None):  # 남은 샘플 비우기
                yield out.to_ndarray().reshape(-1)
    except av.error.FFmpegError as e:
        raise ScriptError("stt_failed", f"오디오를 디코딩하지 못했습니다: {av_detail(e)}") from e


def decode_audio(path: Path) -> np.ndarray:
    """16kHz mono float32 배열 하나(레퍼런스용 짧은 오디오). 긴 파일은 iter_audio_windows를 쓴다."""
    import numpy as np

    chunks = list(_audio_chunks(path))
    if not chunks:
        raise ScriptError("stt_failed", "오디오 트랙이 비어 있습니다")
    return np.concatenate(chunks)


def iter_audio_windows(path: Path, window_sec: float | None = None) -> Iterator[tuple[float, np.ndarray]]:
    """(시작 초, 16kHz mono float32 배열)을 window_sec(기본 WINDOW_SEC)씩 낸다.

    2시간 오디오를 한 배열로 만들면 수백 MB가 겹쳐 쌓인다. 창 하나 분량만 들고 있다가 넘긴다.
    """
    import numpy as np

    size = int((window_sec or WINDOW_SEC) * SAMPLE_RATE)
    pending: list[np.ndarray] = []
    held = 0
    offset = 0
    for chunk in _audio_chunks(path):
        pending.append(chunk)
        held += len(chunk)
        while held >= size:
            joined = np.concatenate(pending)
            yield offset / SAMPLE_RATE, joined[:size]
            offset += size
            rest = joined[size:]
            pending, held = ([rest] if len(rest) else []), len(rest)
    if held:
        yield offset / SAMPLE_RATE, np.concatenate(pending)


def _clock(sec: float) -> str:
    total = int(sec)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def stt_progress_text(done_sec: float, duration_sec: float | None) -> str:
    if not duration_sec:
        return f"음성 인식 중 {_clock(done_sec)}"
    return f"음성 인식 중 {_clock(min(done_sec, duration_sec))} / {_clock(duration_sec)}"


def _wait_reporter(report: Progress | None) -> Callable[[], None] | None:
    if report is None:
        return None
    return lambda: report("wait", None, limits.WAIT_TEXT)


def transcribe(video_id: str, force: bool = False, progress: Progress | None = None) -> dict:
    """progress가 있으면 segment마다 ("stt", 끝 시각/영상 길이, "음성 인식 중 12:30 / 43:31")을 알린다.

    segments는 순서대로 나오는 생성기라 끝 시각이 곧 처리한 음성 위치다. 길이는 Whisper가 잰
    오디오 길이, 없으면 raw 메타의 duration. 둘 다 없으면 진행률은 None.
    """
    report = safe_progress(progress)
    # raw 읽기부터 저장까지 같은 영상 수집 잠금 안에서(다른 사용자의 수집·STT와 raw를 서로 덮지 않게, 설계 §3.3)
    with limits.collecting(video_id, on_wait=limits.collect_waiter(report)):
        return _transcribe(video_id, force, report)


def _transcribe(video_id: str, force: bool, report: Progress | None) -> dict:
    data = load_raw(video_id)
    if data.get("transcript") and not force:
        return {"id": video_id, "status": "exists", "source": data["transcript"]["source"]}

    WhisperModel = _load_whisper()
    model_name = os.environ.get("YT_STT_MODEL") or DEFAULT_MODEL
    tmp_dir = ctx.here().scratch.local_path("tmp")
    try:
        with limits.slot("media", on_wait=_wait_reporter(report)):
            if report is not None:
                report("stt", None, "음성 받는 중")
            audio = download_audio(video_id, tmp_dir, os.environ.get("YT_PROXY") or None)
            if report is not None:
                report("stt", None, "음성 인식 준비 중")
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            segments, info = model.transcribe(decode_audio(audio), language=None, vad_filter=True)
            duration = getattr(info, "duration", None) or (data.get("meta") or {}).get("duration") or None
            segs = []
            for s in segments:
                if s.text.strip():
                    segs.append({"start": round(s.start, 2), "dur": round(s.end - s.start, 2), "text": s.text.strip()})
                if report is not None:
                    fraction = min(1.0, max(0.0, s.end / duration)) if duration else None
                    report("stt", fraction, stt_progress_text(s.end, duration))
            # 말이 없으면(ASMR·음악) 감지 언어는 의미가 없다
            data["transcript"] = {"source": "stt", "language": info.language if segs else None, "segments": segs}
    finally:
        # 받은 오디오는 어떤 경우에도 남기지 않는다
        for leftover in tmp_dir.glob(f"{video_id}.*") if tmp_dir.exists() else []:
            leftover.unlink(missing_ok=True)

    save_raw(data)
    return {
        "id": video_id, "status": "transcribed", "model": model_name,
        "language": data["transcript"]["language"], "segments": len(data["transcript"]["segments"]),
        "speech": bool(data["transcript"]["segments"]),
    }


class AudioProbe(NamedTuple):
    has_audio: bool
    duration: float | None  # 초. 모르면 None


def probe_audio(path: Path) -> AudioProbe:
    """소리 트랙이 있는지와 길이. 열지 못하면 ScriptError("stt_failed")."""
    import av

    try:
        with av.open(str(path), mode="r") as container:
            if not container.streams.audio:
                return AudioProbe(False, None)
            stream = container.streams.audio[0]
            if stream.duration is not None and stream.time_base is not None:
                return AudioProbe(True, float(stream.duration * stream.time_base))
            return AudioProbe(True, container.duration / av.time_base if container.duration else None)
    except av.error.FFmpegError as e:
        raise ScriptError("stt_failed", f"파일을 열지 못했습니다: {av_detail(e)}") from e


_models: dict[tuple[Any, str], Any] = {}


def _model(whisper_model: Any, name: str) -> Any:
    """파일마다 모델을 다시 올리지 않도록 (클래스, 이름)별로 한 번만 만든다."""
    key = (whisper_model, name)
    if key not in _models:
        _models.clear()
        _models[key] = whisper_model(name, device="cpu", compute_type="int8")
    return _models[key]


def transcribe_file(path: str | Path, lang: str = "ko", progress: Progress | None = None) -> list[dict[str, Any]]:
    """로컬 영상·음성 파일을 받아쓴다. 원본은 읽기만 한다. -> [{"start", "end", "text"}]

    소리 트랙이 없으면 빈 목록. faster-whisper가 없으면 ScriptError("stt_unavailable"),
    소리를 풀지 못하면 ScriptError("stt_failed"). progress는 transcribe()와 같은 ("stt", 비율, 글)이고
    비율은 파일 전체 길이 기준(모르면 None). 긴 파일은 WINDOW_SEC씩 끊어 받아쓰고 창 시작 시각을 더한다.
    """
    path = Path(path)
    whisper_model = _load_whisper()
    probe = probe_audio(path)
    if not probe.has_audio:
        return []
    report = safe_progress(progress)
    if report is not None:
        report("stt", None, "음성 인식 준비 중")
    model = _model(whisper_model, os.environ.get("YT_STT_MODEL") or DEFAULT_MODEL)
    duration = probe.duration
    out: list[dict[str, Any]] = []
    with limits.slot("media", on_wait=_wait_reporter(report)):
        for offset, audio in iter_audio_windows(path):
            segments, _ = model.transcribe(audio, language=lang, vad_filter=True)
            for s in segments:
                start, end = offset + s.start, offset + s.end
                text = s.text.strip()
                if text:
                    out.append({"start": round(start, 2), "end": round(end, 2), "text": text})
                if report is not None:
                    fraction = min(1.0, max(0.0, end / duration)) if duration else None
                    report("stt", fraction, stt_progress_text(end, duration))
    return out


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.stt")
    parser.add_argument("video_id")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    return transcribe(args.video_id, args.force)


if __name__ == "__main__":
    run(main)
