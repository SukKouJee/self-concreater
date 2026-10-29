import json
from pathlib import Path

import pytest

from scripts import conti, draft, fetch, refstore
from scripts.common import ScriptError
from tests.test_cuts import sample_video  # noqa: F401 - fixture
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


@pytest.mark.parametrize("fields", [{"size": "없는크기"}, {"guide": " "}, {"guide": ""}])
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
    assert fm["schema_version"] == 3 and fm["cuts"][0]["ref_frame"] == rel
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


def test_export_srt_rejects_conti_outside_drafts(home, draft_md):
    out = home / "elsewhere" / "x-conti.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("---\ncuts: []\n---\n", encoding="utf-8")
    with pytest.raises(ScriptError) as e:
        conti.export_srt(out)
    assert e.value.code == "bad_path" and not list(out.parent.glob("*.srt"))


# --- SRT 시간 맞추기(편집한 영상의 컷) -----------------------------------------------------

def _retime(planned, duration, cuts, snap_min=0.5, snap_ratio=0.0):
    return conti.retime_boundaries(planned, duration, cuts, snap_min, snap_ratio)


def test_retime_snaps_all_when_cut_counts_match():
    out = _retime([0, 2, 4, 6], 6.4, [2.2, 4.1])
    assert out["boundaries"] == [0, 2.2, 4.1, 6.4]
    assert (out["snapped"], out["inner"], out["detected"]) == (2, 2, 2)
    assert out["scale"] == pytest.approx(6.4 / 6)


def test_retime_ignores_extra_cuts():
    out = _retime([0, 2, 4], 4.0, [0.9, 2.1, 3.0])
    assert out["boundaries"] == [0, 2.1, 4.0] and out["snapped"] == 1 and out["detected"] == 3


def test_retime_scales_unmatched_boundaries_when_cuts_are_fewer():
    out = _retime([0, 2, 4, 6], 12.0, [4.1])
    assert out["snapped"] == 1 and out["inner"] == 2
    # t=4.0 -> 4.1에 맞고, 다음 t=8.0은 맞출 컷이 없어 비례값
    assert out["boundaries"] == [0, 4.1, pytest.approx(8.0), 12.0]


def test_retime_without_cuts_uses_ratio_only():
    out = _retime([0, 2, 6], 3.0, [])
    assert out["boundaries"] == [0, 1.0, 3.0] and out["snapped"] == 0 and out["detected"] == 0


def test_retime_is_strictly_increasing_and_never_reuses_a_cut():
    out = _retime([0, 1, 2, 3, 4], 4.0, [1.4, 1.5], snap_min=1.0)
    b = out["boundaries"]
    assert all(x < y for x, y in zip(b, b[1:])) and b[-1] == 4.0
    assert out["snapped"] == 2 and 1.4 in b and 1.5 in b       # 1.4, 1.5가 각각 한 번씩만 쓰인다


def test_retime_stays_increasing_when_scaled_value_is_behind_snapped_cut():
    b = _retime([0, 1, 2, 3], 3.0, [1.4], snap_min=0.5)["boundaries"]
    assert b[1] == 1.4 and b[2] > b[1] and b[-1] == 3.0


def test_retime_tolerance_boundary_is_inclusive():
    assert _retime([0, 2, 4], 4.0, [2.5])["snapped"] == 1       # |c-t| == snap_min
    assert _retime([0, 2, 4], 4.0, [2.51])["snapped"] == 0
    out = conti.retime_boundaries([0, 2, 4], 4.0, [2.9], 0.1, 0.5)   # tol = 0.5 * 2 = 1.0 -> 0.9 안
    assert out["snapped"] == 1 and out["boundaries"][1] == 2.9


def test_retime_prefers_earlier_cut_on_tie():
    assert _retime([0, 2, 4], 4.0, [1.75, 2.25], snap_min=0.25)["boundaries"][1] == 1.75


def _srt_times(raw):
    out = []
    for block in raw.decode("utf-8").strip().split("\r\n\r\n"):
        a, b = block.split("\r\n")[1].split(" --> ")
        out.append(tuple(
            (int(h) * 3600 + int(m) * 60 + int(s)) + int(ms) / 1000
            for h, m, s, ms in (x.replace(",", ":").split(":") for x in (a, b))))
    return out


