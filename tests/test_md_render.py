"""parseMarkdown (scripts/ui/static/md.js) is pure data, so it is exercised under node without a DOM."""
import json
import shutil
import subprocess
import time

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = """
import {parseMarkdown} from %s;
const t0 = Date.now();
const out = parseMarkdown(process.env.MD_IN);
console.log(JSON.stringify({out, ms: Date.now() - t0}));
""" % json.dumps((STATIC_DIR / "md.js").as_uri())


def parse(text: str):
    import os
    env = dict(os.environ, MD_IN=text)
    done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["out"]


def plain(inlines) -> str:
    """Visible text of an inline list (br -> newline)."""
    parts = []
    for n in inlines:
        if n["t"] == "br":
            parts.append("\n")
        elif "v" in n:
            parts.append(n["v"])
        else:
            parts.append(plain(n["c"]))
    return "".join(parts)


def types(inlines):
    return [n["t"] for n in inlines]


def test_headings_keep_their_level():
    blocks = parse("# a\n### b\n###### c\n####### not")
    assert [(b["t"], b.get("level")) for b in blocks] == [("heading", 1), ("heading", 3), ("heading", 6), ("p", None)]
    assert plain(blocks[1]["c"]) == "b"


def test_nested_list_with_quote_inside_item():
    md = "- 이전 훅 대본:\n  - 하나\n    > 자막: 안녕\n  - 둘\n- 다음"
    (ul,) = parse(md)
    assert ul["t"] == "ul" and len(ul["items"]) == 2
    first = ul["items"][0]
    assert plain(first[0]["c"]) == "이전 훅 대본:"
    inner = first[1]
    assert inner["t"] == "ul" and len(inner["items"]) == 2
    assert inner["items"][0][1]["t"] == "quote"
    assert plain(inner["items"][0][1]["c"][0]["c"]) == "자막: 안녕"


def test_ordered_list_start_and_kind_switch():
    blocks = parse("3. a\n4. b\n- c")
    assert [(b["t"], len(b["items"])) for b in blocks] == [("ol", 2), ("ul", 1)]
    assert blocks[0]["start"] == 3


def test_table_header_cells_ragged_rows_and_escaped_pipe():
    md = "| 구간 | 내용 |\n|:--|--:|\n| 훅 | a \| b |\n| 본론 |\n| x | y | z |"
    (tb,) = parse(md)
    assert tb["t"] == "table"
    assert [plain(c) for c in tb["head"]] == ["구간", "내용", ""]
    assert [plain(c) for c in tb["rows"][0]] == ["훅", "a | b", ""]
    assert [plain(c) for c in tb["rows"][1]] == ["본론", "", ""]
    assert [plain(c) for c in tb["rows"][2]] == ["x", "y", "z"]


def test_pipe_line_without_delimiter_row_is_a_paragraph():
    (p,) = parse("a | b\nc | d")
    assert p["t"] == "p"


def test_fenced_code_is_verbatim():
    blocks = parse("```js\n**not bold** # not heading\n- not list\n```\nafter")
    assert blocks[0] == {"t": "code", "lang": "js", "v": "**not bold** # not heading\n- not list"}
    assert blocks[1]["t"] == "p"


def test_unclosed_fence_runs_to_end():
    (code,) = parse("```\nabc\ndef")
    assert code["v"] == "abc\ndef"


def test_quote_and_hr():
    blocks = parse("> 인용 **굵게**\n> 둘째\n\n---\n***")
    assert blocks[0]["t"] == "quote"
    assert "strong" in types(blocks[0]["c"][0]["c"])
    assert [b["t"] for b in blocks[1:]] == ["hr", "hr"]


def test_inline_marks():
    (p,) = parse("가 **굵게** 와 *기울임* 그리고 `a **b**` 끝")
    c = p["c"]
    assert types(c) == ["text", "strong", "text", "em", "text", "code", "text"]
    assert c[5]["v"] == "a **b**"


def test_unclosed_marks_stay_literal():
    (p,) = parse("앞 **닫히지 않음 *도 `도 [링크")
    assert plain(p["c"]) == "앞 **닫히지 않음 *도 `도 [링크"
    assert set(types(p["c"])) == {"text"}


