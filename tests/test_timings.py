"""단계별 실측 기록과 예상 시간: 기본값, 중앙값, 엔진 분리, 손상·쓰기 실패 무시를 확인한다."""
import json
import os

import pytest

from scripts import timings
from scripts.common import workspace_dir


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


def default_cuts(video_sec):
    d = timings.DEFAULTS["cuts"]
    return d["per_video_sec"] * video_sec + d["fixed_sec"]


def test_expected_uses_defaults_without_records():
    assert timings.expected("cuts", video_sec=600) == pytest.approx(default_cuts(600))
    assert timings.expected("cuts", video_sec=None) is None
    assert timings.expected("analyze", video_sec=None) == timings.DEFAULTS["analyze"]["fixed_sec"]


def test_default_cuts_matches_real_measurement():
    assert 440 <= timings.expected("cuts", video_sec=2611) <= 540


def test_expected_median_after_three_records():
    for elapsed in (120, 60, 90):
        timings.record("cuts", elapsed_sec=elapsed, video_sec=600)
    assert timings.expected("cuts", video_sec=1200) == pytest.approx(180)


def test_expected_blends_defaults_under_three_records():
    timings.record("cuts", elapsed_sec=100, video_sec=600)
    got = timings.expected("cuts", video_sec=600)
    assert got == pytest.approx((default_cuts(600) + 100) / 2)


def test_fixed_step_ignores_length():
    for elapsed in (30, 40, 50):
        timings.record("analyze", elapsed_sec=elapsed, video_sec=100, engine="claude-cli")
    assert timings.expected("analyze", video_sec=9999, engine="claude-cli") == 40


def test_engine_keys_are_separate():
    for elapsed in (500, 600, 700):
        timings.record("analyze", elapsed_sec=elapsed, engine="anthropic-api")
    for elapsed in (30, 40, 50):
        timings.record("analyze", elapsed_sec=elapsed, engine="claude-cli")
    assert timings.expected("analyze", video_sec=None, engine="claude-cli") == 40
    assert timings.expected("analyze", video_sec=None, engine="anthropic-api") == 600


def test_keeps_last_20():
    for i in range(25):
        timings.record("cuts", elapsed_sec=float(i), video_sec=100)
    data = json.loads(workspace_dir("timings.json").read_text(encoding="utf-8"))
    assert len(data["cuts"]) == timings.KEEP == 20
    assert data["cuts"][-1]["elapsed_sec"] == 24.0


def test_extra_fields_are_stored():
    timings.record("shots", elapsed_sec=5, engine="claude-cli", frames=12, text_chars=3000)
    entry = json.loads(workspace_dir("timings.json").read_text(encoding="utf-8"))["shots:claude-cli"][0]
    assert entry["frames"] == 12 and entry["text_chars"] == 3000 and "at" in entry


def test_corrupt_file_falls_back():
    path = workspace_dir("timings.json")
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert timings.expected("cuts", video_sec=600) == pytest.approx(default_cuts(600))
    timings.record("cuts", elapsed_sec=10, video_sec=600)
    assert timings.expected("cuts", video_sec=600) is not None


def test_write_failure_is_silent(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    timings.record("cuts", elapsed_sec=10, video_sec=600)


def test_unknown_step_rejected():
    with pytest.raises(ValueError):
        timings.record("x", elapsed_sec=1)
    with pytest.raises(ValueError):
        timings.expected("x", video_sec=1)
