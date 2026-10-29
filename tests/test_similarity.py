import json

import pytest

from scripts import cluster, draft, refstore, similarity
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set, write_video


def _json(home, name, data):
    path = home / "workspace" / "tmp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    standard_set(tmp_path)
    cluster.run(CAT)
    cluster.apply(CAT, _json(tmp_path, "n.json", {
        "axes": {a: "근거" for a in ["structure", "hook", "narrative", "title", "pacing"]},
        "archetypes": [{"id": "A1", "name": "결과 먼저 몰아먹기"}, {"id": "A2", "name": "사이즈별 리스트 비교"}],
    }))
    return tmp_path


def shots(sec, n):
    return [{"size": "클로즈업", "sec": sec / n, "action": "x"} for _ in range(n)]


def make_draft(home, sections, hook="result_first", narrative="journey", title="라면 5봉지 먹방!", script_only=False):
    data = {"title": title, "topic": "t", "source": "A1", "archetype": "A1",
            "hook_type": hook, "narrative_type": narrative, "script_only": script_only,
            "sections": [{"label": l, **({"sub": s} if s else {}), "start": a, "end": b, "script": "x",
                          **({} if script_only else {"shots": shots(b - a, max(1, round((b - a) / 1.3))) })}
                         for l, s, a, b in sections]}
    return home / draft.save(CAT, _json(home, "d.json", data))["path"]


LIKE_A1 = [("hook", None, 0, 3), ("body", "story", 3, 52), ("cta", None, 52, 60)]
UNLIKE = [("intro", None, 0, 20), ("demo", None, 20, 55), ("outro", None, 55, 60)]


# --- compare ----------------------------------------------------------------------

def test_draft_following_archetype_is_close(home):
    out = similarity.compare(CAT, make_draft(home, LIKE_A1))
    assert out["nearest"]["id"] == "A1"
    ax = out["axes"]
    assert ax["structure"]["proximity"] >= 0.5
    assert ax["hook"]["proximity"] == pytest.approx(6 / 7, abs=0.01)
    assert ax["pacing"] == {"proximity": None, "note": "컷 정보 없음"}       # 장면 단위 기획: 컷 길이로 재지 않는다
    assert [g["key"] for g in out["gaps"]] == []
    assert out["preliminary"] is False


def test_small_tight_archetype_still_recognized(home):
    # 실사용에서 나온 회귀: A2(3개, 거의 같은 영상들)를 따라 만든 기획인데 섹션 수가 하나 달라
    # 구성 영상 기준 백분위가 0%가 되고 가장 가까운 아키타입도 A1로 잘못 골랐다
    like_a2_fewer_sections = [("hook", None, 0, 5), ("body", "list_item", 5, 52), ("outro", None, 52, 60)]
    out = similarity.compare(CAT, make_draft(home, like_a2_fewer_sections, narrative="list"))
    assert out["nearest"]["id"] == "A2"
    assert out["axes"]["structure"]["proximity"] >= 0.5


def test_restructured_draft_is_far_with_gaps(home):
    out = similarity.compare(CAT, make_draft(home, UNLIKE, hook="shock_line"))
    assert out["axes"]["structure"]["proximity"] <= 0.2
    keys = [g["key"] for g in out["gaps"]]
    assert "structure:body" in keys and "structure:demo" in keys and "hook" in keys
    body = next(g for g in out["gaps"] if g["key"] == "structure:body")
    assert body["target"] == 0 and body["archetype"] > 0.5


def test_old_draft_stored_pacing_still_scored(home):
    path = make_draft(home, LIKE_A1)
    refstore.update_frontmatter(path, {"pacing": {"avg_cut_sec": 1.3, "by_section": {"hook": 1.5, "body": 1.29}}})
    out = similarity.compare(CAT, path)
    assert out["axes"]["pacing"]["proximity"] is not None


def test_script_only_skips_pacing(home):
    out = similarity.compare(CAT, make_draft(home, LIKE_A1, script_only=True))
    assert out["axes"]["pacing"]["proximity"] is None
    assert out["axes"]["pacing"]["note"] == "컷 정보 없음"


def test_mine_video_md_as_target(home):
    path = write_video(home, "mine01", role="mine")
    out = similarity.compare(CAT, path)
    assert out["target"]["name"] == "mine01" and out["nearest"]["id"] == "A1"


