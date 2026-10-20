import json

import pytest

from scripts import motif, refstore
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set, write_video


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


def index_body(home):
    return refstore.read_md(home / "refs" / f"ref_{CAT}" / "index.md")[1]


def cards_file(home, cards, name="cards.json"):
    path = home / "workspace" / "tmp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    return path


TECH_CARD = {
    "type": "technique", "title": "첫 1초 완성품 클로즈업",
    "idea": "완성된 음식을 첫 1초에 화면 가득 보여 주고 시작한다.",
    "how": ["완성 직후 탑뷰로 2초 촬영", "영상 맨 앞에 붙이기"],
    "evidence": ["a00", "a01"],
}


def write_profile(home):
    (home / "profile.md").write_text(
        "---\ncreated: 2026-10-20\n---\n# 크리에이터 프로필\n\n## 관심 주제\n- 자취 요리\n- 편의점 조합\n\n"
        "## 포맷\n쇼츠\n\n## 피하고 싶은 스타일\n- 얼굴 출연\n\n## 성장 단계\n관찰\n", encoding="utf-8")


def write_techniques(home):
    path = home / "refs" / f"ref_{CAT}" / "techniques.md"
    fm, body = refstore.read_md(path)
    body = body.replace("## 훅\n", "## 훅\n- **결과 선공개**: 완성품 먼저 — [a00](videos/a00.md)\n"
                                   "- **완성품 먼저 보여주기**: 첫 장면에 결과 — [a01](videos/a01.md), [b00](videos/b00.md)\n")
    body = body.replace("## 촬영\n", "## 촬영\n- **탑뷰 손 클로즈업**: 위에서 손만 — [a02](videos/a02.md)\n")
    path.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))


# --- 단계별 허용 카드 -------------------------------------------------------------

@pytest.mark.parametrize("n_a, stage, allowed", [
    (3, 1, ["technique"]),
    (6, 2, ["technique", "angle"]),
    (10, 3, ["flow", "technique", "angle", "combo"]),
])
def test_allowed_types_by_stage(home, n_a, stage, allowed):
    standard_set(home, n_a=n_a, n_b=0)
    ctx = motif.context(CAT)
    assert ctx["stage"] == stage
    assert ctx["allowed_types"] == allowed


# --- context ----------------------------------------------------------------------

def test_context_collects_profile_techniques_videos(home):
    standard_set(home, n_a=3, n_b=0)
    write_profile(home)
    write_techniques(home)
    write_video(home, "m1", role="mine")
    ctx = motif.context(CAT, topic="라면")
    assert ctx["topic"] == "라면"
    assert ctx["profile"]["관심 주제"] == "- 자취 요리\n- 편의점 조합"
    assert ctx["profile"]["피하고 싶은 스타일"] == "- 얼굴 출연"
    hook = [t for t in ctx["techniques"] if t["kind"] == "훅"]
    assert hook[1] == {"kind": "훅", "name": "완성품 먼저 보여주기", "desc": "첫 장면에 결과", "sources": ["a01", "b00"]}
    assert [v["id"] for v in ctx["videos"]] == ["a00", "a01", "a02"]       # ref만
    assert ctx["next_id"] == "M-mukbang-001"
    assert ctx["cards"] == []


def test_context_without_profile(home):
    standard_set(home, n_a=3, n_b=0)
    assert motif.context(CAT)["profile"] is None


def test_context_includes_typify_when_available(home):
    from scripts import cluster
    standard_set(home)
    cluster.run(CAT)
    ctx = motif.context(CAT)
    assert ctx["typify"]["axes"]["structure"]["verdict"] == "typable"
    assert ctx["typify"]["archetypes"][0]["skeleton"] == "hook → body/story → cta"


# --- add --------------------------------------------------------------------------

def test_add_assigns_ids_and_renders(home):
    standard_set(home, n_a=3, n_b=0)
    out = motif.add(CAT, cards_file(home, [TECH_CARD, {**TECH_CARD, "title": "탑뷰 손만 찍기", "evidence": ["a02"]}]))
    assert out["ids"] == ["M-mukbang-001", "M-mukbang-002"]
    section = index_body(home).split("## 모티브 카드")[1].split("## 이상치")[0]
    assert "### M-mukbang-001 · 기법 · 첫 1초 완성품 클로즈업" in section
    assert "- 아이디어: 완성된 음식을 첫 1초에 화면 가득 보여 주고 시작한다." in section
    assert "- 이렇게: 완성 직후 탑뷰로 2초 촬영 → 영상 맨 앞에 붙이기" in section
    assert "- 근거: [a00](videos/a00.md) 라면 5봉지 먹방!, [a01](videos/a01.md) 라면 5봉지 먹방!" in section
    assert motif.context(CAT)["next_id"] == "M-mukbang-003"
    assert not (home / "workspace" / "tmp" / "cards.json").exists()