def test_lines_are_kept_as_line_breaks():
    (p,) = parse("자막: 하나\n자막: 둘\n자막: 셋")
    assert types(p["c"]) == ["text", "br", "text", "br", "text"]


def test_ref_link_to_video_md():
    (p,) = parse("[abc_-12](videos/abc_-12.md) [x](./videos/y.md) [z](../videos/z.md)")
    refs = [n for n in p["c"] if n["t"] == "ref"]
    assert [r["id"] for r in refs] == ["abc_-12", "y", "z"]
    assert plain(refs[0]["c"]) == "abc_-12"


def test_youtube_links_are_external():
    md = ("[a](https://www.youtube.com/watch?v=1) [b](https://youtu.be/1) "
          "[c](https://m.youtube.com/x) [d](https://youtube.com/x)")
    (p,) = parse(md)
    links = [n for n in p["c"] if n["t"] == "link"]
    assert len(links) == 4
    assert links[1]["href"] == "https://youtu.be/1"


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "data:text/html,x", "http://www.youtube.com/x", "https://evil.example/x",
    "https://youtube.com.evil.example/x", "//youtube.com/x", "/etc/passwd", "videos/x.txt", "JaVaScRiPt:alert(1)",
])
def test_rejected_links_show_label_and_url_as_text(href):
    (p,) = parse(f"앞 [라벨]({href}) 뒤")
    assert set(types(p["c"])) == {"text"}
    assert plain(p["c"]) == f"앞 라벨 ({href}) 뒤"


def test_escapes_are_literal():
    blocks = parse("\# 제목 아님\n\n\- 목록 아님\n\n\*별\* a \| b")
    assert [b["t"] for b in blocks] == ["p", "p", "p"]
    assert plain(blocks[0]["c"]) == "# 제목 아님"
    assert plain(blocks[1]["c"]) == "- 목록 아님"
    assert plain(blocks[2]["c"]) == "*별* a | b"


def test_html_is_text():
    (p,) = parse('<script>alert(1)</script> <img src=x onerror=y> **b**')
    assert plain(p["c"]).startswith("<script>alert(1)</script> <img src=x onerror=y> ")
    assert "strong" in types(p["c"])


def test_empty_and_whitespace_input():
    assert parse("") == []
    assert parse("\n \n\t\n") == []


def test_crlf_input():
    blocks = parse("# a\r\n\r\n- b\r\n- c\r\n")
    assert [b["t"] for b in blocks] == ["heading", "ul"]


def test_deep_nesting_is_capped_not_crashing():
    md = "\n".join("  " * i + "- n" + str(i) for i in range(60))
    assert parse(md)[0]["t"] == "ul"


@pytest.mark.parametrize("unit", [
    "**a ", "***a ", "_a ", "[", "`", "*x ", "- a\n", "> b\n", "| a | b |\n", "[a](", "[a](b) ", "  - n\n", "```\n",
])
def test_large_input_finishes_within_a_second(unit):
    text = (unit * (100_000 // len(unit)))[:100_000]
    import os
    t0 = time.monotonic()
    done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT], env=dict(os.environ, MD_IN=text),
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["ms"] < 1000, (unit, time.monotonic() - t0)


def test_real_draft_markdown_sample():
    md = ("### 0:00-0:05 훅\n자막: 이게 된다고?\n- 샷: 클로즈업\n\n| 샷 | 크기 |\n|---|---|\n| 1 | 클로즈업 |\n\n"
          "## 패키징\n- 훅 후보\n  - 이전 훅 대본:\n    > 자막: 옛날 훅\n")
    blocks = parse(md)
    assert [b["t"] for b in blocks] == ["heading", "p", "ul", "table", "heading", "ul"]


def test_unbalanced_link_openers_finish_within_a_second():
    import os
    text = "[a](" * 20000 + ")" * 20000
    done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT], env=dict(os.environ, MD_IN=text),
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["ms"] < 1000


