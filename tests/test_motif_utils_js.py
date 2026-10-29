"""motif-utils.js 순수 함수 검증: 한 줄 요약, 종류별 개수, 필터, 근거 썸네일, 이 카드로 만든 영상 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "motif-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def one(text, *args):
    extra = "".join(f", {json.dumps(a)}" for a in args)
    return run_js(f"m.oneLine({json.dumps(text)}{extra})")


CARDS = [
    {"id": "M-t-001", "type": "angle", "starred": True},
    {"id": "M-t-002", "type": "flow", "starred": True},
    {"id": "M-t-003", "type": "technique", "starred": False},
    {"id": "M-t-004", "type": "technique", "starred": False},
    {"id": "M-t-005", "type": "combo", "starred": False},
    {"id": "M-t-006", "type": "기타", "starred": False},          # unknown kind (hand-edited md): only in 전체
]


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


def test_one_line_first_sentence():
    assert one("아무도 안 한 당일치기에 규칙 하나를 걸어 이야기를 만든다. 레퍼런스 5개가 모두 여러 날짜리다.") \
        == "아무도 안 한 당일치기에 규칙 하나를 걸어 이야기를 만든다."
    assert one("정말 될까? 해 보자.") == "정말 될까?"
    assert one("와! 대단하다") == "와!"
    assert one("문장 끝 표시가 없는 한 줄") == "문장 끝 표시가 없는 한 줄"
    assert one("  줄바꿈과\n\n  공백도   하나로.  다음 ") == "줄바꿈과 공백도 하나로."
    assert one("1.5배 빠르게 걷는다. 다음") == "1.5배 빠르게 걷는다."     # a decimal point is not a sentence end


def test_one_line_cuts_long_text():
    long = "가" * 80 + "."
    got = one(long)
    assert len(got) == 60 and got.endswith("…")
    assert one(long, 10) == "가" * 9 + "…"
    assert one("가나다라마바사.", 10) == "가나다라마바사."           # short enough: untouched


@pytest.mark.parametrize("value", [None, "", "   ", 5, ["a"]])
def test_one_line_empty_or_not_text(value):
    assert run_js(f"m.oneLine({json.dumps(value)})") == ""


def test_kind_counts():
    assert run_js(f"m.kindCounts({json.dumps(CARDS)})") == {
        "all": 6, "star": 2, "flow": 1, "technique": 2, "angle": 1, "combo": 1}
    assert run_js("m.kindCounts([])") == {"all": 0, "star": 0, "flow": 0, "technique": 0, "angle": 0, "combo": 0}
    assert run_js("m.kindCounts(null)")["all"] == 0


def test_filters_in_mockup_order():
    assert run_js("m.FILTERS") == [
        ["all", "전체"], ["star", "★"], ["flow", "흐름"], ["technique", "기법"], ["angle", "각도"], ["combo", "조합"]]


def ids(expr):
    return [c["id"] for c in run_js(expr)]


def test_filter_cards_star():
    assert ids(f"m.filterCards({json.dumps(CARDS)}, 'star')") == ["M-t-001", "M-t-002"]
    # a card being un-starred stays visible while its request is in flight (pinned)
    assert ids(f"m.filterCards({json.dumps(CARDS)}, 'star', new Set(['M-t-004']))") == ["M-t-001", "M-t-002", "M-t-004"]


def test_filter_cards_by_kind_and_all():
    assert ids(f"m.filterCards({json.dumps(CARDS)}, 'technique')") == ["M-t-003", "M-t-004"]
    assert ids(f"m.filterCards({json.dumps(CARDS)}, 'all')") == [c["id"] for c in CARDS]
    assert ids(f"m.filterCards({json.dumps(CARDS)}, 'nope')") == [c["id"] for c in CARDS]     # unknown filter: all
    assert run_js("m.filterCards(null, 'all')") == []


def test_tag_kind_maps_to_ui_kit_kinds():
    assert run_js("['flow','technique','angle','combo','x'].map(m.tagKind)") == ["flow", "tech", "angle", "combo", ""]


def test_evidence_thumbs_first_three_with_ref_frames():
    card = {"evidence": [{"id": "a", "title": "A"}, {"id": "b", "title": None}, {"id": "c", "title": "C"},
                         {"id": "d", "title": "D"}, None, {"title": "no id"}]}
    refs = [{"id": "a", "title": "A ref", "thumb": "workspace/frames/a/1.jpg"}, {"id": "b", "title": "B ref", "thumb": None}]
    got = run_js(f"m.evidenceItems({json.dumps(card)}, {json.dumps(refs)})")
    assert got == [
        {"id": "a", "title": "A", "thumb": "workspace/frames/a/1.jpg"},
        {"id": "b", "title": "B ref", "thumb": None},          # title from the ref list when the card has none
        {"id": "c", "title": "C", "thumb": None},              # not in the ref list: no frame
        {"id": "d", "title": "D", "thumb": None},
    ]
    assert run_js(f"m.evidenceItems({json.dumps(card)}, null).length") == 4
    assert run_js("m.evidenceItems({}, [])") == []


def test_made_from_card():
    drafts = [
        {"path": "drafts/2026-10-25-a.md", "title": "부루마블 1탄", "date": "2026-10-25", "source": "M-t-001"},
        {"path": "drafts/2026-10-20-b.md", "title": "다른 것", "date": "2026-10-20", "source": "A1"},
        {"path": "drafts/2026-10-21-c.md", "title": "", "date": "2026-10-21", "source": "M-t-001"},
    ]
    got = run_js(f"m.madeFrom({json.dumps(drafts)}, 'M-t-001')")
    assert got == [{"path": "drafts/2026-10-25-a.md", "title": "부루마블 1탄"},
                   {"path": "drafts/2026-10-21-c.md", "title": "제목 없는 영상"}]
    assert run_js("m.madeFrom(null, 'x')") == []


def reason(kind, data):
    return run_js(f"m.lockedReason({json.dumps(kind)}, {json.dumps(data)})")


def test_locked_reason_explains_kinds_the_category_cannot_make_yet():
    # /api/motif fields: stage, has_typify, allowed_types
    no_typify = {"stage": 3, "has_typify": False, "allowed_types": ["technique", "angle"]}
    assert reason("flow", no_typify) == "흐름·조합 카드는 유형화를 마친 뒤에 나와요"
    assert reason("combo", no_typify) == "흐름·조합 카드는 유형화를 마친 뒤에 나와요"
    assert reason("technique", no_typify) == ""
    stage1 = {"stage": 1, "has_typify": False, "allowed_types": ["technique"]}
    assert reason("angle", stage1) == "각도 카드는 레퍼런스가 더 모이면 뽑을 수 있어요"
    assert reason("flow", stage1) == "흐름 카드는 레퍼런스가 더 모이면 뽑을 수 있어요"
    full = {"stage": 3, "has_typify": True, "allowed_types": ["flow", "technique", "angle", "combo"]}
    assert [reason(k, full) for k in ("flow", "combo", "angle", "technique")] == ["", "", "", ""]
    assert reason("all", stage1) == "" and reason("star", stage1) == ""
    assert reason("flow", None) == ""
