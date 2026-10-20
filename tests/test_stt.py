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
