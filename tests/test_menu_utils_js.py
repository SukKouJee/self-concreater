"""menu-utils.js 순수 함수 검증: 사이드바·하단 탭 구성, 현재 메뉴 판정, 영상 진행 표시, 폰 보조 메뉴 (node 없으면 skip)."""
import json
import re
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "menu-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_imports_only_video_utils():
    text = UTILS.read_text(encoding="utf-8").replace("import(", "")
    imports = re.findall(r'^import .* from "([^"]+)";$', text, re.MULTILINE)
    assert imports == ["./video-utils.js"]           # relative: node tests load it from disk, the browser from /static
    assert text.count("import ") == 1


def test_sidebar_has_three_groups_in_spec_order():
    got = run_js("m.SIDEBAR.map((g) => [g.group, g.items.map((it) => [it.label, it.href])])")
    assert got == [
        ["영감", [["레퍼런스", "#/refs"], ["유형", "#/typify"], ["모티브", "#/motif"]]],
        ["내 영상", [["+ 새 영상 시작", "#/motif"]]],
        ["성장", [["오늘 미션", "#/mission"], ["주간 리포트", "#/report"]]],
    ]
    assert run_js("m.SIDEBAR[1].items[0].action") is True      # an action link: never marked current
    assert run_js("m.SIDEBAR.map((g) => !!g.videos)") == [False, True, False]   # 영상 목록은 내 영상 그룹에만
    assert run_js("m.SIDEBAR[0].items.map((it) => it.count)") == ["ref", "archetypes", "motifs"]


def test_phone_tabs_are_four():
    got = run_js("m.PHONE_TABS.map((t) => [t.key, t.label, t.href, typeof t.icon])")
    assert got == [
        ["home", "홈", "#/home", "string"], ["inspire", "영감", "#/refs", "string"],
        ["videos", "내 영상", "#/videos", "string"], ["mission", "미션", "#/mission", "string"],
    ]


@pytest.mark.parametrize("href, hash_, expected", [
    ("#/home", "#/home", True),
    ("#/home", "", True),                       # 해시가 없으면 홈
    ("#/refs", "#/refs/abc", True),             # 상세 화면도 목록 메뉴가 현재
    ("#/conti", "#/conti/x", True),
    ("#/draft", "#/draft/x", True),
    ("#/diagnose", "#/diagnose/x", True),
    ("#/report", "#/report/review", True),
    ("#/guide", "#/guide/shorts", True),
    ("#/refs", "#/add", True),                  # 추가 칸은 레퍼런스 화면의 일부
    ("#/draft/new", "#/draft/new/abc", True),   # 출처를 고른 새 기획안도 "+ 새 영상 시작"
    ("#/video", "#/video/x/conti", True),     # 영상 작업 공간의 탭·촬영 모드
    ("#/videos", "#/video/x", False),
    ("#/add", "#/add/x", False),                # 하위 경로가 없는 메뉴는 정확히 같을 때만
    ("#/refs", "#/refsx", False),
    ("#/home", "#/add", False),
])
def test_is_active(href, hash_, expected):
    assert run_js(f"m.isActive({json.dumps(href)}, {json.dumps(hash_)})") is expected


@pytest.mark.parametrize("hash_, expected", [
    ("", "home"), ("#/home", "home"), ("#/zzz", "home"),
    ("#/refs", "inspire"), ("#/refs/abc", "inspire"), ("#/add", "inspire"), ("#/library", "inspire"),
    ("#/typify", "inspire"), ("#/motif", "inspire"),
    ("#/draft", "videos"), ("#/draft/view/drafts%2Fa.md", "videos"), ("#/conti/x", "videos"),
    ("#/diagnose", "videos"), ("#/videos", "videos"), ("#/video/x", "videos"), ("#/video/x/shoot", "videos"),
    ("#/refs?tab=library", "inspire"),
    ("#/mission", "mission"), ("#/report", "mission"), ("#/report/review/2026-W43", "mission"),
    ("#/guide", None), ("#/guide/long", None),
])
def test_phone_tab(hash_, expected):
    assert run_js(f"m.phoneTab({json.dumps(hash_)})") == expected


