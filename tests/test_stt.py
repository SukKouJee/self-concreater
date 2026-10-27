import json
import sys
import types

import pytest

from scripts import fetch, stt
from scripts.common import ScriptError

VID = "dQw4w9WgXcQ"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    fetch.save_raw({"id": VID, "url": f"https://youtu.be/{VID}", "format": "shorts",
                    "meta": {"title": "t", "duration": 30}, "transcript": None})
    return tmp_path


def test_missing_faster_whisper_gives_install_hint(home, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import 실패 유도
    with pytest.raises(ScriptError) as exc:
        stt.transcribe(VID)
    assert exc.value.code == "stt_unavailable"
    assert "pip install faster-whisper" in exc.value.message


def test_requires_raw(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    with pytest.raises(ScriptError) as exc:
        stt.transcribe(VID)
    assert exc.value.code == "raw_not_found"


@pytest.fixture
def fake_whisper(monkeypatch):
    seg = types.SimpleNamespace
    calls = {}

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            calls["model"] = name

        def transcribe(self, path, language=None, vad_filter=True):
            calls["audio"] = path
            info = types.SimpleNamespace(language="ko")
            return iter([seg(start=0.0, end=1.234, text=" 안녕 "), seg(start=1.3, end=2.0, text="반가워")]), info

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    return calls


@pytest.fixture
def fake_audio(monkeypatch, home):
    def download_audio(video_id, out_dir, proxy):
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{video_id}.m4a"
        path.write_bytes(b"audio")
        return path

    monkeypatch.setattr(stt, "download_audio", download_audio)
    monkeypatch.setattr(stt, "decode_audio", lambda path: f"decoded:{path.name}")


def test_transcribe_fills_raw_and_deletes_audio(home, fake_whisper, fake_audio):
    out = stt.transcribe(VID)
    raw = json.loads((home / "workspace" / "raw" / f"{VID}.json").read_text("utf-8"))
    assert raw["transcript"] == {
        "source": "stt", "language": "ko",
        "segments": [{"start": 0.0, "dur": 1.23, "text": "안녕"},
                     {"start": 1.3, "dur": 0.7, "text": "반가워"}],
    }
    assert out["status"] == "transcribed" and out["segments"] == 2
    assert list((home / "workspace" / "tmp").glob("*")) == []
    assert fake_whisper["model"] == "small"


def test_audio_deleted_even_on_failure(home, fake_audio, monkeypatch):
    class Broken:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            raise RuntimeError("model crash")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Broken))
    with pytest.raises(RuntimeError):
        stt.transcribe(VID)
    assert list((home / "workspace" / "tmp").glob("*")) == []


def test_existing_transcript_is_kept_without_force(home, fake_whisper, fake_audio):
    stt.transcribe(VID)
    out = stt.transcribe(VID)
    assert out["status"] == "exists"


def test_model_from_env(home, fake_whisper, fake_audio, monkeypatch):
    monkeypatch.setenv("YT_STT_MODEL", "medium")
    stt.transcribe(VID)
    assert fake_whisper["model"] == "medium"


def test_transcribe_gets_decoded_audio(home, fake_whisper, fake_audio):
    stt.transcribe(VID)
    assert fake_whisper["audio"] == f"decoded:{VID}.m4a"


def test_decode_audio_with_pyav(tmp_path):
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    wav = tmp_path / "tone.wav"
    with av.open(str(wav), mode="w") as out:
        stream = out.add_stream("pcm_s16le", rate=44100, layout="stereo")
        tone = (np.sin(np.arange(44100) * 2 * np.pi * 440 / 44100) * 12000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(np.stack([tone, tone]).reshape(1, -1), format="s16", layout="stereo")
        frame.rate = 44100
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    audio = stt.decode_audio(wav)
    assert audio.dtype.name == "float32"
    assert abs(len(audio) - stt.SAMPLE_RATE) < 200                 # 1초 → 16000 샘플
    assert 0.3 < abs(audio).max() <= 1.0


def test_decode_audio_rejects_garbage(tmp_path):
    pytest.importorskip("av")
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio")
    with pytest.raises(ScriptError) as exc:
        stt.decode_audio(bad)
    assert exc.value.code == "stt_failed" and "bad.m4a" not in exc.value.message


def test_no_speech_keeps_empty_transcript(home, fake_audio, monkeypatch):
    class Silent:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            return iter([types.SimpleNamespace(start=0.0, end=1.0, text="  ")]), types.SimpleNamespace(language="en")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Silent))
    out = stt.transcribe(VID)
    assert out["speech"] is False and out["segments"] == 0 and out["language"] is None
    raw = fetch.load_raw(VID)
    assert raw["transcript"] == {"source": "stt", "language": None, "segments": []}


