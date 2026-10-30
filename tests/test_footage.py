import contextlib
import hashlib
import json
import os
import shutil
import sys
import types
from pathlib import Path

import av
import cv2
import numpy as np
import pytest

from scripts import footage
from scripts.common import ScriptError
from tests.test_cuts import sample_video  # noqa: F401 - fixture
from tests.helpers.paths import footage_base


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


def test_footage_dir_is_under_workspace(home, tmp_path):
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"abc")
    fid = footage.fingerprint(clip)
    assert footage.valid_fid(fid)
    assert footage.footage_key(fid) == f"workspace/footage/{fid}/"
    assert footage_base(fid) == home / "workspace" / "footage" / fid


@pytest.mark.parametrize("fid", ["", "abc-1-2", "../etc", "0123456789ab-1", "0123456789AB-1-2",
                                 "0123456789ab-1-2/..", "0123456789ab-1-2\\x", "0123456789ab--1-2",
                                 "0123456789ab-1-2\n"])
def test_footage_key_rejects_invalid_fid(home, fid):
    assert not footage.valid_fid(fid)
    with pytest.raises(ScriptError) as exc:
        footage.footage_key(fid)
    assert exc.value.code == "bad_fid"


# --- 처리 -----------------------------------------------------------------------

def test_process_extracts_scenes_frames_and_montages(home, sample_video):
    out = footage.process_file(sample_video, run_stt=False)

    assert out["status"] == "done"
    assert out["fid"] == footage.fingerprint(sample_video)
    assert out["duration_sec"] == pytest.approx(6.0, abs=0.3)
    scenes = out["scenes"]
    assert len(scenes) == 3
    for scene, start in zip(scenes, [0.0, 2.0, 4.0]):
        assert scene["start"] == pytest.approx(start, abs=0.25)
    assert scenes[-1]["end"] == pytest.approx(out["duration_sec"], abs=0.25)
    assert [s["i"] for s in scenes] == [0, 1, 2]

    base = footage_base(out["fid"])
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
    footage.process_file(sample_video, progress=lambda *a: calls.append(a), run_stt=False)
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
    first = footage.process_file(sample_video, run_stt=False)

    def boom(*a, **k):
        raise AssertionError("decoded again")

    monkeypatch.setattr(footage, "_read_opencv", boom)
    monkeypatch.setattr(footage, "_read_pyav", boom)
    assert footage.process_file(sample_video, run_stt=False) == first


def test_cached_result_reads_cache_without_processing(home, sample_video, monkeypatch):
    fid = footage.fingerprint(sample_video)
    assert footage.cached_result(fid) is None
    first = footage.process_file(sample_video, run_stt=False)

    def boom(*a, **k):
        raise AssertionError("decoded again")

    monkeypatch.setattr(footage, "_read_opencv", boom)
    monkeypatch.setattr(footage, "_read_pyav", boom)
    assert footage.cached_result(fid) == first and first["transcript"] is None
    with pytest.raises(ScriptError):
        footage.cached_result("../x")


def test_process_restarts_cleanly_after_interrupted_run(home, sample_video):
    base = footage_base(footage.fingerprint(sample_video))
    (base / "frames").mkdir(parents=True)
    (base / "frames" / "leftover.jpg").write_bytes(b"half")

    out = footage.process_file(sample_video, run_stt=False)

    assert out["status"] == "done"
    assert not (base / "frames" / "leftover.jpg").exists()
    assert not list(base.glob("*.tmp"))


def test_process_decodes_with_pyav_first(home, sample_video):
    out = footage.process_file(sample_video, run_stt=False)
    assert out["decoder"] == "pyav"


def _unopenable(path, size):
    raise ScriptError("unreadable", "영상을 열 수 없습니다: test")


def test_process_falls_back_to_opencv_when_pyav_cannot_open(home, sample_video, monkeypatch):
    monkeypatch.setattr(footage, "_read_pyav", _unopenable)
    out = footage.process_file(sample_video, run_stt=False)
    assert out["status"] == "done" and out["decoder"] == "opencv"
    assert [s["start"] for s in out["scenes"]] == pytest.approx([0.0, 2.0, 4.0], abs=0.25)


def _reader(frames, decoder="fake", duration=None):
    @contextlib.contextmanager
    def reader(path, size):
        yield footage.Source(decoder, duration, iter(frames()))
    return reader


def _blank(t=0.0):
    return t, np.zeros((36, 64, 3), dtype=np.uint8)


def test_zero_frames_from_first_decoder_tries_the_other(home, sample_video, monkeypatch):
    monkeypatch.setattr(footage, "_read_pyav", _reader(lambda: [], "pyav", 6.0))
    out = footage.process_file(sample_video, run_stt=False)
    assert out["status"] == "done" and out["decoder"] == "opencv" and len(out["scenes"]) == 3