def test_video_links_go_to_the_workspace():
    got = run_js('m.videoLinks("drafts/2026-10-25 부루마블.md")')
    assert got == {"view": "#/video/2026-10-25%20%EB%B6%80%EB%A3%A8%EB%A7%88%EB%B8%94"}


def test_is_video_active():
    path = "drafts/a b.md"
    for hash_ in ("#/video/a%20b", "#/video/a%20b/conti", "#/video/a%20b/diagnose", "#/video/a%20b/shoot"):
        assert run_js(f"m.isVideoActive({json.dumps(path)}, {json.dumps(hash_)})") is True, hash_
    for hash_ in ("#/video/a%20bc", "#/videos", "#/draft", "#/conti/drafts%2Fa%20b-conti.md"):
        assert run_js(f"m.isVideoActive({json.dumps(path)}, {json.dumps(hash_)})") is False, hash_


def _steps(done):
    keys = [("draft", False), ("polish", True), ("conti", False), ("filmed", False), ("captions", True),
            ("diagnose", False)]
    return [{"key": k, "done": k in done, "optional": opt} for k, opt in keys]


@pytest.mark.parametrize("done, next_, expected", [
    ({"draft"}, "conti", {"pct": 25, "stage": "콘티"}),
    ({"draft", "polish", "conti"}, "filmed", {"pct": 50, "stage": "촬영"}),       # 선택 단계는 진행률에 안 센다
    ({"draft", "conti", "filmed"}, "diagnose", {"pct": 75, "stage": "진단"}),
    ({"draft", "conti", "filmed", "diagnose"}, None, {"pct": 100, "stage": "완료"}),
])
def test_video_progress(done, next_, expected):
    video = {"steps": _steps(done), "next": next_}
    assert run_js(f"m.videoProgress({json.dumps(video)})") == expected


def test_video_progress_tolerates_missing_steps():
    assert run_js('m.videoProgress({ next: "conti" })') == {"pct": 0, "stage": "콘티"}
    assert run_js("m.videoProgress({})") == {"pct": 0, "stage": "완료"}


def test_sidebar_videos_lists_unfinished_first_up_to_limit():
    videos = [{"path": f"drafts/{i}.md", "next": None if i >= 4 else "conti"} for i in range(7)]
    got = run_js(f"m.sidebarVideos({json.dumps(videos)}, 3)")
    assert [v["path"] for v in got["shown"]] == ["drafts/0.md", "drafts/1.md", "drafts/2.md"]
    assert got["total"] == 7
    assert run_js("m.sidebarVideos(undefined, 3)") == {"shown": [], "total": 0}
    done_only = [{"path": "drafts/x.md", "next": None}]
    assert run_js(f"m.sidebarVideos({json.dumps(done_only)}, 3)")["shown"] == []   # 다 만든 영상은 목록에서 뺀다


def test_subnav_items():
    refs = run_js('m.subnavItems("#/refs/abc")')
    assert [(i["label"], i["href"], i["current"], i["phoneOnly"]) for i in refs] == [
        ("레퍼런스", "#/refs", True, False), ("공용 라이브러리", "#/refs?tab=library", False, False),
        ("유형", "#/typify", False, True), ("모티브", "#/motif", False, True),
    ]
    assert [i["current"] for i in run_js('m.subnavItems("#/refs?tab=library")')] == [False, True, False, False]
    growth = run_js('m.subnavItems("#/report/review")')
    assert [(i["label"], i["current"], i["phoneOnly"]) for i in growth] == [
        ("오늘 미션", False, True), ("주간 리포트", True, True)]
    assert run_js('m.subnavItems("#/home")') == []
    assert run_js('m.subnavItems("#/draft")') == []
