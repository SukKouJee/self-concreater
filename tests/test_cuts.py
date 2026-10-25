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

    def download_video(video_id, out_dir, proxy, progress=None):
        calls.append(video_id)
        if progress is not None:
            progress("download", 1.0, "영상 받는 중 100%")
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


def test_flow_pairs_spread_over_scene():
    # 30fps -> 간격 3프레임, 첫 지점 start+2, 마지막 지점 end-2-3
    assert cuts.flow_pairs(0, 60, 30) == [(2, 5), (20, 23), (37, 40), (55, 58)]
    assert cuts.flow_pairs(100, 104, 30) == []          # 너무 짧은 장면


def _seek_gray(path, index):
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    cap.release()
    assert ok
    return cuts._gray(frame), frame


def test_read_scene_frames_returns_the_same_frames_as_seeking(sample_video):
    plans = [([(2, 5), (20, 23)], 30), ([(70, 73)], None), ([], 150)]
    cap = cv2.VideoCapture(str(sample_video))
    try:
        got = list(cuts.read_scene_frames(cap, plans))
    finally:
        cap.release()

    assert [sorted(grays) for grays, _ in got] == [[2, 5, 20, 23], [70, 73], []]
    for grays, _ in got:
        for index, gray in grays.items():
            assert np.array_equal(gray, _seek_gray(sample_video, index)[0])
    assert np.array_equal(got[0][1], _seek_gray(sample_video, 30)[1])
    assert got[1][1] is None
    assert np.array_equal(got[2][1], _seek_gray(sample_video, 150)[1])


def test_read_scene_frames_past_the_end_leaves_frames_out(sample_video):
    # 컨테이너 프레임 수가 실제보다 클 때: 없는 프레임은 빠지고 장면마다 하나씩은 꼭 낸다
    plans = [([(170, 173)], 175), ([(500, 503)], 600)]
    cap = cv2.VideoCapture(str(sample_video))
    try:
        got = list(cuts.read_scene_frames(cap, plans))
    finally:
        cap.release()
    assert [sorted(grays) for grays, _ in got] == [[170, 173], []]
    assert got[0][1] is not None and got[1][1] is None


class FlakyGrab:
    """grab()이 정해진 호출에서 한 번만 False를 내는 VideoCapture (깨진 패킷 하나)."""

    def __init__(self, path, fail_at):
        self._cap = cv2.VideoCapture(str(path))
        self._calls = 0
        self._fail_at = fail_at

    def grab(self):
        self._calls += 1
        if self._calls == self._fail_at:
            return False
        return self._cap.grab()

    def __getattr__(self, name):
        return getattr(self._cap, name)


def test_read_scene_frames_keeps_going_after_one_failed_grab(sample_video):
    # 장면 감지(scenedetect)처럼 다시 시도해서, 깨진 프레임 하나 뒤 장면도 읽는다
    cap = FlakyGrab(sample_video, fail_at=10)
    try:
        got = list(cuts.read_scene_frames(cap, [([(2, 5)], None), ([(70, 73)], 150)]))
    finally:
        cap.release()
    assert [sorted(grays) for grays, _ in got] == [[2, 5], [70, 73]]
    assert got[1][1] is not None


def test_read_scene_frames_skips_negative_targets(sample_video):
    # 프레임 수를 모르는 영상의 대체 구간 [(0, -1)]: 대표 프레임 번호가 -1이 되어도 읽기 전 retrieve를 하지 않는다
    cap = cv2.VideoCapture(str(sample_video))
    try:
        got = list(cuts.read_scene_frames(cap, [([], -1)]))
    finally:
        cap.release()
    assert got == [({}, None)]


def test_analyze_video_reads_frames_in_one_pass_without_seeking(sample_video, tmp_path, monkeypatch):
    detected = cuts.detect_scenes(sample_video)   # 장면 감지(scenedetect) 내부 seek은 세지 않는다
    monkeypatch.setattr(cuts, "detect_scenes", lambda path, progress=None: detected)
    seeks = []
    real = cv2.VideoCapture

    class Spy:
        def __init__(self, *a):
            self._cap = real(*a)

        def set(self, prop, value):
            if prop == cv2.CAP_PROP_POS_FRAMES:
                seeks.append(value)
            return self._cap.set(prop, value)

        def __getattr__(self, name):
            return getattr(self._cap, name)

    monkeypatch.setattr(cuts.cv2, "VideoCapture", Spy)
    result = cuts.analyze_video(sample_video, tmp_path / "frames")
    assert seeks == []
    assert [s["camera"] for s in result["scenes"]] == ["static", "pan", "zoom"]
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == 3


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


