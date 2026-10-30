"""수집 계층(fetch·cuts·shots·stt·timings)이 쓰는 파일의 바이트와 부수 효과를 고정한다(S3 Task 3 특성 테스트).

저장소로 옮기기 전 코드에서 통과해야 하고, 옮긴 뒤에도 같은 바이트여야 한다.
`write_text` 기본 줄바꿈(Windows CRLF)을 기대값으로 직접 계산한다: 개행 -> `os.linesep`.
"""
import json
import os
import time
from pathlib import Path

import pytest

from scripts import cuts, fetch, shots, stt, timings
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set

VID = "dQw4w9WgXcQ"
SVID = "a00"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


def text_file_bytes(text: str) -> bytes:
    """`Path.write_text(text, encoding="utf-8")`가 쓰는 바이트(줄바꿈 번역 포함)."""
    return text.replace("\n", os.linesep).encode("utf-8")


# --- fetch ----------------------------------------------------------------------

RAW = {"id": VID, "url": f"https://youtu.be/{VID}", "format": "shorts",
       "meta": {"title": "한글 제목 🎬", "duration": 30},
       "transcript": {"source": "manual", "language": "ko", "segments": [{"start": 0, "dur": 1, "text": "안녕"}]}}


def test_save_raw_writes_indent2_json_with_platform_newlines(home: Path) -> None:
    fetch.save_raw(RAW)
    path = home / "workspace" / "raw" / f"{VID}.json"
    assert path.read_bytes() == text_file_bytes(json.dumps(RAW, ensure_ascii=False, indent=2))
    assert [p.name for p in path.parent.iterdir()] == [f"{VID}.json"]       # 임시 파일이 남지 않는다


def test_load_raw_roundtrip_and_missing(home: Path) -> None:
    fetch.save_raw(RAW)
    assert fetch.load_raw(VID) == RAW
    with pytest.raises(ScriptError) as exc:
        fetch.load_raw("nope0000000")
    assert exc.value.code == "raw_not_found"
    assert exc.value.message == "수집 데이터가 없습니다: nope0000000 (먼저 fetch)"


def test_fetch_cached_summary_shows_raw_key(home: Path) -> None:
    fetch.save_raw(RAW)
    out = fetch.fetch(f"https://youtu.be/{VID}")
    assert out["status"] == "cached"
    assert out["raw"] == f"workspace/raw/{VID}.json"
    assert out["segments"] == 1


