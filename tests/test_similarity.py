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
    assert ax["pacing"]["proximity"] is not None
    assert [g["key"] for g in out["gaps"]] == []
    assert out["preliminary"] is False


def test_restructured_draft_is_far_with_gaps(home):
    out = similarity.compare(CAT, make_draft(home, UNLIKE, hook="shock_line"))
    assert out["axes"]["structure"]["proximity"] <= 0.2
    keys = [g["key"] for g in out["gaps"]]
    assert "structure:body" in keys and "structure:demo" in keys and "hook" in keys
    body = next(g for g in out["gaps"] if g["key"] == "structure:body")
    assert body["target"] == 0 and body["archetype"] > 0.7


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

NOTES = {"summary": "구조가 A1과 다르다.", "gaps": {"structure:body": "본론이 없다", "structure:demo": "시연이 길다",
                                                "structure:intro": "인트로가 길다", "hook": "충격 훅",
                                                "structure:cta": "구독 요청 없음", "structure:outro": "끝맺음"},
         "actions": ["본론을 40초 이상으로", "훅을 결과 선공개로"]}


def test_save_writes_mine_md_and_history(home):
    target = make_draft(home, UNLIKE, hook="shock_line")
    similarity.compare(CAT, target)
    out = similarity.save(CAT, target, _json(home, "notes.json", NOTES))
    path = home / out["path"]
    assert out["path"] == f"mine/{target.stem}.md"
    fm, body = refstore.read_md(path)
    assert fm["nearest"] == {"id": "A1", "name": "결과 먼저 몰아먹기"}
    assert fm["target"] == f"drafts/{target.name}"
    assert "structure:body" in fm["gaps"]
    assert len(fm["history"]) == 1
    assert "## 다음 행동" in body and "1. 본론을 40초 이상으로" in body
    assert "| structure |" in body
    assert body.rstrip().endswith("## 내 메모")


def test_intentional_gap_excluded_on_rediagnosis(home):
    target = make_draft(home, UNLIKE, hook="shock_line")
    similarity.compare(CAT, target)
    similarity.save(CAT, target, _json(home, "notes.json", NOTES))
    p = home / "mine" / f"{target.stem}.md"
    p.write_text(p.read_text("utf-8") + "직접 쓴 메모\n", encoding="utf-8", newline="")

    similarity.intentional(CAT, target.stem, "hook")
    out = similarity.compare(CAT, target)
    assert "hook" not in [g["key"] for g in out["gaps"]]
    assert out["intentional"] == ["hook"]

    similarity.save(CAT, target, _json(home, "notes.json", NOTES))
    fm, body = refstore.read_md(p)
    assert fm["intentional"] == ["hook"] and "hook" not in fm["gaps"]
    assert len(fm["history"]) == 2
    assert body.endswith("## 내 메모\n직접 쓴 메모\n")

    similarity.intentional(CAT, target.stem, "hook", off=True)
    assert "hook" in [g["key"] for g in similarity.compare(CAT, target)["gaps"]]


def test_save_rejects_missing_gap_notes(home):
    target = make_draft(home, UNLIKE, hook="shock_line")
    similarity.compare(CAT, target)
    with pytest.raises(ScriptError) as exc:
        similarity.save(CAT, target, _json(home, "notes.json", {**NOTES, "gaps": {}}))
    assert exc.value.code == "invalid_notes"


def test_diagnosis_never_changes_archetypes(home):
    before = json.loads((home / "workspace" / "typify" / f"{CAT}.json").read_text("utf-8"))["archetypes"]
    target = make_draft(home, UNLIKE, hook="shock_line")
    similarity.compare(CAT, target)
    similarity.save(CAT, target, _json(home, "notes.json", NOTES))
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
