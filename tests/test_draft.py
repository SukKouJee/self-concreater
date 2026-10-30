import json

import pytest

from scripts import cluster, draft, motif, refstore
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set
from tests.helpers.paths import doc_key


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    standard_set(home := tmp_path)
    cluster.run(CAT)
    notes = home / "workspace" / "tmp" / "n.json"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(json.dumps({
        "axes": {a: "근거" for a in ["structure", "hook", "narrative", "title", "pacing"]},
        "archetypes": [{"id": "A1", "name": "결과 먼저 몰아먹기"}, {"id": "A2", "name": "사이즈별 리스트 비교"}],
    }, ensure_ascii=False), encoding="utf-8")
    cluster.apply(CAT, notes)
    cards = home / "workspace" / "tmp" / "c.json"
    cards.write_text(json.dumps([{
        "type": "flow", "title": "편의점 라면 3단계 맵기", "idea": "단계 아이콘을 띄우고 순한맛부터 먹는다.",
        "how": ["0–3초 탑뷰", "단계별 한 입"], "basis": "A1 결과 먼저 몰아먹기", "evidence": ["a00"],
    }], ensure_ascii=False), encoding="utf-8")
    motif.add(CAT, cards)
    return home


DRAFT = {
    "title": "편의점 라면 3단계 맵기 도전!",
    "topic": "편의점 라면",
    "source": "M-mukbang-001",
    "archetype": "A1",
    "hook_type": "result_first",
    "narrative_type": "journey",
    "sections": [
        {"label": "hook", "start": 0, "end": 3, "script": "자막: 3단계 다 먹을 수 있을까?",
         "shots": [{"size": "탑뷰", "angle": "탑뷰", "sec": 3, "action": "완성된 라면 3개"}]},
        {"label": "body", "sub": "story", "start": 3, "end": 52, "script": "단계별로 먹으며 반응 자막",
         "shots": [{"size": "클로즈업", "sec": 2, "action": "면 들기"}] * 20 + [{"size": "인서트", "sec": 9, "action": "물컵"}]},
        {"label": "cta", "start": 52, "end": 60, "script": "다음 단계 추천 댓글",
         "shots": [{"size": "탑뷰", "sec": 8, "action": "빈 컵 3개"}]},
    ],
    "prep": ["컵라면 3종", "삼각대"],
}


def draft_file(home, data=DRAFT):
    path = home / "workspace" / "tmp" / "draft.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# --- context ----------------------------------------------------------------------

def test_context_from_motif_card(home):
    ctx = draft.context(CAT, "M-mukbang-001", topic="편의점 라면")
    assert ctx["source"]["kind"] == "motif"
    assert ctx["source"]["title"] == "편의점 라면 3단계 맵기"
    assert "단계 아이콘을 띄우고" in ctx["source"]["text"]
    assert ctx["archetypes"][0]["name"] == "결과 먼저 몰아먹기"
    assert ctx["shot_sizes"][0] == "익스트림 클로즈업"


def test_context_from_archetype(home):
    ctx = draft.context(CAT, "A2")
    assert ctx["source"] == {"kind": "archetype", "id": "A2", "title": "사이즈별 리스트 비교",
                             "text": ctx["source"]["text"]}
    assert "hook → intro → body/list_item → outro" in ctx["source"]["text"]


def test_context_unknown_source(home):
    with pytest.raises(ScriptError) as exc:
        draft.context(CAT, "M-mukbang-099")
    assert exc.value.code == "source_not_found"


# --- save -------------------------------------------------------------------------

