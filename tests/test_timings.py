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


def test_default_cuts_matches_real_measurements():
    # 2026-10-25 이 PC, 순차 읽기: 1208초 영상 -> 137초, 1113초 영상 -> 107초. 둘 다 상대 오차 15% 이내.
    assert timings.DEFAULTS["cuts"] == {"per_video_sec": 0.10, "fixed_sec": 7.0}
    for video_sec, measured in ((1208, 137), (1113, 107)):
        got = timings.expected("cuts", video_sec=video_sec)
        assert abs(got - measured) / measured <= 0.15, (video_sec, got)


def test_expected_median_after_three_records():
    for elapsed in (120, 60, 90):
        timings.record("cuts", elapsed_sec=elapsed, video_sec=600)
    assert timings.expected("cuts", video_sec=1200) == pytest.approx(180)


def test_expected_blends_defaults_under_three_records():
    timings.record("cuts", elapsed_sec=100, video_sec=600)
    got = timings.expected("cuts", video_sec=600)
    assert got == pytest.approx((default_cuts(600) + 100) / 2)


def test_cuts_records_carry_method_and_old_method_rows_are_ignored():
    # 카메라 판정 방식이 바뀌기 전(method 없음 = 1) 기록은 느린 방식의 속도라 예상에 쓰지 않는다
    path = workspace_dir("timings.json")
    path.parent.mkdir(parents=True)
    old = [{"elapsed_sec": 600, "video_sec": 600}] * 5
    path.write_text(json.dumps({"cuts": old}), encoding="utf-8")
    assert timings.expected("cuts", video_sec=600) == pytest.approx(default_cuts(600))

    for elapsed in (120, 60, 90):
        timings.record("cuts", elapsed_sec=elapsed, video_sec=600)
    stored = json.loads(path.read_text(encoding="utf-8"))["cuts"]
    assert [r.get("method") for r in stored] == [None] * 5 + [2] * 3
    assert timings.expected("cuts", video_sec=1200) == pytest.approx(180)


@pytest.mark.parametrize("method", ["2", True, None, 3])
def test_cuts_rows_with_other_method_values_are_ignored(method):
    path = workspace_dir("timings.json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cuts": [{"elapsed_sec": 600, "video_sec": 600, "method": method}] * 3}),
                    encoding="utf-8")
    assert timings.expected("cuts", video_sec=600) == pytest.approx(default_cuts(600))


def test_steps_without_method_keep_old_rows():
    path = workspace_dir("timings.json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"stt": [{"elapsed_sec": 60, "video_sec": 600}] * 3}), encoding="utf-8")
    assert timings.expected("stt", video_sec=600) == pytest.approx(60)
    timings.record("stt", elapsed_sec=60, video_sec=600)
    assert "method" not in json.loads(path.read_text(encoding="utf-8"))["stt"][-1]


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


BAD = [float("nan"), float("inf"), float("-inf"), -5.0, 0, 0.0]


def _stored():
    path = workspace_dir("timings.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.mark.parametrize("bad", BAD)
def test_record_skips_bad_elapsed(bad):
    timings.record("cuts", elapsed_sec=bad, video_sec=600)
    assert _stored().get("cuts", []) == []


@pytest.mark.parametrize("bad", BAD)
def test_record_drops_bad_video_sec(bad):
    timings.record("cuts", elapsed_sec=10, video_sec=bad)
    entry = _stored()["cuts"][0]
    assert "video_sec" not in entry and entry["elapsed_sec"] == 10


@pytest.mark.parametrize("bad", [None, "12", "abc", [1], True])
def test_record_never_raises_on_wrong_types(bad):
    timings.record("cuts", elapsed_sec=bad, video_sec=bad)
    timings.record("analyze", elapsed_sec=5, video_sec=bad, frames="x", text_chars=bad)


@pytest.mark.parametrize("bad", BAD)
def test_expected_skips_bad_samples_in_existing_file(bad):
    path = workspace_dir("timings.json")
    path.parent.mkdir(parents=True)
    rows = [{"elapsed_sec": 120, "video_sec": 600}, {"elapsed_sec": 60, "video_sec": 600},
            {"elapsed_sec": 90, "video_sec": 600}, {"elapsed_sec": bad, "video_sec": 600},
            {"elapsed_sec": 50, "video_sec": bad}]
    rows = [{**r, "method": timings.METHOD["cuts"]} for r in rows]
    path.write_text(json.dumps({"cuts": rows}), encoding="utf-8")  # json.dumps가 NaN/Infinity를 그대로 쓴다
    assert timings.expected("cuts", video_sec=1200) == pytest.approx(180)


@pytest.mark.parametrize("bad", BAD + [None])
def test_expected_proportional_needs_valid_length(bad):
    assert timings.expected("cuts", video_sec=bad) is None
    assert timings.expected("stt", video_sec=bad) is None


def test_expected_never_below_floor(monkeypatch):
    path = workspace_dir("timings.json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"analyze": [{"elapsed_sec": 1e-9}] * 3,
                                "cuts": [{"elapsed_sec": 1e-12, "video_sec": 1e6, "method": 2}] * 3}), encoding="utf-8")
    assert timings.expected("analyze", video_sec=None) == timings.MIN_EXPECTED
    assert timings.expected("cuts", video_sec=1) == timings.MIN_EXPECTED


def test_tmp_file_removed_when_replace_fails(monkeypatch):
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    timings.record("cuts", elapsed_sec=10, video_sec=600)
    assert list(workspace_dir().glob("*.tmp")) == []
