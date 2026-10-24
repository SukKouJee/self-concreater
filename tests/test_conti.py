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
    assert got[2]["frame"] == "f3"          # exact가 있으면 근사보다 반복을 택한다(틀린 그림보다 낫다)
    assert got[3]["frame"] == "f3"
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


# --- 컷 편집 · 참고 프레임 고정 · 장면 후보 ------------------------------------------------

@pytest.fixture
def saved(draft_md, home):
    return home / conti.save(draft_md, conti_file(home))["path"]


def pin_frame(home, name="pin.jpg"):
    rel = f"workspace/frames/{name}"
    (home / rel).parent.mkdir(parents=True, exist_ok=True)
    (home / rel).write_bytes(b"\xff\xd8\xff\xe0pinned")
    return rel


def test_update_line_resyncs_section_text(saved, home):
    memo = "## 내 메모\n소품 미리 사기\n"
    saved.write_text(saved.read_text("utf-8").replace("## 내 메모\n", memo), encoding="utf-8")
    out = conti.update_cut(saved, 2, {"line": "1단계 시작!"})
    fm, body = refstore.read_md(saved)
    assert out["status"] == "updated" and out["cut"]["line"] == "1단계 시작!"
    assert fm["script"][1]["text"] == "1단계 시작! 와 순하다"
    assert not [w for w in out["warnings"] if w["kind"] == "script_mismatch"]
    assert "1단계 시작!" in body and "소품 미리 사기" in body
    assert (home / out["html"]).exists()


@pytest.mark.parametrize("fields", [{"size": "바스트"}, {"guide": " "}, {"guide": ""}])
def test_update_rejects_unknown_field_and_empty_guide(saved, fields):
    with pytest.raises(ScriptError) as exc:
        conti.update_cut(saved, 1, fields)
    assert exc.value.code == "invalid_edit"


def test_update_cut_not_found(saved):
    with pytest.raises(ScriptError) as exc:
        conti.update_cut(saved, 99, {"line": "x"})
    assert exc.value.code == "cut_not_found"


def test_update_hash_conflict(saved):
    old = conti.content_hash(saved)
    saved.write_bytes(saved.read_bytes() + "추가 메모\n".encode("utf-8"))
    with pytest.raises(ScriptError) as exc:
        conti.update_cut(saved, 1, {"sfx": "쿵"}, expected_hash=old)
    assert exc.value.code == "conti_changed" and "다시 불러오세요" in exc.value.message
    new = conti.content_hash(saved)
    out = conti.update_cut(saved, 1, {"sfx": "쿵"}, expected_hash=new)
    assert out["hash"] != new and out["hash"] == conti.content_hash(saved)


def test_ref_frame_pin_and_clear(saved, home):
    rel = pin_frame(home)
    out = conti.update_cut(saved, 1, {"ref_frame": rel})
    fm, _ = refstore.read_md(saved)
    assert fm["schema_version"] == 2 and fm["cuts"][0]["ref_frame"] == rel
    assert "참고(고정)" in (home / out["html"]).read_text("utf-8")
    conti.update_cut(saved, 1, {"ref_frame": ""})
    fm, _ = refstore.read_md(saved)
    assert "ref_frame" not in fm["cuts"][0]


def test_ref_frame_must_exist(saved, home):
    with pytest.raises(ScriptError) as exc:
        conti.update_cut(saved, 1, {"ref_frame": "workspace/frames/none.jpg"})
    assert exc.value.code == "invalid_edit"


def test_render_missing_ref_frame_falls_back(saved, home):
    rel = pin_frame(home)
    conti.update_cut(saved, 1, {"ref_frame": rel})
    (home / rel).unlink()
    res = conti.render(saved)
    assert res["status"] == "rendered" and "참고(고정)" not in (home / res["html"]).read_text("utf-8")


def test_v1_file_still_renders(saved):
    fm, _ = refstore.read_md(saved)
    assert "ref_frame" not in fm["cuts"][0]
    refstore.update_frontmatter(saved, {"schema_version": 1})
    assert conti.render(saved)["status"] == "rendered"


