import json
import shutil

import cv2
import numpy as np
import pytest

from scripts import cuts
from scripts.common import ScriptError

VID = "dQw4w9WgXcQ"
FPS = 30
W, H = 320, 240


def textured(seed, tint, size=(H * 3, W * 3)):
    """크게 번진 노이즈 텍스처: 이동해도 장면 전환으로 오인되지 않을 만큼 부드럽다."""
    rng = np.random.default_rng(seed)
    noise = rng.random(size, dtype=np.float32)
    smooth = cv2.GaussianBlur(noise, (0, 0), 12)
    smooth = (smooth - smooth.min()) / (smooth.max() - smooth.min())
    img = np.stack([smooth * t for t in tint], axis=-1)
    return (img * 255).astype(np.uint8)


def make_video(path, seconds_per_scene=2):
    """장면 3개: 고정(가운데 물체만 움직임) → 오른쪽 패닝 → 줌 인. 장면마다 색조가 다르다."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    n = FPS * seconds_per_scene
    cy, cx = H * 3 // 2, W * 3 // 2

    base = textured(1, (1.0, 0.3, 0.3))
    for i in range(n):
        frame = base[cy - H // 2: cy + H // 2, cx - W // 2: cx + W // 2].copy()
        cv2.circle(frame, (60 + i, 120), 12, (255, 255, 255), -1)
        writer.write(frame)

    base = textured(2, (0.3, 1.0, 0.3))
    for i in range(n):
        x = cx - W // 2 - 150 + i * 3
        writer.write(base[cy - H // 2: cy + H // 2, x: x + W].copy())

    base = textured(3, (0.3, 0.3, 1.0))
    for i in range(n):
        scale = 1.6 - i * 0.6 / n            # 보이는 영역이 줄어든다 = 줌 인
        hw, hh = int(W * scale / 2), int(H * scale / 2)
        crop = base[cy - hh: cy + hh, cx - hw: cx + hw]
        writer.write(cv2.resize(crop, (W, H)))
    writer.release()
    return path


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("video") / "sample.mp4")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    monkeypatch.setenv("YT_FETCH_INTERVAL", "0")
    return tmp_path


@pytest.fixture
def fake_download(monkeypatch, sample_video):
    calls = []

    def download_video(video_id, out_dir, proxy):
        calls.append(video_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{video_id}.mp4"
        shutil.copy(sample_video, dest)
        return dest

    monkeypatch.setattr(cuts, "download_video", download_video)
    return calls


# --- 포맷 선택 ----------------------------------------------------------------

def fmt(fid, w, h, vcodec="avc1.4D401E", protocol="https", ext="mp4"):
    return {"format_id": fid, "width": w, "height": h, "vcodec": vcodec,
            "acodec": "none", "protocol": protocol, "ext": ext}


def test_select_format_prefers_largest_within_360_h264_https():
    formats = [
        fmt("a", 144, 256), fmt("b", 360, 640), fmt("c", 480, 854),
        fmt("d", 360, 640, vcodec="vp9", ext="webm"),
        fmt("e", 360, 640, protocol="m3u8_native"),
        {"format_id": "audio", "vcodec": "none", "acodec": "mp4a", "protocol": "https"},
    ]
    assert cuts.select_format(formats)["format_id"] == "b"


def test_select_format_horizontal():
    formats = [fmt("a", 426, 240), fmt("b", 640, 360), fmt("c", 854, 480)]
    assert cuts.select_format(formats)["format_id"] == "b"


def test_select_format_falls_back_to_other_codec():
    formats = [fmt("a", 360, 640, vcodec="vp9", ext="webm"), fmt("b", 720, 1280)]
    assert cuts.select_format(formats)["format_id"] == "a"


def test_select_format_never_above_360():
    with pytest.raises(ScriptError) as exc:
        cuts.select_format([fmt("a", 720, 1280), fmt("b", 1080, 1920)])
    assert exc.value.code == "no_low_res_format"


# --- 장면 분석 ----------------------------------------------------------------

def test_analyze_detects_three_scenes_and_camera(sample_video, tmp_path):
    result = cuts.analyze_video(sample_video, tmp_path / "frames")
    scenes = result["scenes"]
    assert len(scenes) == 3
    for scene, start in zip(scenes, [0.0, 2.0, 4.0]):
        assert scene["start"] == pytest.approx(start, abs=0.15)
    assert [s["camera"] for s in scenes] == ["static", "pan", "zoom"]
    assert result["duration_sec"] == pytest.approx(6.0, abs=0.1)
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == 3


def test_frames_capped(sample_video, tmp_path, monkeypatch):
    monkeypatch.setattr(cuts, "MAX_FRAMES", 2)
    result = cuts.analyze_video(sample_video, tmp_path / "frames")
    assert len(result["scenes"]) == 3
    assert len([s for s in result["scenes"] if s["frame"]]) == 2
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == 2


@pytest.mark.parametrize("n, cap, expected", [
    (3, 40, [0, 1, 2]), (5, 2, [0, 4]), (10, 4, [0, 3, 6, 9]), (0, 40, []),
])
def test_pick_frame_scenes(n, cap, expected):
    assert cuts.pick_frame_scenes(n, cap) == expected


def test_summarize_camera_is_duration_weighted():
    scenes = [
        {"start": 0, "end": 3, "camera": "static"},
        {"start": 3, "end": 4, "camera": "pan"},
    ]
    assert cuts.summarize_camera(scenes) == {"static": 0.75, "pan": 0.25, "zoom": 0.0}


# --- 전체 흐름 ----------------------------------------------------------------

def test_cut_stats_writes_json_frames_and_deletes_video(home, fake_download):
    out = cuts.cut_stats(VID)

    assert out["status"] == "analyzed"
    assert out["scene_count"] == 3
    assert out["avg_cut_sec"] == pytest.approx(2.0, abs=0.1)
    assert set(out["camera"]) == {"static", "pan", "zoom"}
    assert len(out["frames"]) == 3
    assert all(f.startswith(f"workspace/frames/{VID}/") for f in out["frames"])

    saved = json.loads((home / "workspace" / "cuts" / f"{VID}.json").read_text("utf-8"))
    assert saved["scenes"] == out["scenes"]
    assert list((home / "workspace" / "tmp").glob("*")) == []


def test_video_deleted_even_when_analysis_fails(home, fake_download, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("decode error")

    monkeypatch.setattr(cuts, "analyze_video", boom)
    with pytest.raises(RuntimeError):
        cuts.cut_stats(VID)
    assert list((home / "workspace" / "tmp").glob("*")) == []


def test_cached_result_skips_download(home, fake_download):
    cuts.cut_stats(VID)
    out = cuts.cut_stats(VID)
    assert out["status"] == "cached"
    assert fake_download == [VID]


def test_force_reanalyzes_and_replaces_frames(home, fake_download):
    cuts.cut_stats(VID)
    stale = home / "workspace" / "frames" / VID / "stale.jpg"
    stale.write_bytes(b"x")
    assert cuts.cut_stats(VID, force=True)["status"] == "analyzed"
    assert not stale.exists()
    assert fake_download == [VID, VID]


def test_main_parses_url(home, fake_download):
    out = cuts.main([f"https://youtube.com/shorts/{VID}"])
    assert out["id"] == VID


# --- 컷 찾기(가벼운 버전) --------------------------------------------------------

def test_detect_cuts_finds_inner_cuts_only(sample_video):
    result = cuts.detect_cuts(sample_video)
    assert result["duration_sec"] == pytest.approx(6.0, abs=0.1)
    assert result["fps"] == pytest.approx(30.0, abs=0.1)
    assert len(result["cuts"]) == 2
    for found, expected in zip(result["cuts"], [2.0, 4.0]):
        assert found == pytest.approx(expected, abs=0.15)
    assert result["cuts"] == sorted(result["cuts"])
    assert 0 < result["cuts"][0] and result["cuts"][-1] < result["duration_sec"]


def test_detect_cuts_leaves_video_untouched(sample_video):
    before = (sample_video.read_bytes(), sample_video.stat().st_mtime_ns)
    cuts.detect_cuts(sample_video)
    assert (sample_video.read_bytes(), sample_video.stat().st_mtime_ns) == before


def test_detect_cuts_rejects_unreadable_file(tmp_path):
    bad = tmp_path / "not-a-video.mp4"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(ScriptError) as exc:
        cuts.detect_cuts(bad)
    assert exc.value.code == "bad_video"
    with pytest.raises(ScriptError) as exc:
        cuts.detect_cuts(tmp_path / "missing.mp4")
    assert exc.value.code == "bad_video"


class _NoFrameCount:
    """CAP_PROP_FRAME_COUNT를 0으로 돌려주는 VideoCapture 래퍼(컨테이너에 프레임 수가 없는 영상 흉내)."""

    def __init__(self, real):
        self._real = real

    def get(self, prop):
        return 0 if prop == cv2.CAP_PROP_FRAME_COUNT else self._real.get(prop)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_detect_cuts_falls_back_when_frame_count_is_missing(sample_video, monkeypatch):
    real = cv2.VideoCapture
    monkeypatch.setattr(cuts.cv2, "VideoCapture", lambda *a, **k: _NoFrameCount(real(*a, **k)))
    result = cuts.detect_cuts(sample_video)
    assert result["duration_sec"] == pytest.approx(6.0, abs=0.1) and len(result["cuts"]) == 2


def test_detect_cuts_with_korean_file_name(sample_video, tmp_path):
    korean = tmp_path / "편집본.mp4"
    shutil.copy(sample_video, korean)
    result = cuts.detect_cuts(korean)
    assert len(result["cuts"]) == 2 and result["duration_sec"] == pytest.approx(6.0, abs=0.1)
