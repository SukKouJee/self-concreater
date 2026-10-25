"""labels.js: 유형 코드의 한국어 이름, 카메라 최대 비중, 컷 길이·섹션 띠 설명 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts import videomd
from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
LABELS = STATIC_DIR / "labels.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(expr: str):
    code = (
        f"import({json.dumps(LABELS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_imports():
    assert "import " not in LABELS.read_text(encoding="utf-8").replace("import(", "")


def test_names_cover_the_fixed_codes():
    got = run_js("[Object.keys(m.HOOK_KO), Object.keys(m.NARRATIVE_KO), Object.keys(m.CAMERA_KO)]")
    assert got == [videomd.HOOK_TYPES, videomd.NARRATIVE_TYPES, list(videomd.CAMERA_KO)]
    assert run_js("m.CAMERA_KO") == videomd.CAMERA_KO


def test_names_fall_back_to_the_code():
    assert run_js("[m.hookName('result_first'), m.narrativeName('journey'), m.formatName('short')]") == [
        "결과 먼저", "여정", "쇼츠"]
    assert run_js("[m.hookName('my_hook'), m.hookName(''), m.hookName(null), m.hookName(3), m.hookName('toString')]") == [
        "my_hook", "", "", "", "toString"]


def test_top_camera():
    assert run_js("m.topCamera({static: 0.88, pan: 0.12, zoom: 0})") == {"key": "static", "label": "고정", "pct": 88}
    assert run_js("m.topCamera({static: 0.3, pan: 0.6, zoom: 0.1})") == {"key": "pan", "label": "패닝", "pct": 60}
    assert run_js("m.topCamera({static: 0.5, pan: 0.5})")["key"] == "static"      # 같으면 고정 먼저
    assert run_js("[m.topCamera(null), m.topCamera({}), m.topCamera({pan: NaN, zoom: 2}), m.topCamera('x')]") == [
        None, None, None, None]


def test_fmt_cut():
    assert run_js("[m.fmtCut(2.38), m.fmtCut(1.04), m.fmtCut(12.4), m.fmtCut(0), m.fmtCut(-1), m.fmtCut(NaN), m.fmtCut('2')]") == [
        "2.4초", "1초", "12초", "", "", "", ""]


def test_band_caption():
    sections = [
        {"label": "hook", "start": 0, "end": 27}, {"label": "intro", "start": 27, "end": 52.67},
        {"label": "body", "sub": "story", "start": 52.67, "end": 1180}, {"label": "outro", "start": 1180, "end": 1208},
    ]
    assert run_js(f"m.bandCaption({json.dumps(sections)})") == "훅 0:27 · 본문 19:13 · 마무리 0:28"
    assert run_js("m.bandCaption([{label: 'body', start: 0, end: 40}])") == "본문 0:40"
    bad = '[null, {label: "hook", start: NaN, end: 3}, {label: "hook", start: 5, end: 2}, {start: 0, end: 9}]'
    assert run_js(f"m.bandCaption({bad})") == ""
    assert run_js("[m.bandCaption(null), m.bandCaption([])]") == ["", ""]