@pytest.mark.parametrize("error", [ValueError("bad frame"), cv2.error("cv fail"),
                                   av.error.InvalidDataError(1094995529, "Invalid data")])
def test_decode_errors_mark_file_unreadable_and_folder_continues(home, tmp_path, sample_video, monkeypatch, error):
    folder, good = _shoot(tmp_path, sample_video)
    shutil.copy(sample_video, folder / "A000.mov")

    def failing():
        yield _blank()
        raise error

    for name in ("_read_pyav", "_read_opencv"):
        real = getattr(footage, name)
        broken = _reader(failing)
        monkeypatch.setattr(footage, name,
                            lambda path, size, real=real, broken=broken:
                            broken(path, size) if Path(path).name == "A000.mov" else real(path, size))

    results = [footage.process_file(f["path"], run_stt=False) for f in footage.scan_folder(folder)]

    assert [r["status"] for r in results] == ["unreadable", "done"]
    assert results[0]["reason"] and "A000.mov" not in results[0]["reason"]
    assert not list(footage_base(results[0]["fid"]).glob("frames/*"))


def test_locked_file_is_retried_not_cached(home, sample_video, monkeypatch):
    def locked(path, size):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(footage, "_read_pyav", locked)
    out = footage.process_file(sample_video, run_stt=False)
    assert out["status"] == "retry" and out["reason"]
    assert out["scenes"] == [] and out["transcript"] is None
    assert not (footage_base(out["fid"]) / "scenes.json").exists()

    monkeypatch.undo()
    monkeypatch.setenv("YT_COACH_HOME", str(home))
    assert footage.process_file(sample_video, run_stt=False)["status"] == "done"


class _Spy:
    def __init__(self, inner):
        self._inner = inner
        self.closed = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self):
        self.closed = True
        self._inner.close()

    def release(self):
        self.closed = True
        self._inner.release()


def test_decoder_handles_closed_when_processing_fails_early(home, sample_video, monkeypatch):
    containers, caps = [], []
    real_open, real_cap = av.open, cv2.VideoCapture

    def spy_open(*a, **k):
        containers.append(_Spy(real_open(*a, **k)))
        return containers[-1]

    def spy_cap(*a):
        caps.append(_Spy(real_cap(*a)))
        return caps[-1]

    def explode(*a, **k):
        raise ValueError("failed before reading")

    monkeypatch.setattr(av, "open", spy_open)
    monkeypatch.setattr(footage.cv2, "VideoCapture", spy_cap)
    monkeypatch.setattr(footage, "extract_scenes", explode)

    out = footage.process_file(sample_video, run_stt=False)

    assert out["status"] == "unreadable"
    assert containers and all(c.closed for c in containers)
    assert caps and all(c.closed for c in caps)


class _VfrCapture:
    """CAP_PROP_POS_MSEC가 고르지 않은 가짜 캡처(아이폰 VFR처럼)."""

    TIMES_MS = [0, 33, 66, 100, 250, 260, 420, 700, 710, 720, 1000]

    def __init__(self, path):
        self.i = -1

    def isOpened(self):
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FPS:
            return 30.0
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return len(self.TIMES_MS)
        if prop == cv2.CAP_PROP_POS_MSEC:
            return float(self.TIMES_MS[self.i])
        return 0.0

    def grab(self):
        self.i += 1
        return self.i < len(self.TIMES_MS)

    def retrieve(self):
        return True, np.zeros((240, 320, 3), dtype=np.uint8)

    def release(self):
        pass


def test_opencv_reader_uses_real_timestamps_for_vfr(monkeypatch, tmp_path):
    monkeypatch.setattr(footage.cv2, "VideoCapture", _VfrCapture)
    with footage._read_opencv(tmp_path / "vfr.mov", 360) as source:
        times = [t for t, _ in source.frames]
    assert times == pytest.approx([0.0, 0.25, 0.7, 1.0])


@pytest.mark.parametrize("msec, index, fps, expected", [
    (0.0, 0, 30.0, 0.0), (250.0, 6, 30.0, 0.25), (0.0, 6, 30.0, 0.2), (-1.0, 3, 30.0, 0.1),
    (float("nan"), 3, 30.0, 0.1),
])
def test_frame_time(msec, index, fps, expected):
    assert footage.frame_time(msec, index, fps) == pytest.approx(expected)


def _color(t, value):
    return t, np.full((36, 64, 3), value, dtype=np.uint8)