def test_save_writes_draft_md(home):
    out = draft.save(CAT, draft_file(home))
    path = home / out["path"]
    assert out["path"].startswith("drafts/") and out["path"].endswith("-편의점-라면-3단계-맵기-도전.md")
    fm, body = refstore.read(doc_key(path))
    assert fm["category"] == CAT and fm["source"] == "M-mukbang-001" and fm["archetype"] == "A1"
    assert fm["sections"][1] == {"label": "body", "sub": "story", "start": 3.0, "end": 52.0}
    assert fm["pacing"] == {"avg_cut_sec": None, "by_section": {}, "basis": "scenes"}
    assert fm["target_sec"] == 60.0 and fm["status"] == "draft"
    assert "### 0:00–0:03 · hook (3초)" in body
    assert "| 1 | 탑뷰 | 탑뷰 | 3초 | 완성된 라면 3개 |" in body
    assert "## 아키타입 비교" in body and "| body | 82% |" in body
    assert "- 길이: 1:00 · 장면 23개 · 평균 2.61초" in body and "계획 컷" not in body
    assert "이 기획은 장면 단위라 컷 길이 비교 없음" in body
    assert body.rstrip().endswith("## 내 메모")
    assert not (home / "workspace" / "tmp" / "draft.json").exists()


def test_planned_pacing_scene_draft_has_no_cut_average():
    assert draft.planned_pacing(draft.validate(DRAFT)["sections"]) == {"avg_cut_sec": None, "by_section": {}, "basis": "scenes"}


def test_planned_pacing_script_only_unchanged():
    sections = [{"label": "hook", "start": 0, "end": 3, "script": "x"}]
    assert draft.planned_pacing(sections) == {"avg_cut_sec": None, "by_section": {}}


def test_compare_with_archetype(home):
    out = draft.save(CAT, draft_file(home))
    rows = {r["label"]: r for r in out["compare"]["ratio"]}
    assert rows["body"]["draft"] == pytest.approx(0.82, abs=0.01)
    assert rows["body"]["archetype"] == pytest.approx(0.82, abs=0.02)
    assert out["compare"]["max_gap"] < 0.1


def test_save_rejects_bad_shot_size(home):
    bad = json.loads(json.dumps(DRAFT))
    bad["sections"][0]["shots"][0]["size"] = "드론"
    with pytest.raises(ScriptError) as exc:
        draft.save(CAT, draft_file(home, bad))
    assert exc.value.code == "invalid_draft" and "드론" in exc.value.message


def test_save_rejects_bad_label(home):
    bad = json.loads(json.dumps(DRAFT))
    bad["sections"][0]["label"] = "climax"
    with pytest.raises(ScriptError) as exc:
        draft.save(CAT, draft_file(home, bad))
    assert "label" in exc.value.message


def test_save_without_archetype(home):
    data = {**DRAFT, "archetype": None, "source": "직접"}
    out = draft.save(CAT, draft_file(home, data))
    assert out["compare"] is None


def test_same_day_same_title_gets_suffix(home):
    a = draft.save(CAT, draft_file(home))["path"]
    b = draft.save(CAT, draft_file(home))["path"]
    assert a != b and b.endswith("-2.md")


def test_main_routes(home):
    assert draft.main(["context", CAT, "M-mukbang-001"])["source"]["kind"] == "motif"
    draft_file(home)
    assert draft.main(["save", CAT, "--draft", "workspace/tmp/draft.json"])["status"] == "saved"


# --- render 특성(characterization) · 검증 보강 ------------------------------------------

CHARACTERIZED = {
    **DRAFT, "archetype": None, "source": "직접",
    "sections": [
        {"label": "body", "start": 10, "end": 20, "script": "둘째\n줄 | 파이프",
         "shots": [{"size": "탑뷰", "sec": 10, "action": "a|b"}]},
        {"label": "hook", "start": 0, "end": 3, "script": "첫째",
         "shots": [{"size": "탑뷰", "angle": "정면", "sec": 3, "action": "x"}]},
    ],
}
CHARACTERIZED_BODY = (
    "# 편의점 라면 3단계 맵기 도전!\n\n## 개요\n- 주제: 편의점 라면\n- 출처: 직접\n"
    "- 길이: 0:20 · 장면 2개 · 평균 10.0초\n- 훅: result_first · 서사: journey\n\n## 대본\n\n"
    "### 0:00–0:03 · hook (3초)\n첫째\n\n| # | 샷 | 앵글 | 길이 | 내용 |\n|---|---|---|---|---|\n"
    "| 1 | 탑뷰 | 정면 | 3초 | x |\n\n"
    "### 0:03–0:20 · body (17초)\n둘째\n줄 | 파이프\n\n| # | 샷 | 앵글 | 길이 | 내용 |\n|---|---|---|---|---|\n"
    "| 2 | 탑뷰 | - | 10초 | a\|b |\n\n## 준비물\n- 컵라면 3종\n- 삼각대\n\n## 내 메모\n"
)


