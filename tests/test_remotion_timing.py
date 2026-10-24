"""remotion/src/timing.mjs 순수 함수 검증 (node 없으면 skip)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
TIMING = Path(__file__).resolve().parent.parent / "remotion" / "src" / "timing.mjs"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(expr: str):
    code = (
        f"import({json.dumps(TIMING.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_to_frames_rounds():
    assert run_js("[m.toFrames(1.0, 30), m.toFrames(0.5, 30), m.toFrames(0.016, 30), m.toFrames(0.017, 30)]") == [30, 15, 0, 1]


def test_caption_frames_basic():
    r = run_js('m.captionFrames([{start: 0, end: 1, text: "a"}, {start: 2, end: 3.5, text: "b"}], 30)')
    assert r == [{"from": 0, "duration": 30, "text": "a"}, {"from": 60, "duration": 45, "text": "b"}]


def test_caption_frames_trims_overlap():
    r = run_js('m.captionFrames([{start: 0, end: 2, text: "a"}, {start: 1, end: 3, text: "b"}], 30)')
    assert r == [{"from": 0, "duration": 30, "text": "a"}, {"from": 30, "duration": 60, "text": "b"}]


def test_caption_frames_drops_zero_length():
    r = run_js('m.captionFrames([{start: 1, end: 1, text: "z"}, {start: 2, end: 1.5, text: "n"}, {start: 3, end: 4, text: "ok"}], 30)')
    assert r == [{"from": 90, "duration": 30, "text": "ok"}]


def test_caption_frames_drops_empty_text():
    r = run_js('m.captionFrames([{start: 0, end: 1, text: ""}, {start: 1, end: 2, text: "   "}, {start: 2, end: 3, text: "ok"}], 30)')
    assert r == [{"from": 60, "duration": 30, "text": "ok"}]


def test_caption_frames_drops_fully_covered():
    # second starts at same frame as first -> first has zero length after trim
    r = run_js('m.captionFrames([{start: 1, end: 2, text: "a"}, {start: 1, end: 3, text: "b"}], 30)')
    assert r == [{"from": 30, "duration": 60, "text": "b"}]


def test_emphasis_tokens():
    r = run_js('m.emphasisTokens("조회수 100만 돌파  했어요")')
    assert r == [
        {"text": "조회수", "emphasis": False},
        {"text": "100만", "emphasis": True},
        {"text": "돌파", "emphasis": False},
        {"text": "했어요", "emphasis": False},
    ]


def test_animate_short_caption_static():
    # 0.4초(12프레임) 미만 -> 움직임 없음
    assert run_js("[m.animate(0, 11, 30), m.animate(10, 11, 30)]") == [
        {"scale": 1, "opacity": 1},
        {"scale": 1, "opacity": 1},
    ]


def test_animate_in_and_out():
    first = run_js("m.animate(0, 60, 30)")
    assert first["scale"] == pytest.approx(0.85)
    assert run_js("m.animate(8, 60, 30)") == {"scale": 1, "opacity": 1}
    assert run_js("m.animate(30, 60, 30)") == {"scale": 1, "opacity": 1}
    last = run_js("m.animate(59, 60, 30)")
    assert 0 < last["opacity"] < 1
    mid = run_js("m.animate(4, 60, 30)")
    assert 0.85 < mid["scale"] < 1
