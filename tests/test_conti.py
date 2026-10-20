import json

import pytest

from scripts import conti, draft, fetch, refstore
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set

DRAFT = {
    "title": "컵라면 4단계 | 도전", "topic": "편의점 라면", "source": "직접", "hook_type": "question",
    "narrative_type": "list",
    "sections": [
        {"label": "hook", "start": 0, "end": 2, "script": "자막: 4단계 다 먹을 수 있을까?",
         "shots": [{"size": "탑뷰", "angle": "탑뷰", "sec": 2, "action": "컵라면 4개 나란히"}]},
        {"label": "body", "sub": "list_item", "start": 2, "end": 6, "script": "말: 1단계부터 갑니다",
         "shots": [{"size": "클로즈업", "angle": "45도", "sec": 2, "action": "뚜껑 | 뜯기"},
                   {"size": "익스트림 클로즈업", "sec": 2, "action": "면발 후루룩"}]},
        {"label": "outro", "start": 6, "end": 8, "script": "자막: 다음은 불닭",
         "shots": [{"size": "인서트", "angle": "탑뷰", "sec": 2, "action": "빈 컵"}]},
    ],
}

CONTI = {
    "script": [{"text": "4단계 다 먹을 수 있을까?"}, {"text": "1단계부터 갑니다. 와 순하다"}, {"text": ""}],
    "cuts": [
        {"section": 0, "size": "탑뷰", "angle": "탑뷰", "sec": 2, "action": "컵라면 4개", "line": "4단계 다 먹을 수 있을까?",
         "caption": "4단계 도전", "sfx": "띵", "guide": "카메라 고정, 컵 순서대로 놓기"},
        {"section": 1, "size": "클로즈업", "angle": "45도", "sec": 2, "action": "뚜껑 뜯기", "line": "1단계부터 갑니다.",
         "caption": "", "sfx": "뜯는 소리", "guide": "소리 크게 천천히"},
        {"section": 1, "size": "익스트림 클로즈업", "sec": 2, "action": "면발", "line": "와 순하다",
         "caption": "순한맛", "sfx": "후루룩", "guide": "입은 프레임 밖"},
        {"section": 2, "size": "인서트", "angle": "탑뷰", "sec": 2, "action": "빈 컵", "line": "",
         "caption": "다음은 불닭", "sfx": "", "guide": "3초 정지"},
    ],
}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    standard_set(tmp_path)
    return tmp_path


@pytest.fixture
def draft_md(home):
    path = home / "workspace" / "tmp" / "draft.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    return home / draft.save(CAT, path)["path"]


def conti_file(home, data=CONTI):
    path = home / "workspace" / "tmp" / "conti.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def with_cuts(**patch):
    data = json.loads(json.dumps(CONTI))
    for i, p in patch.items():
        data["cuts"][int(i[1:])].update(p)
    return data


# --- draft 파싱 · context ----------------------------------------------------------

def test_parse_draft_reads_body_shots(draft_md):
    d = conti.parse_draft(draft_md)
    assert [s["label"] for s in d["sections"]] == ["hook", "body", "outro"]
    body = d["sections"][1]
    assert body["sub"] == "list_item" and body["start"] == 2 and body["end"] == 6
    assert body["script"] == "말: 1단계부터 갑니다"
    assert [s["size"] for s in body["shots"]] == ["클로즈업", "익스트림 클로즈업"]
    assert body["shots"][0]["action"] == "뚜껑 | 뜯기" and body["shots"][1]["angle"] is None


def test_parse_draft_follows_user_edits(draft_md):
    text = draft_md.read_text("utf-8").replace("| 4 | 인서트 | 탑뷰 | 2초 | 빈 컵 |", "| 4 | 와이드 | 정면 | 2초 | 식탁 전체 |")
    draft_md.write_text(text, encoding="utf-8")
    assert conti.parse_draft(draft_md)["sections"][2]["shots"][0]["size"] == "와이드"


def test_context(draft_md, home):
    ctx = conti.context(draft_md.relative_to(home).as_posix())
    assert ctx["format"] == "shorts" and ctx["target_sec"] == 8
    assert ctx["conti"].endswith("-conti.md") and ctx["conti_exists"] is False
    assert ctx["speech_cps"] == conti.DEFAULT_CPS and ctx["speech_basis"] == 0
    assert ctx["sections"][1]["max_chars"] == int(conti.DEFAULT_CPS * 4)


def test_context_missing_draft(home):
    with pytest.raises(ScriptError) as exc:
        conti.context("drafts/none.md")
    assert exc.value.code == "draft_not_found"