def test_scene_bounds_follow_uneven_timestamps(tmp_path):
    times = [(0.0, 0), (0.2, 0), (0.4, 0), (1.3, 0), (1.5, 0), (2.9, 255), (3.0, 255), (3.1, 255), (4.5, 255),
             (4.7, 255)]
    frames = (_color(t, v) for t, v in times)
    duration, scenes = footage.extract_scenes(footage.Source("test", None, frames), tmp_path / "out")
    assert [s["start"] for s in scenes] == [0.0, 2.9]
    assert scenes[0]["end"] == 2.9 and duration == pytest.approx(4.9)


def test_short_final_scene_is_merged_into_previous(tmp_path):
    frames = [_color(i / 5, 0) for i in range(16)] + [_color(3.2, 255), _color(3.4, 255)]
    duration, scenes = footage.extract_scenes(footage.Source("test", None, iter(frames)), tmp_path / "out")
    assert len(scenes) == 1
    assert scenes[0]["end"] == pytest.approx(duration) == pytest.approx(3.6)
    assert len(scenes[0]["frames"]) == 4
    assert sorted(p.name for p in (tmp_path / "out" / "montage").iterdir()) == ["s000.jpg"]


def test_broken_file_is_unreadable_and_others_continue(home, tmp_path, sample_video):
    folder, good = _shoot(tmp_path, sample_video)
    (folder / "A000.mov").write_bytes(b"\x00garbage" * 500)

    results = [footage.process_file(f["path"], run_stt=False) for f in footage.scan_folder(folder)]

    assert [r["status"] for r in results] == ["unreadable", "done"]
    assert results[0]["reason"].startswith("영상을 열 수 없습니다")
    assert str(folder) not in results[0]["reason"] and "A000.mov" not in results[0]["reason"]
    assert results[0]["scenes"] == [] and results[0]["transcript"] is None
    assert len(results[1]["scenes"]) == 3
    # 읽을 수 없음도 캐시해서 다시 열어 보지 않는다
    assert footage.process_file(folder / "A000.mov", run_stt=False) == results[0]


def test_original_file_is_untouched(home, tmp_path, sample_video):
    folder, good = _shoot(tmp_path, sample_video)
    before = (good.stat().st_mtime_ns, good.read_bytes())
    footage.process_file(good, run_stt=False)
    assert (good.stat().st_mtime_ns, good.read_bytes()) == before
    assert sorted(p.name for p in folder.iterdir()) == ["A001.MP4"]


# --- 받아쓰기 -------------------------------------------------------------------

def test_stt_missing_keeps_scenes_and_retries_later(home, sample_video, no_whisper, monkeypatch):
    out = footage.process_file(sample_video)
    assert out["status"] == "done" and len(out["scenes"]) == 3
    assert out["transcript"] is None and out["stt_missing"] is True
    base = footage_base(out["fid"])
    assert not (base / "transcript.json").exists()


def test_transcript_cached_and_scenes_not_redone(home, sample_video, fake_whisper, monkeypatch):
    monkeypatch.setattr(footage.stt, "probe_audio", lambda path: footage.stt.AudioProbe(True, 6.0))
    monkeypatch.setattr(footage.stt, "iter_audio_windows",
                        lambda path, window_sec=None: iter([(0.0, np.zeros(16000, dtype=np.float32))]))
    footage.process_file(sample_video, run_stt=False)

    def boom(*a, **k):
        raise AssertionError("decoded again")

    monkeypatch.setattr(footage, "_read_opencv", boom)
    monkeypatch.setattr(footage, "_read_pyav", boom)
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


@pytest.mark.parametrize("name", ["scenes.json", "transcript.json"])
def test_cached_result_treats_corrupt_cache_as_missing(home, name):
    fid = "0123456789ab-100-1"
    base = footage_base(fid)
    base.mkdir(parents=True, exist_ok=True)
    record = {"schema": 1, "fid": fid, "name": "a.mp4", "status": "done", "decoder": "pyav",
              "duration_sec": 3.0, "scenes": []}
    (base / "scenes.json").write_text(json.dumps(record), encoding="utf-8")
    (base / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
    (base / name).write_text("{half writ", encoding="utf-8")
    cached = footage.cached_result(fid)
    if name == "scenes.json":
        assert cached is None                  # 다음 처리 때 다시 뽑는다
    else:
        assert cached is not None and cached["transcript"] is None


def test_cached_result_none_when_record_has_no_status(home):
    fid = "0123456789ab-100-1"
    base = footage_base(fid)
    base.mkdir(parents=True, exist_ok=True)
    (base / "scenes.json").write_text(json.dumps({"schema": 1, "fid": fid}), encoding="utf-8")
    assert footage.cached_result(fid) is None
