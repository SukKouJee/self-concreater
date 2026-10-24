import json
from datetime import datetime, timezone

import pytest

from scripts import cluster, refstore
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, SKELETON_B, standard_set, write_video


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


def axis(result, name):
    return result["axes"][name]


# --- 단계 -------------------------------------------------------------------------

def test_too_few_refs(home):
    standard_set(home, n_a=4, n_b=0)
    out = cluster.run(CAT)
    assert out["status"] == "too_few"
    assert out["ref"] == 4 and out["need"] == 1


def test_preliminary_between_5_and_9(home):
    standard_set(home, n_a=5, n_b=2)
    assert cluster.run(CAT)["status"] == "preliminary"


def test_formal_from_10(home):
    standard_set(home)
    assert cluster.run(CAT)["status"] == "formal"


# --- 판정 -------------------------------------------------------------------------

def test_axis_verdicts(home):
    standard_set(home)
    out = cluster.run(CAT)
    s = axis(out, "structure")
    assert s["verdict"] == "typable"
    assert s["max_share"] == pytest.approx(0.7)
    assert sorted(c["size"] for c in s["clusters"]) == [3, 7]
    assert axis(out, "hook")["verdict"] == "typable"            # result_first 8/10
    assert axis(out, "narrative")["verdict"] == "untypable"     # 5종류에 고르게 흩어짐
    assert axis(out, "pacing")["verdict"] in ("typable", "weak")


def test_cluster_summaries(home):
    standard_set(home)
    out = cluster.run(CAT)
    big = max(axis(out, "structure")["clusters"], key=lambda c: c["size"])
    assert big["summary"]["skeleton"] == "hook → body/story → cta"
    hook_big = max(axis(out, "hook")["clusters"], key=lambda c: c["size"])
    assert hook_big["summary"]["hook_type"] == {"result_first": 8}


def test_mine_is_ignored(home):
    standard_set(home)
    before = cluster.run(CAT)
    for i in range(5):
        write_video(home, f"m{i}", role="mine", skeleton=SKELETON_B, hook="shock_line", avg_cut=8.0)
    after = cluster.run(CAT)
    strip = lambda r: {k: v for k, v in r.items() if k != "run_at"}
    assert strip(before) == strip(after)
    assert after["ref"] == 10


def test_edited_md_changes_result(home):
    standard_set(home)
    before = axis(cluster.run(CAT), "structure")
    # 사용자가 a00의 섹션을 골격 B로 고쳤다
    path = home / "refs" / f"ref_{CAT}" / "videos" / "a00.md"
    refstore.update_frontmatter(path, {"sections": [
        {"label": label, **({"sub": sub} if sub else {}), "start": float(start), "end": float(end)}
        for label, sub, start, end in SKELETON_B]})
    after = axis(cluster.run(CAT), "structure")
    assert sorted(c["size"] for c in before["clusters"]) == [3, 7]
    assert sorted(c["size"] for c in after["clusters"]) == [4, 6]


def test_outliers(home):
    standard_set(home, odd=True)
    out = cluster.run(CAT)
    assert "zodd" in [o["id"] for o in out["outliers"]]
    assert "structure" in next(o for o in out["outliers"] if o["id"] == "zodd")["axes"]


def test_categorical_singletons_are_outliers_only_on_typable_axes(home):
    standard_set(home)
    # a05의 서사를 바꿔 a00이 problem_solution으로 혼자 남게 한다 (narrative는 여전히 typable 아님)
    refstore.update_frontmatter(home / "refs" / f"ref_{CAT}" / "videos" / "a05.md", {"narrative_type": "list"})
    out = cluster.run(CAT)
    assert out["axes"]["narrative"]["verdict"] != "typable"
    assert "a00" in out["axes"]["narrative"]["singletons"]
    # hook은 typable(result_first 8/10) → 혼자인 question·situation 영상은 이상치
    # narrative는 untypable → 혼자인 값이 있어도 이상치가 아니다
    flagged = {o["id"]: o["axes"] for o in out["outliers"]}
    assert "a06" in flagged and "hook" in flagged["a06"]
    assert all("narrative" not in axes and "title" not in axes for axes in flagged.values())


def test_archetype_card_skips_missing_pacing_values(home):
    standard_set(home)
    for vid in ["b00", "b01", "b02"]:
        path = home / "refs" / f"ref_{CAT}" / "videos" / f"{vid}.md"
        refstore.update_frontmatter(path, {"pacing": {"avg_cut_sec": 4.0, "by_section": {"hook": 2.0}}})
    cluster.run(CAT)
    cluster.apply(CAT, notes(home, NOTES))
    _, body = refstore.read_md(home / "refs" / f"ref_{CAT}" / "index.md")
    assert "None" not in body
    assert "평균 컷 4.0초 (hook 2.0)" in body


