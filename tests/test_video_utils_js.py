"""video-utils.js 순수 함수 검증: 영상 id↔기획안 경로, 레거시 라우트 변환, 6단계 진행과 다음 행동 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "video-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def enc(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="-_.!~*'()")      # same set encodeURIComponent leaves alone


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


# ---- id <-> path -------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "drafts/2026-10-25-주사위가-정하면-부루마블-1.md",
    "drafts/a/b.md",                      # 하위 폴더도 그대로
    "drafts/a b#?%.md",                   # 인코딩이 필요한 문자
    "drafts/x.y.md",
])
def test_video_id_roundtrip(path):
    vid = run_js(f"m.videoId({json.dumps(path)})")
    assert not vid.startswith("drafts/") and not vid.endswith(".md")
    assert run_js(f"m.draftPath({json.dumps(vid)})") == path
    # through the router: the id is one hash segment, decoded back unchanged
    href = run_js(f"m.videoHref({json.dumps(vid)}, 'conti')")
    assert href == f"#/video/{enc(vid)}/conti"
    assert run_js(f"m.parseVideoHash({json.dumps(href)})") == {"mode": "video", "id": vid, "tab": "conti"}


def test_video_id_of_examples():
    assert run_js("m.videoId('drafts/a/b.md')") == "a/b"
    assert run_js("m.videoId('drafts/x.MD')") == "x"
    assert run_js("m.draftPath('a/b')") == "drafts/a/b.md"
    assert run_js("m.videoHref('a/b')") == "#/video/a%2Fb"


def test_parse_video_hash():
    assert run_js("m.parseVideoHash('#/videos')") == {"mode": "list"}
    assert run_js("m.parseVideoHash('#/video/x')") == {"mode": "video", "id": "x", "tab": None}
    for tab in ("plan", "conti", "check", "diagnose"):
        assert run_js(f"m.parseVideoHash('#/video/x/{tab}')") == {"mode": "video", "id": "x", "tab": tab}
    assert run_js("m.parseVideoHash('#/video/x/shoot')") == {"mode": "shoot", "id": "x"}
    assert run_js("m.parseVideoHash('#/video/x/nope')") == {"mode": "video", "id": "x", "tab": None}
    assert run_js("m.parseVideoHash('#/video/')") == {"mode": "list"}
    assert run_js("m.parseVideoHash('#/video/%E0%A4')") == {"mode": "list"}       # undecodable id
    assert run_js("m.parseVideoHash('#/home')") is None
    assert run_js("m.TABS.map((t) => [t.key, t.label])") == [
        ["plan", "기획안"], ["conti", "콘티"], ["check", "촬영 체크"], ["diagnose", "진단"]]


# ---- legacy routes ------------------------------------------------------------------------------

DRAFT = "drafts/2026-10-25-부루마블 1.md"
VID = "2026-10-25-부루마블 1"


@pytest.mark.parametrize("hash_, expected", [
    # 기획안: 목록 / 상세(실제 형식 #/draft/view/<path>) / ?path= 형식
    ("#/draft", "#/videos"),
    (f"#/draft/view/{enc(DRAFT)}", f"#/video/{enc(VID)}/plan"),
    (f"#/draft?path={enc(DRAFT)}", f"#/video/{enc(VID)}/plan"),
    ("#/draft/view/%E0%A4", "#/videos"),
    # 콘티: 목록 / 기획안 경로 / 콘티 경로 / ?path=
    ("#/conti", "#/videos"),
    (f"#/conti/{enc(DRAFT)}", f"#/video/{enc(VID)}/conti"),
    (f"#/conti/{enc('drafts/2026-10-25-부루마블 1-conti.md')}", f"#/video/{enc(VID)}/conti"),
    (f"#/conti?path={enc('drafts/2026-10-25-부루마블 1-conti.md')}", f"#/video/{enc(VID)}/conti"),
    # 진단: 목록 / 기획안 대상 / ?path=
    ("#/diagnose", "#/videos"),
    (f"#/diagnose/{enc(DRAFT)}", f"#/video/{enc(VID)}/diagnose"),
    (f"#/diagnose?path={enc(DRAFT)}", f"#/video/{enc(VID)}/diagnose"),
    # 하위 폴더 기획안
    (f"#/conti/{enc('drafts/a/b-conti.md')}", "#/video/a%2Fb/conti"),
    # 공용 라이브러리는 레퍼런스 안 탭
    ("#/library", "#/refs?tab=library"),
])
def test_legacy_routes_redirect(hash_, expected):
    assert run_js(f"m.legacyRoute({json.dumps(hash_)})") == expected


@pytest.mark.parametrize("hash_", [
    "#/home", "#/refs", "#/refs?tab=library", "#/motif", "#/videos", "#/video/x/plan", "",
    "#/draft/new", f"#/draft/new/{enc('M-travel-001')}",                 # 새 기획안 만들기는 그대로
    f"#/diagnose/{enc('refs/ref_travel/videos/abc.md')}",                # 올린 내 영상 진단은 진단 화면 그대로
])
def test_legacy_routes_leave_current_routes_alone(hash_):
    assert run_js(f"m.legacyRoute({json.dumps(hash_)})") is None


# ---- stages / next action -----------------------------------------------------------------------

def states(draft: dict, extras: dict):
    return run_js(f"m.stages({json.dumps(draft)}, {json.dumps(extras)}).map((s) => [s.key, s.label, s.state])")


NONE_DONE = {"polish": False, "captions": False, "diagnose": False}


def test_stages_from_draft():
    # 기획안만
    assert states({"conti": None, "filmed_at": None}, NONE_DONE) == [
        ["draft", "기획안", "done"], ["polish", "다듬기", "skip"], ["conti", "콘티", "now"],
        ["filmed", "촬영", "todo"], ["captions", "편집", "todo"], ["diagnose", "진단", "todo"]]
    # 콘티 있음
    assert [s[2] for s in states({"conti": "drafts/x-conti.md", "filmed_at": None}, NONE_DONE)] == [
        "done", "skip", "done", "now", "todo", "todo"]
    # filmed_at 있음
    assert [s[2] for s in states({"conti": "drafts/x-conti.md", "filmed_at": "2026-10-28"}, NONE_DONE)] == [
        "done", "skip", "done", "done", "skip", "now"]
    # 진단 있음(모두 끝): 남은 선택 단계는 건너뜀
    assert [s[2] for s in states({"conti": "c", "filmed_at": "2026-10-28"},
                                 {"polish": True, "captions": False, "diagnose": True})] == [
        "done", "done", "done", "done", "skip", "done"]


def test_stages_from_status_steps_and_unknowns():
    steps = [{"key": "draft", "done": True}, {"key": "polish", "done": True}, {"key": "conti", "done": True},
             {"key": "filmed", "done": False}, {"key": "captions", "done": True}, {"key": "diagnose", "done": False}]
    assert [s[2] for s in states({"conti": "c"}, {"steps": steps})] == ["done", "done", "done", "now", "done", "todo"]
    # nothing known beyond the draft and its conti: unknown steps show "–" (skip), not "todo"
    assert [s[2] for s in states({"conti": None}, {})] == ["done", "skip", "now", "skip", "skip", "skip"]


def test_stage_marks():
    got = run_js("m.stages({conti: 'c', filmed_at: null}, {polish: false, captions: false, diagnose: false})"
                 ".map(m.stageText)")
    assert got == ["기획안 ✓", "다듬기 –", "콘티 ✓", "촬영 ●", "편집", "진단"]


@pytest.mark.parametrize("draft, extras, expected", [
    ({"conti": None, "filmed_at": None}, NONE_DONE, {"label": "콘티 만들기", "kind": "conti"}),
    ({"conti": "c", "filmed_at": None}, NONE_DONE, {"label": "폰 촬영 모드", "kind": "shoot"}),
    ({"conti": "c", "filmed_at": "2026-10-28"}, NONE_DONE, {"label": "진단하기", "kind": "diagnose"}),
    ({"conti": "c", "filmed_at": "2026-10-28"}, {**NONE_DONE, "diagnose": True}, {"label": "모든 단계를 마쳤어요", "kind": "done"}),
])
def test_next_action(draft, extras, expected):
    assert run_js(f"m.nextAction(m.stages({json.dumps(draft)}, {json.dumps(extras)}))") == expected


def test_next_action_unknown_when_required_stages_are_unknown():
    # status missing or failed: filmed/diagnose unknown -> never claim everything is done
    assert run_js("m.nextAction(m.stages({conti: 'c'}, {}))") == {"label": "", "kind": "unknown"}
    assert run_js("m.nextAction(m.stages({conti: 'c', filmed_at: '2026-10-28'}, {}))")["kind"] == "unknown"


def test_default_tab():
    assert run_js("m.defaultTab(m.stages({conti: null}, {polish: false, captions: false, diagnose: false}))") == "conti"
    assert run_js("m.defaultTab(m.stages({conti: 'c', filmed_at: '2026-10-28'}, "
                  "{polish: false, captions: false, diagnose: false}))") == "diagnose"


# ---- header text --------------------------------------------------------------------------------

def test_video_meta_line_and_chips():
    draft = {"date": "2026-10-25", "target_sec": 900, "source": "M-travel-005", "archetype": "A1"}
    cards = [{"id": "M-travel-005", "type": "angle", "type_ko": "각도", "title": "대중교통 당일치기"}]
    archetypes = [{"id": "A1", "name": "길 위의 여정 기록"}]
    got = run_js(f"m.videoMeta({json.dumps(draft)}, {json.dumps(cards)}, {json.dumps(archetypes)})")
    assert got == {"when": "10월 25일 시작 · 목표 15분",
                   "motif": {"label": "각도 · 대중교통 당일치기", "kind": "angle"},
                   "type": {"label": "유형 A1 · 길 위의 여정 기록"}}
    # source is the archetype itself, no names known, short target
    got = run_js("m.videoMeta({date: '2026-10-20', target_sec: 45, source: 'A1', archetype: 'A1'}, [], null)")
    assert got == {"when": "10월 20일 시작 · 목표 45초", "motif": None, "type": {"label": "유형 A1"}}
    assert run_js("m.videoMeta({}, null, null)") == {"when": "", "motif": None, "type": None}


def test_short_title():
    assert run_js("m.shortTitle('짧은 제목')") == "짧은 제목"
    long = "주사위가 정하면 무조건 출발! 20만 원 들고 떠난 국내 여행 부루마블 1탄"
    got = run_js(f"m.shortTitle({json.dumps(long)})")
    assert got.endswith("…") and len(got) <= 25


def test_conti_summary_and_shoot_line():
    conti = {"cuts": [{"sec": 2}, {"sec": 4}, {"sec": 3}], "total_sec": 900}
    assert run_js(f"m.contiSummary({json.dumps(conti)})") == "3컷 · 15:00 · 평균 컷 300.0초"
    assert run_js("m.contiSummary({cuts: [{sec: 3}, {sec: 3}], total_sec: 6})") == "2컷 · 0:06 · 평균 컷 3.0초"
    assert run_js("m.contiSummary(null)") == ""
    assert run_js("m.contiSummary({cuts: [], total_sec: 0})") == ""
    assert run_js("m.shootLine(6, false)") == "촬영 0 / 6컷"
    assert run_js("m.shootLine(6, true)") == "촬영 6 / 6컷"
