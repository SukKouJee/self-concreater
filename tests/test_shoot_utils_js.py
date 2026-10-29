"""shoot-utils.js 순수 함수 검증: 촬영 모드 버튼 문구, 촬영 팁, 장면 체크 저장(브라우저 저장이 막혀도 동작) (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "shoot-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# In-memory Storage stand-ins for node (no real localStorage there).
FAKES = """
const okStorage = () => { const d = new Map(); return {
  data: d, getItem: (k) => (d.has(k) ? d.get(k) : null), setItem: (k, v) => { d.set(k, String(v)); },
  removeItem: (k) => { d.delete(k); } }; };
const boom = () => { throw new Error("SecurityError"); };
const throwing = { getItem: boom, setItem: boom, removeItem: boom };
const quotaFull = (d = new Map()) => ({ getItem: (k) => (d.has(k) ? d.get(k) : null), setItem: boom, removeItem: boom });
"""


def run_js(body: str):
    code = (
        f"{FAKES}\nimport({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ {body} }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def js(expr: str):
    return run_js(f"return {expr};")


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


def test_main_label_states():
    # not filmed yet -> 찍었어요 ✓ ; filmed, others left -> 다음 장면으로 ; every scene filmed -> 전부 찍었어요!
    assert js("m.mainLabel(new Set(), 0, 3)") == "찍었어요 ✓"
    assert js("m.mainLabel(new Set([1]), 0, 3)") == "찍었어요 ✓"
    assert js("m.mainLabel(new Set([0]), 0, 3)") == "다음 장면으로"
    assert js("m.mainLabel(new Set([0, 1]), 1, 3)") == "다음 장면으로"
    assert js("m.mainLabel(new Set([0, 1, 2]), 2, 3)") == "전부 찍었어요!"
    assert js("m.mainLabel(new Set([0, 1, 2]), 0, 3)") == "전부 찍었어요!"
    assert js("m.mainLabel(new Set([0]), 0, 1)") == "전부 찍었어요!"
    # the last scene filmed but an earlier one left: not done yet
    assert js("m.mainLabel(new Set([2]), 2, 3)") == "다음 장면으로"


def test_next_todo_wraps_to_the_first_scene_left():
    assert js("m.nextTodo(new Set([0]), 0, 3)") == 1
    assert js("m.nextTodo(new Set([0, 1]), 0, 3)") == 2
    assert js("m.nextTodo(new Set([1, 2]), 2, 3)") == 0          # back to the one left at the start
    assert js("m.nextTodo(new Set([0, 1, 2]), 1, 3)") is None
    assert js("m.firstTodo(new Set([0, 1]), 3)") == 2
    assert js("m.firstTodo(new Set([0, 1, 2]), 3)") == 0         # all filmed: open at the start
    assert js("m.firstTodo(new Set(), 0)") == 0


def test_bar_segments_one_per_scene_when_few():
    # filmed green, current white, rest grey; a filmed current scene is "done-now" (green, outlined)
    assert js("m.barSegments(new Set([0]), 1, 4)") == ["done", "now", "todo", "todo"]
    assert js("m.barSegments(new Set([1]), 1, 3)") == ["todo", "done-now", "todo"]
    assert js("m.barSegments(new Set(), 0, 0)") == []


def test_bar_segments_bucket_many_scenes_into_40():
    # an unmerged conti (469 cuts) must not overflow a 390px bar: at most 40 segments
    segs = js("m.barSegments(new Set(), 300, 469)")
    assert len(segs) == 40
    now = segs.index("now")
    assert segs.count("now") == 1 and now == js("Math.floor(300 * 40 / 469)")
    assert set(segs) == {"now", "todo"}
    # a bucket is done only when every scene in it is filmed
    all_but_last = js("m.barSegments(new Set(Array.from({length: 468}, (_, k) => k)), 0, 469)")
    assert all_but_last[0] == "done-now" and all_but_last[-1] == "todo" and all_but_last[1:-1] == ["done"] * 38
    assert js("m.barSegments(new Set(), 468, 469)")[-1] == "now"
    assert js("m.barSegments(new Set(), 0, 40).length") == 40
    assert js("m.barSegments(new Set(), 0, 41).length") == 40


def test_tips_from_guide():
    assert js("m.tipsFrom('삼각대 고정, 풍경만 3초 홀드. 내레이션은 담담한 톤으로')") == [
        "삼각대 고정, 풍경만 3초 홀드", "내레이션은 담담한 톤으로"]
    # at most two; ! and ? end a sentence too; decimals stay whole
    assert js("m.tipsFrom('1.5초 멈춘다. 웃는다! 진짜? 셋째 문장.')") == ["1.5초 멈춘다", "웃는다!"]
    assert js("m.tipsFrom('첫 줄\\n둘째 줄')") == ["첫 줄", "둘째 줄"]
    assert js("m.tipsFrom('  ')") == []
    assert js("m.tipsFrom(null)") == []
    assert js("m.tipsFrom('한 문장뿐.')") == ["한 문장뿐"]


def test_scene_time_and_count_lines():
    assert js("m.sceneTime({start: 45, end: 240})") == "0:45–4:00"
    assert js("m.sceneTime({start: 0, end: 9.6})") == "0:00–0:10"
    assert js("m.sceneTime({})") == ""
    assert js("m.shootCount(2, 6)") == "촬영 2 / 6장면"
    # only ids of scenes still in the conti count
    assert js("m.doneCount(new Set(['a', 'gone']), [{id: 'a'}, {id: 'b'}])") == 1
    assert js("m.doneIndexes(new Set(['b']), [{id: 'a'}, {id: 'b'}])") == [1]


def test_shoot_store_keeps_checks_in_local_storage():
    out = run_js("""
      const s = okStorage();
      const a = m.shootStore('vid/one', s);
      const on = a.toggle('c1'); a.toggle('c2'); const off = a.toggle('c2');
      const again = m.shootStore('vid/one', s).get();
      return { on, off, key: [...s.data.keys()], raw: JSON.parse(s.data.get('ytc.shoot.vid/one')),
               again: [...again], persistent: a.persistent(), other: [...m.shootStore('vid/two', s).get()] };
    """)
    assert out == {"on": True, "off": False, "key": ["ytc.shoot.vid/one"], "raw": ["c1"], "again": ["c1"],
                   "persistent": True, "other": []}


def test_shoot_store_drops_bad_saved_values():
    out = run_js("""
      const s = okStorage();
      s.setItem('ytc.shoot.x', '{not json'); const a = [...m.shootStore('x', s).get()];
      s.setItem('ytc.shoot.y', '[1, "ok", null]'); const b = [...m.shootStore('y', s).get()];
      return { a, b };
    """)
    assert out == {"a": [], "b": ["ok"]}


def test_shoot_store_survives_storage_errors():
    # private window / blocked storage: every access throws (or the localStorage getter itself throws) ->
    # the checks live in memory for this session (another store for the same video sees them), nothing throws
    out = run_js("""
      const a = m.shootStore('v', throwing);
      const on = a.toggle('c1');
      const same = [...m.shootStore('v', throwing).get()];
      const getter = m.shootStore('v', () => { throw new Error('denied'); });
      const viaGetter = [...getter.get()];
      getter.toggle('c2');
      const both = [...m.shootStore('v', throwing).get()].sort();
      return { on, same, viaGetter, both, persistent: a.persistent() };
    """)
    assert out == {"on": True, "same": ["c1"], "viaGetter": ["c1"], "both": ["c1", "c2"], "persistent": False}


def test_shoot_store_keeps_checks_when_writes_fail():
    # storage reads fine but writes throw (quota): the read must not wipe the check kept in memory
    out = run_js("""
      const q = quotaFull();
      const a = m.shootStore('q', q);
      a.toggle('c1');
      return { now: [...a.get()], fresh: [...m.shootStore('q', q).get()], persistent: a.persistent() };
    """)
    assert out == {"now": ["c1"], "fresh": ["c1"], "persistent": False}
