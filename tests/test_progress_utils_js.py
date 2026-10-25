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
