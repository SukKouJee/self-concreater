"""add-utils.js 순수 함수 검증 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "add-utils.js"

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


def test_start_label():
    assert run_js("m.startLabel(0)") == "분석할 영상이 없어요"
    assert run_js("m.startLabel(3)") == "영상 3개 분석 시작"


def test_status_text_has_six_keys():
    assert run_js("m.STATUS_TEXT") == {
        "new": "새 영상", "exists": "이미 있음", "other_role": "다른 구분으로 있음",
        "dup": "중복", "invalid": "영상 링크 아님", "reanalyze": "다시 분석",
    }


def test_remove_line_drops_only_that_line():
    assert run_js(r'm.removeLine("a\nb\nc", "b")') == "a\nc"
    assert run_js(r'm.removeLine("a\nb\nc\n", "c")') == "a\nb\n"
    assert run_js(r'm.removeLine("a\r\nb\r\nc", "b")') == "a\r\nc"


def test_remove_line_removes_one_of_duplicate_lines():
    assert run_js(r'm.removeLine("a\nb\na\nc", "a")') == "b\na\nc"


def test_remove_line_keeps_other_links_on_a_shared_line():
    assert run_js(r'm.removeLine("a b\nc", "b")') == "a\nc"
    assert run_js(r'm.removeLine("a\nb", "zzz")') == "a\nb"


def test_split_links_by_whitespace():
    assert run_js(r'm.splitLinks("  a\nb  c\r\n\n")') == ["a", "b", "c"]


def test_summary_text_counts_by_status():
    rows = '[{status:"new"},{status:"new"},{status:"exists"},{status:"reanalyze"},{status:"dup"}]'
    assert run_js(f"m.summaryText({rows})") == "새 영상 2개 · 다시 분석 1개 · 건너뜀 2개"
    assert run_js('m.summaryText([{status:"new"}])') == "새 영상 1개"
    assert run_js("m.summaryText([])") == ""


def test_needs_attention():
    assert run_js("m.needsAttention(null)") is False
    assert run_js('m.needsAttention({status:"running",items:[]})') is True
    assert run_js('m.needsAttention({status:"awaiting",items:[]})') is True
    # finished job with nothing left: not shown again
    assert run_js('m.needsAttention({status:"done",items:[{state:"saved"},{state:"skipped"}]})') is False
    # stopped job that still holds a review: shown
    assert run_js('m.needsAttention({status:"stopped",items:[{state:"awaiting",ask:"review"}]})') is True
    assert run_js('m.needsAttention({status:"stopped",items:[{state:"awaiting",ask:"other"}]})') is False