def test_export_srt_with_video_fits_blocks_to_real_cuts(saved, home, monkeypatch, sample_video):
    _srt_cuts(monkeypatch, [_cut(1.8, "A"), _cut(2.3, "B"), _cut(1.9, "C")])
    before = (sample_video.read_bytes(), sample_video.stat().st_mtime_ns)
    out = conti.export_srt(saved, video=sample_video)
    assert (sample_video.read_bytes(), sample_video.stat().st_mtime_ns) == before
    times = _srt_times((home / out["path"]).read_bytes())
    assert times[0][0] == 0 and times[2][1] == pytest.approx(6.0, abs=0.1)
    assert times[0][1] == pytest.approx(2.0, abs=0.15) and times[1][1] == pytest.approx(4.0, abs=0.15)
    assert times[0][1] == times[1][0] and times[1][1] == times[2][0]
    assert out["duration_sec"] == pytest.approx(6.0, abs=0.1)
    retime = out["retime"]
    assert retime["video"] == "sample.mp4" and (retime["detected"], retime["snapped"], retime["inner"]) == (2, 2, 2)
    assert retime["warnings"] == []


def test_export_srt_with_video_accepts_quoted_path_and_keeps_empty_cut_rules(saved, home, monkeypatch, sample_video):
    _srt_cuts(monkeypatch, [_cut(2, "A"), _cut(2, "A"), _cut(2, "")])
    out = conti.export_srt(saved, video=f'  "{sample_video}" ')
    assert out["count"] == 1                                   # 이어지는 같은 자막은 합쳐지고 빈 컷은 건너뜀
    start, end = _srt_times((home / out["path"]).read_bytes())[0]
    assert start == 0 and end == pytest.approx(4.0, abs=0.15)


def test_export_srt_with_video_warns_on_big_length_gap(saved, monkeypatch, sample_video):
    _srt_cuts(monkeypatch, [_cut(30, "A"), _cut(30, "B")])
    warnings = conti.export_srt(saved, video=sample_video)["retime"]["warnings"]
    assert any("많이 다릅니다" in w and "6.0초" in w and "60.0초" in w for w in warnings)


def test_export_srt_with_video_warns_when_no_cut_found(saved, monkeypatch, sample_video):
    from scripts import cuts
    _srt_cuts(monkeypatch, [_cut(3, "A"), _cut(3, "B")])
    monkeypatch.setattr(cuts, "detect_cuts", lambda path: {"duration_sec": 6.0, "fps": 30.0, "cuts": []})
    out = conti.export_srt(saved, video=sample_video)
    assert out["retime"]["warnings"] == ["실제 컷을 찾지 못해 길이 비율로만 맞췄습니다"]
    assert out["retime"]["snapped"] == 0


@pytest.mark.parametrize("value", ["", "   ", '""', 5])
def test_export_srt_rejects_empty_video_value(saved, value):
    with pytest.raises(ScriptError) as e:
        conti.export_srt(saved, video=value)
    assert e.value.code == "bad_video"


def test_export_srt_rejects_missing_or_unreadable_video(saved, home, tmp_path):
    for target in (tmp_path / "missing.mp4", tmp_path):
        with pytest.raises(ScriptError) as e:
            conti.export_srt(saved, video=target)
        assert e.value.code == "bad_video"
    assert not list((home / "drafts").glob("*.srt"))


def test_main_srt_with_video(saved, home, sample_video):
    out = conti.main(["srt", saved.relative_to(home).as_posix(), "--video", str(sample_video)])
    assert out["retime"]["video"] == "sample.mp4"


@pytest.mark.parametrize("actual,warns", [(3.0, False), (2.99, True), (12.0, False), (12.01, True)])
def test_export_srt_with_video_scale_warning_bounds(saved, monkeypatch, sample_video, actual, warns):
    from scripts import cuts
    _srt_cuts(monkeypatch, [_cut(3, "A"), _cut(3, "B")])          # 계획 6초
    monkeypatch.setattr(cuts, "detect_cuts", lambda path: {"duration_sec": actual, "fps": 30.0, "cuts": [actual / 2]})
    warnings = conti.export_srt(saved, video=sample_video)["retime"]["warnings"]
    assert any("많이 다릅니다" in w for w in warnings) is warns


# --- 검증·경계 보강 -------------------------------------------------------------------------