# --- 진행 콜백 ------------------------------------------------------------------

def _segments_model(ends, duration):
    class Model:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            info = types.SimpleNamespace(language="ko", duration=duration)
            return iter(types.SimpleNamespace(start=e - 1, end=e, text=f"말{e}") for e in ends), info

    return Model


def test_stt_reports_segment_progress(home, fake_audio, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=_segments_model([10, 20, 40], 40)))
    calls = []
    stt.transcribe(VID, progress=lambda *a: calls.append(a))
    seg_calls = [c for c in calls if c[1] is not None]
    assert [c[1] for c in seg_calls] == [0.25, 0.5, 1.0]
    assert all(c[0] == "stt" for c in calls)
    assert seg_calls[0][2] == "음성 인식 중 0:10 / 0:40"


def test_stt_progress_text_formats():
    assert stt.stt_progress_text(750, 2611) == "음성 인식 중 12:30 / 43:31"
    assert stt.stt_progress_text(3725, 4000) == "음성 인식 중 1:02:05 / 1:06:40"
    assert stt.stt_progress_text(5, None) == "음성 인식 중 0:05"


def test_stt_progress_falls_back_to_meta_duration(home, fake_audio, monkeypatch):
    # info에 길이가 없으면 raw 메타(30초)를 쓴다
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=_segments_model([15, 30], None)))
    calls = []
    stt.transcribe(VID, progress=lambda *a: calls.append(a))
    assert [c[1] for c in calls if c[1] is not None] == [0.5, 1.0]


def test_stt_progress_survives_raising_callback(home, fake_audio, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=_segments_model([10, 20], 20)))

    def boom(*a):
        raise RuntimeError("ui gone")

    out = stt.transcribe(VID, progress=boom)
    assert out["segments"] == 2
    assert "ui gone" in capsys.readouterr().err


# --- 로컬 파일 받아쓰기 ---------------------------------------------------------

def _local_model(calls):
    class Model:
        def __init__(self, name, device, compute_type):
            calls["model"] = (name, device, compute_type)

        def transcribe(self, audio, language=None, vad_filter=True, **kwargs):
            calls["audio"], calls["language"] = audio, language
            info = types.SimpleNamespace(language=language, duration=4.0)
            segs = [types.SimpleNamespace(start=0.0, end=1.234, text=" a "),
                    types.SimpleNamespace(start=1.5, end=2.0, text="  "),
                    types.SimpleNamespace(start=2.0, end=4.0, text="b")]
            return iter(segs), info

    return Model


def test_transcribe_file_returns_segments_in_korean(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=_local_model(calls)))
    monkeypatch.setattr(stt, "has_audio", lambda path: True)
    monkeypatch.setattr(stt, "decode_audio", lambda path: f"decoded:{path.name}")
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"x")
    progress = []

    out = stt.transcribe_file(clip, progress=lambda *a: progress.append(a))

    assert out == [{"start": 0.0, "end": 1.23, "text": "a"}, {"start": 2.0, "end": 4.0, "text": "b"}]
    assert calls["language"] == "ko" and calls["audio"] == "decoded:clip.mov"
    assert calls["model"] == ("small", "cpu", "int8")
    assert [p[1] for p in progress if p[1] is not None] == pytest.approx([0.3085, 0.5, 1.0])
    assert clip.read_bytes() == b"x"


def test_transcribe_file_without_whisper(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(ScriptError) as exc:
        stt.transcribe_file(tmp_path / "clip.mov")
    assert exc.value.code == "stt_unavailable"


def test_transcribe_file_without_audio_track_is_empty(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=_local_model(calls)))
    monkeypatch.setattr(stt, "has_audio", lambda path: False)
    assert stt.transcribe_file(tmp_path / "clip.mov") == []
    assert "audio" not in calls


def test_has_audio(tmp_path):
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    wav = tmp_path / "tone.wav"
    with av.open(str(wav), mode="w") as out:
        stream = out.add_stream("pcm_s16le", rate=16000, layout="mono")
        frame = av.AudioFrame.from_ndarray(np.zeros((1, 1600), dtype=np.int16), format="s16", layout="mono")
        frame.rate = 16000
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    assert stt.has_audio(wav) is True
    bad = tmp_path / "bad.mov"
    bad.write_bytes(b"garbage")
    with pytest.raises(ScriptError) as exc:
        stt.has_audio(bad)
    assert exc.value.code == "stt_failed" and "bad.mov" not in exc.value.message