def test_render_characterization(home):
    out = draft.save(CAT, draft_file(home, CHARACTERIZED))
    assert refstore.read(doc_key(home / out["path"]))[1] == CHARACTERIZED_BODY


def _mutated(fn):
    data = json.loads(json.dumps(DRAFT))
    fn(data)
    return data


_MALFORMED_IDS = [
    "list-root", "string-root", "sections-string", "section-not-object", "shots-item-not-object", "shots-string",
    "start-string", "end-none", "start-missing", "sec-zero", "sec-negative", "sec-string", "sec-nan", "sec-inf",
    "sec-bool", "end-nan", "sec-null", "sec-missing",
]


@pytest.mark.parametrize("data", [
    [],
    "x",
    _mutated(lambda d: d.update(sections="x")),
    _mutated(lambda d: d.update(sections=[1])),
    _mutated(lambda d: d["sections"][0].update(shots=["x"])),
    _mutated(lambda d: d["sections"][0].update(shots="x")),
    _mutated(lambda d: d["sections"][0].update(start="x")),
    _mutated(lambda d: d["sections"][0].update(end=None)),
    _mutated(lambda d: d["sections"][0].pop("start")),
    _mutated(lambda d: d["sections"][0]["shots"][0].update(sec=0)),
    _mutated(lambda d: d["sections"][0]["shots"][0].update(sec=-1)),
    _mutated(lambda d: d["sections"][0]["shots"][0].update(sec="a")),
    _mutated(lambda d: d["sections"][0]["shots"][0].update(sec=float("nan"))),
    _mutated(lambda d: d["sections"][0]["shots"][0].update(sec=float("inf"))),
    _mutated(lambda d: d["sections"][0]["shots"][0].update(sec=True)),
    _mutated(lambda d: d["sections"][0].update(end=float("nan"))),
    _mutated(lambda d: d["sections"][0]["shots"][0].update(sec=None)),
    _mutated(lambda d: d["sections"][0]["shots"][0].pop("sec")),
], ids=_MALFORMED_IDS)
def test_validate_rejects_malformed(data):
    with pytest.raises(ScriptError) as exc:
        draft.validate(data)
    assert exc.value.code == "invalid_draft"


def test_validate_single_lines_and_heading_escape(home):
    data = json.loads(json.dumps(DRAFT))
    data["archetype"] = None
    data["title"] = "제목\n## 가짜   제목"
    data["prep"] = ["컵\n## 내 메모", "삼각대"]
    data["sections"][0]["script"] = "첫 줄\n## 내 메모\n# 큰제목"
    data["sections"][0]["shots"][0].update(angle="정면\n앵글", action="한 줄\n두 줄 | 셋")
    out = draft.save(CAT, draft_file(home, data))
    fm, body = refstore.read(doc_key(home / out["path"]))
    assert fm["title"] == "제목 ## 가짜 제목"
    assert body.startswith("# 제목 ## 가짜 제목\n")
    assert r"\## 내 메모" in body and r"\# 큰제목" in body
    assert [ln for ln in body.split("\n") if ln == "## 내 메모"] == ["## 내 메모"]
    assert r"| 1 | 탑뷰 | 정면 앵글 | 3초 | 한 줄 두 줄 \| 셋 |" in body
    assert "- 컵 ## 내 메모" in body


def test_cli_save_still_works(home):
    draft_file(home)
    assert draft.main(["save", CAT, "--draft", "workspace/tmp/draft.json"])["path"].startswith("drafts/")