def test_clean_video_path_requires_video_extension_and_content(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "empty.mp4").write_bytes(b"")
    (tmp_path / "ok.MOV").write_bytes(b"x")
    for name in ("a.txt", "empty.mp4"):
        with pytest.raises(ScriptError) as e:
            conti._clean_video_path(str(tmp_path / name))
        assert e.value.code == "bad_video"
    assert conti._clean_video_path(str(tmp_path / "ok.MOV")).name == "ok.MOV"      # 대소문자 무시


def test_retime_never_snaps_to_a_cut_at_the_very_end():
    out = conti.retime_boundaries([0, 1, 2, 3], 3.0, [1.0, 2.9996], 1.5, 0.0)
    b = out["boundaries"]
    assert all(y - x >= 0.01 for x, y in zip(b, b[1:])) and b[-1] == 3.0


def test_retime_fallback_is_clamped_below_the_end():
    b = conti.retime_boundaries([0, 1, 1.001, 2], 2.0, [], 0.4, 0.0)["boundaries"]
    assert b[-1] == 2.0 and all(x < y for x, y in zip(b, b[1:])) and b[-2] <= 2.0 - 0.01


def test_srt_blocks_skip_zero_length_spans():
    blocks = conti._srt_blocks([{"caption": "A"}, {"caption": "B"}], "caption", [(0, 1000), (1000, 1000)])
    assert [(a, b, t) for a, b, t in blocks] == [(0, 1000, "A")]


def test_trash_moves_conti_and_its_outputs_keeps_draft(saved, draft_md, home):
    stem = draft_md.stem
    extras = [draft_md.with_name(f"{stem}-captions.srt"), draft_md.with_name(f"{stem}-lines.srt"),
              draft_md.with_name(f"{stem}-captions.mp4")]
    for p in extras:
        p.write_bytes(b"x")
    other = draft_md.with_name("other-captions.srt")           # 다른 기획안의 파일은 건드리지 않는다
    other.write_bytes(b"y")
    html_path = saved.with_suffix(".html")
    result = conti.trash(saved)
    trash = home / result["trash"]
    assert trash.is_dir() and trash.is_relative_to(home / "workspace" / "trash")
    names = sorted(Path(p).name for p in result["moved"])
    assert names == sorted([saved.name, html_path.name, saved.stem, *(p.name for p in extras)])   # saved.stem = 이력 폴더
    assert not saved.exists() and not html_path.exists() and not any(p.exists() for p in extras)
    assert all((trash / "drafts" / n).exists() for n in names if n != saved.stem)     # 같은 상대 경로로 옮긴다
    assert (trash / "workspace" / "conti_history" / saved.stem).is_dir()
    assert draft_md.exists() and other.exists()


def test_trash_twice_in_a_row_uses_separate_folders(draft_md, home):
    first = conti.trash(conti.save(draft_md, conti_file(home))["path"])
    second = conti.trash(conti.save(draft_md, conti_file(home), force=True)["path"])
    assert first["trash"] != second["trash"]
    assert (home / first["trash"]).is_dir() and (home / second["trash"]).is_dir()


def test_trash_missing_conti(draft_md, home):
    with pytest.raises(ScriptError) as e:
        conti.trash(conti.conti_path(draft_md))
    assert e.value.code == "not_found"


def test_trash_rolls_back_when_a_file_is_locked(saved, draft_md, home, monkeypatch):
    video = draft_md.with_name(f"{draft_md.stem}-captions.mp4")
    video.write_bytes(b"x")
    from scripts import common
    real_move = common.shutil.move

    def move(src, dst):
        if str(src).endswith(".mp4"):
            raise PermissionError("locked")
        return real_move(src, dst)

    monkeypatch.setattr(common.shutil, "move", move)
    with pytest.raises(ScriptError) as e:
        conti.trash(saved)
    assert e.value.code == "trash_failed"
    assert saved.exists() and saved.with_suffix(".html").exists() and video.exists()
    trash_root = home / "workspace" / "trash"
    assert not trash_root.exists() or not any(trash_root.iterdir())


def test_trash_refuses_a_draft_whose_name_ends_with_conti(home):
    fake = home / "drafts" / "2026-10-26-라면-conti.md"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("---\ntitle: 라면 conti\n---\n본문\n", encoding="utf-8")
    with pytest.raises(ScriptError) as e:
        conti.trash(fake)
    assert e.value.code == "not_conti" and fake.exists()