# --- 진행 콜백 ------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, phase, fraction, text):
        self.calls.append((phase, fraction, text))

    def of(self, phase):
        return [c for c in self.calls if c[0] == phase]


def test_download_progress_hook():
    rec = Recorder()
    hook = cuts.download_progress_hook(rec)
    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
    assert rec.calls == [("download", 0.5, "영상 받는 중 50%")]


def test_download_progress_uses_estimate_and_finishes_at_one():
    rec = Recorder()
    hook = cuts.download_progress_hook(rec)
    hook({"status": "downloading", "downloaded_bytes": 25, "total_bytes": None, "total_bytes_estimate": 100})
    hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
    assert [c[1] for c in rec.calls] == [0.25, 1.0]
    assert rec.calls[-1][2] == "영상 받는 중 100%"


def test_download_progress_never_goes_backward():
    rec = Recorder()
    hook = cuts.download_progress_hook(rec)
    hook({"status": "downloading", "downloaded_bytes": 60, "total_bytes_estimate": 100})
    hook({"status": "downloading", "downloaded_bytes": 61, "total_bytes_estimate": 200})   # 추정치가 커짐
    assert [c[1] for c in rec.calls] == [0.6, 0.6]
    assert rec.calls[-1][2] == "영상 받는 중 60%"


def test_download_progress_without_total():
    rec = Recorder()
    hook = cuts.download_progress_hook(rec)
    hook({"status": "downloading", "downloaded_bytes": 3_500_000})
    hook({"status": "downloading"})
    assert [c[:2] for c in rec.calls] == [("download", None), ("download", None)]
    assert all(c[2].startswith("영상 받는 중") for c in rec.calls)


def test_download_progress_hook_survives_raising_callback(capsys):
    def boom(*a):
        raise RuntimeError("ui gone")

    hook = cuts.download_progress_hook(boom)
    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})   # 예외가 새지 않는다
    assert "ui gone" in capsys.readouterr().err


def _fake_ydl(seen, tmp_path, emit):
    class FakeYDL:
        def __init__(self, opts):
            seen["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            f = {"format_id": "b", "width": 640, "height": 360, "vcodec": "avc1", "protocol": "https", "ext": "mp4"}
            list(seen["opts"]["format"]({"formats": [f]}))
            if emit:
                for hook in seen["opts"]["progress_hooks"]:
                    hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 4})
            (tmp_path / f"{VID}.mp4").write_bytes(b"v")
            return {}

    return FakeYDL


def test_download_video_registers_progress_hook(monkeypatch, tmp_path):
    yt_dlp = pytest.importorskip("yt_dlp")
    seen = {}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _fake_ydl(seen, tmp_path, emit=True))
    rec = Recorder()
    cuts.download_video(VID, tmp_path, None, progress=rec)
    assert rec.calls == [("download", 0.25, "영상 받는 중 25%")]


def test_download_video_without_progress_has_no_hook(monkeypatch, tmp_path):
    yt_dlp = pytest.importorskip("yt_dlp")
    seen = {}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _fake_ydl(seen, tmp_path, emit=False))
    cuts.download_video(VID, tmp_path, None)
    assert "progress_hooks" not in seen["opts"]


def test_analyze_video_reports_scenes_and_camera(sample_video, tmp_path):
    rec = Recorder()
    with_progress = cuts.analyze_video(sample_video, tmp_path / "a", progress=rec)
    without = cuts.analyze_video(sample_video, tmp_path / "b")
    assert with_progress == without

    scenes = [c[1] for c in rec.of("scenes") if c[1] is not None]
    assert len(scenes) >= 3
    assert scenes == sorted(scenes)
    assert scenes[0] < 0.5 and scenes[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in scenes)
    assert rec.of("scenes")[-1][2] == "장면 확인 중 (180 / 180 프레임)"

    camera = rec.of("camera")
    assert [c[1] for c in camera] == pytest.approx([1 / 3, 2 / 3, 1.0])
    assert camera[-1][2] == "카메라 움직임 확인 중 (3 / 3 장면)"
    phases = [c[0] for c in rec.calls]
    assert phases.index("camera") > max(i for i, p in enumerate(phases) if p == "scenes")