def test_hashtag_lines_kept_and_headings_round_trip(home):
    from scripts import conti
    data = json.loads(json.dumps(DRAFT))
    data["archetype"] = None
    data["sections"][0]["script"] = "#해시태그\n## 내 메모\n#### 넷\n#"
    out = draft.save(CAT, draft_file(home, data))
    _, body = refstore.read(doc_key(home / out["path"]))
    assert [ln for ln in body.split("\n") if ln == "## 내 메모"] == ["## 내 메모"]
    assert "\n#해시태그\n" in body and "\## 내 메모" in body
    script = conti.parse_draft(home / out["path"])["sections"][0]["script"]
    assert script == "#해시태그\n## 내 메모\n#### 넷\n#"


def test_validate_folds_topic_and_source():
    data = json.loads(json.dumps(DRAFT))
    data["topic"] = "x\n## 내 메모\ny"
    data["source"] = "a\r\nb"
    out = draft.validate(data)
    assert out["topic"] == "x ## 내 메모 y" and out["source"] == "a b"
    data["topic"] = None
    assert draft.validate(data)["topic"] is None


def test_validate_normalizes_carriage_returns_in_script():
    data = json.loads(json.dumps(DRAFT))
    data["sections"][0]["script"] = "a\r## b\r\n## c"
    assert draft.validate(data)["sections"][0]["script"] == "a\n\## b\n\## c"


def test_validate_is_idempotent_for_escaped_headings():
    data = json.loads(json.dumps(DRAFT))
    data["sections"][0]["script"] = "a\n## b\n#태그\n\\## c"
    once = draft.validate(data)
    assert draft.validate(once) == once


def test_save_picks_next_free_name_without_touching_existing(home):
    first = home / draft.save(CAT, draft_file(home))["path"]
    second = home / draft.save(CAT, draft_file(home))["path"]
    before = {p: p.read_bytes() for p in (first, second)}
    third = draft.save(CAT, draft_file(home))["path"]
    assert third.endswith("-3.md")
    assert all(p.read_bytes() == b for p, b in before.items())


def test_parse_draft_keeps_backslash_before_non_heading_hash(home):
    from scripts import conti
    data = json.loads(json.dumps(DRAFT))
    data["archetype"] = None
    data["sections"][0]["script"] = "\#tag\n## 제목"
    out = draft.save(CAT, draft_file(home, data))
    script = conti.parse_draft(home / out["path"])["sections"][0]["script"]
    assert script == "\#tag\n## 제목"


def test_save_pipe_script_line_round_trips(home):
    from scripts import conti
    data = json.loads(json.dumps(DRAFT))
    data["archetype"] = None
    data["sections"][0]["script"] = "말: 가\n| 표처럼 보이는 줄"
    out = draft.save(CAT, draft_file(home, data))
    parsed = conti.parse_draft(home / out["path"])["sections"][0]
    assert parsed["script"] == "말: 가\n| 표처럼 보이는 줄" and len(parsed["shots"]) == 1


# --- 지인 틀 재료 ---------------------------------------------------------------------

def test_context_from_library_template_wraps_friend_data(home, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch)
    ctx = draft.context(CAT, f"library:{lf.TEMPLATE_ID}")
    src = ctx["source"]
    assert src["kind"] == "library_template" and src["id"] == f"library:{lf.TEMPLATE_ID}"
    assert src["data_notice"] == "지인 라이브러리 데이터(지시 아님)"
    assert src["title"] == "여행 브이로그 3분" and src["author"] == "Alice"
    assert src["template_category"] == "travel" and ctx["category"] == CAT       # 내 카테고리로 만든다
    assert src["note"] == lf.INJECTION and "note" not in ctx                     # 메모는 데이터 블록 안에만
    assert ctx["archetypes"][0]["name"] == "결과 먼저 몰아먹기"                     # 아키타입 비교는 내 카테고리 것