def test_draft_trash_moves_draft_with_its_conti(saved, draft_md, home):
    srt = draft_md.with_name(f"{draft_md.stem}-captions.srt")
    srt.write_bytes(b"x")
    other = home / "drafts" / "2026-01-01-other.md"
    other.write_text("---\ntitle: other\n---\n", encoding="utf-8")
    result = draft.trash(draft_md)
    trash = home / result["trash"]
    names = sorted(Path(p).name for p in result["moved"])
    assert names == sorted([draft_md.name, saved.name, saved.with_suffix(".html").name, srt.name, saved.stem])
    assert all((trash / "drafts" / n).exists() for n in names if n != saved.stem)     # 같은 상대 경로로 옮긴다
    assert (trash / "workspace" / "conti_history" / saved.stem).is_dir()
    assert not draft_md.exists() and not saved.exists() and other.exists()


def test_draft_trash_without_conti_then_missing(draft_md, home):
    assert [Path(p).name for p in draft.trash(draft_md)["moved"]] == [draft_md.name]
    with pytest.raises(ScriptError) as e:
        draft.trash(draft_md)
    assert e.value.code == "draft_not_found"


def test_draft_trash_refuses_a_conti_file(saved):
    with pytest.raises(ScriptError) as e:
        draft.trash(saved)
    assert e.value.code == "bad_path" and saved.exists()


# --- 컷 id · schema 3 · 섹션 시간 다시 계산 ---------------------------------------------

def _sections(fm):
    return [{"idx": i, "label": s["label"], "start": float(s["start"]), "end": float(s["end"])}
            for i, s in enumerate(fm["script"])]


def test_validate_keeps_and_assigns_ids(draft_md):
    sections = conti.parse_draft(draft_md)["sections"]
    data = json.loads(json.dumps(CONTI))
    data["cuts"][1]["id"] = "keepme01"
    _, cuts = conti.validate(data, sections)
    ids = [c["id"] for c in cuts]
    assert ids[1] == "keepme01"
    assert all(isinstance(i, str) and len(i) == 8 and i == i.lower() and i.isalnum() for i in ids)
    assert len(set(ids)) == len(ids)


def test_retime_moves_later_sections(saved):
    fm = conti._load_conti(saved)
    fm["cuts"][0]["sec"] += 2
    out = conti.retime(fm)
    assert [(s["start"], s["end"]) for s in out["script"]] == [(0, 4), (4, 8), (8, 10)]
    assert [(c["n"], c["start"], c["end"]) for c in out["cuts"]] == [(1, 0, 4), (2, 4, 6), (3, 6, 8), (4, 8, 10)]
    assert out["script"][1]["text"] == "1단계부터 갑니다. 와 순하다"
    assert [c["id"] for c in out["cuts"]] == [c["id"] for c in fm["cuts"]]


def test_ensure_ids_migrates_v2_keeping_memo_and_crlf(saved):
    fm, body = refstore.read_md(saved)
    for c in fm["cuts"]:
        del c["id"]
    fm["schema_version"] = 2
    body = body.replace("## 내 메모\n", "## 내 메모\n소품 미리 사기\n").replace("\n", "\r\n")
    saved.write_bytes(refstore.join_frontmatter(fm, body, "\r\n").encode("utf-8"))

    assert conti.ensure_ids(saved) is True
    fm, body = refstore.read_md(saved)
    assert fm["schema_version"] == 3 and all(len(c["id"]) == 8 for c in fm["cuts"])
    raw = saved.read_bytes()
    assert raw.startswith(b"---\r\n") and b"\n" not in raw.replace(b"\r\n", b"")
    assert "소품 미리 사기" in body
    assert conti.ensure_ids(saved) is False


def _set_ids(path, ids, schema=3):
    fm, body = refstore.read_md(path)
    for c, i in zip(fm["cuts"], ids):
        c["id"] = i
    fm["schema_version"] = schema
    path.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))


@pytest.mark.parametrize("bad", ["A b/1", "ABCDEFGH", "short", "toolong123", ""])
def test_validate_replaces_malformed_id(draft_md, bad):
    data = json.loads(json.dumps(CONTI))
    data["cuts"][0]["id"] = bad
    _, cuts = conti.validate(data, conti.parse_draft(draft_md)["sections"])
    assert cuts[0]["id"] != bad and len(cuts[0]["id"]) == 8


