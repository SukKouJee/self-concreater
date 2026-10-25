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


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


def test_constants():
    assert run_js("m.STEP_NAMES") == {"draft": "기획안", "polish": "다듬기", "conti": "콘티", "filmed": "촬영함",
                                      "captions": "자막 영상", "diagnose": "진단"}
    assert run_js("m.NEXT_REASON") == {"conti": "기획안을 촬영용 콘티로 바꿔요",
                                       "filmed": "콘티대로 찍었으면 체크해 두세요",
                                       "diagnose": "찍은 영상을 아키타입과 비교해 고칠 점을 찾아요"}
    assert run_js("m.VISIBLE_VIDEOS") == 3


def test_next_button_conti_encodes_path():
    path = "drafts/2026-10-25 부루마블 #1.md"
    r = run_js(f"m.nextButton('conti', {json.dumps(path)})")
    assert r == {"text": "콘티 만들기", "href": "#/conti/drafts%2F2026-10-25%20%EB%B6%80%EB%A3%A8%EB%A7%88%EB%B8%94%20%231.md"}


def test_next_button_diagnose_and_filmed_and_null():
    r = run_js("m.nextButton('diagnose', 'drafts/a b#.md')")
    assert r == {"text": "진단하기", "href": "#/diagnose/drafts%2Fa%20b%23.md"}
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


def test_next_headline():
    assert run_js("m.nextHeadline('부루마블 1탄', 'diagnose')") == "「부루마블 1탄」 진단해 보기"
    assert run_js("m.nextHeadline('A', 'conti')") == "「A」 콘티 만들기"
    assert run_js("m.nextHeadline('A', 'filmed')") == "「A」 찍었으면 촬영함 체크"


def test_stage_line():
    text = "- ref: 19개 / mine: 0개\\n- 현재 단계: 3 (10개 이상) — 정식 유형화\\n- 마지막 단계입니다."
    assert run_js(f"m.stageLine({{stage: 3, ref: 19, stage_text: '{text}'}})") == "레퍼런스 10개 이상 · 3단계"
    assert run_js("m.stageLine({stage: 1, ref: 2, stage_text: ''})") == "레퍼런스 2개 · 1단계"


def test_target_text_seconds_under_a_minute_whole_minutes_from_60():
    assert run_js("[15, 45, 59.4, 59.6, 60, 90, 900, 0, -3, null, NaN].map(m.targetText)") == [
        "15초 목표", "45초 목표", "59초 목표", "1분 목표", "1분 목표", "2분 목표", "15분 목표", "", "", "", ""]
    assert run_js("m.videoMeta({date: '2026-10-25', target_sec: 15})") == "10월 25일 · 15초 목표"


def test_is_folded_only_for_paths_behind_the_more_button():
    vids = "[{path:'a'},{path:'b'},{path:'c'},{path:'d'},{path:'e'}]"
    assert run_js(f"['a','c','d','e','x',null].map(p => m.isFolded({vids}, p))") == [
        False, False, True, True, False, False]
    assert run_js("m.isFolded([], 'a')") is False