def test_speech_rate_median_skips_sparse(home):
    def raw(vid, text, dur=10):
        fetch.save_raw({"id": vid, "url": "u", "format": "shorts", "meta": {"duration": dur},
                        "transcript": {"source": "auto", "language": "ko",
                                       "segments": [{"start": 0, "dur": 1, "text": text}]}})
    raw("a00", "가" * 30)       # 3.0
    raw("a01", "가" * 50)       # 5.0
    raw("a02", "가" * 40)       # 4.0
    raw("a03", "가" * 2)        # sparse → 제외
    assert conti.speech_rate(CAT) == {"cps": 4.0, "basis": 3}


# --- 검증 · 경고 ---------------------------------------------------------------------

def test_save_writes_md_and_html(draft_md, home):
    out = conti.save(draft_md, conti_file(home))
    md = home / out["path"]
    assert md.name == draft_md.stem + "-conti.md" and out["cuts"] == 4
    fm, body = refstore.read_md(md)
    assert [c["start"] for c in fm["cuts"]] == [0, 2, 4, 6] and fm["cuts"][-1]["end"] == 8
    assert fm["script"][1] == {"label": "body", "start": 2.0, "end": 6.0, "text": "1단계부터 갑니다. 와 순하다"}
    for text in ["## 전체 대사", "## 콘티", "## 내 메모", "카메라 고정, 컵 순서대로 놓기", "(대사 없음)"]:
        assert text in body, text
    assert (home / out["html"]).exists() and not (home / "workspace" / "tmp" / "conti.json").exists()
    assert out["warnings"] == []


def test_save_does_not_touch_draft(draft_md, home):
    before = draft_md.read_bytes()
    conti.save(draft_md, conti_file(home))
    assert draft_md.read_bytes() == before


@pytest.mark.parametrize("data, word", [
    ({**CONTI, "script": CONTI["script"][:2]}, "script는 섹션마다"),
    (with_cuts(c1={"sec": 3}), "컷 길이 합"),
    (with_cuts(c0={"size": "드론"}), "드론"),
    (with_cuts(c2={"guide": " "}), "guide"),
    (with_cuts(c3={"section": 0}), "순서"),
    (with_cuts(c0={"section": 9}), "section=9"),
])
def test_save_validates(draft_md, home, data, word):
    with pytest.raises(ScriptError) as exc:
        conti.save(draft_md, conti_file(home, data))
    assert exc.value.code == "invalid_conti" and word in exc.value.message


def test_section_without_cuts_is_rejected(draft_md, home):
    data = json.loads(json.dumps(CONTI))
    data["cuts"] = [c for c in data["cuts"] if c["section"] != 2]
    with pytest.raises(ScriptError) as exc:
        conti.save(draft_md, conti_file(home, data))
    assert "컷이 없습니다" in exc.value.message


def test_warns_long_line_and_script_mismatch(draft_md, home):
    data = with_cuts(c1={"line": "1단계부터 갑니다 오늘은 정말 맵기 단계별로 끝까지 먹어볼 건데요"})
    out = conti.save(draft_md, conti_file(home, data))
    kinds = {w["kind"]: w for w in out["warnings"]}
    assert kinds["line_too_long"]["cut"] == 2 and kinds["line_too_long"]["fits"] == 9
    assert kinds["script_mismatch"]["section"] == 1
    assert "⚠ 대사" in (home / out["html"]).read_text("utf-8")


def test_existing_conti_needs_force_and_keeps_memo(draft_md, home):
    first = home / conti.save(draft_md, conti_file(home))["path"]
    first.write_text(first.read_text("utf-8").replace("## 내 메모\n", "## 내 메모\n소품 미리 사기\n"), encoding="utf-8")
    with pytest.raises(ScriptError) as exc:
        conti.save(draft_md, conti_file(home))
    assert exc.value.code == "conti_exists"
    conti.save(draft_md, conti_file(home), force=True)
    assert "소품 미리 사기" in first.read_text("utf-8")


# --- HTML ------------------------------------------------------------------------------

def test_html_is_self_contained_and_escaped(draft_md, home):
    data = with_cuts(c0={"line": "<script>x</script>"})
    data["script"][0]["text"] = "<script>x</script>"
    page = (home / conti.save(draft_md, conti_file(home, data))["html"]).read_text("utf-8")
    assert page.startswith("<!doctype html>") and "<script" not in page and "<link" not in page
    assert "&lt;script&gt;" in page and "prefers-color-scheme: dark" in page
    assert page.count('class="cut"') == 4 and page.count('class="sk"') == 4
    assert "--ar:9/16" in page and "표로 보기" in page and 'class="legend"' in page


@pytest.mark.parametrize("size", conti.SHOT_SIZES)
@pytest.mark.parametrize("vertical", [True, False])
def test_sketch_every_size(size, vertical):
    svg = conti.sketch(size, vertical)
    assert svg.startswith('<svg class="sk"') and ("0 0 90 160" if vertical else "0 0 160 90") in svg
    assert svg.count("<") > 3


def test_long_format_is_landscape(home):
    assert conti.video_format(600) == "long" and conti.video_format(60) == "shorts"