def test_ensure_ids_fixes_duplicate_and_malformed_ids_stably(saved):
    _set_ids(saved, ["dup00001", "dup00001", "A b/1", "ok000004"])
    assert conti.ensure_ids(saved) is True
    first = [c["id"] for c in conti._load_conti(saved)["cuts"]]
    assert len(set(first)) == 4 and first[0] == "dup00001" and first[3] == "ok000004"
    assert [c["id"] for c in conti._load_conti(saved)["cuts"]] == first
    assert conti.ensure_ids(saved) is False


def test_ensure_ids_keeps_memo_under_heading(saved):
    fm, body = refstore.read_md(saved)
    body = body.replace("## 내 메모\n", "## 내 메모\n소품 미리 사기\n")
    saved.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))
    _set_ids(saved, ["dup00001"] * 4)
    assert conti.ensure_ids(saved) is True
    _, body = refstore.read_md(saved)
    assert body.split("## 내 메모")[1].strip().startswith("소품 미리 사기")


def test_render_saves_ids_of_v2_file(saved):
    fm, body = refstore.read_md(saved)
    for c in fm["cuts"]:
        del c["id"]
    fm["schema_version"] = 2
    saved.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))
    conti.render(saved)
    fm, _ = refstore.read_md(saved)
    assert fm["schema_version"] == 3 and all(len(c["id"]) == 8 for c in fm["cuts"])


# --- 컷 편집 연산(edit_cuts) -----------------------------------------------------------

def _ids(path):
    fm, _ = refstore.read_md(path)
    return [c["id"] for c in fm["cuts"]]


def _edit(path, op, **kw):
    return conti.edit_cuts(path, op, conti.content_hash(path), **kw)


def _cuts(path):
    fm, _ = refstore.read_md(path)
    return fm["cuts"]


def test_insert_defaults_take_previous_cut_section(saved, home):
    out = _edit(saved, {"op": "insert", "at": 2})
    cuts = _cuts(saved)
    new = cuts[2]
    assert out["status"] == "updated" and out["cut_id"] == new["id"] and out["hash"] == conti.content_hash(saved)
    assert (new["size"], new["sec"], new["action"], new["guide"]) == ("미디엄", 4, "새 컷", "촬영 지시를 적어 주세요")
    assert new["section"] == cuts[1]["section"] == 1 and len(cuts) == 5
    fm, _ = refstore.read_md(saved)
    assert fm["script"][1]["end"] == 10 and fm["script"][2]["start"] == 10 and fm["script"][2]["end"] == 12
    assert [c["n"] for c in cuts] == [1, 2, 3, 4, 5]


def test_insert_at_front_uses_section_zero_and_explicit_section(saved):
    _edit(saved, {"op": "insert", "at": 0, "cut": {"action": "맨 앞", "sec": 1.5}})
    first = _cuts(saved)[0]
    assert first["section"] == 0 and first["action"] == "맨 앞" and first["sec"] == 1.5
    _edit(saved, {"op": "insert", "at": 3, "section": 1})            # 섹션 1의 마지막 자리
    assert [c["section"] for c in _cuts(saved)] == [0, 0, 1, 1, 1, 2]


def test_insert_rejects_bad_position_and_section(saved):
    for op in ({"op": "insert", "at": 9}, {"op": "insert", "at": 1, "section": 2}, {"op": "insert", "at": 1, "section": 7}):
        with pytest.raises(ScriptError) as exc:
            _edit(saved, op)
        assert exc.value.code == "invalid_edit"


def test_delete_cut_and_last_cut_guard(saved):
    ids = _ids(saved)
    _edit(saved, {"op": "delete", "id": ids[1]})
    assert _ids(saved) == [ids[0], ids[2], ids[3]]
    with pytest.raises(ScriptError) as exc:
        _edit(saved, {"op": "delete", "n": 1})                       # 섹션 0의 유일한 컷
    assert exc.value.code == "last_cut" and "하나는" in exc.value.message
    assert _ids(saved) == [ids[0], ids[2], ids[3]]


