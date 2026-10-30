import pytest

from scripts import conti, conti_art
from scripts.common import ScriptError

SVG_NS = "http://www.w3.org/2000/svg"
CUT = {"action": "컵라면 클로즈업", "guide": "천천히", "size": "클로즈업", "line": "안녕", "caption": "c", "sfx": "s", "sec": 2}


def test_basis_changes_only_on_art_fields():
    base = conti_art.basis(CUT, "16:9")
    assert len(base) == 8 and int(base, 16) >= 0
    for key, value in (("line", "다른 대사"), ("caption", "x"), ("sfx", "y"), ("sec", 9)):
        assert conti_art.basis({**CUT, key: value}, "16:9") == base
    for key, value in (("action", "다른 행동"), ("guide", "빠르게"), ("size", "풀샷")):
        assert conti_art.basis({**CUT, key: value}, "16:9") != base
    assert conti_art.basis(CUT, "9:16") != base
    spaced = {**CUT, "action": "  컵라면   클로즈업 "}
    assert conti_art.basis(spaced, "16:9") == base


def test_aspect_follows_format():
    assert conti_art.aspect({"format": "shorts"}) == "9:16"
    assert conti_art.aspect({"format": "long"}) == "16:9"
    assert conti_art.aspect({}) == "16:9"


def test_art_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    assert conti_art.art_state(CUT, "16:9") is None
    rel = "workspace/conti-art/d/1.svg"
    cut = {**CUT, "art": {"file": rel, "basis": conti_art.basis(CUT, "16:9")}}
    assert conti_art.art_state(cut, "16:9") is None  # 파일 없음
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text("<svg/>", encoding="utf-8")
    assert conti_art.art_state(cut, "16:9") == "fresh"
    assert conti_art.art_state({**cut, "action": "바뀐 행동"}, "16:9") == "stale"


