"""ui.js 공통 컴포넌트 검증: 작은 가짜 document로 만든 요소의 속성·클래스·키보드 동작을 본다 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
KIT = STATIC_DIR / "ui.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# Just enough DOM for dom.js h() and ui.js: elements keep attributes, children, listeners and focus.
FAKE_DOM = r"""
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.listeners = {}; this.className = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...kids) { for (const k of kids) this.children.push(typeof k === "string" ? { text: k } : k); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, extra = {}) {
    const ev = { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, target: this, ...extra };
    for (const fn of this.listeners[type] || []) fn(ev);
    return ev;
  }
  focus() { globalThis.document.activeElement = this; }
  get textContent() { return this.children.map((c) => (c instanceof El ? c.textContent : c.text)).join(""); }
}
globalThis.document = { createElement: (t) => new El(t), activeElement: null };
const dump = (el) => ({ tag: el.tagName, cls: el.className, attrs: el.attrs, text: el.textContent });
"""


def run_js(body: str):
    code = (
        FAKE_DOM
        + f"import({json.dumps(KIT.as_uri())}).then(async (m) => {{ const out = await (async () => {{ {body} }})(); "
        "console.log(JSON.stringify(out)); }).catch((e) => { console.error(e); process.exit(1); });"
    )
    out = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_only_imports_dom_helper():
    text = KIT.read_text(encoding="utf-8")
    imports = [line for line in text.splitlines() if line.startswith("import ")]
    assert imports == ['import { h } from "./dom.js";']
    for banned in ["innerHTML", "outerHTML", "insertAdjacentHTML", "confirm(", "alert(", "prompt("]:
        assert banned not in text, banned


@pytest.mark.parametrize("size, variant, expected", [
    ("m", "outline", "btn"),
    (None, None, "btn"),
    ("s", "outline", "btn btn-s"),
    ("m", "rec", "btn btn-rec"),
    ("m", "primary", "btn btn-primary"),
    ("s", "ghost", "btn btn-s btn-ghost"),
    ("m", "link", "btn btn-link"),
    ("x", "nope", "btn"),                     # 모르는 값은 기본 모양
])
def test_button_class(size, variant, expected):
    got = run_js(f"return m.buttonClass({json.dumps(size)}, {json.dumps(variant)});")
    assert got == expected


def test_button_builds_a_typed_button_and_runs_on_click():
    got = run_js("""
      let clicks = 0;
      const b = m.button("저장", { size: "s", variant: "primary", onClick: () => { clicks += 1; }, "aria-describedby": "x" });
      b.fire("click");
      return [dump(b), clicks];
    """)
    el, clicks = got
    assert el["tag"] == "BUTTON" and el["text"] == "저장" and el["cls"] == "btn btn-s btn-primary"
    assert el["attrs"]["type"] == "button" and el["attrs"]["aria-describedby"] == "x"
    assert clicks == 1


def test_chip_is_a_toggle_button_with_optional_count():
    got = run_js("""
      const a = m.chip("도입", { pressed: true, count: 3 });
      const b = m.chip("전체", {});
      return [dump(a), dump(b), a.children.length, b.children.length];
    """)
    a, b, a_kids, b_kids = got
    assert a["tag"] == "BUTTON" and a["attrs"]["aria-pressed"] == "true" and a["attrs"]["type"] == "button"
    assert a["cls"] == "ui-chip" and a["text"] == "도입3" and a_kids == 2
    assert b["attrs"]["aria-pressed"] == "false" and b["text"] == "전체" and b_kids == 1


@pytest.mark.parametrize("kind", ["flow", "tech", "angle", "combo", "hook", "intro", "body", "check", "outro"])
def test_tag_kinds(kind):
    got = run_js(f"return dump(m.tag('라벨', {json.dumps(kind)}));")
    assert got["tag"] == "SPAN" and got["cls"] == f"ui-tag ui-tag-{kind}" and got["text"] == "라벨"


def test_tag_unknown_kind_is_plain():
    assert run_js("return dump(m.tag('라벨', 'zzz')).cls;") == "ui-tag"


def test_card_wraps_children():
    got = run_js("const c = m.card('가', m.tag('나', 'flow')); return [dump(c), c.children.length];")
    assert got[0]["cls"] == "ui-card" and got[0]["text"] == "가나" and got[1] == 2


@pytest.mark.parametrize("i, key, n, expected", [
    (0, "ArrowRight", 4, 1), (3, "ArrowRight", 4, 0),          # 끝에서 처음으로 돈다
    (0, "ArrowLeft", 4, 3), (2, "ArrowLeft", 4, 1),
    (2, "Home", 4, 0), (1, "End", 4, 3),
    (1, "Enter", 4, None), (0, "ArrowDown", 4, None),           # 가로 탭: 위아래·Enter는 이동하지 않는다
])
def test_next_tab_index(i, key, n, expected):
    assert run_js(f"return m.nextTabIndex({i}, {json.dumps(key)}, {n});") == expected


TABS = '[{ key: "plan", label: "기획안" }, { key: "conti", label: "콘티" }, { key: "shoot", label: "촬영 체크" }]'


def test_tabs_roles_selection_and_roving_tabindex():
    got = run_js(f"""
      const t = m.tabs({TABS}, "conti", () => {{}}, {{ label: "작업 탭" }});
      return [dump(t), t.children.map(dump)];
    """)
    root, kids = got
    assert root["attrs"]["role"] == "tablist" and root["attrs"]["aria-label"] == "작업 탭" and root["cls"] == "ui-tabs"
    assert [k["attrs"]["role"] for k in kids] == ["tab"] * 3
    assert [k["attrs"]["aria-selected"] for k in kids] == ["false", "true", "false"]
    assert [k["attrs"]["tabindex"] for k in kids] == ["-1", "0", "-1"]
    assert [k["text"] for k in kids] == ["기획안", "콘티", "촬영 체크"]
    assert all(k["attrs"]["type"] == "button" and k["cls"] == "ui-tab" for k in kids)


def test_tabs_click_and_arrow_keys_select_and_report():
    got = run_js(f"""
      const seen = [];
      const t = m.tabs({TABS}, "plan", (key) => seen.push(key));
      const [a, b, c] = t.children;
      c.fire("click");
      const afterClick = t.children.map((k) => k.attrs["aria-selected"]);
      const ev = c.fire("keydown", {{ key: "ArrowRight" }});
      const focused = document.activeElement === a;
      a.fire("click");                               // 이미 고른 탭: 다시 알리지 않는다
      const ignored = b.fire("keydown", {{ key: "Enter" }});
      return [seen, afterClick, ev.defaultPrevented, focused, ignored.defaultPrevented,
              t.children.map((k) => k.attrs.tabindex)];
    """)
    seen, after_click, prevented, focused, ignored_prevented, tabindex = got
    assert seen == ["shoot", "plan"]
    assert after_click == ["false", "false", "true"]
    assert prevented is True and focused is True and ignored_prevented is False
    assert tabindex == ["0", "-1", "-1"]


def test_every_static_module_parses():
    bad = []
    for path in sorted(STATIC_DIR.rglob("*.js")):
        out = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=30)
        if out.returncode != 0:
            bad.append((path.name, out.stderr.strip().splitlines()[-1:]))
    assert bad == []