def test_move_across_sections_changes_membership(saved):
    ids = _ids(saved)
    _edit(saved, {"op": "move", "id": ids[2], "to": 3})              # 섹션 1의 마지막 컷을 섹션 2 뒤로
    cuts = _cuts(saved)
    assert [c["id"] for c in cuts] == [ids[0], ids[1], ids[3], ids[2]]
    assert [c["section"] for c in cuts] == [0, 1, 2, 2]


def test_move_to_front_adopts_next_section_and_blocks_emptying(saved):
    ids = _ids(saved)
    _edit(saved, {"op": "move", "id": ids[1], "to": 0})              # 섹션 1 컷을 맨 앞으로: 섹션 0 소속
    assert [c["section"] for c in _cuts(saved)] == [0, 0, 1, 2]
    with pytest.raises(ScriptError) as exc:
        _edit(saved, {"op": "move", "id": ids[3], "to": 0})          # 섹션 2의 유일한 컷을 밖으로
    assert exc.value.code == "last_cut"


def test_move_within_section_and_explicit_section(saved):
    ids = _ids(saved)
    _edit(saved, {"op": "move", "id": ids[1], "to": 2})
    assert _ids(saved)[1:3] == [ids[2], ids[1]]
    _edit(saved, {"op": "move", "id": ids[1], "to": 3, "section": 2})
    assert [c["section"] for c in _cuts(saved)] == [0, 1, 2, 2]


def test_duplicate_gets_new_id_right_after(saved):
    ids = _ids(saved)
    out = _edit(saved, {"op": "duplicate", "id": ids[1]})
    cuts = _cuts(saved)
    assert len(cuts) == 5 and cuts[2]["id"] == out["cut_id"] and out["cut_id"] not in ids
    assert cuts[2]["action"] == cuts[1]["action"] and cuts[2]["section"] == 1


@pytest.mark.parametrize("fields", [
    {"sec": 0.4}, {"sec": 600.5}, {"sec": 1.3}, {"sec": "abc"}, {"sec": True},
    {"size": "없는크기"}, {"action": " "}, {"guide": ""},
])
def test_update_rejects_bad_sec_size_action(saved, fields):
    before = saved.read_bytes()
    with pytest.raises(ScriptError) as exc:
        _edit(saved, {"op": "update", "n": 1, "fields": fields})
    assert exc.value.code == "invalid_edit" and saved.read_bytes() == before


def test_update_allows_long_scene_up_to_ten_minutes(saved):
    _edit(saved, {"op": "update", "n": 1, "fields": {"sec": 600}})
    assert _cuts(saved)[0]["sec"] == 600


def test_update_sec_size_action_shifts_later_sections(saved):
    out = _edit(saved, {"op": "update", "n": 1, "fields": {"sec": 3.5, "size": "미디엄", "action": "새 동작"}})
    fm, _ = refstore.read_md(saved)
    assert out["cut_id"] == fm["cuts"][0]["id"]
    assert (fm["cuts"][0]["sec"], fm["cuts"][0]["size"], fm["cuts"][0]["action"]) == (3.5, "미디엄", "새 동작")
    assert [(s["start"], s["end"]) for s in fm["script"]] == [(0, 3.5), (3.5, 7.5), (7.5, 9.5)]
    assert fm["target_sec"] == 8                                      # 목표 길이는 그대로


def test_edit_with_stale_hash_is_rejected(saved):
    old = conti.content_hash(saved)
    conti.edit_cuts(saved, {"op": "insert", "at": 1}, old)
    before = saved.read_bytes()
    with pytest.raises(ScriptError) as exc:
        conti.edit_cuts(saved, {"op": "delete", "n": 1}, old)
    assert exc.value.code == "conti_changed" and saved.read_bytes() == before


def test_unknown_op_and_missing_cut(saved):
    for op, code in (({"op": "explode"}, "invalid_edit"), ({"op": "delete", "id": "zzzzzzzz"}, "cut_not_found"),
                     ({"op": "delete", "n": 99}, "cut_not_found"), ({"op": "delete"}, "invalid_edit")):
        with pytest.raises(ScriptError) as exc:
            _edit(saved, op)
        assert exc.value.code == code