def test_add_keeps_existing_cards_and_comment(home):
    standard_set(home, n_a=3, n_b=0)
    motif.add(CAT, cards_file(home, [TECH_CARD]))
    motif.add(CAT, cards_file(home, [{**TECH_CARD, "title": "다른 카드"}]))
    section = index_body(home).split("## 모티브 카드")[1].split("## 이상치")[0]
    assert section.index("M-mukbang-001") < section.index("M-mukbang-002")
    assert "<!-- M-<category>-001 형식" in section
    assert [c["id"] for c in motif.context(CAT)["cards"]] == ["M-mukbang-001", "M-mukbang-002"]


def test_add_rejects_type_not_allowed_at_stage(home):
    standard_set(home, n_a=3, n_b=0)
    with pytest.raises(ScriptError) as exc:
        motif.add(CAT, cards_file(home, [{**TECH_CARD, "type": "flow"}]))
    assert exc.value.code == "invalid_cards"
    assert "flow" in exc.value.message


@pytest.mark.parametrize("patch, word", [
    ({"evidence": []}, "evidence"),
    ({"evidence": ["nope123"]}, "nope123"),
    ({"title": ""}, "title"),
    ({"type": "random"}, "type"),
])
def test_add_validates_cards(home, patch, word):
    standard_set(home, n_a=3, n_b=0)
    with pytest.raises(ScriptError) as exc:
        motif.add(CAT, cards_file(home, [{**TECH_CARD, **patch}]))
    assert exc.value.code == "invalid_cards"
    assert word in exc.value.message


def test_add_rejects_mine_as_evidence(home):
    standard_set(home, n_a=3, n_b=0)
    write_video(home, "m1", role="mine")
    with pytest.raises(ScriptError) as exc:
        motif.add(CAT, cards_file(home, [{**TECH_CARD, "evidence": ["m1"]}]))
    assert "m1" in exc.value.message


def test_add_rejects_duplicate_title(home):
    standard_set(home, n_a=3, n_b=0)
    motif.add(CAT, cards_file(home, [TECH_CARD]))
    with pytest.raises(ScriptError) as exc:
        motif.add(CAT, cards_file(home, [{**TECH_CARD, "title": " 첫 1초  완성품 클로즈업 "}]))
    assert exc.value.code == "invalid_cards"
    assert "M-mukbang-001" in exc.value.message


# --- star -------------------------------------------------------------------------

def test_star_toggle(home):
    standard_set(home, n_a=3, n_b=0)
    motif.add(CAT, cards_file(home, [TECH_CARD, {**TECH_CARD, "title": "둘째"}]))
    motif.star(CAT, "M-mukbang-002")
    assert "### ★ M-mukbang-002 · 기법 · 둘째" in index_body(home)
    assert [c["starred"] for c in motif.context(CAT)["cards"]] == [False, True]
    motif.star(CAT, "M-mukbang-002")                        # 이미 ★ → 그대로
    assert index_body(home).count("### ★") == 1
    motif.star(CAT, "M-mukbang-002", off=True)
    assert "### ★" not in index_body(home)


def test_star_unknown_card(home):
    standard_set(home, n_a=3, n_b=0)
    with pytest.raises(ScriptError) as exc:
        motif.star(CAT, "M-mukbang-009")
    assert exc.value.code == "card_not_found"


# --- 기법 병합 ---------------------------------------------------------------------

def test_merge_techniques_unions_sources(home):
    standard_set(home, n_a=3, n_b=1)
    write_techniques(home)
    plan = cards_file(home, [{"kind": "훅", "keep": "결과 선공개", "merge": ["완성품 먼저 보여주기"]}], "plan.json")
    out = motif.merge_techniques(CAT, plan)
    assert out["merged"] == 1
    hooks = [t for t in motif.context(CAT)["techniques"] if t["kind"] == "훅"]
    assert hooks == [{"kind": "훅", "name": "결과 선공개", "desc": "완성품 먼저", "sources": ["a00", "a01", "b00"]}]


def test_merge_techniques_unknown_name(home):
    standard_set(home, n_a=3, n_b=0)
    write_techniques(home)
    plan = cards_file(home, [{"kind": "훅", "keep": "결과 선공개", "merge": ["없는 기법"]}], "plan.json")
    with pytest.raises(ScriptError) as exc:
        motif.merge_techniques(CAT, plan)
    assert "없는 기법" in exc.value.message


# --- CLI --------------------------------------------------------------------------

def test_main_routes(home):
    standard_set(home, n_a=3, n_b=0)
    assert motif.main(["context", CAT, "--topic", "라면"])["topic"] == "라면"
    cards_file(home, [TECH_CARD])
    assert motif.main(["add", CAT, "--cards", "workspace/tmp/cards.json"])["ids"] == ["M-mukbang-001"]
    assert motif.main(["star", CAT, "M-mukbang-001"])["starred"] is True