def test_crlf_preserved_on_update(saved):
    saved.write_bytes(saved.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    conti.update_cut(saved, 1, {"sfx": "쿵"})
    data = saved.read_bytes()
    assert data.startswith(b"---\r\n") and b"\n" not in data.replace(b"\r\n", b"")


def test_scenes_order_and_limit(saved, home):
    f = make_frames(home, "a00")
    cuts_path = home / "workspace" / "cuts" / "a00.json"          # 첫 장면을 hook(0~3초) 안으로 맞춘다
    cuts = json.loads(cuts_path.read_text("utf-8"))
    cuts["scenes"][0]["end"] = 3
    cuts_path.write_text(json.dumps(cuts), encoding="utf-8")
    shots = home / "workspace" / "shots" / "a00.json"
    shots.parent.mkdir(parents=True, exist_ok=True)
    shots.write_text(json.dumps({"labels": [
        {"frame": f[0], "size": "클로즈업", "section": "hook"},
        {"frame": f[1], "size": "클로즈업", "section": "body"},
        {"frame": f[2], "size": "탑뷰", "section": "body"}]}), encoding="utf-8")
    g = make_frames(home, "a01")
    refstore.update_frontmatter(home / "refs" / f"ref_{CAT}" / "videos" / "a01.md",
                                {"shots": {"by_section": {"body": "클로즈업"}}})
    got = conti.scenes(saved, 2, limit=10)
    frames = [s["frame"] for s in got]
    assert frames[0] == f[1] and got[0]["match"] == "exact"
    assert frames[-1] == f[0]                                   # 같은 크기만 맞는 것은 뒤
    assert f[2] not in frames and len(frames) == len(set(frames))
    assert [s["match"] for s in got[1:-1]] == ["approx"] * (len(got) - 2) and set(frames[1:-1]) <= set(g)
    assert [s["frame"] for s in conti.scenes(saved, 2, limit=2)] == frames[:2]
    assert set(got[0]) == {"frame", "size", "section", "video", "title", "match"}


def test_cut_context_values(saved):
    ctx = conti.cut_context(saved, 2)
    assert ctx["prev_line"] == "4단계 다 먹을 수 있을까?" and ctx["next_line"] == "와 순하다"
    assert ctx["max_chars"] == int(ctx["speech_cps"] * 2) and ctx["hook_type"] == "question"
    assert ctx["narrative_type"] == "list" and ctx["section_label"] == "body"
    assert ctx["section_text"] == "1단계부터 갑니다. 와 순하다" and ctx["cut"]["n"] == 2
    assert {"title", "profile"} <= set(ctx)


def test_main_update_scenes_cut_context(saved, home):
    rel = saved.relative_to(home).as_posix()
    assert conti.main(["update", rel, "--cut", "1", "--sfx", "쿵"])["cut"]["sfx"] == "쿵"
    assert conti.main(["update", rel, "--cut", "1", "--caption", "새 자막", "--expect-hash",
                       conti.content_hash(saved)])["cut"]["caption"] == "새 자막"
    assert isinstance(conti.main(["scenes", rel, "--cut", "1", "--limit", "3"]), list)
    assert conti.main(["cut-context", rel, "--cut", "1"])["cut"]["n"] == 1


# --- SRT 내보내기 ----------------------------------------------------------------------

def _srt_cuts(monkeypatch, cuts):
    fm = {"script": [{"label": "hook", "text": "x", "start": 0, "end": 1}], "cuts": cuts}
    monkeypatch.setattr(conti, "_load_conti", lambda path: fm)


def _cut(sec, caption="", line=""):
    return {"sec": sec, "caption": caption, "line": line}


def test_export_srt_basic_format(saved, home):
    out = conti.export_srt(saved)
    assert out == {"path": "drafts/" + saved.name.replace("-conti.md", "-captions.srt"), "track": "caption",
                   "count": 3, "duration_sec": 8.0}
    raw = (home / out["path"]).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert text == ("1\r\n00:00:00,000 --> 00:00:02,000\r\n4단계 도전\r\n\r\n"
                    "2\r\n00:00:04,000 --> 00:00:06,000\r\n순한맛\r\n\r\n"
                    "3\r\n00:00:06,000 --> 00:00:08,000\r\n다음은 불닭\r\n\r\n")


def test_export_srt_line_track_name(saved, home):
    out = conti.export_srt(saved, "line")
    assert out["path"].endswith("-lines.srt") and out["track"] == "line" and out["count"] == 3
    assert "1단계부터 갑니다." in (home / out["path"]).read_text("utf-8")


def test_export_srt_merges_same_caption_only_when_adjacent(saved, monkeypatch):
    _srt_cuts(monkeypatch, [_cut(2, "A"), _cut(1, "A"), _cut(1, ""), _cut(1, "A")])
    out = conti.export_srt(saved)
    text = conti._resolve(out["path"]).read_bytes().decode("utf-8")
    assert out["count"] == 2
    assert "00:00:00,000 --> 00:00:03,000\r\nA" in text and "00:00:04,000 --> 00:00:05,000\r\nA" in text


def test_export_srt_skips_empty_cuts_but_keeps_time(saved, monkeypatch):
    _srt_cuts(monkeypatch, [_cut(1.5, ""), _cut(2, "B")])
    out = conti.export_srt(saved)
    assert "1\r\n00:00:01,500 --> 00:00:03,500\r\nB" in conti._resolve(out["path"]).read_bytes().decode("utf-8")


def test_export_srt_no_float_drift(saved, monkeypatch):
    _srt_cuts(monkeypatch, [_cut(0.1, f"c{i}") for i in range(30)])
    out = conti.export_srt(saved)
    assert out["duration_sec"] == 3.0
    assert "00:00:02,900 --> 00:00:03,000\r\nc29" in conti._resolve(out["path"]).read_bytes().decode("utf-8")


def test_export_srt_cleans_text(saved, monkeypatch):
    _srt_cuts(monkeypatch, [_cut(1, "  a \n\n b\nc\nd  "), _cut(1, "x --> y")])
    text = conti._resolve(conti.export_srt(saved)["path"]).read_bytes().decode("utf-8")
    assert "a\r\nb c d\r\n" in text and "x → y" in text and "-->" not in text.replace(" --> ", "")


def test_export_srt_no_blocks_writes_nothing(draft_md, home):
    data = with_cuts(**{f"c{i}": {"caption": ""} for i in range(4)})
    path = conti_file(home, data)
    saved = home / conti.save(draft_md, path)["path"]
    out = conti.export_srt(saved)
    assert out["path"] is None and out["count"] == 0
    assert not list((home / "drafts").glob("*.srt"))


def test_export_srt_rejects_bad_track_and_path(saved, draft_md):
    with pytest.raises(ScriptError) as e:
        conti.export_srt(saved, "sfx")
    assert e.value.code == "bad_request"
    with pytest.raises(ScriptError) as e:
        conti.export_srt(draft_md)
    assert e.value.code == "bad_path"


def test_export_srt_leaves_conti_files_untouched(saved):
    md, html = saved, saved.with_suffix(".html")
    before = (md.read_bytes(), html.read_bytes())
    conti.export_srt(saved)
    conti.export_srt(saved, "line")
    assert (md.read_bytes(), html.read_bytes()) == before


def test_main_srt(saved, home):
    out = conti.main(["srt", saved.relative_to(home).as_posix(), "--track", "line"])
    assert out["track"] == "line"
