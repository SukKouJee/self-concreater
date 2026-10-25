"""progress-utils.js 순수 함수 검증 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "progress-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


def test_fmt_dur():
    assert run_js("[38, 490, 3723, -5, NaN, null, 59.6].map(m.fmtDur)") == ["0:38", "8:10", "1:02:03", "0:00", "0:00", "0:00", "1:00"]


def test_fmt_clock_uses_offset():
    # 1970-01-01T00:00:00Z + 9h (KST) -> 09:00; epoch 3600*10+60*16 UTC with offset 0 -> 10:16
    assert run_js("m.fmtClock(3600*10+60*16, 0)") == "10:16"
    assert run_js("m.fmtClock(0, 540)") == "09:00"
    assert run_js("m.fmtClock(0, -60)") == "23:00"


def test_row_view_measured():
    r = run_js('m.rowView({state: "running", started_at: 1000, expected: 490, fraction: 0.62}, 1302)')
    assert r["elapsed"] == 302 and r["over"] is False and r["fraction"] == 0.62
    assert abs(r["remaining"] - 302 * 0.38 / 0.62) < 0.01


def test_row_view_unmeasured_over():
    r = run_js('m.rowView({state: "running", started_at: 0, expected: 150, fraction: null}, 220)')
    assert r["over"] is True and r["remaining"] == 0 and r["fraction"] == 1


def test_row_view_unmeasured_within():
    r = run_js('m.rowView({state: "running", started_at: 0, expected: 200, fraction: null}, 50)')
    assert r["fraction"] == 0.25 and r["remaining"] == 150 and r["over"] is False


def test_row_view_early_fraction_falls_back_to_expected():
    r = run_js('m.rowView({state: "running", started_at: 0, expected: 100, fraction: 0.05}, 10)')
    assert r["remaining"] == 90


def test_row_view_no_expected():
    r = run_js('m.rowView({state: "running", started_at: 0, expected: null, fraction: null}, 10)')
    assert r["remaining"] is None and r["over"] is False and r["fraction"] is None


def test_elapsed_uses_server_offset():
    r = run_js('m.rowView({state: "running", started_at: 500, expected: 100, fraction: null}, 400)')
    assert r["elapsed"] == 0


def test_paused_freezes_elapsed():
    r = run_js('m.rowView({state: "running", started_at: 100, paused_at: 130, expected: 100, fraction: null}, 900)')
    assert r["elapsed"] == 30


def test_done_row_uses_elapsed_field():
    r = run_js('m.rowView({state: "done", elapsed: 38, expected: 40}, 9999)')
    assert r["elapsed"] == 38 and r["remaining"] == 0


def test_item_remaining_sums_pending():
    timing = ('{steps: {fetch: {state: "done", elapsed: 5, expected: 5},'
              ' cuts: {state: "running", started_at: 0, expected: 490, fraction: 0.62},'
              ' analyze: {state: "pending", expected: 150}}}')
    r = run_js(f"m.itemRemaining({timing}, 302)")
    assert abs(r - (302 * 0.38 / 0.62 + 150)) < 0.01


def test_item_remaining_null_when_pending_unknown():
    timing = '{steps: {cuts: {state: "running", started_at: 0, expected: 10, fraction: 0.5}, analyze: {state: "pending", expected: null}}}'
    assert run_js(f"m.itemRemaining({timing}, 5)") is None


def test_item_remaining_skipped_and_empty():
    assert run_js('m.itemRemaining({steps: {a: {state: "skipped", expected: 0}}}, 1)') == 0
    assert run_js("m.itemRemaining(null, 1)") is None


def test_fmt_remain():
    assert run_js("[20, 180, 330, 355, 3900, -3].map(s => m.fmtRemain(s, false))") == [
        "약 20초", "약 3분", "약 6분", "약 6분", "약 1시간 5분", "약 0초"]
    assert run_js("[330, 355, 359, 20].map(s => m.fmtRemain(s, true))") == [
        "약 5분 30초", "약 6분", "약 6분", "약 20초"]


def test_spoken_dur():
    assert run_js("[302, 490, 3723, 0, 60].map(m.spokenDur)") == ["5분 2초", "8분 10초", "1시간 2분 3초", "0초", "1분"]


def test_monotonic_fraction_never_decreases():
    assert run_js("[[null, null], [null, 0.3], [0.3, 0.04], [0.3, null], [0.3, 0.5]].map(a => m.monotonicFraction(a[0], a[1]))") == [
        None, 0.3, 0.3, 0.3, 0.5]


def test_row_view_measured_over():
    r = run_js('m.rowView({state: "running", started_at: 0, expected: 230, fraction: 0.65}, 247)')
    assert r["over"] is True and r["byFraction"] is True
    assert abs(r["remaining"] - 247 * 0.35 / 0.65) < 0.01


def test_row_view_unmeasured_over_not_by_fraction():
    r = run_js('m.rowView({state: "running", started_at: 0, expected: 150, fraction: 0.05}, 220)')
    assert r["over"] is True and r["byFraction"] is False and r["remaining"] == 0


def test_row_view_last_phase_uses_phase_clock():
    # 카메라 구간: 시작 100초 뒤 그 구간의 40% -> 남은 150초(단계 전체 진행률 0.6으로는 133초)
    row = ('{state: "running", started_at: 0, expected: 400, fraction: 0.6, phase: "camera",'
           ' phase_started_at: 100, phase_fraction: 0.4}')
    r = run_js(f"m.rowView({row}, 200)")
    assert abs(r["remaining"] - 150) < 0.01 and r["byFraction"] is True
    timing = "{steps: {cuts: " + row + ', analyze: {state: "pending", expected: 90}}}'
    assert abs(run_js(f"m.itemRemaining({timing}, 200)") - 240) < 0.01


def test_row_view_last_phase_early_falls_back():
    row = ('{state: "running", started_at: 0, expected: 400, fraction: 0.4, phase: "camera",'
           ' phase_started_at: 100, phase_fraction: 0.05}')
    r = run_js(f"m.rowView({row}, 110)")
    assert abs(r["remaining"] - 110 * 0.6 / 0.4) < 0.01


def test_row_view_last_phase_paused_uses_paused_at():
    row = ('{state: "running", started_at: 0, paused_at: 200, expected: 400, fraction: 0.6, phase: "camera",'
           ' phase_started_at: 100, phase_fraction: 0.5}')
    assert abs(run_js(f"m.rowView({row}, 900)")["remaining"] - 100) < 0.01


def test_row_view_other_phase_ignores_phase_clock():
    row = ('{state: "running", started_at: 0, expected: 400, fraction: 0.5, phase: "scenes",'
           ' phase_started_at: 90, phase_fraction: 0.9}')
    assert abs(run_js(f"m.rowView({row}, 100)")["remaining"] - 100) < 0.01


def test_done_row_adds_prior_run():
    r = run_js('m.rowView({state: "done", elapsed: 30, expected: 20, prior_elapsed: 6, prior_expected: 5}, 9999)')
    assert r["elapsed"] == 36 and r["expected"] == 25


def test_last_phases_match_server_weights():
    from scripts.ui import progress
    last = [k for k, v in progress.PHASE_WEIGHTS["cuts"].items() if v > 0][-1]
    assert f'LAST_PHASES = ["{last}"]' in UTILS.read_text(encoding="utf-8")


# --- 영상 전체 진행률과 단계 칸 폭 (기다리는 화면 위쪽 큰 숫자) -------------------------

def test_item_progress_is_time_based():
    timing = ('{steps: {fetch: {state: "done", elapsed: 40, expected: 40},'
              ' cuts: {state: "running", started_at: 0, expected: 135, fraction: 0.5},'
              ' analyze: {state: "pending", expected: 90}}}')
    r = run_js(f"m.itemProgress({timing}, 60)")
    # 걸린 시간 = 40 + 60, 남은 시간 = 60*(0.5/0.5) + 90 = 150 -> 100 / 250
    assert r["elapsed"] == 100 and abs(r["remaining"] - 150) < 1e-9
    assert abs(r["fraction"] - 0.4) < 1e-9 and r["total"] == 250


def test_item_progress_unknown_and_finished():
    unknown = '{steps: {cuts: {state: "running", started_at: 0, expected: 10, fraction: 0.5}, analyze: {state: "pending", expected: null}}}'
    r = run_js(f"m.itemProgress({unknown}, 5)")
    assert r["fraction"] is None and r["remaining"] is None and r["elapsed"] == 5
    done = '{steps: {fetch: {state: "done", elapsed: 4, expected: 5}, cuts: {state: "skipped", expected: 0}}}'
    assert run_js(f"m.itemProgress({done}, 99)")["fraction"] == 1
    assert run_js("m.itemProgress(null, 1)") is None
    assert run_js('m.itemProgress({steps: {}}, 1)')["fraction"] is None


def test_step_weights_follow_time_with_a_floor():
    timing = ('{steps: {fetch: {state: "done", elapsed: 5, expected: 5},'
              ' cuts: {state: "running", started_at: 0, expected: 135, fraction: 0.5},'
              ' analyze: {state: "pending", expected: 90}, shots: {state: "skipped", expected: 60}}}')
    w = run_js(f"m.stepWeights({timing}, 60)")
    assert list(w) == ["fetch", "cuts", "analyze", "shots"]
    total = sum(w.values())
    assert abs(total - 1) < 1e-9
    assert all(v >= 0.14 - 1e-9 for v in w.values())          # 글자가 들어갈 만큼은 남긴다
    assert w["cuts"] > w["analyze"] > w["fetch"]                # 오래 걸리는 단계가 넓다
    # 시간을 모르면 똑같이 나눈다
    eq = run_js('m.stepWeights({steps: {a: {state: "pending", expected: null}, b: {state: "pending", expected: null}}}, 0)')
    assert eq == {"a": 0.5, "b": 0.5}
