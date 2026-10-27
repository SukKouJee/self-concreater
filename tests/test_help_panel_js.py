"""help-utils.js 순수 함수 검증 (node 없으면 skip). help-panel.js가 다시 내보낸다."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "help-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_help_page_from_hash():
    r = run_js('["#/draft/view/x", "#/refs/abc", "#/guide/long", "", "#/", "#/soon/x"].map(m.helpPage)')
    assert r == ["draft", "refs", "guide", "home", "home", "soon"]


def test_help_page_ignores_query_and_odd_hash():
    assert run_js('["#/report/review/2026-W01", "#main", "#/add?x=1", "#/Draft"].map(m.helpPage)') == [
        "report", "home", "refs", "home"]


def test_trim_history_keeps_last_six():
    r = run_js('m.trimHistory(Array.from({length: 8}, (_, i) => ({role: "user", text: String(i)}))).map(x => x.text)')
    assert r == ["2", "3", "4", "5", "6", "7"]


def test_trim_history_short_list_and_max():
    assert run_js('m.trimHistory([{role: "user", text: "a"}]).length') == 1
    assert run_js('m.trimHistory([1, 2, 3, 4], 2)') == [3, 4]
    assert run_js('m.trimHistory([1, 2, 3], 0)') == []


def test_poll_delay_backs_off():
    assert run_js("[m.pollDelay(0), m.pollDelay(1), m.pollDelay(2), m.pollDelay(9)]") == [1000, 2000, 4000, 15000]
