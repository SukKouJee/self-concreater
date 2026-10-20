"""자막이 없는 영상의 음성을 받아 STT로 자막을 만든다 (선택 기능).

사용법 (작업 폴더에서, fetch 이후):
  python -m scripts.stt <video_id> [--force]

- 선택 의존성: pip install faster-whisper
- 모델: YT_STT_MODEL 환경변수 (기본 small). CPU int8로 실행
- 오디오는 workspace/tmp/에 받았다가 성공·실패와 관계없이 삭제한다
- 디코딩은 PyAV로 직접 한다(16kHz mono float32). faster-whisper 1.2.1 내장 디코더는
  av 19에서 사라진 metadata_errors 인자를 넘겨 실패한다
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.common import ArgParser, ScriptError, run, workspace_dir
from scripts.fetch import load_raw, save_raw, ytdlp_error, ytdlp_options

DEFAULT_MODEL = "small"
SAMPLE_RATE = 16000


def _load_whisper():
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ScriptError(
            "stt_unavailable",
            "음성 인식 기능이 설치돼 있지 않습니다. 플러그인 폴더에서 "
            "`pip install faster-whisper`를 실행한 뒤 다시 시도하세요.",
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


def decode_audio(path: Path):
    """PyAV(faster-whisper 의존성)로 16kHz mono float32 배열을 만든다."""
    import av
    import numpy as np

    chunks = []
    try:
        with av.open(str(path), mode="r") as container:
            resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):  # 남은 샘플 비우기
                chunks.append(out.to_ndarray().reshape(-1))
    except av.error.FFmpegError as e:
        raise ScriptError("stt_failed", f"오디오를 디코딩하지 못했습니다: {e}") from e
    if not chunks:
        raise ScriptError("stt_failed", "오디오 트랙이 비어 있습니다")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def transcribe(video_id: str, force: bool = False) -> dict:
    data = load_raw(video_id)
    if data.get("transcript") and not force:
        return {"id": video_id, "status": "exists", "source": data["transcript"]["source"]}

    WhisperModel = _load_whisper()
    model_name = os.environ.get("YT_STT_MODEL") or DEFAULT_MODEL
    tmp_dir = workspace_dir("tmp")
    try:
        audio = download_audio(video_id, tmp_dir, os.environ.get("YT_PROXY") or None)
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, info = model.transcribe(decode_audio(audio), language=None, vad_filter=True)
        segs = [{"start": round(s.start, 2), "dur": round(s.end - s.start, 2), "text": s.text.strip()}
                for s in segments if s.text.strip()]
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


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.stt")
    parser.add_argument("video_id")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    return transcribe(args.video_id, args.force)


if __name__ == "__main__":
    run(main)
