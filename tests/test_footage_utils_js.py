"""footage-utils.js 순수 함수 검증 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "footage-utils.js"

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


def test_kind_text_covers_every_server_kind():
    assert run_js("m.KIND_TEXT") == {"auto": "찍음", "pinned": "찍음", "review": "확인 필요",
                                     "missing": "남은 컷", "not_filmed": "못 찍음"}


def test_kind_group():
    assert run_js("['auto','pinned','review','missing','not_filmed','x'].map(m.kindGroup)") == \
        ["filmed", "filmed", "review", "remaining", "remaining", "remaining"]


def test_verdict_label_marks_user_fixed():
    assert run_js("m.verdictLabel({kind:'auto', user:false})") == "찍음"
    assert run_js("m.verdictLabel({kind:'pinned', user:true})") == "찍음 · 직접"
    assert run_js("m.verdictLabel({kind:'not_filmed', user:true})") == "못 찍음 · 직접"
    assert run_js("m.verdictLabel({kind:'review'})") == "확인 필요"


def test_summary_line():
    assert run_js("m.summaryLine({summary:{filmed:18, review:3, missing:6, total:27}})") == \
        "찍음 18 · 확인 필요 3 · 남은 컷 6"
    assert run_js("m.summaryLine({})") == ""
    assert run_js("m.summaryLine(null)") == ""


def test_filter_cuts():
    cuts = "[{id:'a',kind:'auto'},{id:'b',kind:'review'},{id:'c',kind:'missing'},{id:'d',kind:'not_filmed'},{id:'e',kind:'pinned'}]"
    assert run_js(f"m.filterCuts({cuts}, 'all').map(c => c.id)") == ["a", "b", "c", "d", "e"]
    assert run_js(f"m.filterCuts({cuts}, 'remaining').map(c => c.id)") == ["c", "d"]
    assert run_js(f"m.filterCuts({cuts}, 'review').map(c => c.id)") == ["b"]
    assert run_js(f"m.filterCuts({cuts}, 'nope').map(c => c.id)") == ["a", "b", "c", "d", "e"]
    assert run_js("m.filterCuts(null, 'all')") == []


def test_fmt_time_and_range():
    assert run_js("[0, 4.2, 65, 3723, -1, null].map(m.fmtTime)") == ["0:00", "0:04", "1:05", "1:02:03", "0:00", "0:00"]
    # start rounds down, end rounds up: the shown range covers the whole span
    assert run_js("m.fmtRange(4.0, 7.2)") == "0:04–0:08"
    assert run_js("m.fmtRange(61.9, 62)") == "1:01–1:02"


def test_score_text():
    assert run_js("['high','medium','low','x'].map(m.scoreText)") == ["확신 높음", "확신 중간", "확신 낮음", ""]


def test_media_and_frame_urls_are_encoded():
    assert run_js("m.mediaUrl('ab-1-2', 'drafts/가 b-conti.md', 1, 2.5)") == \
        "/api/footage/media/ab-1-2?path=drafts%2F%EA%B0%80%20b-conti.md#t=1,2.5"
    assert run_js("m.frameUrl('ab-1-2', 'frames/s000_0.jpg')") == "/api/footage/frame/ab-1-2/frames/s000_0.jpg"
    assert run_js("m.frameUrl('a#b', 'x y/z.jpg')") == "/api/footage/frame/a%23b/x%20y/z.jpg"


def test_folder_line():
    assert run_js("m.folderLine({missing:true, files:0})") == "폴더를 찾을 수 없어요 — 드라이브가 빠졌거나 옮겨졌어요"
    assert run_js("m.folderLine({files:0, processed:0, pending:0, unreadable:0, duration_sec:0})") == "영상 파일 없음"
    assert run_js("m.folderLine({files:5, processed:3, pending:1, unreadable:1, duration_sec:3723})") == \
        "영상 5개 · 처리함 3개(1:02:03) · 새 파일 1개 · 읽을 수 없음 1개"
    assert run_js("m.folderLine({files:2, processed:0, pending:2, unreadable:0, duration_sec:0})") == \
        "영상 2개 · 새 파일 2개"


def job(items):
    return "{status:'running', items:" + items + "}"


def test_job_progress_counts_only_work_done_this_run():
    # a: cached (done, never started) / b: done this run / c: running half way / d: waiting / match
    items = ("[{id:'a',state:'done'},{id:'b',state:'done',started_at:100,finished_at:160},"
             "{id:'c',state:'running',started_at:160,fraction:0.5},{id:'d',state:'waiting'},"
             "{id:'match',state:'waiting'}]")
    p = run_js(f"m.jobProgress({job(items)}, 190)")
    assert p["files"] == 4 and p["done"] == 2
    assert p["current"]["id"] == "c" and p["matching"] is False
    assert p["fraction"] == pytest.approx(0.5)            # (1 + 0.5 + 0) / 3
    assert p["remaining"] == pytest.approx(90)            # 90s elapsed for half the work


def test_job_progress_unknown_early_and_while_matching():
    early = "[{id:'a',state:'running',started_at:100,fraction:0.01},{id:'match',state:'waiting'}]"
    p = run_js(f"m.jobProgress({job(early)}, 110)")
    assert p["remaining"] is None and p["fraction"] == pytest.approx(0.01)
    matching = "[{id:'a',state:'done',started_at:1},{id:'match',state:'running',started_at:5}]"
    p = run_js(f"m.jobProgress({job(matching)}, 20)")
    assert p["matching"] is True and p["remaining"] is None and p["fraction"] == 1
    cached = "[{id:'a',state:'done'},{id:'match',state:'waiting'}]"
    assert run_js(f"m.jobProgress({job(cached)}, 20)")["fraction"] == 1
    assert run_js("m.jobProgress(null, 1)") is None


def test_clip_note():
    assert run_js("m.clipNote({state:'done', message:'다시 압축함'})") == "다시 압축함"
    assert run_js("m.clipNote({state:'done', message:null})") == ""
    assert run_js("m.clipNote({state:'skipped', message:'원본 없음'})") == "건너뜀: 원본 없음"
    assert run_js("m.clipNote({state:'failed', message:'열 수 없음'})") == "실패: 열 수 없음"


def test_suggest_after_picks_cut_filmed_just_before_in_same_file():
    cuts = ("[{id:'a',choice:{fid:'f1',start:1}},{id:'b',choice:{fid:'f1',start:9}},"
            "{id:'c',choice:{fid:'f2',start:2}},{id:'d',choice:null}]")
    assert run_js(f"m.suggestAfter({cuts}, {{fid:'f1', start:5}})") == "a"
    assert run_js(f"m.suggestAfter({cuts}, {{fid:'f1', start:20}})") == "b"
    assert run_js(f"m.suggestAfter({cuts}, {{fid:'f9', start:1}})") == "d"     # unknown file: the last cut
    assert run_js("m.suggestAfter([], {fid:'f1', start:1})") is None
