import json

import pytest

from scripts import cluster, draft, motif, refstore
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set


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
    fm, body = refstore.read_md(path)
    assert fm["category"] == CAT and fm["source"] == "M-mukbang-001" and fm["archetype"] == "A1"
    assert fm["sections"][1] == {"label": "body", "sub": "story", "start": 3.0, "end": 52.0}
    assert fm["pacing"] == {"avg_cut_sec": 2.61, "by_section": {"hook": 3.0, "body": 2.33, "cta": 8.0}}
    assert fm["target_sec"] == 60.0 and fm["status"] == "draft"
    assert "### 0:00–0:03 · hook (3초)" in body
    assert "| 1 | 탑뷰 | 탑뷰 | 3초 | 완성된 라면 3개 |" in body
    assert "## 아키타입 비교" in body and "| body | 82% |" in body
    assert body.rstrip().endswith("## 내 메모")
    assert not (home / "workspace" / "tmp" / "draft.json").exists()


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
    "- 길이: 0:20 · 계획 컷 2개 (평균 10.0초)\n- 훅: result_first · 서사: journey\n\n## 대본\n\n"
    "### 0:00–0:03 · hook (3초)\n첫째\n\n| # | 샷 | 앵글 | 길이 | 내용 |\n|---|---|---|---|---|\n"
    "| 1 | 탑뷰 | 정면 | 3초 | x |\n\n"
    "### 0:03–0:20 · body (17초)\n둘째\n줄 | 파이프\n\n| # | 샷 | 앵글 | 길이 | 내용 |\n|---|---|---|---|---|\n"
    "| 2 | 탑뷰 | - | 10초 | a\|b |\n\n## 준비물\n- 컵라면 3종\n- 삼각대\n\n## 내 메모\n"
)


def test_render_characterization(home):
    out = draft.save(CAT, draft_file(home, CHARACTERIZED))
    assert refstore.read_md(home / out["path"])[1] == CHARACTERIZED_BODY


def _mutated(fn):
    data = json.loads(json.dumps(DRAFT))
    fn(data)
    return data


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
], ids=lambda d: "case")
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
    fm, body = refstore.read_md(home / out["path"])
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
    _, body = refstore.read_md(home / out["path"])
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
