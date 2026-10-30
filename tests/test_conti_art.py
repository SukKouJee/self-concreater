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


# --- Task 3: 그리기와 저장 ---------------------------------------------------------------------

import json  # noqa: E402

from scripts import conti_history, llm, refstore  # noqa: E402
from tests.test_conti import conti_file, draft_md, home, saved  # noqa: E402,F401 - fixtures

GOOD_SVG = f'<svg xmlns="{SVG_NS}" viewBox="0 0 1600 900"><rect x="1" y="1" width="9" height="9"/></svg>'


class FakeDraw:
    """llm.ask_validated 대역: 묶음에 든 장면마다 svg를 낸다. hook(payload, call_no)으로 응답을 바꾼다."""

    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook

    def __call__(self, rules, payload, task, validate, **kw):
        self.calls.append({"rules": rules, "payload": payload, "task": task, "kw": kw})
        data = {"scenes": [{"id": s["id"], "svg": GOOD_SVG} for s in payload["scenes"]]}
        if self.hook:
            data = self.hook(payload, len(self.calls), data) or data
        return data, validate(data)


@pytest.fixture
def fake(monkeypatch):
    f = FakeDraw()
    monkeypatch.setattr(llm, "ask_validated", f)
    return f


def cuts_of(path):
    return conti.validated(refstore.read_md(path)[0])["cuts"]


def pin(cuts, i):
    return [{**c, "ref_frame": "workspace/frames/p.jpg"} if k == i else c for k, c in enumerate(cuts)]


def test_targets_skip_pinned_and_cap(home):
    fm = {"cuts": [{"id": f"c{i}", "action": f"a{i}", "guide": "g", "size": "미디엄"} for i in range(81)]}
    assert len(conti_art.targets(fm, None, False)) == conti_art.MAX_SCENES == 80
    fm["cuts"][0]["ref_frame"] = "x.jpg"
    assert [c["id"] for c in conti_art.targets(fm, None, False)][:2] == ["c1", "c2"]
    assert [c["id"] for c in conti_art.targets(fm, ["c2", "c0"], False)] == ["c2"]
    rel = "workspace/conti-art/d/c1.svg"
    (home / rel).parent.mkdir(parents=True)
    (home / rel).write_text("<svg/>", encoding="utf-8")
    fm["cuts"][1]["art"] = {"file": rel, "basis": conti_art.basis(fm["cuts"][1], "16:9")}
    fm["cuts"][2]["art"] = {"file": rel, "basis": "00000000"}
    got = [c["id"] for c in conti_art.targets(fm, None, True)]
    assert "c1" not in got and "c2" in got and "c3" in got


def test_draw_saves_files_and_art(saved, home, fake):
    ids = [c["id"] for c in cuts_of(saved)][:3]
    out = conti_art.draw(saved, ids, False)
    assert out == {"drawn": 3, "skipped": 0, "failed": 0, "warnings": []}
    assert len(fake.calls) == 2 and fake.calls[0]["kw"]["max_tokens"] == 16000
    cuts = cuts_of(saved)
    asp = conti_art.aspect(refstore.read_md(saved)[0])
    for c in cuts[:3]:
        assert c["art"]["basis"] == conti_art.basis(c, asp)
        assert c["art"]["file"] == f"workspace/conti-art/{saved.stem}/{c['id']}-{c['art']['basis']}.svg"
        assert (home / c["art"]["file"]).read_bytes().startswith(b"<svg") and b"\r" not in (home / c["art"]["file"]).read_bytes()
    assert "art" not in cuts[3]
    assert [v["kind"] for v in conti_history.list_versions(saved)].count("auto") == 1


def test_draw_payload_is_small_and_has_context(saved, fake):
    conti_art.draw(saved, None, False)
    scene = fake.calls[0]["payload"]["scenes"][0]
    assert set(scene) == {"id", "action", "guide", "size", "angle", "section", "prev", "next"}
    assert fake.calls[0]["payload"]["format"] == conti_art.aspect(refstore.read_md(saved)[0]) and "title" in fake.calls[0]["payload"]


def test_draw_skips_scene_edited_during_draw(saved, home, monkeypatch):
    def hook(payload, call_no, data):
        conti.update_cut(saved, 2, {"action": "완전히 다른 행동"})

    f = FakeDraw(hook)
    monkeypatch.setattr(llm, "ask_validated", f)
    ids = [c["id"] for c in cuts_of(saved)]
    out = conti_art.draw(saved, ids[:2], False)
    assert out["drawn"] == 1 and out["skipped"] == 1
    cuts = cuts_of(saved)
    assert "art" in cuts[0] and "art" not in cuts[1]


def test_draw_batch_failure_keeps_earlier(saved, monkeypatch):
    def hook(payload, call_no, data):
        if call_no == 2:
            raise ScriptError("llm_failed", "boom")

    monkeypatch.setattr(llm, "ask_validated", FakeDraw(hook))
    out = conti_art.draw(saved, None, False)
    assert out["drawn"] == 2 and out["failed"] == 2 and len(out["warnings"]) == 1
    cuts = cuts_of(saved)
    assert "art" in cuts[0] and "art" in cuts[1] and "art" not in cuts[2]