def test_wait_turn_writes_last_fetch_stamp(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YT_FETCH_INTERVAL", "0")
    fetch.wait_turn()
    stamp = home / "workspace" / ".last_fetch"
    assert abs(float(stamp.read_text("utf-8")) - time.time()) < 5


def test_wait_turn_sleeps_for_remaining_interval(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YT_FETCH_INTERVAL", "10")
    stamp = home / "workspace" / ".last_fetch"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(str(time.time()), encoding="utf-8")
    slept: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", slept.append)
    fetch.wait_turn()
    assert len(slept) == 1 and 8 < slept[0] <= 10


def test_wait_turn_ignores_garbled_stamp(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YT_FETCH_INTERVAL", "10")
    stamp = home / "workspace" / ".last_fetch"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("not-a-number", encoding="utf-8")
    monkeypatch.setattr(fetch.time, "sleep", lambda s: pytest.fail("대기하면 안 된다"))
    fetch.wait_turn()
    assert float(stamp.read_text("utf-8")) > 0


# --- cuts -----------------------------------------------------------------------

SCENES = [
    {"start": 0.0, "end": 2.0, "camera": "static", "frame": "scene001_1.0s.jpg"},
    {"start": 2.0, "end": 5.0, "camera": "pan", "frame": "scene002_3.5s.jpg"},
    {"start": 5.0, "end": 6.0, "camera": "zoom", "frame": None},
]


@pytest.fixture
def fake_cuts(home: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {"order": []}

    def download(video_id, out_dir, proxy, progress=None):
        seen["order"].append("download")
        seen["frames_existed_at_download"] = (home / "workspace" / "frames" / video_id).exists()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{video_id}.mp4"
        path.write_bytes(b"video")
        return path

    def analyze(path, frames_dir, progress=None):
        seen["order"].append("analyze")
        frames_dir.mkdir(parents=True, exist_ok=True)
        for s in SCENES:
            if s["frame"]:
                (frames_dir / s["frame"]).write_bytes(b"jpg")
        return {"duration_sec": 6.0, "fps": 30.0, "scenes": [dict(s) for s in SCENES]}

    monkeypatch.setattr(cuts, "download_video", download)
    monkeypatch.setattr(cuts, "analyze_video", analyze)
    monkeypatch.setattr(cuts, "wait_turn", lambda report=None: seen["order"].append("wait"))
    return seen


def test_cut_stats_writes_cuts_json_bytes_and_frame_keys(home: Path, fake_cuts: dict) -> None:
    out = cuts.cut_stats(VID)
    base = f"workspace/frames/{VID}"
    expected = {
        "id": VID, "duration_sec": 6.0, "fps": 30.0, "scene_count": 3, "avg_cut_sec": 2.0,
        "cuts": [2.0, 5.0], "camera": cuts.summarize_camera(SCENES),
        "frames": [f"{base}/scene001_1.0s.jpg", f"{base}/scene002_3.5s.jpg"],
        "scenes": [{**s, "frame": f"{base}/{s['frame']}" if s["frame"] else None} for s in SCENES],
    }
    assert out == {**expected, "status": "analyzed"}
    path = home / "workspace" / "cuts" / f"{VID}.json"
    assert path.read_bytes() == text_file_bytes(json.dumps(expected, ensure_ascii=False, indent=2))
    assert sorted(p.name for p in (home / "workspace" / "frames" / VID).iterdir()) == [
        "scene001_1.0s.jpg", "scene002_3.5s.jpg"]
    assert fake_cuts["order"] == ["wait", "download", "analyze"]


def test_cut_stats_clears_old_frames_before_download(home: Path, fake_cuts: dict) -> None:
    old = home / "workspace" / "frames" / VID
    old.mkdir(parents=True)
    (old / "stale.jpg").write_bytes(b"old")
    cuts.cut_stats(VID, force=True)
    assert fake_cuts["frames_existed_at_download"] is False
    assert not (old / "stale.jpg").exists()


def test_cut_stats_cached_returns_stored_json(home: Path, fake_cuts: dict) -> None:
    cuts.cut_stats(VID)
    fake_cuts["order"].clear()
    out = cuts.cut_stats(VID)
    assert out["status"] == "cached" and out["scene_count"] == 3
    assert fake_cuts["order"] == []


def test_cut_stats_failed_download_leaves_no_frames_folder(home: Path, fake_cuts: dict,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    old = home / "workspace" / "frames" / VID
    old.mkdir(parents=True)
    (old / "stale.jpg").write_bytes(b"old")

    def boom(*a, **k):
        raise ScriptError("download_failed", "x")

    monkeypatch.setattr(cuts, "download_video", boom)
    with pytest.raises(ScriptError):
        cuts.cut_stats(VID, force=True)
    assert not old.exists()
    assert not (home / "workspace" / "cuts" / f"{VID}.json").exists()


def test_cut_stats_early_analysis_failure_leaves_no_frames_folder(home: Path, fake_cuts: dict,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path, frames_dir, progress=None):
        raise ScriptError("bad_video", "영상을 열 수 없습니다")     # detect_scenes 단계: 폴더를 만들기 전

    monkeypatch.setattr(cuts, "analyze_video", boom)
    with pytest.raises(ScriptError) as exc:
        cuts.cut_stats(VID)
    assert exc.value.code == "bad_video"
    assert not (home / "workspace" / "frames" / VID).exists()
    assert list((home / "workspace" / "tmp").iterdir()) == []


def test_cut_stats_late_analysis_failure_keeps_partial_frames(home: Path, fake_cuts: dict,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path, frames_dir, progress=None):
        frames_dir.mkdir(parents=True, exist_ok=True)
        (frames_dir / "scene001_1.0s.jpg").write_bytes(b"jpg")
        raise RuntimeError("decode error")

    monkeypatch.setattr(cuts, "analyze_video", boom)
    with pytest.raises(RuntimeError):
        cuts.cut_stats(VID)
    assert [p.name for p in (home / "workspace" / "frames" / VID).iterdir()] == ["scene001_1.0s.jpg"]


# --- shots ----------------------------------------------------------------------

def _label(frame: str) -> dict:
    return {"frame": frame, "size": "클로즈업", "angle": "정면", "face": False, "hands_only": True,
            "broll": False, "text_overlay": True}


@pytest.fixture
def shot_home(home: Path) -> Path:
    standard_set(home)
    scene_list = [{"start": i * 3.0, "end": (i + 1) * 3.0, "camera": "static",
                   "frame": f"workspace/frames/{SVID}/scene{i + 1:03d}.jpg"} for i in range(20)]
    path = home / "workspace" / "cuts" / f"{SVID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": SVID, "duration_sec": 60, "scenes": scene_list}), encoding="utf-8")
    return home


def test_shots_apply_writes_labels_json_indent1_with_platform_newlines(shot_home: Path) -> None:
    frames = [f["frame"] for f in shots.context(SVID, CAT)["frames"]]
    tmp = shot_home / "workspace" / "tmp" / "shots.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps([_label(f) for f in frames], ensure_ascii=False), encoding="utf-8")

    shots.apply(SVID, CAT, tmp)

    out = shot_home / "workspace" / "shots" / f"{SVID}.json"
    stored = json.loads(out.read_text("utf-8"))
    assert [lab["frame"] for lab in stored["labels"]] == frames
    assert out.read_bytes() == text_file_bytes(json.dumps(stored, ensure_ascii=False, indent=1))
    assert [p.name for p in out.parent.iterdir()] == [f"{SVID}.json"]      # 임시 파일이 남지 않는다
    assert not tmp.exists()                                                # 핸드오프 파일은 지운다


def test_shots_read_labels_tolerates_missing_and_broken_file(shot_home: Path) -> None:
    assert shots._read_labels(SVID) == []
    out = shot_home / "workspace" / "shots" / f"{SVID}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("{broken", encoding="utf-8")
    assert shots._read_labels(SVID) == []
    out.write_text(json.dumps({"id": SVID, "labels": [{"frame": "a.jpg"}, {"x": 1}, "s"]}), encoding="utf-8")
    assert shots._read_labels(SVID) == [{"frame": "a.jpg"}]


def test_shots_describe_context_skips_frames_whose_file_is_gone(shot_home: Path) -> None:
    for i in (0, 1, 2):
        f = shot_home / "workspace" / "frames" / SVID / f"scene{i + 1:03d}.jpg"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"jpg")
    out = shots.describe_context(SVID, CAT)
    assert [f["frame"] for f in out["frames"]] == [f"workspace/frames/{SVID}/scene{i:03d}.jpg" for i in (1, 2, 3)]
    assert out["remaining"] == 3


def test_shots_pending_lists_videos_without_shots(shot_home: Path) -> None:
    out = shots.pending(CAT)
    assert out["category"] == CAT and SVID in out["pending"]
    assert out["relabel"] == []


def test_shots_unknown_category_and_video_errors(shot_home: Path) -> None:
    with pytest.raises(ScriptError) as exc:
        shots.context(SVID, "nocat")
    assert exc.value.code == "category_not_found"
    with pytest.raises(ScriptError) as exc:
        shots.context("zzz", CAT)
    assert exc.value.code == "video_not_found"


def test_shots_cli_resolves_relative_labels_against_work_folder(
        shot_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(shots, "apply", lambda vid, cat, path: seen.append(path) or {"ok": True})
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    shots.main(["apply", SVID, "--category", CAT, "--labels", "workspace/tmp/shots.json"])
    assert seen == [shot_home / "workspace" / "tmp" / "shots.json"]


# --- stt ------------------------------------------------------------------------

def test_stt_save_raw_keeps_platform_newline_bytes(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch.save_raw({**RAW, "transcript": None})
    seen_dirs: list[Path] = []

    def download_audio(video_id, out_dir, proxy):
        seen_dirs.append(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{video_id}.m4a"
        path.write_bytes(b"audio")
        return path

    class Seg:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class Model:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            return iter([Seg(0.0, 1.0, "안녕")]), type("Info", (), {"language": "ko", "duration": 1.0})()

    monkeypatch.setattr(stt, "download_audio", download_audio)
    monkeypatch.setattr(stt, "decode_audio", lambda p: "pcm")
    monkeypatch.setattr(stt, "_load_whisper", lambda: Model)
    stt.transcribe(VID)

    seg = {"start": 0.0, "dur": 1.0, "text": "안녕"}
    data = {**RAW, "transcript": {"source": "stt", "language": "ko", "segments": [seg]}}
    assert (home / "workspace" / "raw" / f"{VID}.json").read_bytes() == text_file_bytes(
        json.dumps(data, ensure_ascii=False, indent=2))
    assert seen_dirs == [home / "workspace" / "tmp"]
    assert list((home / "workspace" / "tmp").iterdir()) == []


# --- timings --------------------------------------------------------------------

def test_timings_record_writes_compact_json_without_leftovers(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timings.time, "time", lambda: 1000.5)
    timings.record("cuts", elapsed_sec=2.5, video_sec=60)
    path = home / "workspace" / "timings.json"
    entry = {"elapsed_sec": 2.5, "at": 1000.5, "video_sec": 60.0}
    if "cuts" in timings.METHOD:
        entry = {"elapsed_sec": 2.5, "at": 1000.5, "method": timings.METHOD["cuts"], "video_sec": 60.0}
    assert path.read_bytes() == text_file_bytes(json.dumps({"cuts": [entry]}, ensure_ascii=False))
    assert [p.name for p in path.parent.iterdir()] == ["timings.json"]


def test_timings_record_unwritable_target_is_ignored(home: Path) -> None:
    (home / "workspace").mkdir()
    (home / "workspace" / "timings.json").mkdir()          # 파일 자리에 폴더: 교체가 실패한다
    timings.record("cuts", elapsed_sec=1.0, video_sec=10)  # 예외 없이 끝난다
    assert [p.name for p in (home / "workspace").iterdir()] == ["timings.json"]