def test_scene_progress_text_uses_thousands_separator():
    assert cuts.scene_progress_text(38_800, 62_600) == "장면 확인 중 (38,800 / 62,600 프레임)"
    assert cuts.camera_progress_text(300, 1_688) == "카메라 움직임 확인 중 (300 / 1,688 장면)"


def test_analyze_video_survives_raising_callback(sample_video, tmp_path, capsys):
    def boom(*a):
        raise RuntimeError("ui gone")

    result = cuts.analyze_video(sample_video, tmp_path / "frames", progress=boom)
    assert len(result["scenes"]) == 3
    assert "ui gone" in capsys.readouterr().err


def test_cut_stats_reports_phases_in_order(home, fake_download):
    rec = Recorder()
    cuts.cut_stats(VID, progress=rec)
    order = []
    for phase, _, _ in rec.calls:
        if not order or order[-1] != phase:
            order.append(phase)
    assert order == ["download", "scenes", "camera"]       # 간격 0이면 기다림 보고는 없다


def test_cut_stats_reports_wait(home, fake_download, monkeypatch):
    from scripts import fetch

    monkeypatch.setenv("YT_FETCH_INTERVAL", "3")
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    (home / "workspace").mkdir(exist_ok=True)
    (home / "workspace" / ".last_fetch").write_text(str(fetch.time.time()), encoding="utf-8")
    rec = Recorder()
    cuts.cut_stats(VID, progress=rec)
    assert rec.calls[0][0] == "wait"
    assert [c[0] for c in rec.calls if c[0] != "wait"][0] == "download"


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


# --- 장면 감지기 진행(버전 호환·전체 프레임 수 오차) -------------------------------

def _frame():
    return np.zeros((H, W, 3), dtype=np.uint8)


@pytest.fixture
def plain_super(monkeypatch):
    """설치된 0.7의 ContentDetector는 int를 못 받으므로, 0.6처럼 int를 받는 부모로 바꾼다."""
    from scenedetect import ContentDetector

    monkeypatch.setattr(ContentDetector, "process_frame", lambda self, timecode, frame: [])


def test_reporting_detector_accepts_int_frame_numbers(plain_super):
    # scenedetect 0.6.x는 process_frame에 FrameTimecode가 아니라 int를 넘긴다
    rec = Recorder()
    detector = cuts.reporting_detector(rec, total=4)
    for n in range(4):
        detector.process_frame(n, _frame())
    assert [c[1] for c in rec.of("scenes")][:3] == [0.25, 0.5, 0.75]


def test_reporting_detector_accepts_frame_timecodes():
    from scenedetect import FrameTimecode

    rec = Recorder()
    detector = cuts.reporting_detector(rec, total=4)
    for n in range(3):
        detector.process_frame(FrameTimecode(n, float(FPS)), _frame())
    assert [c[1] for c in rec.of("scenes")] == [0.25, 0.5, 0.75]
    assert rec.of("scenes")[-1][2] == "장면 확인 중 (3 / 4 프레임)"


def test_reporting_detector_does_not_pin_100_when_total_is_short(plain_super):
    # 컨테이너 프레임 수(3)가 실제보다 작으면 1.0에 멈추지 않고 측정 불가로 실제 읽은 수를 보인다
    rec = Recorder()
    detector = cuts.reporting_detector(rec, total=3)
    for n in range(5):
        detector.process_frame(n, _frame())
    calls = rec.of("scenes")
    assert [c[1] for c in calls] == [1 / 3, 2 / 3, None, None, None]
    assert calls[-1][2] == "장면 확인 중 (5 프레임)"


def test_cut_stats_reports_download_start_right_after_wait(home, fake_download, monkeypatch):
    """yt-dlp의 첫 progress hook 전(extract_info) 구간이 차단 방지 대기로 남지 않게 한다."""
    from scripts import fetch

    monkeypatch.setenv("YT_FETCH_INTERVAL", "3")
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    (home / "workspace").mkdir(exist_ok=True)
    (home / "workspace" / ".last_fetch").write_text(str(fetch.time.time()), encoding="utf-8")
    rec = Recorder()
    cuts.cut_stats(VID, progress=rec)
    last_wait = max(i for i, c in enumerate(rec.calls) if c[0] == "wait")
    assert rec.calls[last_wait + 1] == ("download", None, cuts.DOWNLOAD_START_TEXT)


def test_cut_stats_reports_download_start_without_wait(home, fake_download):
    rec = Recorder()
    cuts.cut_stats(VID, progress=rec)
    assert rec.calls[0] == ("download", None, cuts.DOWNLOAD_START_TEXT)
