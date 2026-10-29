"""home-utils.js 순수 함수 검증 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "home-utils.js"

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
    assert text.count("import ") == 1 and 'from "./video-utils.js";' in text


def test_constants():
    assert run_js("m.STEP_NAMES") == {"draft": "기획안", "polish": "다듬기", "conti": "콘티", "filmed": "촬영함",
                                      "captions": "자막 영상", "diagnose": "진단"}
    assert run_js("m.VISIBLE_VIDEOS") == 3


def test_home_order_desktop_and_phone():
    # spec 4.1: desktop next -> videos -> mission -> recent; phone next (dark card) -> mission -> videos
    assert run_js("m.HOME_ORDER") == {"desktop": ["next", "videos", "mission", "recent"],
                                      "phone": ["next", "mission", "videos"]}


def test_next_button_conti_encodes_path():
    path = "drafts/2026-10-25 부루마블 #1.md"
    r = run_js(f"m.nextButton('conti', {json.dumps(path)})")
    assert r == {"text": "콘티 만들기", "href": "#/video/2026-10-25%20%EB%B6%80%EB%A3%A8%EB%A7%88%EB%B8%94%20%231/conti"}


def test_next_button_diagnose_and_filmed_and_null():
    r = run_js("m.nextButton('diagnose', 'drafts/a b#.md')")
    assert r == {"text": "진단하기", "href": "#/video/a%20b%23/diagnose"}
    assert run_js("m.nextButton('filmed', 'drafts/a.md')") == {"text": "촬영함 체크", "action": "film"}
    assert run_js("m.nextButton(null, 'drafts/a.md')") is None


def test_step_state_four_cases():
    assert run_js("m.stepState({key: 'conti', done: true, optional: false}, 'filmed')") == "done"
    assert run_js("m.stepState({key: 'polish', done: true, optional: true}, 'conti')") == "done"
    assert run_js("m.stepState({key: 'filmed', done: false, optional: false}, 'filmed')") == "next"
    assert run_js("m.stepState({key: 'diagnose', done: false, optional: false}, 'filmed')") == "todo"
    assert run_js("m.stepState({key: 'captions', done: false, optional: true}, 'diagnose')") == "optional"


def test_step_sr():
    assert run_js("['done', 'next', 'todo', 'optional'].map(m.stepSr)") == ["완료", "다음 단계", "아직", "선택, 아직"]


def test_video_meta():
    assert run_js("m.videoMeta({date: '2026-10-05', target_sec: 900})") == "10월 5일 · 15분 목표"
    assert run_js("m.videoMeta({date: null, target_sec: 45})") == "45초 목표"
    assert run_js("m.videoMeta({date: '2026-01-31', target_sec: null})") == "1월 31일"
    assert run_js("m.videoMeta({date: null, target_sec: null})") == ""


def test_target_text_seconds_under_a_minute_whole_minutes_from_60():
    assert run_js("[15, 45, 59.4, 59.6, 60, 90, 900, 0, -3, null, NaN].map(m.targetText)") == [
        "15초 목표", "45초 목표", "59초 목표", "1분 목표", "1분 목표", "2분 목표", "15분 목표", "", "", "", ""]
    assert run_js("m.videoMeta({date: '2026-10-25', target_sec: 15})") == "10월 25일 · 15초 목표"


def test_is_folded_only_for_paths_behind_the_more_button():
    vids = "[{path:'a'},{path:'b'},{path:'c'},{path:'d'},{path:'e'}]"
    assert run_js(f"['a','c','d','e','x',null].map(p => m.isFolded({vids}, p))") == [
        False, False, True, True, False, False]
    assert run_js("m.isFolded([], 'a')") is False


# 최근 결과물은 파일 이름 대신 영상 제목·날짜로 보인다 (로컬 경로·파일 이름 숨기기)
@pytest.mark.parametrize("item, expected", [
    ({"path": "drafts/2026-10-25-a.md", "kind": "draft"}, "부루마블 1탄"),
    ({"path": "drafts/2026-10-25-a-conti.html", "kind": "conti"}, "부루마블 1탄"),
    ({"path": "drafts/sub/b-conti.html", "kind": "conti"}, "b"),                       # 제목을 모르면 이름에서 확장자를 뗀다
    ({"path": "reports/travel-2026-10-27.html", "kind": "report"}, "travel 리포트 · 10월 27일"),
    ({"path": "reports/odd.html", "kind": "report"}, "odd"),
    ({"path": "drafts/2026-10-20-x-y.md", "kind": "draft"}, "x y"),                          # 앞 날짜는 떼고 - 는 띄어쓰기
])
def test_recent_name(item, expected):
    titles = {"drafts/2026-10-25-a.md": "부루마블 1탄"}
    assert run_js(f"m.recentName({json.dumps(item)}, {json.dumps(titles, ensure_ascii=False)})") == expected


def cat(na, videos=None):
    return json.dumps({"category": "여행", "next_action": na, "videos": videos or []}, ensure_ascii=False)


def test_next_step_follows_the_video_step():
    film = run_js(f"m.nextStep({cat({'kind': 'video', 'path': 'drafts/a b.md', 'step': 'filmed', 'title': '부루마블 1탄'})})")
    assert film == {"title": "부루마블 1탄, 촬영할 차례예요", "why": "촬영 모드로 장면을 하나씩 찍어요",
                    "action": {"label": "촬영 모드 시작", "href": "#/video/a%20b/shoot"}, "film": True, "path": "drafts/a b.md"}
    conti = run_js(f"m.nextStep({cat({'kind': 'video', 'path': 'drafts/a.md', 'step': 'conti', 'title': 'A'})})")
    assert conti["title"] == "A, 콘티 만들 차례예요" and conti["film"] is False
    assert conti["action"] == {"label": "콘티 만들기", "href": "#/video/a/conti"}
    diag = run_js(f"m.nextStep({cat({'kind': 'video', 'path': 'drafts/a.md', 'step': 'diagnose', 'title': 'A'})})")
    assert diag["title"] == "A, 진단할 차례예요"
    assert diag["action"] == {"label": "진단하기", "href": "#/video/a/diagnose"}
    # no title in next_action: the video's own title
    r = run_js(f"m.nextStep({cat({'kind': 'video', 'path': 'drafts/x.md', 'step': 'conti'}, [{'path': 'drafts/x.md', 'title': 'X 제목'}])}).title")
    assert r == "X 제목, 콘티 만들 차례예요"


def test_next_step_category_and_new_video():
    r = run_js(f"m.nextStep({cat({'kind': 'category', 'text': '레퍼런스 3개 더 모으면 유형화를 할 수 있어요', 'menu': 'add'})})")
    assert r == {"title": "레퍼런스 3개 더 모으면 유형화를 할 수 있어요", "why": "카테고리 단계를 이어가요",
                 "action": {"label": "레퍼런스 모으기", "href": "#/add"}, "film": False, "path": None}
    menus = {"typify": ("유형 나누기", "#/typify"), "draft": ("새 영상 시작", "#/motif"),
             "conti": ("내 영상 보기", "#/videos"), "diagnose": ("내 영상 보기", "#/videos")}
    for menu, (label, href) in menus.items():
        got = run_js(f"m.nextStep({cat({'kind': 'category', 'text': 't', 'menu': menu})}).action")
        assert got == {"label": label, "href": href}, menu
    # every video finished -> the category step says so
    r = run_js(f"m.nextStep({cat({'kind': 'category', 'text': 't', 'menu': 'typify'}, [{'path': 'drafts/a.md', 'next': None}])}).why")
    assert r == "만들고 있는 영상은 모두 마쳤어요. 카테고리 단계를 이어가요"
    new = run_js(f"m.nextStep({cat({'kind': 'new_draft'})})")
    assert new == {"title": "다음 영상을 시작할 차례예요", "why": "모티브 카드를 골라 새 영상을 시작해요",
                   "action": {"label": "새 영상 시작", "href": "#/motif"}, "film": False, "path": None}
    assert run_js("m.nextStep({category: 'x'}).action.href") == "#/motif"        # no next_action at all


def test_shoot_progress_counts_checked_scenes_of_the_conti():
    cuts = "[{id:'a'},{id:'b'},{id:'c'}]"
    assert run_js(f"m.shootProgress({cuts}, new Set(['a','c','gone']), false)") == {"done": 2, "total": 3}
    assert run_js(f"m.shootProgress({cuts}, new Set(), true)") == {"done": 3, "total": 3}      # 다 찍음 counts all
    assert run_js("m.shootProgress(null, new Set(['a']), false)") == {"done": 0, "total": 0}


def video(next_key, done):
    keys = ["draft", "polish", "conti", "filmed", "captions", "diagnose"]
    optional = {"polish", "captions"}
    return {"path": "drafts/a.md", "title": "A", "next": next_key,
            "steps": [{"key": k, "done": k in done, "optional": k in optional} for k in keys]}


def test_stage_dots_and_word():
    v = video("filmed", {"draft", "conti"})
    assert run_js(f"m.stageDots({json.dumps(v)})") == ["done", "skip", "done", "now", "todo", "todo"]
    assert run_js(f"m.stageWord({json.dumps(v)})") == "촬영 차례"
    v = video(None, {"draft", "polish", "conti", "filmed", "captions", "diagnose"})
    assert run_js(f"m.stageDots({json.dumps(v)})") == ["done"] * 6
    assert run_js(f"m.stageWord({json.dumps(v)})") == "완료"
    assert run_js(f"m.stageWord({json.dumps(video('conti', {'draft'}))})") == "콘티 차례"


def test_phone_videos_unfinished_first_three():
    vids = [{"path": f"drafts/{i}.md", "next": None if i in (1, 4) else "conti"} for i in range(6)]
    r = run_js(f"m.phoneVideos({json.dumps(vids)})")
    assert [v["path"] for v in r["shown"]] == ["drafts/0.md", "drafts/2.md", "drafts/3.md"] and r["total"] == 6
    assert run_js("m.phoneVideos(null)") == {"shown": [], "total": 0}


def test_mission_view():
    none = run_js("m.missionView(null, {date: '2026-10-29', exists: false})")
    assert none == {"title": "아직 받지 않았어요", "text": "받으면 지금 단계에 맞춘 15~30분 미션이 나와요",
                    "meta": "", "action": "오늘 미션 받기"}
    today = {"title": "훅 3개 써 보기", "status": "open", "minutes": 15, "kind": "write"}
    r = run_js(f"m.missionView({json.dumps(today, ensure_ascii=False)}, {{exists: true}})")
    assert r == {"title": "훅 3개 써 보기", "text": "", "meta": "진행 중 · 15분", "action": "미션 보기"}
    r = run_js("m.missionView({title: null, status: 'submitted', minutes: null}, {exists: true})")
    assert r == {"title": "오늘 미션", "text": "", "meta": "완료", "action": "미션 보기"}
    # /api/mission failed: the home's own mission card (status only)
    r = run_js("m.missionView(undefined, {exists: true, status: 'skipped', kind: 'shoot'})")
    assert r == {"title": "오늘 미션", "text": "", "meta": "건너뜀", "action": "미션 보기"}


@pytest.mark.parametrize("item, expected", [
    ({"path": "drafts/2026-10-25-a.md", "kind": "draft"},
     {"text": "부루마블 1탄 · 기획안 · 10월 29일", "href": "#/video/2026-10-25-a/plan"}),
    ({"path": "drafts/2026-10-25-a-conti.html", "kind": "conti"},
     {"text": "부루마블 1탄 · 콘티 · 10월 29일", "href": "#/video/2026-10-25-a/conti"}),
    ({"path": "reports/travel-2026-10-27.html", "kind": "report"},
     {"text": "travel · 리포트 · 10월 27일", "href": "#/report"}),
    ({"path": "drafts/2026-10-20-x-y.md", "kind": "draft"},
     {"text": "x y · 기획안 · 10월 29일", "href": "#/video/2026-10-20-x-y/plan"}),
])
def test_recent_line_title_kind_date(item, expected):
    titles = {"drafts/2026-10-25-a.md": "부루마블 1탄"}
    it = dict(item, mtime=1793235600)       # 2026-10-29 01:00 UTC: 10월 29일 from UTC-1 to UTC+14 (KST 10:00)
    got = run_js(f"m.recentLine({json.dumps(it)}, {json.dumps(titles, ensure_ascii=False)})")
    assert got == expected


def test_conti_of_draft():
    assert run_js("m.contiOf('drafts/sub/a b.md')") == "drafts/sub/a b-conti.md"
