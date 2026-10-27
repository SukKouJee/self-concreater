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


def _job(*items):
    return {"status": "running", "items": list(items)}


def _it(state, ask=None, category="travel", **extra):
    return {"id": extra.pop("id", "x"), "state": state, "ask": ask, "opts": {"category": category}, **extra}


def test_review_items_only_awaiting_review_in_this_category():
    job = _job(
        _it("awaiting", "review", id="a"),
        _it("awaiting", "review", category="food", id="b"),
        _it("awaiting", None, id="c"),
        _it("running", "review", id="d"),
        _it("saved", "review", id="e"),
        {"id": "f", "state": "awaiting", "ask": "review"},
    )
    got = run_js(f"m.reviewItems({json.dumps(job)}, 'travel').map(i => i.id)")
    assert got == ["a"]
    assert run_js("m.reviewItems(null, 'travel')") == []


def test_queue_line_three_cases():
    waiting = _job(_it("running"), _it("waiting"), _it("waiting"), _it("awaiting", "review"))
    assert run_js(f"m.queueLine({json.dumps(waiting)})") == "남은 영상 2개"
    only_review = _job(_it("awaiting", "review"), _it("awaiting", "review"), _it("saved"))
    assert run_js(f"m.queueLine({json.dumps(only_review)})") == "확인을 기다리는 영상 2개"
    saved_for_shots = _job(_it("running"), _it("waiting", step="shots"), _it("waiting", step="queued"))
    assert run_js(f"m.queueLine({json.dumps(saved_for_shots)})") == "남은 영상 1개"
    only_shots = _job(_it("waiting", step="shots"), _it("awaiting", "review"))
    assert run_js(f"m.queueLine({json.dumps(only_shots)})") == "확인을 기다리는 영상 1개"
    last = _job(_it("running"), _it("awaiting", "review"))
    assert run_js(f"m.queueLine({json.dumps(last)})") == ""
    assert run_js("m.queueLine(null)") == ""


def test_item_category_falls_back_to_the_job():
    assert run_js("m.itemCategory({opts: {category: 'a'}}, {category: 'b'})") == "a"
    assert run_js("m.itemCategory({}, {category: 'b'})") == "b"
    assert run_js("m.itemCategory({}, null)") == ""


def test_save_all_plan_edited_carry_analysis_and_each_id_once():
    items = [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"id": "c"}]
    plan = run_js(f"m.saveAllPlan({json.dumps(items)}, {{b: {{sections: [1]}}}})")
    assert [p["id"] for p in plan] == ["a", "b", "c"]
    assert plan[0]["body"] == {"action": "save"}
    assert plan[1]["body"] == {"action": "save", "analysis": {"sections": [1]}}
    assert plan[2]["body"] == {"action": "save"}
    assert run_js("m.saveAllPlan(null, null)") == []


def test_stale_ids_keeps_ids_the_new_job_adopted():
    job = _job(_it("awaiting", "review", id="a"), _it("waiting", id="n"))
    assert run_js(f"m.staleIds(['a', 'b'], {json.dumps(job)})") == ["b"]
    assert run_js(f"m.staleIds(new Map([['a', 1], ['c', 2]]).keys(), {json.dumps(job)})") == ["c"]
    assert run_js("m.staleIds(['a'], null)") == ["a"]
    assert run_js("m.staleIds(undefined, null)") == []