# --- 참고 프레임 ----------------------------------------------------------------------

def make_frames(home, vid, n=4, length=60):
    scenes = []
    for i in range(n):
        rel = f"workspace/frames/{vid}/scene{i + 1:03d}.jpg"
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_bytes(b"\xff\xd8\xff\xe0jpeg" + vid.encode() + bytes([i]))
        step = length / n
        scenes.append({"start": i * step, "end": (i + 1) * step, "frame": rel})
    cuts = home / "workspace" / "cuts" / f"{vid}.json"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text(json.dumps({"id": vid, "scenes": scenes}), encoding="utf-8")
    return [s["frame"] for s in scenes]


def test_frame_pool_exact_and_approx(home):
    frames = make_frames(home, "a00")
    shots = home / "workspace" / "shots" / "a00.json"
    shots.parent.mkdir(parents=True, exist_ok=True)
    shots.write_text(json.dumps({"labels": [{"frame": frames[0], "size": "탑뷰", "section": "hook"}]}), encoding="utf-8")
    make_frames(home, "a01")
    refstore.update_frontmatter(home / "refs" / f"ref_{CAT}" / "videos" / "a01.md",
                                {"shots": {"by_section": {"body": "클로즈업"}}})
    pool = conti.frame_pool(CAT)
    exact = [p for p in pool if p["match"] == "exact"]
    approx = [p for p in pool if p["match"] == "approx"]
    assert len(exact) == 1 and exact[0]["size"] == "탑뷰"
    # a01 장면 0~15·15~30·30~45·45~60 → 중간점이 body(3~52)에 드는 장면 3개가 클로즈업 근사
    assert {p["size"] for p in approx} == {"클로즈업"} and len(approx) == 3


def test_match_frames_prefers_section_then_spreads(home):
    pool = [{"frame": "f1", "size": "클로즈업", "section": "body", "match": "approx"},
            {"frame": "f2", "size": "클로즈업", "section": "hook", "match": "exact"},
            {"frame": "f3", "size": "클로즈업", "section": "body", "match": "exact"}]
    sections = [{"label": "hook"}, {"label": "body"}]
    cuts = [{"n": i, "section": 1, "size": "클로즈업"} for i in (1, 2, 3)] + [{"n": 4, "section": 0, "size": "탑뷰"}]
    got = conti.match_frames(cuts, sections, pool)
    assert got[1]["frame"] == "f3"          # 같은 섹션 + exact 우선
    assert got[2]["frame"] == "f1"          # 같은 섹션에서 아직 안 쓴 프레임
    assert got[3]["frame"] == "f3"          # 같은 섹션 후보를 다 쓰면 덜 쓴 순으로 다시
    assert 4 not in got                     # 같은 크기 프레임이 없으면 없음


def test_html_embeds_reference_frames(draft_md, home):
    frames = make_frames(home, "a00")
    shots = home / "workspace" / "shots" / "a00.json"
    shots.parent.mkdir(parents=True, exist_ok=True)
    shots.write_text(json.dumps({"labels": [{"frame": frames[0], "size": "탑뷰", "section": "hook"}]}), encoding="utf-8")
    page = (home / conti.save(draft_md, conti_file(home))["html"]).read_text("utf-8")
    assert page.count("data:image/jpeg;base64,") == 1      # 탑뷰 컷 #1만 같은 크기 프레임이 있다
    assert page.count("같은 샷의 레퍼런스 프레임 없음") == 3


# --- render ------------------------------------------------------------------------

def test_render_after_user_edit(draft_md, home):
    out = conti.save(draft_md, conti_file(home))
    md = home / out["path"]
    fm, _ = refstore.read_md(md)
    fm["cuts"][1]["guide"] = "사용자가 고친 가이드"
    fm["cuts"][1]["sec"], fm["cuts"][2]["sec"] = 3, 1
    refstore.update_frontmatter(md, {"cuts": fm["cuts"]})
    res = conti.render(out["path"])
    page = (home / res["html"]).read_text("utf-8")
    assert res["status"] == "rendered" and "사용자가 고친 가이드" in page
    assert "0:05 · 1초" in page                             # 컷 3 시작이 2+3=5초로 다시 계산


def test_render_rejects_broken_edit(draft_md, home):
    out = conti.save(draft_md, conti_file(home))
    md = home / out["path"]
    fm, _ = refstore.read_md(md)
    fm["cuts"][1]["sec"] = 9
    refstore.update_frontmatter(md, {"cuts": fm["cuts"]})
    with pytest.raises(ScriptError) as exc:
        conti.render(out["path"])
    assert "컷 길이 합" in exc.value.message


def test_main_routes(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    assert conti.main(["context", rel])["title"] == DRAFT["title"]
    saved = conti.main(["save", rel, "--conti", conti_file(home).relative_to(home).as_posix()])
    assert conti.main(["render", saved["path"]])["cuts"] == 4
