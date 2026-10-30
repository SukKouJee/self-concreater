"""conti-utils.js 순수 함수 검증 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "conti-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# 섹션 0: a, b / 섹션 1: c / 섹션 2: d, e
CUTS = ("[{id:'a',section:0},{id:'b',section:0},{id:'c',section:1},{id:'d',section:2},{id:'e',section:2}]")


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ const C = {CUTS}; return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


def test_fmt_delta():
    assert run_js("m.fmtDelta(908, 900)") == "목표 15:00 → 지금 15:08 (+0:08)"
    assert run_js("m.fmtDelta(892, 900)") == "목표 15:00 → 지금 14:52 (-0:08)"
    assert run_js("m.fmtDelta(900, 900)") == "목표 15:00 · 지금 15:00"
    assert run_js("m.fmtDelta(61.2, null)") == "지금 1:01"


def test_can_remove():
    assert run_js("['a','b','c','d','e','zz'].map(id => m.canRemove(C, id))") == [True, True, False, True, True, False]


def test_snap_sec():
    assert run_js("[0.3, 601, 4.26, 4.2, 0, 1.75, 600].map(m.snapSec)") == [0.5, 600, 4.5, 4, 0.5, 2, 600]


def test_drop_target_down_one_row_is_old_index_plus_one():
    # gap 2 = right after b (pre-removal slot); a moves from 0 to final index 1, same section
    assert run_js("m.dropTarget(C, 'a', 2)") == {"to": 1, "section": 0}
    assert run_js("m.dropTarget(C, 'a', 1)") == {"to": 0, "section": 0}      # dropping on its own place


def test_drop_target_crosses_section_boundary():
    # a dropped after c (gap 3): final list [b,c,a,d,e] -> index 2, takes c's section
    assert run_js("m.dropTarget(C, 'a', 3)") == {"to": 2, "section": 1}
    # d dropped between b and c (gap 2): section 2 no longer fits there -> follows the previous cut (server rule)
    assert run_js("m.dropTarget(C, 'd', 2)") == {"to": 2, "section": 0}
    assert run_js("m.dropTarget(C, 'd', 2, 1)") == {"to": 2, "section": 1}


def test_drop_target_front_and_back():
    assert run_js("m.dropTarget(C, 'e', 0)") == {"to": 0, "section": 0}
    assert run_js("m.dropTarget(C, 'a', 5)") == {"to": 4, "section": 2}


def test_drop_target_explicit_section_when_it_fits():
    # gap 2 (between b and c) is on a section edge: explicit section decides
    assert run_js("m.dropTarget(C, 'a', 2, 1)") == {"to": 1, "section": 1}
    assert run_js("m.dropTarget(C, 'a', 2, 0)") == {"to": 1, "section": 0}
    assert run_js("m.dropTarget(C, 'a', 2, 2)") == {"to": 1, "section": 0}   # does not fit -> inferred


def test_step_move():
    assert run_js("m.stepMove(C, 'a', -1)") is None
    assert run_js("m.stepMove(C, 'e', 1)") is None
    assert run_js("m.stepMove(C, 'a', 1)") == {"to": 1, "section": 0, "crosses": False}
    assert run_js("m.stepMove(C, 'b', 1)") == {"to": 2, "section": 1, "crosses": True}
    assert run_js("m.stepMove(C, 'd', -1)") == {"to": 2, "section": 1, "crosses": True}


def test_shot_sizes_match_server():
    from scripts.features import SHOT_SIZES
    assert run_js("m.SHOT_SIZES") == SHOT_SIZES


# ---- history panel helpers ----

def test_kind_text_and_version_label():
    assert run_js("m.KIND_TEXT") == {"created": "처음 만듦", "auto": "자동", "named": None,
                                     "before_recreate": "다시 만들기 전",
                                     "before_merge": "합치기 전", "external": "파일 직접 수정", "restored": "되돌림"}
    assert run_js("m.versionLabel({kind:'named',name:'촬영 전'})") == "촬영 전"
    assert run_js("m.versionLabel({kind:'auto',name:null})") == "자동"
    assert run_js("m.versionLabel({kind:'restored',name:'되돌림: 촬영 전'})") == "되돌림: 촬영 전"
    assert run_js("m.versionLabel({kind:'restored',name:null})") == "되돌림"


def test_fmt_when():
    assert run_js("m.fmtWhen('2026-10-27T09:03:05')") == "10/27 09:03"
    assert run_js("m.fmtWhen('nonsense')") == "nonsense"


def test_change_text_six_kinds():
    assert run_js("m.changeText({kind:'changed',n_from:3,n_to:3,field:'line',from:'A',to:'B'})") == "컷 3 대사: 「A」 → 「B」"
    assert run_js("m.changeText({kind:'removed',n_from:7,n_to:null})") == "컷 7 삭제"
    assert run_js("m.changeText({kind:'added',n_from:null,n_to:9}, '본문')") == "컷 9 새로 생김(본문)"
    assert run_js("m.changeText({kind:'added',n_from:null,n_to:9})") == "컷 9 새로 생김"
    assert run_js("m.changeText({kind:'moved',n_from:4,n_to:6})") == "컷 4 → 6으로 옮김"
    assert run_js("m.changeText({kind:'moved',n_from:6,n_to:4})") == "컷 6 → 4로 옮김"
    assert run_js("m.changeText({kind:'changed',n_from:2,n_to:2,field:'sec',from:4,to:6})") == "컷 2 길이 4초 → 6초"
    assert run_js("m.changeText({kind:'total',field:'total_sec',from:900,to:908})") == "총 길이 15:00 → 15:08"


def test_change_text_section_and_edge_values():
    assert run_js("m.changeText({kind:'changed',field:'script',section:1,from:'a',to:'b'})") == "섹션 2 대사 바뀜"
    assert run_js("m.changeText({kind:'changed',field:'script',section:0,from:'a',to:'a'})") == "섹션 1 시간 바뀜"
    assert run_js("m.changeText({kind:'changed',n_from:1,n_to:1,field:'caption',from:'x',to:''})") == "컷 1 자막: 「x」 → (비움)"
    long = "가" * 40
    assert run_js(f"m.changeText({{kind:'changed',n_from:1,n_to:1,field:'guide',from:'{long}',to:'b'}})") == f"컷 1 가이드: 「{'가' * 24}…」 → 「b」"


def test_align_cuts_by_id():
    code = ("m.alignCuts([{id:'a',n:1},{id:'b',n:2},{id:'c',n:3}], [{id:'a',n:1},{id:'c',n:2},{id:'d',n:3}], "
            "[{kind:'changed',n_from:3,n_to:2,field:'line'}]).map(r => [r.status, r.l && r.l.id, r.r && r.r.id, r.fields])")
    assert run_js(code) == [["same", "a", "a", []], ["removed", "b", None, []], ["changed", "c", "c", ["line"]], ["added", None, "d", []]]


def test_align_cuts_moved_and_by_index():
    moved = ("m.alignCuts([{id:'a',n:1},{id:'b',n:2},{id:'c',n:3}], [{id:'b',n:1},{id:'c',n:2},{id:'a',n:3}], "
             "[{kind:'moved',n_from:1,n_to:3}]).map(r => [r.status, r.l.id, r.r.id])")
    assert run_js(moved) == [["same", "b", "b"], ["same", "c", "c"], ["moved", "a", "a"]]
    by_index = ("m.alignCuts([{n:1},{n:2}], [{n:1}], [{kind:'removed',n_from:2,n_to:null}]).map(r => r.status)")
    assert run_js(by_index) == ["same", "removed"]


def test_collapse_changes_merges_time_only_section_runs():
    code = ("m.collapseChanges([{kind:'added',n_from:null,n_to:1},{kind:'total',field:'total_sec',from:1200,to:1204},"
            "{kind:'changed',field:'script',section:0,from:'a',to:'a'},{kind:'changed',field:'script',section:1,from:'b',to:'b'},"
            "{kind:'changed',field:'script',section:2,from:'c',to:'x'},{kind:'changed',field:'script',section:3,from:'d',to:'d'}])"
            ".map(c => c.kind === 'shifted' ? m.changeText(c) : c.kind + ':' + (c.section ?? ''))")
    assert run_js(code) == ["added:", "total:", "뒤 섹션 2개 시간이 밀렸어요 (+0:04)", "changed:2", "뒤 섹션 1개 시간이 밀렸어요 (+0:04)"]


def test_collapse_changes_without_total_has_no_delta():
    code = ("m.collapseChanges([{kind:'changed',field:'script',section:0,from:'a',to:'a'}]).map(m.changeText)")
    assert run_js(code) == ["뒤 섹션 1개 시간이 밀렸어요"]
    assert run_js("m.collapseChanges([{kind:'removed',n_from:2}]).length") == 1


def test_remap_by_cut_id_follows_cuts_after_an_insert():
    # a draft on #2 (id 'y') must stay with 'y' after a cut is inserted before it (y becomes #3)
    expr = ("(() => { const before = {cuts:[{n:1,id:'x'},{n:2,id:'y'},{n:3,id:'z'}]};"
            " const after = {cuts:[{n:1,id:'x'},{n:2,id:'new'},{n:3,id:'y'},{n:4,id:'z'}]};"
            " const map = new Map([[2,'draft-y'],[3,'draft-z'],[9,'gone']]);"
            " m.remapByCutId(map, before, after); return [...map.entries()].sort(); })()")
    assert run_js(expr) == [[3, "draft-y"], [4, "draft-z"]]
    assert run_js("m.idOfN({cuts:[{n:1,id:'x'}]}, 1)") == "x"
    assert run_js("m.idOfN({cuts:[{n:1,id:'x'}]}, 2)") is None


# ---- card board (Phase 3) ----

BOARD = ("[{n:1,id:'a',section:0,label:'hook',action:'시장 입구 걷기',line:'여기가 시장입니다',guide:'눈높이',size:'와이드'},"
         "{n:2,id:'b',section:1,label:'intro',action:'메뉴판',line:'',guide:'탑뷰로',size:'탑뷰',"
         "ref:{frame:'f',match:'content',desc:'국수 그릇'}},"
         "{n:3,id:'c',section:2,label:'body',action:'국수 먹기',line:'맛있다 Wow',guide:'클로즈업',size:'클로즈업'},"
         "{n:4,id:'d',section:3,label:'transition',action:'이동',line:'다음 가게',guide:'걷기',size:'POV'},"
         "{n:5,id:'e',section:4,label:'outro',action:'인사',line:'구독',guide:'바스트',size:'바스트'},"
         "{n:12,id:'f',section:4,label:'cta',action:'손 흔들기',line:'',guide:'',size:'바스트'}]")


def board(expr: str):
    return run_js(f"(() => {{ const B = {BOARD}; return {expr}; }})()")


def test_section_group_of_labels():
    assert run_js("['hook','intro','body','demo','transition','cta','outro','zzz'].map(m.sectionGroupOf)") == [
        "intro", "intro", "main", "main", "main", "end", "end", "main"]


def test_section_groups():
    script = "[{label:'hook'},{label:'intro'},{label:'body'},{label:'transition'},{label:'outro'}]"
    assert run_js(f"m.sectionGroups({script})") == [
        {"key": "intro", "label": "도입", "count": 2}, {"key": "main", "label": "본론", "count": 2},
        {"key": "end", "label": "마무리", "count": 1}]
    # with cuts the count is the number of cuts in the group; empty groups are left out
    assert board("m.sectionGroups([{label:'hook'},{label:'body'}], B.filter(c => c.label === 'hook' || c.label === 'body'))") == [
        {"key": "intro", "label": "도입", "count": 1}, {"key": "main", "label": "본론", "count": 1}]
    assert run_js("m.sectionGroups(null)") == []


def test_filter_cuts_by_section_and_query():
    assert board("m.filterCuts(B, {}).length") == 6
    assert board("m.filterCuts(B, {section: 'all', q: '  '}).length") == 6
    assert board("m.filterCuts(B, {section: 'intro'}).map(c => c.id)") == ["a", "b"]
    assert board("m.filterCuts(B, {section: 'main'}).map(c => c.id)") == ["c", "d"]
    assert board("m.filterCuts(B, {section: 'end'}).map(c => c.id)") == ["e", "f"]
    assert board("m.filterCuts(B, {q: '시장'}).map(c => c.id)") == ["a"]            # action and line
    assert board("m.filterCuts(B, {q: '국수'}).map(c => c.id)") == ["b", "c"]       # photo description too
    assert board("m.filterCuts(B, {q: 'wow'}).map(c => c.id)") == ["c"]             # case-insensitive
    assert board("m.filterCuts(B, {q: '국수 먹기'}).map(c => c.id)") == ["c"]        # every word must match
    assert board("m.filterCuts(B, {q: '12'}).map(c => c.id)") == ["f"]              # a scene number
    assert board("m.filterCuts(B, {q: '#3'}).map(c => c.id)") == ["c"]
    assert board("m.filterCuts(B, {q: '국수', section: 'main'}).map(c => c.id)") == ["c"]
    assert run_js("m.filterCuts(null, {q: 'x'})") == []


def test_ref_caption_three_kinds():
    assert run_js("m.refCaption({frame:'f',match:'content',desc:'시장 골목',title:'영상'})") == "참고 · 시장 골목"
    assert run_js("m.refCaption({frame:'f',match:'composition',desc:'국물',title:'영상'})") == "구도만 참고 · 국물"
    assert run_js("m.refCaption({frame:'f',match:'pinned',desc:null,title:'scene001.jpg'})") == "참고(고정)"
    assert run_js("m.refCaption({frame:'f',match:'pinned',desc:'시장',title:'scene001.jpg'})") == "참고(고정) · 시장"
    assert run_js("m.refCaption({frame:'f',match:'content',desc:null,title:'여행 영상'})") == "참고 · 여행 영상"
    assert run_js("m.refCaption({frame:'f',match:'composition',desc:null,title:null})") == "구도만 참고"
    assert run_js("m.refCaption(null)") == ""


def test_about_sec_and_card_stamp():
    assert run_js("[25, 2.5, 3, 0.5, 61.4, 12.6, 12.5, 2.25].map(m.aboutSec)") == [
        "약 25초", "약 2.5초", "약 3초", "약 0.5초", "약 61초", "약 13초", "약 13초", "약 2.3초"]
    assert run_js("m.cardStamp({n:1,start:0,end:10})") == "01 · 0:00–0:10"
    assert run_js("m.cardStamp({n:123,start:600,end:645})") == "123 · 10:00–10:45"


def test_tag_kind_for_sections():
    assert run_js("['hook','intro','body','demo','transition','cta','outro','zzz'].map(m.tagKind)") == [
        "hook", "intro", "body", "body", "check", "outro", "outro", "body"]


def test_has_repeats_same_section_same_action():
    assert run_js("m.hasRepeats([{section:0,action:'공원 걷기'},{section:0,action:'벤치'},{section:0,action:'공원  걷기!'}])") is True
    assert run_js("m.hasRepeats([{section:0,action:'공원 걷기'},{section:1,action:'공원 걷기'}])") is False
    assert run_js("m.hasRepeats([{section:0,action:'벤치에서 쉬기'},{section:0,action:'벤치에 앉아 쉬며 한마디'}])") is False
    assert run_js("m.hasRepeats([])") is False


def test_describe_hint():
    assert run_js("m.describeHint({frames: 840, described: 0, videos_todo: ['a']})") == {
        "todo": 840, "minutes": 79, "text": "레퍼런스 사진 설명을 만들면 장면에 맞는 사진이 붙어요 (사진 840장, 약 79분)"}
    assert run_js("m.describeHint({frames: 20, described: 18, videos_todo: ['a']})") == {
        "todo": 2, "minutes": 1, "text": "레퍼런스 사진 설명을 만들면 장면에 맞는 사진이 붙어요 (사진 2장, 약 1분)"}
    assert run_js("m.describeHint({frames: 20, described: 20, videos_todo: []})") is None
    assert run_js("m.describeHint({frames: 0, described: 0, videos_todo: []})") is None
    assert run_js("m.describeHint(null)") is None


MERGE = ("{before: 5, after: 3, hash: 'h1', warnings: [],"
         " sections: [{idx:0,label:'hook',split:false,scenes:[{id:'a',merged_ids:['a']}]},"
         "            {idx:1,label:'body',split:true,scenes:[{id:'b',merged_ids:['b','d']},{id:'c',merged_ids:['c','e']}]}],"
         " cuts: [{n:1,id:'a',section:0,action:'훅',sec:3,line:'안녕',size:'바스트'},"
         "        {n:2,id:'c',section:1,action:'먹기',sec:8,line:'맛있다',size:'클로즈업'},"
         "        {n:3,id:'b',section:1,action:'걷기',sec:10,line:'가 보자',size:'와이드'}]}")


def test_merge_view_groups_scenes_by_section():
    assert run_js(f"m.mergeView({MERGE})") == [
        {"idx": 0, "label": "hook", "changed": False, "before": 1,
         "scenes": [{"n": 1, "id": "a", "action": "훅", "sec": 3, "line": "안녕", "size": "바스트"}]},
        {"idx": 1, "label": "body", "changed": True, "before": 4,
         "scenes": [{"n": 2, "id": "c", "action": "먹기", "sec": 8, "line": "맛있다", "size": "클로즈업"},
                    {"n": 3, "id": "b", "action": "걷기", "sec": 10, "line": "가 보자", "size": "와이드"}]}]
    assert run_js("m.mergeView(null)") == []


def test_merge_apply_cuts_send_order_section_line():
    assert run_js(f"m.mergeApplyCuts({MERGE})") == [
        {"id": "a", "section": 0, "line": "안녕"}, {"id": "c", "section": 1, "line": "맛있다"},
        {"id": "b", "section": 1, "line": "가 보자"}]


def test_parse_view():
    assert run_js("['cards','list',null,'zzz',''].map(m.parseView)") == ["cards", "list", "cards", "cards", "cards"]


def test_grid_move_by_rows_from_card_tops():
    # 3 columns: tops 0,0,0,250,250,250,500 (positioned cards report offsetTop 0, so tops come from the layout)
    tops = "[0,0,0,250,250,250,500]"
    assert run_js(f"['ArrowDown','ArrowUp','ArrowRight','ArrowLeft','Home','End'].map(k => m.gridMove({tops}, 1, k))") == [
        4, 1, 2, 0, 0, 6]
    assert run_js(f"m.gridMove({tops}, 4, 'ArrowDown')") == 6            # last row is short: clamp to the last card
    assert run_js(f"m.gridMove({tops}, 5, 'ArrowUp')") == 2
    assert run_js("m.gridMove([0, 0.4, 0.6, 260], 0, 'ArrowDown')") == 3  # sub-pixel tops count as one row
    assert run_js("m.gridMove([0, 200, 400], 0, 'ArrowDown')") == 1      # one column (phone)
    assert run_js("m.gridMove([0, 0], 0, 'Tab')") is None


# ---- scene illustrations: auto redraw plan, what a scene shows, captions, tool button ----------

ART_STEP = """
const S0 = { pending: [], running: false, disabled: false };
const run = (events) => { let st = S0; const out = [];
  for (const e of events) { const r = m.artRedrawPlan(st, e); st = r.state; out.push({ start: r.start, delayMs: r.delayMs }); }
  return { state: st, out }; };