def test_draw_merge_first(saved):
    fm = refstore.read_md(saved)[0]
    fm["cuts"][2]["action"] = fm["cuts"][1]["action"]
    refstore.update_frontmatter(saved, {"cuts": fm["cuts"]})
    with pytest.raises(ScriptError) as exc:
        conti_art.draw(saved, None, False)
    assert exc.value.code == "merge_first" and exc.value.message == "반복 컷을 먼저 장면으로 합쳐 주세요"


def test_draw_rejects_unknown_ids_and_bad_svg(saved, home, monkeypatch):
    def hook(payload, call_no, data):
        a, b = payload["scenes"]
        return {"scenes": [{"id": a["id"], "svg": "<html><body/></html>"}, {"id": "nope", "svg": GOOD_SVG},
                           {"id": b["id"], "svg": None}]}

    monkeypatch.setattr(llm, "ask_validated", FakeDraw(hook))
    ids = [c["id"] for c in cuts_of(saved)][:2]
    out = conti_art.draw(saved, ids, False)
    assert out["drawn"] == 0 and out["failed"] == 2
    assert not (home / "workspace" / "conti-art").exists() or not list((home / "workspace" / "conti-art").rglob("*.svg"))
    assert all("art" not in c for c in cuts_of(saved))


def test_validator_rejects_foreign_id(saved, monkeypatch):
    f = FakeDraw(lambda p, n, d: {"scenes": [{"id": "zzz", "svg": GOOD_SVG}]})
    seen = {}

    def ask(rules, payload, task, validate, **kw):
        data = f.hook(payload, 1, None)
        with pytest.raises(ScriptError) as exc:
            validate(data)
        seen["code"] = exc.value.code
        return data, {}

    monkeypatch.setattr(llm, "ask_validated", ask)
    conti_art.draw(saved, None, False)
    assert seen["code"] == "invalid_art"


def test_draw_progress_and_cancel(saved, fake):
    class Job:
        def __init__(self):
            self.updates, self.checks = [], 0

        def update(self, index, **fields):
            self.updates.append(fields)

        def check_cancelled(self):
            self.checks += 1

    job = Job()
    conti_art.draw(saved, None, False, job)
    assert job.checks >= 2 and job.updates[-1]["done"] == 4 and job.updates[-1]["total"] == 4


def test_save_art_skips_pinned_and_stale(saved, home):
    cuts = cuts_of(saved)
    refstore.update_frontmatter(saved, {"cuts": pin(refstore.read_md(saved)[0]["cuts"], 0)})
    drawn = {cuts[0]["id"]: (GOOD_SVG, conti_art.basis(cuts[0], "16:9")),
             cuts[1]["id"]: (GOOD_SVG, "deadbeef")}
    out = conti_art.save_art(saved, drawn)
    assert out["saved"] == [] and sorted(out["skipped"]) == sorted(drawn)


def test_memo_and_body_untouched(saved, home, fake):
    memo = "## 내 메모\n소품 미리 사기\n"
    saved.write_text(saved.read_text("utf-8").replace("## 내 메모\n", memo), encoding="utf-8")
    before = refstore.read_md(saved)[1]
    conti_art.draw(saved, None, False)
    after = refstore.read_md(saved)[1]
    assert after == before and "소품 미리 사기" in after
    assert "art" in cuts_of(saved)[0]


def test_draw_legacy_conti_without_ids(saved, home, fake):
    fm = refstore.read_md(saved)[0]
    refstore.update_frontmatter(saved, {"cuts": [{k: v for k, v in c.items() if k != "id"} for c in fm["cuts"]],
                                        "schema_version": 2})
    assert not any(c.get("id") for c in refstore.read_md(saved)[0]["cuts"])
    out = conti_art.draw(saved, None, False)
    assert out["drawn"] == 4 and out["skipped"] == 0
    assert all(c.get("art") for c in cuts_of(saved))


def test_draw_progress_reports_zero_first_and_message(saved, fake):
    class Job:
        def __init__(self):
            self.updates = []

        def update(self, index, **fields):
            self.updates.append(fields)

        def check_cancelled(self):
            pass

    job = Job()
    conti_art.draw(saved, None, False, job)
    assert job.updates[0] == {"message": "0 / 4장면", "done": 0, "total": 4}
    assert job.updates[-1] == {"message": "4 / 4장면", "done": 4, "total": 4}


def test_draw_warns_once_for_scenes_without_art(saved, monkeypatch):
    def hook(payload, call_no, data):
        a, b = payload["scenes"]
        return {"scenes": [{"id": a["id"], "svg": "<html/>"}, {"id": b["id"], "svg": None}]} if call_no == 1 else None

    monkeypatch.setattr(llm, "ask_validated", FakeDraw(hook))
    out = conti_art.draw(saved, None, False)
    assert out["failed"] == 2 and out["drawn"] == 2
    assert out["warnings"] == ["2장면은 그림을 받지 못했어요"]