def test_display_art_hidden_when_pinned(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    rel = "workspace/conti-art/d/1.svg"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("<svg/>", encoding="utf-8")
    cut = {**CUT, "art": {"file": rel, "basis": conti_art.basis(CUT, "16:9")}}
    assert conti_art.display_art(cut, "16:9") == {"file": rel, "state": "fresh"}
    assert conti_art.display_art({**cut, "ref_frame": "a/b.jpg"}, "16:9") is None
    assert conti_art.display_art({**cut, "ref_frame": ""}, "16:9") == {"file": rel, "state": "fresh"}
    assert conti_art.display_art(CUT, "16:9") is None


def test_art_fields_match_pick_fields():
    assert conti_art.ART_FIELDS == conti.PICK_FIELDS


DANGEROUS = f"""<svg xmlns="{SVG_NS}" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1600 900" onload="alert(1)">
<script>alert(1)</script>
<image href="http://evil/x.png"/>
<foreignObject><div>hi</div></foreignObject>
<style>rect{{fill:red}}</style>
<filter id="f"/>
<use href="http://x"/>
<rect width="10" height="10" style="fill:url(http://x)" onclick="x()"/>
<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient>
<marker id="m"><path d="M0 0L1 1"/></marker></defs>
<g id="a"><circle r="3" style="fill:#e5484d"/></g>
<use href="#a"/>
<use xlink:href="#a"/>
<rect fill="url(#g)" width="5" height="5"/>
</svg>"""


def test_sanitize_strips_dangerous():
    out = conti_art.sanitize_svg(DANGEROUS, "16:9")
    assert isinstance(out, str)
    for bad in ("script", "onload", "onclick", "image", "foreignObject", "<style", "filter", "http://x", "evil", "ns0:"):
        assert bad not in out
    assert 'href="#a"' in out
    assert "marker" in out
    assert "fill:#e5484d" in out
    assert "url(#g)" in out
    assert f'xmlns="{SVG_NS}"' in out


@pytest.mark.parametrize("text", [
    "not xml at all",
    "<html><body/></html>",
    "<g/>",
    "<!DOCTYPE svg [<!ENTITY a 'b'>]><svg xmlns='http://www.w3.org/2000/svg'>&a;</svg>",
    "<!doctype svg><svg xmlns='http://www.w3.org/2000/svg'/>",
    "<svg xmlns='http://evil/ns'/>",
    "<svg xmlns='http://www.w3.org/2000/svg'>" + "<g/>" * 20000 + "</svg>",
], ids=["not_xml", "html_root", "g_root", "entity", "doctype_lower", "foreign_ns", "too_big"])
def test_sanitize_rejects(text):
    with pytest.raises(ScriptError) as exc:
        conti_art.sanitize_svg(text, "16:9")
    assert exc.value.code == "invalid_svg"


def test_sanitize_forces_viewbox():
    out = conti_art.sanitize_svg(f'<svg xmlns="{SVG_NS}" width="100" height="50"><rect width="1" height="1"/></svg>', "16:9")
    assert 'viewBox="0 0 1600 900"' in out
    assert 'width="100"' not in out and 'height="50"' not in out
    out = conti_art.sanitize_svg(f'<svg xmlns="{SVG_NS}" viewBox="0 0 10 10"/>', "9:16")
    assert 'viewBox="0 0 900 1600"' in out and "0 0 10 10" not in out
    # xmlns 없는 입력도 기본 네임스페이스가 붙는다
    assert f'xmlns="{SVG_NS}"' in conti_art.sanitize_svg("<svg/>", "16:9")


def test_sanitize_drops_obfuscated_urls():
    out = conti_art.sanitize_svg(
        f'<svg xmlns="{SVG_NS}"><rect style="fill:URL( http://x)"/><rect fill="u\\72l(http://y)"/>'
        f'<a href="javascript:alert(1)"/></svg>', "16:9")
    body = out.replace(SVG_NS, "")
    assert "http" not in body and "javascript" not in body


def _svg(inner: str, attrs: str = "") -> str:
    return f'<svg xmlns="{SVG_NS}" xmlns:xlink="http://www.w3.org/1999/xlink"{attrs}>{inner}</svg>'


def test_sanitize_css_function_allowlist():
    out = conti_art.sanitize_svg(_svg(
        "<rect style=\"mask-image:image-set('http://x' 1x)\" width=\"1\"/>"
        "<rect style=\"background:-webkit-image-set('http://y' 1x)\"/>"
        "<rect style=\"x:cross-fade(url(#a), red)\"/>"
        '<g transform="translate(10 20) rotate(5)"><rect fill="rgb(1,2,3)" stroke="URL(#g)"/></g>',
        ' style="background:image-set(\'http://z\' 1x)"'), "16:9")
    assert "image-set" not in out and "cross-fade" not in out and "http" not in out.replace(SVG_NS, "")
    assert 'transform="translate(10 20) rotate(5)"' in out
    assert 'fill="rgb(1,2,3)"' in out and 'stroke="URL(#g)"' in out


def test_sanitize_keeps_tail_text_of_removed_element():
    out = conti_art.sanitize_svg(_svg("<text>A<script/>B<rect/>C<set attributeName='x'/>D</text>"), "16:9")
    assert "AB" in out and "CD" in out
    out = conti_art.sanitize_svg(_svg("<text>A<script>x</script>B</text>"), "16:9")
    assert "AB" in out


def test_sanitize_output_has_no_prefixes_and_global_state_untouched():
    import xml.etree.ElementTree as ET
    out = conti_art.sanitize_svg(_svg('<g id="a"/><use xlink:href="#a"/><use href="#a"/>'), "16:9")
    assert "ns0:" not in out and "ns1:" not in out and "xlink" not in out
    assert out.count('href="#a"') == 2
    assert ET.tostring(ET.Element(f"{{{SVG_NS}}}svg"), encoding="unicode").startswith("<ns0:svg")


def test_sanitize_checks_size_after_serialization():
    text = _svg("<text><![CDATA[" + "<" * 30000 + "]]></text>")
    assert len(text.encode()) < conti_art.MAX_SVG_BYTES
    with pytest.raises(ScriptError) as exc:
        conti_art.sanitize_svg(text, "16:9")
    assert exc.value.code == "invalid_svg"


def test_sanitize_misc_vectors():
    out = conti_art.sanitize_svg(
        '<?xml-stylesheet href="http://x"?>' + _svg(
            '<use href="javascript:alert(1)"/><set attributeName="href" to="x"/><animate attributeName="x"/>'
            '<rect xml:base="http://x/" width="1"/>'), "16:9")
    for bad in ("javascript", "<set", "<animate", "stylesheet", "xml:base", "base="):
        assert bad not in out
    assert "http" not in out.replace(SVG_NS, "")