"""


def art_js(body: str):
    return run_js(f"(function(){{ {ART_STEP} {body} }})()")


def test_art_debounce_constant():
    assert run_js("m.ART_DEBOUNCE_MS") == 4000


def test_art_redraw_stale_three_times_then_timer_starts_once():
    r = art_js("return run([{type:'stale',ids:['a']},{type:'stale',ids:['b','a']},{type:'stale',ids:['c']},{type:'timer'}]);")
    assert [o["delayMs"] for o in r["out"][:3]] == [4000, 4000, 4000]
    assert all(o["start"] is None for o in r["out"][:3])
    assert r["out"][3] == {"start": ["a", "b", "c"], "delayMs": None}
    assert r["state"] == {"pending": [], "running": True, "disabled": False}


def test_art_redraw_waits_while_running_then_runs_again_after_done():
    r = art_js("return run([{type:'stale',ids:['a']},{type:'timer'},{type:'stale',ids:['b']},{type:'timer'},"
               "{type:'done'},{type:'timer'}]);")
    assert r["out"][1]["start"] == ["a"]
    assert r["out"][2]["delayMs"] == 4000
    assert r["out"][3] == {"start": None, "delayMs": None}          # running: wait
    assert r["out"][4] == {"start": None, "delayMs": 0}             # done with pending: go now
    assert r["out"][5]["start"] == ["b"]


def test_art_redraw_done_without_pending_is_quiet():
    r = art_js("return run([{type:'stale',ids:['a']},{type:'timer'},{type:'done'},{type:'timer'}]);")
    assert r["out"][2] == {"start": None, "delayMs": None}
    assert r["out"][3]["start"] is None
    assert r["state"]["running"] is False


def test_art_redraw_timer_with_nothing_pending_starts_nothing():
    assert art_js("return run([{type:'timer'}]);")["out"] == [{"start": None, "delayMs": None}]


def test_art_redraw_fail_ready_stops_auto_start():
    r = art_js("return run([{type:'stale',ids:['a']},{type:'timer'},{type:'fail_ready'},{type:'stale',ids:['b']},"
               "{type:'timer'}]);")
    assert r["out"][3] == {"start": None, "delayMs": None}
    assert r["out"][4]["start"] is None
    assert r["state"] == {"pending": [], "running": False, "disabled": True}


def test_art_redraw_leave_drops_pending_but_keeps_running():
    r = art_js("return run([{type:'stale',ids:['a']},{type:'timer'},{type:'stale',ids:['b']},{type:'leave'}]);")
    assert r["state"] == {"pending": [], "running": True, "disabled": False}
    assert r["out"][3] == {"start": None, "delayMs": None}


def test_art_redraw_requeue_keeps_ids_for_after_the_running_job():
    # conti_busy: another drawing job is on this conti; the ids wait for it to end
    r = art_js("return run([{type:'stale',ids:['a']},{type:'timer'},{type:'requeue',ids:['a']},{type:'timer'},"
               "{type:'done'},{type:'timer'}]);")
    assert r["out"][2] == {"start": None, "delayMs": None}
    assert r["out"][3]["start"] is None
    assert r["out"][4]["delayMs"] == 0
    assert r["out"][5]["start"] == ["a"]


def test_art_redraw_does_not_mutate_the_given_state():
    r = art_js("const st = { pending: ['x'], running: false, disabled: false }; m.artRedrawPlan(st, {type:'stale',ids:['y']});"
               " m.artRedrawPlan(st, {type:'timer'}); return st;")
    assert r == {"pending": ["x"], "running": False, "disabled": False}


def test_scene_visual_order():
    art = "{url:'/files/workspace/conti-art/a/c1-1.svg',state:'fresh'}"
    pinned = "{frame:'workspace/frames/p.jpg',match:'pinned'}"
    photo = "{frame:'workspace/frames/q.jpg',match:'content'}"
    assert run_js(f"m.sceneVisual({{art:{art},ref:{pinned}}}).kind") == "pinned"
    assert run_js(f"m.sceneVisual({{art:{art},ref:null}}).kind") == "art"
    assert run_js(f"m.sceneVisual({{art:{art},ref:{photo}}}).kind") == "art"
    assert run_js(f"m.sceneVisual({{art:null,ref:{photo}}}).kind") == "photo"
    assert run_js("m.sceneVisual({art:null,ref:null}).kind") == "sketch"
    assert run_js("m.sceneVisual({}).kind") == "sketch"


def test_art_caption():
    assert run_js("m.artCaption({state:'fresh'}, false)") == "장면 그림"
    assert run_js("m.artCaption({state:'fresh'}, true)") == "장면 그림"
    assert run_js("m.artCaption({state:'stale'}, true)") == "다시 그리는 중…"
    assert run_js("m.artCaption({state:'stale'}, false)") == "설명이 바뀌었어요"


def test_art_eta_minutes():
    # 2장면씩 묶어 3묶음 동시, 묶음 하나 약 30초
    assert run_js("[1, 2, 6, 80, 266, 0].map(m.artEta)") == [1, 1, 1, 7, 23, 1]


def counts(total, missing, stale, fresh):
    return f"{{total:{total},missing:{missing},stale:{stale},fresh:{fresh}}}"


def test_art_buttons_when_nothing_is_drawn():
    assert run_js(f"m.artButtons({counts(80, 80, 0, 0)})") == [
        {"fk": "art", "label": "장면 그림 그리기 (80장면 · 약 7분)", "staleOnly": True}]


def test_art_buttons_when_some_are_drawn():
    assert run_js(f"m.artButtons({counts(266, 200, 6, 60)})") == [
        {"fk": "art", "label": "남은 장면 그리기 (206장면 · 약 18분)", "staleOnly": True},
        {"fk": "art-all", "label": "처음부터 다시 그리기 (266장면 · 약 23분)", "staleOnly": False}]


def test_art_buttons_when_all_are_drawn():
    assert run_js(f"m.artButtons({counts(4, 0, 0, 4)})") == [
        {"fk": "art-all", "label": "처음부터 다시 그리기 (4장면 · 약 1분)", "staleOnly": False}]
    # only old drawings: they count as left to draw
    assert run_js(f"m.artButtons({counts(4, 0, 4, 0)})")[0]["label"] == "남은 장면 그리기 (4장면 · 약 1분)"


def test_art_buttons_when_nothing_can_be_drawn():
    assert run_js(f"m.artButtons({counts(0, 0, 0, 0)})") == []
    assert run_js("m.artButtons(null)") == []
    assert run_js("m.artButtons(undefined)") == []


def test_art_trouble_count():
    left = counts(10, 3, 1, 6)
    # done with failures: what is still left to draw now
    assert run_js(f"m.artTrouble({{status:'done',failed:2,remaining:4}}, {left})") == 4
    # done without failures: no line
    assert run_js(f"m.artTrouble({{status:'done',failed:0,remaining:4}}, {left})") == 0
    # stopped / failed: what is left
    assert run_js(f"m.artTrouble({{status:'stopped'}}, {left})") == 4
    assert run_js(f"m.artTrouble({{status:'failed'}}, {left})") == 4
    # everything got drawn since (or nothing left): no line
    assert run_js(f"m.artTrouble({{status:'stopped'}}, {counts(10, 0, 0, 10)})") == 0
    # no ended job yet (or a new one started)
    assert run_js(f"m.artTrouble(null, {left})") == 0
    # counts not known: the job summary says
    assert run_js("m.artTrouble({status:'done',failed:1,remaining:2}, null)") == 2
    assert run_js("m.artTrouble({status:'stopped'}, null)") == 0


def test_art_redraw_followed_job_not_found_waits_instead_of_looping():
    # conti_busy -> requeue -> the running job is not found (another conti's job, or the request failed):
    # "missed" must not restart at once (delay 0 would POST again and loop); it waits a full ART_DEBOUNCE_MS
    r = art_js("return run([{type:'stale',ids:['a']},{type:'timer'},{type:'requeue',ids:['a']},{type:'missed'},"
               "{type:'timer'}]);")
    assert r["out"][3] == {"start": None, "delayMs": 4000}
    assert r["out"][4]["start"] == ["a"]
    # nothing pending, or auto redraw stopped: missed only clears running
    r = art_js("return run([{type:'stale',ids:['a']},{type:'timer'},{type:'missed'}]);")
    assert r["out"][2] == {"start": None, "delayMs": None} and r["state"]["running"] is False
    r = art_js("return run([{type:'stale',ids:['a']},{type:'timer'},{type:'requeue',ids:['a']},{type:'fail_ready'},"
               "{type:'missed'}]);")
    assert r["out"][4] == {"start": None, "delayMs": None}


def test_art_redraw_scene_edited_again_while_its_job_runs_is_drawn_again():
    # the server reports an already-stale scene again (I-001): each report restarts the wait, and a scene edited while
    # its own job runs stays pending and goes right after that job ends
    r = art_js("return run([{type:'stale',ids:['a']},{type:'stale',ids:['a']},{type:'timer'},{type:'stale',ids:['a']},"
               "{type:'timer'},{type:'done'},{type:'timer'}]);")
    assert [o["delayMs"] for o in r["out"][:2]] == [4000, 4000]
    assert r["out"][2]["start"] == ["a"]
    assert r["out"][3] == {"start": None, "delayMs": 4000}
    assert r["out"][4] == {"start": None, "delayMs": None}          # its job still runs: pending kept
    assert r["out"][5] == {"start": None, "delayMs": 0}
    assert r["out"][6]["start"] == ["a"]


def test_art_busy_only_for_the_job_scenes_or_pending():
    st = "{active:true, ids:['a'], pending:['c']}"
    assert run_js(f"m.artBusy({st}, 'a')") is True
    assert run_js(f"m.artBusy({st}, 'b')") is False                  # another stale scene: not being drawn
    assert run_js(f"m.artBusy({st}, 'c')") is True                   # waits to be drawn again
    # a whole / stale-only job (ids null) may draw any scene
    assert run_js("m.artBusy({active:true, ids:null, pending:[]}, 'b')") is True
    assert run_js("m.artBusy({active:false, ids:null, pending:[]}, 'b')") is False
    assert run_js("m.artBusy({active:false, ids:['a'], pending:['b']}, 'b')") is True