def test_edit_keeps_memo_crlf_and_records_history(saved):
    from scripts import conti_history
    saved.write_bytes(saved.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    _edit(saved, {"op": "insert", "at": 1})
    data = saved.read_bytes()
    assert data.startswith(b"---\r\n") and b"\n" not in data.replace(b"\r\n", b"")
    assert "## 내 메모" in data.decode("utf-8")
    versions = conti_history.list_versions(saved)
    assert versions[0]["kind"] == "auto" and versions[0]["summary"]["cuts"] == 5


def test_srt_render_and_view_work_with_new_times(saved, home):
    ids = _ids(saved)
    _edit(saved, {"op": "move", "id": ids[2], "to": 3})
    _edit(saved, {"op": "update", "id": ids[0], "fields": {"sec": 5}})
    srt = conti.export_srt(saved, "line")
    text = (home / srt["path"]).read_text("utf-8")
    assert "00:00:05,000" in text and srt["duration_sec"] == 11
    assert conti.render(saved)["status"] == "rendered"
    fm = conti._load_conti(saved)
    assert fm["cuts"][-1]["end"] == 11 == fm["script"][-1]["end"]
    assert saved.with_suffix(".html").exists()


def test_update_cut_wrapper_keeps_shape(saved):
    out = conti.update_cut(saved, 2, {"sec": 3})
    assert out["cut"]["sec"] == 3 and out["cut"]["n"] == 2 and {"hash", "warnings", "status"} <= set(out)


def test_baseline_holds_history_lock(saved, monkeypatch):
    from scripts import conti_history
    seen = []
    real = conti.ensure_ids
    monkeypatch.setattr(conti, "ensure_ids", lambda p: (seen.append(conti.HISTORY_LOCK._is_owned()), real(p))[1])
    conti_history.baseline(saved)
    assert seen == [True]


def _hand_edit_text(path, idx, text):
    fm, _ = refstore.read_md(path)
    fm["script"][idx]["text"] = text
    refstore.update_frontmatter(path, {"script": fm["script"]})


def test_hand_edited_section_text_survives_unrelated_edit(saved):
    _hand_edit_text(saved, 0, "손으로 고친 훅")
    _edit(saved, {"op": "update", "n": 2, "fields": {"guide": "다른 가이드"}})
    fm, _ = refstore.read_md(saved)
    assert fm["script"][0]["text"] == "손으로 고친 훅"
    _edit(saved, {"op": "update", "n": 1, "fields": {"sec": 3}})      # 길이만 바뀐 섹션도 대사는 그대로
    fm, _ = refstore.read_md(saved)
    assert fm["script"][0]["text"] == "손으로 고친 훅"


def test_line_edit_and_move_rebuild_affected_sections(saved):
    _hand_edit_text(saved, 0, "손으로 고친 훅")
    _hand_edit_text(saved, 2, "손으로 고친 끝")
    ids = _ids(saved)
    _edit(saved, {"op": "update", "n": 2, "fields": {"line": "새 대사"}})
    fm, _ = refstore.read_md(saved)
    assert fm["script"][1]["text"] == "새 대사 와 순하다" and fm["script"][0]["text"] == "손으로 고친 훅"
    _edit(saved, {"op": "move", "id": ids[2], "to": 3})               # 섹션 1에서 섹션 2로
    fm, _ = refstore.read_md(saved)
    assert fm["script"][1]["text"] == "새 대사" and fm["script"][2]["text"] == "와 순하다"


# --- 되돌리기: 컷 정체성 유지 · 한 번만 읽기 ----------------------------------------------------

def _stored_cut(path, i):
    return {k: v for k, v in _cuts(path)[i].items() if k not in ("n", "start", "end")}


def test_undo_insert_keeps_deleted_cut_id_and_angle(saved):
    fm, _ = refstore.read_md(saved)
    fm["cuts"][1]["angle"] = "로우앵글"
    refstore.update_frontmatter(saved, {"cuts": fm["cuts"]})
    cut = _stored_cut(saved, 1)
    _edit(saved, {"op": "delete", "id": cut["id"]})
    out = _edit(saved, {"op": "insert", "at": 1, "section": 1, "cut": cut})
    back = _stored_cut(saved, 1)
    assert out["cut_id"] == cut["id"] == back["id"]
    assert back == cut and back["angle"] == "로우앵글"


def test_insert_with_used_or_malformed_id_gets_new_id(saved):
    for given in ("used", "BAD-ID!!", 12345678, "abc"):
        ids = _ids(saved)
        out = _edit(saved, {"op": "insert", "at": 1, "cut": {"id": ids[0] if given == "used" else given}})
        assert out["cut_id"] not in ids and conti._ID_RE.fullmatch(out["cut_id"])
        assert _ids(saved)[1] == out["cut_id"] and len(set(_ids(saved))) == len(ids) + 1


def test_insert_angle_null_and_section_hint(saved):
    out = _edit(saved, {"op": "insert", "at": 1, "cut": {"angle": None, "section": 1}})
    cuts = _cuts(saved)
    assert cuts[1]["id"] == out["cut_id"] and cuts[1]["angle"] is None and cuts[1]["section"] == 1
    _edit(saved, {"op": "insert", "at": 1, "cut": {"angle": "  ", "section": 2}})    # 맞지 않는 섹션은 자리에 맞춘다
    cuts = _cuts(saved)
    assert cuts[1]["angle"] is None and cuts[1]["section"] == 0


@pytest.mark.parametrize("cut", [{"n": 3}, {"start": 0}, {"bogus": 1}, {"angle": 5}, {"angle": ["a"]},
                                 {"section": "1"}, {"section": 9}, {"section": True}])
def test_insert_rejects_unknown_or_bad_cut_keys(saved, cut):
    before = saved.read_bytes()
    with pytest.raises(ScriptError) as exc:
        _edit(saved, {"op": "insert", "at": 1, "cut": cut})
    assert exc.value.code == "invalid_edit" and saved.read_bytes() == before


def count_reads(monkeypatch, target):
    """target 파일을 디스크에서 읽은 횟수(read_bytes/read_text)."""
    seen = []
    real_bytes, real_text = Path.read_bytes, Path.read_text

    def read_bytes(self):
        if Path(self).resolve() == target.resolve():
            seen.append("bytes")
        return real_bytes(self)

    def read_text(self, *args, **kwargs):
        if Path(self).resolve() == target.resolve():
            seen.append("text")
        return real_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)
    return seen


