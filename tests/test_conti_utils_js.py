"""conti-utils.js 순수 함수 검증 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "conti-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# 섹션 0: a, b / 섹션 1: c / 섹션 2: d, e
CUTS = ("[{id:'a',section:0},{id:'b',section:0},{id:'c',section:1},{id:'d',section:2},{id:'e',section:2}]")


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ const C = {CUTS}; return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


def test_fmt_delta():
    assert run_js("m.fmtDelta(908, 900)") == "목표 15:00 → 지금 15:08 (+0:08)"
    assert run_js("m.fmtDelta(892, 900)") == "목표 15:00 → 지금 14:52 (-0:08)"
    assert run_js("m.fmtDelta(900, 900)") == "목표 15:00 · 지금 15:00"
    assert run_js("m.fmtDelta(61.2, null)") == "지금 1:01"


def test_can_remove():
    assert run_js("['a','b','c','d','e','zz'].map(id => m.canRemove(C, id))") == [True, True, False, True, True, False]


def test_snap_sec():
    assert run_js("[0.3, 121, 4.26, 4.2, 0, 1.75, 120].map(m.snapSec)") == [0.5, 120, 4.5, 4, 0.5, 2, 120]


def test_drop_target_down_one_row_is_old_index_plus_one():
    # gap 2 = right after b (pre-removal slot); a moves from 0 to final index 1, same section
    assert run_js("m.dropTarget(C, 'a', 2)") == {"to": 1, "section": 0}
    assert run_js("m.dropTarget(C, 'a', 1)") == {"to": 0, "section": 0}      # dropping on its own place


def test_drop_target_crosses_section_boundary():
    # a dropped after c (gap 3): final list [b,c,a,d,e] -> index 2, takes c's section
    assert run_js("m.dropTarget(C, 'a', 3)") == {"to": 2, "section": 1}
    # d dropped between b and c (gap 2): section 2 no longer fits there -> follows the previous cut (server rule)
    assert run_js("m.dropTarget(C, 'd', 2)") == {"to": 2, "section": 0}
    assert run_js("m.dropTarget(C, 'd', 2, 1)") == {"to": 2, "section": 1}


def test_drop_target_front_and_back():
    assert run_js("m.dropTarget(C, 'e', 0)") == {"to": 0, "section": 0}
    assert run_js("m.dropTarget(C, 'a', 5)") == {"to": 4, "section": 2}


def test_drop_target_explicit_section_when_it_fits():
    # gap 2 (between b and c) is on a section edge: explicit section decides
    assert run_js("m.dropTarget(C, 'a', 2, 1)") == {"to": 1, "section": 1}
    assert run_js("m.dropTarget(C, 'a', 2, 0)") == {"to": 1, "section": 0}
    assert run_js("m.dropTarget(C, 'a', 2, 2)") == {"to": 1, "section": 0}   # does not fit -> inferred


def test_step_move():
    assert run_js("m.stepMove(C, 'a', -1)") is None
    assert run_js("m.stepMove(C, 'e', 1)") is None
    assert run_js("m.stepMove(C, 'a', 1)") == {"to": 1, "section": 0, "crosses": False}
    assert run_js("m.stepMove(C, 'b', 1)") == {"to": 2, "section": 1, "crosses": True}
    assert run_js("m.stepMove(C, 'd', -1)") == {"to": 2, "section": 1, "crosses": True}


def test_shot_sizes_match_server():
    from scripts.features import SHOT_SIZES
    assert run_js("m.SHOT_SIZES") == SHOT_SIZES
