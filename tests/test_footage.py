import hashlib
import json
import os
import shutil
import sys
import types

import cv2
import numpy as np
import pytest

from scripts import footage
from scripts.common import ScriptError
from tests.test_cuts import sample_video  # noqa: F401 - fixture


@pytest.fixture
def home(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("YT_COACH_HOME", str(work))
    return work


@pytest.fixture
def no_whisper(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import 실패 유도


@pytest.fixture
def fake_whisper(monkeypatch):
    calls = {"transcribe": 0}

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            calls["model"] = (name, device, compute_type)

        def transcribe(self, audio, language=None, vad_filter=True, **kwargs):
            calls["transcribe"] += 1
            calls["language"] = language
            seg = types.SimpleNamespace
            info = types.SimpleNamespace(language="ko", duration=6.0)
            return iter([seg(start=0.5, end=1.25, text=" 안녕하세요 "), seg(start=2.0, end=2.5, text=" ")]), info

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    return calls


def _shoot(tmp_path, sample):
    folder = tmp_path / "shoot"
    folder.mkdir(exist_ok=True)
    good = folder / "A001.MP4"
    shutil.copy(sample, good)
    return folder, good


# --- 폴더 스캔 ------------------------------------------------------------------

def test_scan_lists_video_files_case_insensitive_and_skips_subfolders(tmp_path):
    folder = tmp_path / "day1"
    (folder / "sub").mkdir(parents=True)
    for name in ["b.MOV", "a.mp4", "c.m4v", "d.MKV", "notes.txt", "e.jpg"]:
        (folder / name).write_bytes(b"x" * 10)
    (folder / "sub" / "z.mp4").write_bytes(b"x")
    (folder / "dir.mp4").mkdir()                       # 폴더는 이름이 영상 같아도 빠진다

    files = footage.scan_folder(folder)

    assert [f["name"] for f in files] == ["a.mp4", "b.MOV", "c.m4v", "d.MKV"]
    first = files[0]
    assert set(first) == {"path", "name", "size", "mtime_ns", "fid"}
    assert first["size"] == 10
    assert first["fid"] == footage.fingerprint(folder / "a.mp4")
    assert first["path"] == str((folder / "a.mp4").resolve())


@pytest.mark.parametrize("make", [lambda p: None, lambda p: p.write_text("file")])
def test_scan_missing_folder_raises(tmp_path, make):
    target = tmp_path / "nope"
    make(target)
    with pytest.raises(ScriptError) as exc:
        footage.scan_folder(target)
    assert exc.value.code == "folder_not_found"


# --- 지문 -----------------------------------------------------------------------

def test_fingerprint_is_stable_and_follows_path_size_mtime(tmp_path):
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"abc")
    st = clip.stat()

    first = footage.fingerprint(clip)
    assert footage.fingerprint(clip) == first
    assert footage.fingerprint(tmp_path / "." / "clip.mov") == first

    norm = os.path.normcase(str(clip.resolve()))
    assert first == f"{hashlib.sha1(norm.encode('utf-8')).hexdigest()[:12]}-{st.st_size}-{st.st_mtime_ns}"

    os.utime(clip, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert footage.fingerprint(clip) != first

    other = tmp_path / "other.mov"
    shutil.copy2(clip, other)
    assert footage.fingerprint(other) != footage.fingerprint(clip)


def test_footage_dir_is_under_workspace(home):
    assert footage.footage_dir("abc-1-2") == home / "workspace" / "footage" / "abc-1-2"


# --- 처리 -----------------------------------------------------------------------

def test_process_extracts_scenes_frames_and_montages(home, sample_video):
    out = footage.process_file(sample_video, stt=False)

    assert out["status"] == "done"
    assert out["fid"] == footage.fingerprint(sample_video)
    assert out["duration_sec"] == pytest.approx(6.0, abs=0.3)
    scenes = out["scenes"]
    assert len(scenes) == 3
    for scene, start in zip(scenes, [0.0, 2.0, 4.0]):
        assert scene["start"] == pytest.approx(start, abs=0.25)
    assert scenes[-1]["end"] == pytest.approx(out["duration_sec"], abs=0.25)
    assert [s["i"] for s in scenes] == [0, 1, 2]

    base = footage.footage_dir(out["fid"])
    for scene in scenes:
        assert 4 <= len(scene["frames"]) <= 6
        for rel in scene["frames"]:
            img = cv2.imread(str(base / rel))
            assert img is not None and max(img.shape[:2]) == footage.FRAME_LONG_EDGE
        montage = cv2.imread(str(base / scene["montage"]))
        assert montage is not None and max(montage.shape[:2]) == footage.MONTAGE_LONG_EDGE
    assert out["transcript"] is None
    assert json.loads((base / "scenes.json").read_text("utf-8"))["scenes"] == scenes


def test_process_reports_scene_progress(home, sample_video):
    calls = []
    footage.process_file(sample_video, progress=lambda *a: calls.append(a), stt=False)
    fractions = [c[1] for c in calls if c[0] == "scenes" and c[1] is not None]
    assert fractions and fractions[-1] == 1.0
    assert fractions == sorted(fractions)
    assert all("장면" in c[2] for c in calls if c[0] == "scenes")


def test_scene_progress_without_known_duration(home, tmp_path):
    frames = ((t / 5, np.zeros((36, 64, 3), dtype=np.uint8)) for t in range(150))   # 30초
    calls = []
    duration, scenes = footage.extract_scenes(footage.Source("test", None, frames), tmp_path / "out",
                                              lambda *a: calls.append(a))
    assert duration == pytest.approx(30.0) and len(scenes) == 1
    assert [c[1] for c in calls[:-1]] == [None, None]
    assert calls[0][2] == "장면 확인 중 0:10" and calls[-1][1] == 1.0


def test_process_reuses_cache_without_decoding(home, sample_video, monkeypatch):
    first = footage.process_file(sample_video, stt=False)

    def boom(*a, **k):
        raise AssertionError("decoded again")

    monkeypatch.setattr(footage, "_read_opencv", boom)
    monkeypatch.setattr(footage, "_read_pyav", boom)
    assert footage.process_file(sample_video, stt=False) == first


def test_process_restarts_cleanly_after_interrupted_run(home, sample_video):
    base = footage.footage_dir(footage.fingerprint(sample_video))
    (base / "frames").mkdir(parents=True)
    (base / "frames" / "leftover.jpg").write_bytes(b"half")

    out = footage.process_file(sample_video, stt=False)

    assert out["status"] == "done"
    assert not (base / "frames" / "leftover.jpg").exists()
    assert not list(base.glob("*.tmp"))


def test_process_falls_back_to_pyav_when_opencv_cannot_open(home, sample_video, monkeypatch):
    monkeypatch.setattr(footage, "_read_opencv", lambda path, size: None)
    out = footage.process_file(sample_video, stt=False)
    assert out["status"] == "done" and out["decoder"] == "pyav"
    assert len(out["scenes"]) == 3


def test_broken_file_is_unreadable_and_others_continue(home, tmp_path, sample_video):
    folder, good = _shoot(tmp_path, sample_video)
    (folder / "A000.mov").write_bytes(b"\x00garbage" * 500)

    results = [footage.process_file(f["path"], stt=False) for f in footage.scan_folder(folder)]

    assert [r["status"] for r in results] == ["unreadable", "done"]
    assert results[0]["reason"].startswith("영상을 열 수 없습니다")
    assert str(folder) not in results[0]["reason"] and "A000.mov" not in results[0]["reason"]
    assert results[0]["scenes"] == [] and results[0]["transcript"] is None
    assert len(results[1]["scenes"]) == 3
    # 읽을 수 없음도 캐시해서 다시 열어 보지 않는다
    assert footage.process_file(folder / "A000.mov", stt=False) == results[0]


def test_original_file_is_untouched(home, tmp_path, sample_video):
    folder, good = _shoot(tmp_path, sample_video)
    before = (good.stat().st_mtime_ns, good.read_bytes())
    footage.process_file(good, stt=False)
    assert (good.stat().st_mtime_ns, good.read_bytes()) == before
    assert sorted(p.name for p in folder.iterdir()) == ["A001.MP4"]


# --- 받아쓰기 -------------------------------------------------------------------

def test_stt_missing_keeps_scenes_and_retries_later(home, sample_video, no_whisper, monkeypatch):
    out = footage.process_file(sample_video)
    assert out["status"] == "done" and len(out["scenes"]) == 3
    assert out["transcript"] is None and out["stt_missing"] is True
    base = footage.footage_dir(out["fid"])
    assert not (base / "transcript.json").exists()


def test_transcript_cached_and_scenes_not_redone(home, sample_video, fake_whisper, monkeypatch):
    monkeypatch.setattr(footage.stt, "decode_audio", lambda path: np.zeros(16000, dtype=np.float32))
    monkeypatch.setattr(footage.stt, "has_audio", lambda path: True)
    footage.process_file(sample_video, stt=False)

    def boom(*a, **k):
        raise AssertionError("decoded again")

    monkeypatch.setattr(footage, "_read_opencv", boom)
    out = footage.process_file(sample_video)
    assert out["transcript"] == [{"start": 0.5, "end": 1.25, "text": "안녕하세요"}]
    assert "stt_missing" not in out
    assert fake_whisper["language"] == "ko"

    again = footage.process_file(sample_video)
    assert again == out and fake_whisper["transcribe"] == 1


def test_file_without_audio_gets_empty_transcript(home, sample_video, fake_whisper):
    out = footage.process_file(sample_video)        # cv2가 만든 영상은 소리 트랙이 없다
    assert out["transcript"] == []
    assert fake_whisper["transcribe"] == 0


# --- 보조 함수 ------------------------------------------------------------------

@pytest.mark.parametrize("seconds, expected", [(1.0, 4), (9.9, 4), (10.0, 5), (29.9, 5), (30.0, 6), (600.0, 6)])
def test_photos_per_scene(seconds, expected):
    assert footage.photos_per_scene(seconds) == expected


@pytest.mark.parametrize("shape, count", [((180, 320), 6), ((320, 180), 6), ((180, 320), 4), ((240, 320), 5)])
def test_montage_long_edge(shape, count):
    tiles = [np.full((*shape, 3), i * 20, dtype=np.uint8) for i in range(count)]
    out = footage.make_montage(tiles)
    assert max(out.shape[:2]) == footage.MONTAGE_LONG_EDGE


@pytest.mark.parametrize("rotation, expected", [(0, None), (-90, cv2.ROTATE_90_CLOCKWISE),
                                                 (270, cv2.ROTATE_90_CLOCKWISE),
                                                 (90, cv2.ROTATE_90_COUNTERCLOCKWISE), (180, cv2.ROTATE_180)])
def test_rotation_code(rotation, expected):
    assert footage.rotation_code(rotation) == expected


def test_scene_buffer_keeps_even_spread_with_bounded_memory():
    buf = footage.SceneBuffer(limit=8)
    for i in range(100):
        buf.add(float(i), np.full((2, 2, 3), i, dtype=np.uint8))
    assert len(buf.items) <= 8
    picked = buf.pick(4)
    times = [t for t, _ in picked]
    assert len(picked) == 4 and times == sorted(times)
    assert times[0] < 30 and times[-1] > 60


# --- CLI ------------------------------------------------------------------------

def test_cli_scan_and_process(home, tmp_path, sample_video):
    folder, good = _shoot(tmp_path, sample_video)
    scanned = footage.main(["scan", str(folder)])
    assert [f["name"] for f in scanned["files"]] == ["A001.MP4"]
    out = footage.main(["process", str(good), "--no-stt"])
    assert out["status"] == "done"


def test_cli_process_missing_file(home, tmp_path):
    with pytest.raises(ScriptError) as exc:
        footage.main(["process", str(tmp_path / "none.mov")])
    assert exc.value.code == "file_not_found"