@pytest.mark.parametrize("op", [{"op": "update", "n": 2, "fields": {"line": "새 대사"}}, {"op": "insert", "at": 1},
                                {"op": "delete", "n": 2}, {"op": "move", "n": 2, "to": 0}])
def test_edit_reads_the_conti_md_at_most_three_times(saved, monkeypatch, op):
    _edit(saved, {"op": "update", "n": 1, "fields": {"sfx": "쿵"}})    # 이력·id가 이미 있는 평소 상태
    expected = conti.content_hash(saved)
    seen = count_reads(monkeypatch, saved)
    out = conti.edit_cuts(saved, op, expected)
    assert len(seen) <= 3, seen
    monkeypatch.undo()
    assert out["hash"] == conti.content_hash(saved)


def test_ensure_ids_reads_once_when_nothing_to_fix(saved, monkeypatch):
    seen = count_reads(monkeypatch, saved)
    assert conti.ensure_ids(saved) is False
    assert len(seen) == 1, seen


def test_saved_conti_round_trips_byte_for_byte(saved):
    _edit(saved, {"op": "update", "n": 2, "fields": {"caption": "최애 1위 🏆"}})
    fm, body = refstore.read_md(saved)
    assert refstore.join_frontmatter(fm, body).encode("utf-8") == saved.read_bytes()


def test_trash_moves_footage_outputs_and_state_with_conti(saved, draft_md, home):
    stem = draft_md.stem
    html_r, csv_f = draft_md.with_name(f"{stem}-remaining.html"), draft_md.with_name(f"{stem}-footage.csv")
    cuts_dir = draft_md.with_name(f"{stem}-cuts")
    cuts_dir.mkdir()
    (cuts_dir / "01_a.mp4").write_bytes(b"x")
    for p in (html_r, csv_f):
        p.write_bytes(b"x")
    state = home / "workspace" / "footage_match" / f"{saved.stem}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}", encoding="utf-8")
    result = draft.trash(draft_md)
    trash = home / result["trash"]
    assert not html_r.exists() and not csv_f.exists() and not cuts_dir.exists() and not state.exists()
    assert (trash / "drafts" / html_r.name).is_file() and (trash / "drafts" / csv_f.name).is_file()
    assert (trash / "drafts" / cuts_dir.name / "01_a.mp4").is_file()
    assert (trash / "workspace" / "footage_match" / state.name).is_file()
