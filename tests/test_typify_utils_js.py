"""typify-utils.js 순수 함수 검증 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "typify-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# travel 사본의 A1 아키타입과 같은 값
A1 = """{
  id: 'A1', size: 18, skeleton: 'hook → intro → body/story → outro',
  mean_ratio: { hook: 0.03, intro: 0.05, body: 0.87, demo: 0.0, transition: 0.01, cta: 0.0, outro: 0.03 },
  mean_duration: 1605.67,
  hook_type: { result_first: 8, shock_line: 1, situation: 4, question: 5 },
  narrative_type: { journey: 16, problem_solution: 2 },
  pacing: { avg_cut_sec: 3.16, by_section: { hook: 2.89, body: 3.13 } },
  camera: { static: 0.86, pan: 0.1, zoom: 0.04 },
  shots: { top_sizes: [['와이드', 0.49], ['POV', 0.24], ['클로즈업', 0.15]],
           face: 0.15, hands_only: 0.09, broll: 0.66, text_overlay: 0.75 },
}"""

LABELS = ["훅", "서사", "컷 속도", "카메라", "샷", "화면 요소", "평균 길이"]


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ const A1 = {A1}; return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_profile_rows_full_archetype():
    assert run_js("m.profileRows(A1)") == [
        {"label": "훅", "value": "결과 먼저 44% · 질문형 28%"},
        {"label": "서사", "value": "여정 89% · 문제 해결 11%"},
        {"label": "컷 속도", "value": "평균 3.2초 · 훅 2.9초 · 본문 3.1초"},
        {"label": "카메라", "value": "고정 86% · 패닝 10%"},
        {"label": "샷", "value": "와이드 49% · POV 24% · 클로즈업 15%"},
        {"label": "화면 요소", "value": "얼굴 15% · 손만 9% · B롤 66% · 자막 75%"},
        {"label": "평균 길이", "value": "26분 46초"},
    ]


def test_profile_rows_keep_every_line_with_dash_when_empty():
    rows = run_js("m.profileRows({ id: 'A9', size: 2 })")
    assert [r["label"] for r in rows] == LABELS
    assert all(r["value"] == "—" for r in rows)
    rows = run_js("m.profileRows({ hook_type: {}, pacing: { by_section: {} }, camera: { static: 0, pan: 0, zoom: 0 },"
                  " shots: { top_sizes: [] }, mean_duration: null })")
    assert all(r["value"] == "—" for r in rows)


def test_profile_rows_edge_values():
    # 카메라는 5% 미만이어도 가장 큰 것 하나는 남긴다; 컷 속도는 구간만 있어도 나온다; 1분 미만 길이
    rows = run_js("m.profileRows({ camera: { static: 0.03, pan: 0.01 }, pacing: { by_section: { hook: 2 } },"
                  " mean_duration: 45, hook_type: { question: 3 } })")
    got = {r["label"]: r["value"] for r in rows}
    assert got["카메라"] == "고정 3%"
    assert got["컷 속도"] == "훅 2초"
    assert got["평균 길이"] == "45초"
    assert got["훅"] == "질문형 100%"


def test_ratio_legend():
    assert run_js("m.ratioLegend(A1.mean_ratio, A1.skeleton)") == [
        {"label": "hook", "name": "훅", "pct": 3},
        {"label": "intro", "name": "인트로", "pct": 5},
        {"label": "body", "name": "본문(이야기)", "pct": 87},
        {"label": "transition", "name": "전환", "pct": 1},
        {"label": "outro", "name": "아웃트로", "pct": 3},
    ]
    assert run_js("m.legendText(m.ratioLegend(A1.mean_ratio, A1.skeleton))") == \
        "훅 3% · 인트로 5% · 본문(이야기) 87% · 전환 1% · 아웃트로 3%"
    # 1% 미만은 생략, 값 없으면 빈 목록
    assert run_js("m.ratioLegend({ hook: 0.004, body: 0.996 }, '')") == [{"label": "body", "name": "본문", "pct": 100}]
    assert run_js("m.ratioLegend(null, '')") == []


def test_outlier_axes_text():
    assert run_js("m.outlierText({ axes: ['pacing'] })") == "컷 속도에서 혼자 떨어짐"
    assert run_js("m.outlierText({ axes: ['pacing', 'shots'] })") == "컷 속도·샷에서 혼자 떨어짐"
    assert run_js("m.outlierText({ axes: [] })") == "일부 축에서 혼자 떨어짐"
    assert run_js("m.AXIS_NAMES") == {
        "structure": "구조", "hook": "훅", "narrative": "서사", "title": "제목", "pacing": "컷 속도", "shots": "샷"}