def test_compare_requires_typify(home, monkeypatch, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    monkeypatch.setenv("YT_COACH_HOME", str(other))
    standard_set(other)
    with pytest.raises(ScriptError) as exc:
        similarity.compare(CAT, other / "refs" / f"ref_{CAT}" / "videos" / "a00.md")
    assert exc.value.code == "no_typify_result"


def test_ref_video_is_not_a_diagnosis_target(home):
    with pytest.raises(ScriptError) as exc:
        similarity.compare(CAT, home / "refs" / f"ref_{CAT}" / "videos" / "a00.md")
    assert exc.value.code == "not_mine"


# --- save · intentional ------------------------------------------------------------

NOTES = {"summary": "구조가 아키타입과 다르다.", "actions": ["본론을 40초 이상으로", "훅을 결과 선공개로"]}


def notes_for(home, target):
    """compare가 낸 갭마다 설명을 붙인 notes 파일."""
    gaps = similarity.compare(CAT, target)["gaps"]
    return _json(home, "notes.json", {**NOTES, "gaps": {g["key"]: f"{g['key']} 설명" for g in gaps}})


def test_save_writes_mine_md_and_history(home):
    target = make_draft(home, UNLIKE, hook="shock_line")
    out = similarity.save(CAT, target, notes_for(home, target))
    path = home / out["path"]
    assert out["path"] == f"mine/{target.stem}.md"
    fm, body = refstore.read_md(path)
    assert fm["nearest"] == {"id": "A2", "name": "사이즈별 리스트 비교"}
    assert fm["target"] == f"drafts/{target.name}"
    assert "structure:body" in fm["gaps"]
    assert len(fm["history"]) == 1
    assert "## 다음 행동" in body and "1. 본론을 40초 이상으로" in body
    assert "structure:body 설명" in body
    assert "| structure |" in body
    assert body.rstrip().endswith("## 내 메모")


def test_intentional_gap_excluded_on_rediagnosis(home):
    target = make_draft(home, UNLIKE, hook="shock_line")
    similarity.save(CAT, target, notes_for(home, target))
    p = home / "mine" / f"{target.stem}.md"
    p.write_text(p.read_text("utf-8") + "직접 쓴 메모\n", encoding="utf-8", newline="")

    similarity.intentional(CAT, target.stem, "hook")
    out = similarity.compare(CAT, target)
    assert "hook" not in [g["key"] for g in out["gaps"]]
    assert out["intentional"] == ["hook"]

    similarity.save(CAT, target, notes_for(home, target))
    fm, body = refstore.read_md(p)
    assert fm["intentional"] == ["hook"] and "hook" not in fm["gaps"]
    assert len(fm["history"]) == 2
    assert body.endswith("## 내 메모\n직접 쓴 메모\n")

    similarity.intentional(CAT, target.stem, "hook", off=True)
    assert "hook" in [g["key"] for g in similarity.compare(CAT, target)["gaps"]]


def test_save_rejects_missing_gap_notes(home):
    target = make_draft(home, UNLIKE, hook="shock_line")
    with pytest.raises(ScriptError) as exc:
        similarity.save(CAT, target, _json(home, "notes.json", {**NOTES, "gaps": {}}))
    assert exc.value.code == "invalid_notes"


def test_diagnosis_never_changes_archetypes(home):
    before = json.loads((home / "workspace" / "typify" / f"{CAT}.json").read_text("utf-8"))["archetypes"]
    target = make_draft(home, UNLIKE, hook="shock_line")
    similarity.save(CAT, target, notes_for(home, target))
    write_video(home, "mine01", role="mine")
    after = cluster.run(CAT)["archetypes"]
    strip = lambda arcs: [{k: v for k, v in a.items() if k != "prev_name"} for a in arcs]
    assert strip(after) == strip(before)


def test_main_routes(home):
    target = make_draft(home, LIKE_A1)
    rel = f"drafts/{target.name}"
    assert similarity.main(["compare", CAT, rel])["nearest"]["id"] == "A1"
    _json(home, "notes.json", {"summary": "가깝다", "gaps": {}, "actions": ["그대로 찍기"]})
    assert similarity.main(["save", CAT, rel, "--notes", "workspace/tmp/notes.json"])["status"] == "saved"
    assert similarity.main(["intentional", CAT, target.stem, "hook"])["intentional"] == ["hook"]


# --- save 검증 메시지 특성 · validate_notes ------------------------------------------

def _save_error(home, notes):
    target = make_draft(home, UNLIKE, hook="shock_line")
    with pytest.raises(ScriptError) as exc:
        similarity.save(CAT, target, _json(home, "notes.json", notes))
    return exc.value


def test_save_validation_messages_are_kept(home):
    target = make_draft(home, UNLIKE, hook="shock_line")
    gaps = {g["key"]: "설명" for g in similarity.compare(CAT, target)["gaps"]}
    base = "요약, 모든 갭의 설명, 다음 행동 1~3개가 필요합니다"
    err = _save_error(home, {"summary": "요약", "gaps": {}, "actions": ["a"]})
    assert err.code == "invalid_notes" and err.message.startswith(base + " (빠진 갭: ")
    assert err.message.endswith(", ".join(gaps) + ")")
    assert _save_error(home, {"summary": " ", "gaps": gaps, "actions": ["a"]}).message == base
    assert _save_error(home, {"summary": "요약", "gaps": gaps, "actions": []}).message == base
    assert _save_error(home, {"summary": "요약", "gaps": gaps, "actions": ["a", "b", "c", "d"]}).message == base


def test_validate_notes_returns_cleaned_notes(home):
    target = make_draft(home, UNLIKE, hook="shock_line")
    result = similarity.compare(CAT, target)
    gaps = {g["key"]: f" {g['key']} 설명 " for g in result["gaps"]}
    out = similarity.validate_notes(result, {"summary": "  한 줄   요약 ", "gaps": {**gaps, "사라진:갭": "무시"},
                                              "actions": [" 하나 ", "", "둘"]})
    assert out["summary"] == "한 줄 요약"
    assert out["actions"] == ["하나", "둘"]
    assert out["gaps"] == {k: v.strip() for k, v in gaps.items()}      # 여분 갭 설명은 버린다


@pytest.mark.parametrize("notes", [
    None, [], "x", {"summary": 5, "gaps": {}, "actions": ["a"]},
    {"summary": "s", "gaps": [], "actions": ["a"]}, {"summary": "s", "gaps": {}, "actions": "a"},
    {"summary": "s", "gaps": {}, "actions": [1]}, {"summary": "s", "gaps": {"hook": 3}, "actions": ["a"]},
])
def test_validate_notes_rejects_bad_shape(home, notes):
    target = make_draft(home, LIKE_A1)
    with pytest.raises(ScriptError) as exc:
        similarity.validate_notes(similarity.compare(CAT, target), notes)
    assert exc.value.code == "invalid_notes" and "형식" in exc.value.message