def test_underscore_emphasis_with_word_boundaries():
    (p,) = parse("앞 _기울임_ 뒤")
    assert types(p["c"]) == ["text", "em", "text"] and plain(p["c"]) == "앞 기울임 뒤"
    for literal in ["a_b_c", "abc_-12", "snake_case_name", "_열림만", "x _ y _ z", "__두개__"]:
        (p,) = parse(literal)
        assert set(types(p["c"])) == {"text"} and plain(p["c"]) == literal, literal


def test_strong_and_triple_caches_are_independent():
    (p,) = parse("***a **b** c")
    assert "strong" in types(p["c"])


def test_video_link_variants():
    (p,) = parse("[a](../refs/x/videos/a00.md) [b](/videos/b11.md)")
    assert [n["id"] for n in p["c"] if n["t"] == "ref"] == ["a00", "b11"]
    for bad in ["https://evil.example/videos/a.md", "//evil.example/videos/a.md", "ftp://h/videos/a.md"]:
        (p,) = parse(f"[a]({bad})")
        assert "ref" not in types(p["c"]), bad


RENDER = """
class N {
  constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  set className(v) { this.attrs.class = v; }
  append(...kids) { for (const k of kids) { if (k instanceof N && k.tag === "#f") this.kids.push(...k.kids); else this.kids.push(k); } }
  json() { return { tag: this.tag, attrs: this.attrs, kids: this.kids.map((k) => (typeof k === "string" ? k : k.json())) }; }
}
globalThis.document = { createElement: (t) => new N(t), createDocumentFragment: () => new N("#f") };
const { renderMarkdown } = await import(%s);
console.log(JSON.stringify(renderMarkdown(process.env.MD_IN, JSON.parse(process.env.MD_OPTS || "{}")).json()));
""" % json.dumps((STATIC_DIR / "md.js").as_uri())


def render(text: str, opts: dict | None = None):
    import os
    env = dict(os.environ, MD_IN=text, MD_OPTS=json.dumps(opts or {}))
    done = subprocess.run([NODE, "--input-type=module", "-e", RENDER], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["kids"]


def find(nodes, tag):
    found = []
    for n in nodes:
        if isinstance(n, dict):
            if n["tag"] == tag:
                found.append(n)
            found += find(n["kids"], tag)
    return found


def test_render_links_and_attributes():
    kids = render("[y](https://youtu.be/x) [r](videos/a%2000.md) [b](javascript:alert(1))")
    ext, ref = find(kids, "a")
    assert ext["attrs"] == {"href": "https://youtu.be/x", "target": "_blank", "rel": "noopener noreferrer"}
    assert ref["attrs"] == {"href": "#/refs/a%2000"}
    assert len(find(kids, "a")) == 2


def test_render_heading_levels_are_clamped_to_h3_h6():
    kids = render("# a\n\n## b\n\n#### c\n\n###### d")
    assert [k["tag"] for k in kids] == ["h3", "h3", "h4", "h6"]


def test_render_table_cell_keeps_literal_pipe():
    kids = render("| a | b |\n|---|---|\n| x \| y | z |")
    td = find(kids, "td")[0]
    assert td["kids"] == ["x | y"]
    assert find(kids, "th")[0]["attrs"] == {"scope": "col"}


def test_render_drops_leading_title_heading_only_when_it_matches():
    kids = render("# 내 기획안\n본문", {"dropTitle": "내 기획안"})
    assert [k["tag"] for k in kids] == ["p"]
    kids = render("# 다른 제목\n본문", {"dropTitle": "내 기획안"})
    assert [k["tag"] for k in kids] == ["h3", "p"]
    kids = render("### 내 기획안\n본문", {"dropTitle": "내 기획안"})
    assert kids[0]["tag"] == "h3"


def test_route_links_are_opt_in():
    text = "[홈](#/home) [x](#/draft/view/a%20b) [bad](#/a<script>)"
    plain_kids = render(text)
    assert find(plain_kids, "a") == []                     # default: plain text "label (#/..)"
    kids = render(text, {"routeLinks": True})
    links = find(kids, "a")
    assert [a["attrs"] for a in links] == [{"href": "#/home"}, {"href": "#/draft/view/a%20b"}]