def test_identical_points_are_one_type(home):
    refstore.init_category(CAT)
    for i in range(6):
        write_video(home, f"s{i}", hook="question")
    hook = axis(cluster.run(CAT), "hook")
    assert hook["verdict"] == "typable"
    assert hook["k"] == 1 and hook["max_share"] == 1.0


# --- 아키타입 ---------------------------------------------------------------------

def test_archetypes_from_structure_clusters(home):
    standard_set(home, odd=True)
    out = cluster.run(CAT)
    arcs = out["archetypes"]
    assert [a["size"] for a in arcs] == [7, 3]                  # 단독 군집(이상치)은 아키타입이 아니다
    a1 = arcs[0]
    assert a1["id"] == "A1"
    assert a1["skeleton"] == "hook → body/story → cta"
    assert set(a1["mean_ratio"]) == {"hook", "body", "cta"}
    assert a1["representative"]["id"] in a1["members"]
    assert a1["hook_type"]["result_first"] == 6
    assert a1["camera"] == {"static": 0.9, "pan": 0.1, "zoom": 0.0}
    assert a1["pacing"]["avg_cut_sec"] == pytest.approx(1.35, abs=0.01)
    assert a1["prev_name"] is None


def test_run_saves_result(home):
    standard_set(home)
    cluster.run(CAT)
    saved = json.loads((home / "workspace" / "typify" / f"{CAT}.json").read_text("utf-8"))
    assert saved["status"] == "formal"


# --- apply ------------------------------------------------------------------------

def notes(home, data):
    path = home / "workspace" / "tmp" / "typify_notes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


NOTES = {
    "axes": {
        "structure": "10개 중 7개가 짧은 훅 뒤 긴 이야기형 본론, 끝에 구독 요청으로 끝난다.",
        "hook": "8개가 완성품을 먼저 보여 주며 시작한다.",
        "narrative": "다섯 유형에 2개씩 흩어져 공통 서사가 없다.",
        "title": "제목 패턴은 두 갈래로 나뉜다.",
        "pacing": "대부분 1초대 빠른 컷이고 3개만 4초대다.",
    },
    "archetypes": [{"id": "A1", "name": "결과 먼저 몰아먹기"}, {"id": "A2", "name": "사이즈별 리스트 비교"}],
}


def test_apply_writes_index_sections(home):
    standard_set(home, odd=True)
    index = home / "refs" / f"ref_{CAT}" / "index.md"
    index.write_text(index.read_text("utf-8").replace("## 개요\n", "## 개요\n내가 쓴 개요\n"), encoding="utf-8")
    cluster.run(CAT)

    out = cluster.apply(CAT, notes(home, NOTES))

    fm, body = refstore.read_md(index)
    assert out["status"] == "applied"
    assert "내가 쓴 개요" in body
    judge = body.split("## 판정")[1].split("## 아키타입")[0]
    assert "| structure | typable" in judge
    assert "10개 중 7개가 짧은 훅" in judge
    arc = body.split("## 아키타입")[1].split("## 모티브 카드")[0]
    assert "### A1. 결과 먼저 몰아먹기 (7개)" in arc
    assert "- 골격: hook → body/story → cta" in arc
    assert "- 촬영 경향: 고정 90% · 패닝 10% · 줌 0%" in arc
    assert "[a0" in arc and "](videos/a0" in arc
    outliers = body.split("## 이상치")[1]
    assert "zodd" in outliers
    assert fm["archetypes"][0] == {"id": "A1", "name": "결과 먼저 몰아먹기",
                                   "members": ["a00", "a01", "a02", "a03", "a04", "a05", "a06"]}


def test_apply_requires_run_and_names(home):
    standard_set(home)
    with pytest.raises(ScriptError) as exc:
        cluster.apply(CAT, notes(home, NOTES))
    assert exc.value.code == "no_typify_result"
    cluster.run(CAT)
    with pytest.raises(ScriptError) as exc:
        cluster.apply(CAT, notes(home, {**NOTES, "archetypes": [{"id": "A1", "name": "x"}]}))
    assert exc.value.code == "invalid_notes"
    assert "A2" in exc.value.message


def test_rerun_suggests_previous_names(home):
    standard_set(home)
    cluster.run(CAT)
    cluster.apply(CAT, notes(home, NOTES))
    write_video(home, "a99", shift=0.3)
    arcs = cluster.run(CAT)["archetypes"]
    assert arcs[0]["prev_name"] == "결과 먼저 몰아먹기"
    assert arcs[1]["prev_name"] == "사이즈별 리스트 비교"


