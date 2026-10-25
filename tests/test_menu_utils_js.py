"""menu-utils.js 순수 함수 검증: 현재 메뉴 판정, 접힌 그룹 기억, 현재 그룹 자동 펼침 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "menu-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

MENU = [
    {"group": None, "items": [{"name": "홈", "route": "#/home"}]},
    {"group": "모으기", "items": [{"name": "레퍼런스 추가", "route": "#/add"}, {"name": "레퍼런스 목록", "route": "#/refs"}]},
    {"group": "만들기", "items": [{"name": "기획안", "route": "#/draft"}, {"name": "콘티", "route": "#/conti"}]},
    {"group": None, "items": [{"name": "가이드", "route": "#/guide"}]},
]


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => {{ const MENU = {json.dumps(MENU, ensure_ascii=False)}; "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))); }})"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


@pytest.mark.parametrize("href, hash_, expected", [
    ("#/home", "#/home", True),
    ("#/home", "", True),                       # 해시가 없으면 홈
    ("#/refs", "#/refs/abc", True),             # 상세 화면도 목록 메뉴가 현재
    ("#/conti", "#/conti/x", True),
    ("#/draft", "#/draft/x", True),
    ("#/diagnose", "#/diagnose/x", True),
    ("#/report", "#/report/x", True),
    ("#/guide", "#/guide/shorts", True),
    ("#/add", "#/add/x", False),                # 하위 경로가 없는 메뉴는 정확히 같을 때만
    ("#/refs", "#/refsx", False),
    ("#/home", "#/add", False),
])
def test_is_active(href, hash_, expected):
    assert run_js(f"m.isActive({json.dumps(href)}, {json.dumps(hash_)})") is expected


def test_active_group():
    assert run_js('m.activeGroup(MENU, "#/conti/abc")') == "만들기"
    assert run_js('m.activeGroup(MENU, "#/add")') == "모으기"
    assert run_js('m.activeGroup(MENU, "#/home")') is None        # 그룹 밖 메뉴
    assert run_js('m.activeGroup(MENU, "#/nope")') is None
    assert run_js('m.activeGroup(MENU, "#/soon/x", (it) => it.route === "#/draft" ? "#/soon/x" : it.route)') == "만들기"


@pytest.mark.parametrize("raw, expected", [
    ('["모으기","만들기"]', ["모으기", "만들기"]),
    (None, []),
    ("", []),
    ("not json", []),
    ('{"a":1}', []),
    ('["모으기", 3, null, "모으기"]', ["모으기"]),              # 문자열만, 중복 없이
])
def test_parse_collapsed(raw, expected):
    assert run_js(f"m.parseCollapsed({json.dumps(raw)})") == expected


def test_toggle_collapsed_adds_and_removes_without_mutating():
    assert run_js('(() => { const a = ["분석"]; const b = m.toggleCollapsed(a, "모으기"); return [a, b]; })()') == [
        ["분석"], ["분석", "모으기"]]
    assert run_js('m.toggleCollapsed(["분석", "모으기"], "분석")') == ["모으기"]


def test_expand_for_opens_only_the_current_group():
    assert run_js('m.expandFor(["모으기", "분석"], "모으기")') == ["분석"]   # 지금 보는 화면이 든 그룹은 펼친다
    assert run_js('m.expandFor(["분석"], "모으기")') == ["분석"]
    assert run_js('m.expandFor(["분석"], null)') == ["분석"]