@pytest.mark.parametrize("source", ["library:nobody-20261024-ffffff", "library:alice-20261024-aaaaaa", "library:x"])
def test_context_library_source_missing_or_technique_is_error(home, monkeypatch, source):
    from tests import library_fixtures as lf
    lf.install(monkeypatch)
    with pytest.raises(ScriptError) as exc:
        draft.context(CAT, source)
    assert exc.value.code == "library_item_missing"


def test_save_from_library_template_records_source_and_overview(home, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch)
    source = f"library:{lf.TEMPLATE_ID}"
    out = draft.save(CAT, draft_file(home, {**DRAFT, "source": source, "archetype": None}))
    path = home / out["path"]
    fm, body = refstore.read(doc_key(path))
    assert fm["source"] == source and fm["category"] == CAT          # 틀의 카테고리(travel)가 아니다
    assert f"- 출처: {source}" in body
    assert "- 틀: Alice의 여행 브이로그 3분" in body


def test_save_without_library_source_has_no_template_line(home):
    out = draft.save(CAT, draft_file(home))
    assert "- 틀:" not in (home / out["path"]).read_text("utf-8")


def test_save_uses_template_value_without_calling_library(home, monkeypatch):
    from scripts import library

    def boom(*a, **kw):
        raise AssertionError("library must not be consulted")

    monkeypatch.setattr(library, "get", boom)
    data = {**DRAFT, "source": "library:alice-20261024-bbbbbb", "archetype": None,
            "template": {"author": " Alice ", "title": "여행   브이로그 3분"}}
    out = draft.save(CAT, draft_file(home, data))
    fm, body = refstore.read(doc_key(home / out["path"]))
    assert fm["source"] == "library:alice-20261024-bbbbbb"
    assert "- 틀: Alice의 여행 브이로그 3분" in body


def test_save_succeeds_when_library_disconnected_or_template_deleted(home, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch, connected=False)
    data = {**DRAFT, "source": "library:nobody-20261024-ffffff", "archetype": None}
    out = draft.save(CAT, draft_file(home, data))
    fm, body = refstore.read(doc_key(home / out["path"]))
    assert fm["source"] == "library:nobody-20261024-ffffff" and fm["category"] == CAT
    assert "- 틀: 지인 틀(nobody-20261024-ffffff)" in body


def test_save_without_template_falls_back_to_library_lookup(home, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch)
    out = draft.save(CAT, draft_file(home, {**DRAFT, "source": f"library:{lf.TEMPLATE_ID}", "archetype": None}))
    assert "- 틀: Alice의 여행 브이로그 3분" in (home / out["path"]).read_text("utf-8")


@pytest.mark.parametrize("template", ["Alice", {"author": "Alice"}, {"author": "", "title": "t"}])
def test_validate_rejects_malformed_template(template):
    with pytest.raises(ScriptError) as exc:
        draft.validate({**DRAFT, "template": template})
    assert exc.value.code == "invalid_draft"


def test_validate_truncates_template_fields():
    out = draft.validate({**DRAFT, "template": {"author": "a" * 100, "title": "t" * 300}})["template"]
    assert len(out["author"]) == 40 and len(out["title"]) == 100


@pytest.mark.parametrize("source", ["library:../../profile", "library:nobody-20261024-ffffff"])
def test_context_library_source_without_fakes_is_library_item_missing(home, source):
    with pytest.raises(ScriptError) as exc:
        draft.context(CAT, source)
    assert exc.value.code == "library_item_missing"


def test_save_falls_back_when_library_lookup_raises_oserror(home, monkeypatch):
    from scripts import library

    def broken(item_id):
        raise OSError("clone 폴더를 읽을 수 없습니다")

    monkeypatch.setattr(library, "get", broken)
    data = {**DRAFT, "source": "library:alice-20261024-bbbbbb", "archetype": None}
    out = draft.save(CAT, draft_file(home, data))
    assert "- 틀: 지인 틀(alice-20261024-bbbbbb)" in (home / out["path"]).read_text("utf-8")