def test_preliminary_apply_marks_header(home):
    standard_set(home, n_a=5, n_b=2)
    cluster.run(CAT)
    cluster.apply(CAT, notes(home, {**NOTES, "archetypes": [
        {"id": "A1", "name": "a"}, {"id": "A2", "name": "b"}]}))
    _, body = refstore.read_md(home / "refs" / f"ref_{CAT}" / "index.md")
    assert "예비 관찰" in body.split("## 판정")[1].split("## 아키타입")[0]


def test_main_routes(home):
    standard_set(home)
    assert cluster.main(["run", CAT])["status"] == "formal"
    notes(home, NOTES)
    out = cluster.main(["apply", CAT, "--notes", "workspace/tmp/typify_notes.json"])
    assert out["status"] == "applied"


# --- apply 스냅숏 · 검증 -------------------------------------------------------------

def test_apply_writes_applied_snapshot(home):
    standard_set(home, odd=True)
    result = cluster.run(CAT)

    cluster.apply(CAT, notes(home, NOTES))

    snap = json.loads(cluster.applied_path(CAT).read_text("utf-8"))
    assert snap["run_at"] == result["run_at"] and snap["status"] == "formal" and snap["ref"] == result["ref"]
    assert snap["applied_at"]
    assert snap["axes"]["structure"]["note"] == NOTES["axes"]["structure"]
    assert set(snap["axes"]["structure"]) == {"verdict", "n", "k", "silhouette", "max_share", "note"}
    assert snap["archetypes"][0]["name"] == "결과 먼저 몰아먹기" and snap["archetypes"][0]["id"] == "A1"
    assert any(o["id"] == "zodd" for o in snap["outliers"])


def test_run_at_has_microseconds_and_report_prefix_still_date(home):
    standard_set(home)
    run_at = cluster.run(CAT)["run_at"]
    assert "." in run_at and run_at[:10] == datetime.now(timezone.utc).date().isoformat()


def test_video_titles_covers_result_ids_and_applied_snapshot(home):
    standard_set(home, odd=True)
    result = cluster.run(CAT)
    titles = cluster.video_titles(CAT, result)
    ids = {v for a in result["archetypes"] for v in a["members"]} | {o["id"] for o in result["outliers"]}
    ids |= {v for res in result["axes"].values() for c in res["clusters"] for v in c["members"]}
    assert ids <= set(titles) and all(isinstance(t, str) for t in titles.values())

    cluster.apply(CAT, notes(home, NOTES))
    assert json.loads(cluster.applied_path(CAT).read_text("utf-8"))["titles"] == titles


@pytest.mark.parametrize("bad", [
    [], "x", {"axes": []}, {"axes": {}, "archetypes": ["x"]}, {"axes": {}, "archetypes": [{"name": "a"}]},
    {"axes": {}, "archetypes": {"A1": "a"}}, {"axes": "x"},
])
def test_validate_notes_malformed_shape(home, bad):
    standard_set(home)
    result = cluster.run(CAT)
    with pytest.raises(ScriptError) as exc:
        cluster.validate_notes(result, bad)
    assert exc.value.code == "invalid_notes" and "notes 형식이 맞지 않습니다" in exc.value.message


def test_apply_rejects_stale_run_at(home):
    standard_set(home)
    cluster.run(CAT)
    index = home / "refs" / f"ref_{CAT}" / "index.md"
    before = index.read_bytes()

    with pytest.raises(ScriptError) as exc:
        cluster.apply(CAT, notes(home, NOTES), expected_run_at="2000-01-01T00:00:00+00:00")

    assert exc.value.code == "typify_changed"
    assert index.read_bytes() == before
    assert not cluster.applied_path(CAT).exists()


@pytest.mark.parametrize("broken", [
    {**NOTES, "axes": {k: v for k, v in NOTES["axes"].items() if k != "hook"}},
    {**NOTES, "archetypes": [{"id": "A1", "name": "x"}, {"id": "A2", "name": " "}]},
])
def test_validate_notes_missing(home, broken):
    standard_set(home)
    result = cluster.run(CAT)
    with pytest.raises(ScriptError) as exc:
        cluster.validate_notes(result, broken)
    assert exc.value.code == "invalid_notes"
    assert "빠졌습니다" in exc.value.message


def test_validate_notes_returns_axis_notes_and_names(home):
    standard_set(home)
    result = cluster.run(CAT)
    axis_notes, names = cluster.validate_notes(result, NOTES)
    assert axis_notes["hook"] == NOTES["axes"]["hook"] and names == {"A1": "결과 먼저 몰아먹기", "A2": "사이즈별 리스트 비교"}
