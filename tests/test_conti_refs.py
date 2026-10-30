"""Task 4: 장면에 사진 고르기 — 설명(desc)이 있는 프레임만 후보, Claude는 글로만 고르고 결과는 컷 ref_auto."""
import json

import pytest

from scripts import claude_cli, conti, conti_history, conti_refs, llm, refstore
from scripts.common import ScriptError
from tests.test_conti import conti_file, draft_md, home, make_frames, saved  # noqa: F401 - fixtures
from tests.helpers.paths import doc_key


def describe(home, vid, descs, size="클로즈업", angle="눈높이"):
    """make_frames로 만든 영상 프레임에 라벨을 붙인다. descs[i]가 None이면 desc 없는 라벨."""
    frames = make_frames(home, vid)
    labels = []
    for frame, desc in zip(frames, descs):
        lab = {"frame": frame, "size": size, "angle": angle, "section": "body"}
        if desc is not None:
            lab["desc"] = desc
        labels.append(lab)
    path = home / "workspace" / "shots" / f"{vid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": vid, "labels": labels}, ensure_ascii=False), encoding="utf-8")
    return frames


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, rules, payload, task, validate, **kw):
        self.calls.append((rules, payload, task))
        data = self.answer(payload) if callable(self.answer) else self.answer
        return data, validate(data)


@pytest.fixture
def fake(monkeypatch):
    f = FakeLLM({"picks": []})
    monkeypatch.setattr(llm, "ask_validated", f)
    return f


def test_candidates_only_described_frames(home):
    a = describe(home, "a00", ["시장 골목, 사람 많음", None, "국물 클로즈업", None])
    make_frames(home, "a01")                                   # 라벨 파일이 없는 영상: 후보 아님
    refstore.update_frontmatter(doc_key(home / "refs" / "ref_mukbang" / "videos" / "a01.md"),
                                {"shots": {"by_section": {"body": "클로즈업"}}})
    got = conti_refs.candidates("mukbang")
    assert [c["frame"] for c in got] == [a[0], a[2]]
    assert got[0] == {"frame": a[0], "desc": "시장 골목, 사람 많음", "size": "클로즈업", "angle": "눈높이"}


def test_candidates_skip_missing_files_and_mine(home):
    a = describe(home, "a00", ["하나", "둘", None, None])
    (home / a[1]).unlink()
    refstore.update_frontmatter(doc_key(home / "refs" / "ref_mukbang" / "videos" / "a01.md"), {"role": "mine"})
    describe(home, "a01", ["내 영상", None, None, None])
    assert [c["frame"] for c in conti_refs.candidates("mukbang")] == [a[0]]
    assert conti_refs.candidates(None) == []


def key(payload, desc):
    """후보 목록(짧은 키 → "desc | size | angle")에서 desc로 키를 찾는다."""
    return next(k for k, line in payload["candidates"].items() if line.split(" | ")[0] == desc)


def scene_cuts(n):
    return [{"id": f"c{i:07d}", "action": f"장면 {i}", "guide": "가이드", "size": "클로즈업", "line": "말"}
            for i in range(n)]


def test_pick_rejects_unknown_frame(home, fake):
    a = describe(home, "a00", ["시장 골목", "국물", None, None])
    other = describe(home, "zz9", ["다른 카테고리 사진", None, None, None])   # 이 카테고리 md가 없는 영상
    fake.answer = lambda payload: {"picks": [
        {"id": "c0000000", "frame": key(payload, "시장 골목")},
        {"id": "c0000001", "frame": "c99"},                      # 없는 키
        {"id": "c0000004", "frame": a[1]},                       # 키가 아니라 경로: 버린다
        {"id": "nope0000", "frame": key(payload, "국물")},        # 없는 장면
        {"id": "c0000003", "frame": None},
        {"id": ["c0000001"], "frame": key(payload, "국물")},      # id가 글이 아니면 버린다
    ]}
    result = conti_refs.pick(scene_cuts(5), "mukbang")
    assert result.picks == {"c0000000": a[0]} and result.error is None
    rules, payload, _ = fake.calls[0]
    assert rules == [claude_cli.SKILLS_DIR / "yt-conti" / "pick-refs.md"]
    assert payload["scenes"][0] == {"id": "c0000000", "action": "장면 0", "guide": "가이드", "size": "클로즈업"}
    # 후보는 짧은 키 → 한 줄(설명 | 크기 | 앵글). 다른 카테고리·설명 없는 사진은 없다
    assert payload["candidates"] == {"c1": "시장 골목 | 클로즈업 | 눈높이", "c2": "국물 | 클로즈업 | 눈높이"}
    assert other[0] not in str(payload)


def test_pick_bad_shape_is_invalid(home, fake):
    describe(home, "a00", ["시장 골목", None, None, None])
    fake.answer = {"pick": []}
    with pytest.raises(ScriptError) as exc:
        conti_refs.pick(scene_cuts(1), "mukbang")
    assert exc.value.code == "invalid_refs"


def test_pick_batches_120(home, fake):
    a = describe(home, "a00", ["하나", "둘", "셋", "넷"])

    def answer(payload):
        return {"picks": [{"id": s["id"], "frame": key(payload, "하나")} for s in payload["scenes"]]}

    fake.answer = answer
    result = conti_refs.pick(scene_cuts(250), "mukbang")
    assert [len(p["scenes"]) for _, p, _ in fake.calls] == [120, 120, 10]
    assert len(result.picks) == 250 and set(result.picks.values()) == {a[0]}
    assert fake.calls[0][1]["used"] == [] and fake.calls[1][1]["used"] == ["c1"]   # 앞 묶음에서 쓴 사진(키)을 알려 준다


def test_pick_later_batch_failure_keeps_earlier_batches(home, fake):
    describe(home, "a00", ["하나", None, None, None])

    def answer(payload):
        if len(fake.calls) > 1:
            raise ScriptError("claude_failed", "응답 없음")
        return {"picks": [{"id": s["id"], "frame": key(payload, "하나")} for s in payload["scenes"]]}

    fake.answer = answer
    cuts = scene_cuts(130)
    result = conti_refs.pick(cuts, "mukbang")
    assert len(result.picks) == 120 and result.error.code == "claude_failed"
    assert set(result.basis) == {c["id"] for c in cuts[:120]}
    assert result.basis["c0000000"] == {"action": "장면 0", "guide": "가이드", "size": "클로즈업"}


def test_pick_first_batch_failure_raises(home, fake):
    describe(home, "a00", ["하나", None, None, None])

    def answer(payload):
        raise ScriptError("claude_failed", "응답 없음")

    fake.answer = answer
    with pytest.raises(ScriptError) as exc:
        conti_refs.pick(scene_cuts(3), "mukbang")
    assert exc.value.code == "claude_failed"


def test_check_merged_refuses_repeats():
    cuts = [{"n": 1, "id": "a", "section": 0, "action": "라면 먹기"}, {"n": 2, "id": "b", "section": 0, "action": "물"},
            {"n": 3, "id": "c", "section": 0, "action": "라면 먹기"}]
    with pytest.raises(ScriptError) as exc:
        conti_refs.check_merged(cuts)
    assert exc.value.code == "merge_first" and exc.value.message == "반복 컷을 먼저 장면으로 합쳐 주세요"
    conti_refs.check_merged(cuts[:2])


def test_pick_without_candidates_does_not_call(home, fake):
    make_frames(home, "a00")
    result = conti_refs.pick(scene_cuts(3), "mukbang")
    assert result.picks == {} and result.basis == {} and not fake.calls


def test_apply_picks_sets_and_clears():
    cuts = [{"id": "a", "ref_auto": {"frame": "workspace/frames/x.jpg", "match": "content"}},
            {"id": "b"}, {"id": "c", "ref_frame": "workspace/frames/pin.jpg"}]
    out = conti_refs.apply_picks(cuts, {"b": "workspace/frames/y.jpg", "c": "workspace/frames/z.jpg"})
    assert "ref_auto" not in out[0]
    assert out[1]["ref_auto"] == {"frame": "workspace/frames/y.jpg", "match": "content"}
    assert out[2]["ref_frame"] == "workspace/frames/pin.jpg" and out[2]["ref_auto"]["frame"] == "workspace/frames/z.jpg"
    assert "ref_auto" in cuts[0] and "ref_auto" not in cuts[1]          # 원본은 그대로


def basis_of(cuts):
    return {c["id"]: {f: c[f] for f in conti.PICK_FIELDS} for c in cuts}


def test_save_picks_writes_ref_auto_and_history(saved, home):
    a = describe(home, "a00", ["하나", None, None, None])
    fm = conti._load_conti(saved)
    picks = {fm["cuts"][1]["id"]: a[0]}
    result = conti_refs.save_picks(saved, picks, basis_of(fm["cuts"]))
    stored, body = refstore.read(doc_key(saved))
    assert stored["cuts"][1]["ref_auto"] == {"frame": a[0], "match": "content"}
    assert all("ref_auto" not in c for i, c in enumerate(stored["cuts"]) if i != 1)
    assert result["hash"] == conti.content_hash(saved) and result["picked"] == 1 and result["scenes"] == 4
    assert "## 내 메모" in body
    [latest, *_] = conti_history.list_versions(saved)
    assert latest["kind"] == "auto"
    state = conti_history.get_version(saved, latest["n"])["state"]
    assert state["cuts"][1]["ref_auto"] == {"frame": a[0], "match": "content"}


def test_save_picks_survives_unrelated_edit(saved, home):
    a = describe(home, "a00", ["하나", None, None, None])
    fm = conti._load_conti(saved)
    basis = basis_of(fm["cuts"])
    conti.update_cut(saved, 4, {"line": "고르는 사이 고친 대사"})          # 다른 컷의 대사: 고른 근거와 무관
    result = conti_refs.save_picks(saved, {fm["cuts"][0]["id"]: a[0], fm["cuts"][2]["id"]: a[0]}, basis)
    stored, _ = refstore.read(doc_key(saved))
    assert stored["cuts"][0]["ref_auto"]["frame"] == a[0] and stored["cuts"][2]["ref_auto"]["frame"] == a[0]
    assert stored["cuts"][3]["line"] == "고르는 사이 고친 대사"
    assert result["skipped"] == 0 and result["picked"] == 2


def test_save_picks_skips_cut_edited_or_unpicked_during_pick(saved, home):
    a = describe(home, "a00", ["하나", None, None, None])
    fm = conti._load_conti(saved)
    ids = [c["id"] for c in fm["cuts"]]
    basis = basis_of(fm["cuts"])
    stored, _ = refstore.read(doc_key(saved))
    stored["cuts"][3]["ref_auto"] = {"frame": a[0], "match": "content"}      # 고르기 전부터 있던 사진
    refstore.update_frontmatter(doc_key(saved), {"cuts": stored["cuts"]})
    conti.update_cut(saved, 1, {"action": "바뀐 동작"})                        # 고른 근거가 바뀐 컷
    picks = {ids[0]: a[0], ids[1]: a[0]}                                     # ids[2]는 맞는 사진 없음
    del basis[ids[3]]                                                        # ids[3]은 실패한 묶음(고르지 않음)
    result = conti_refs.save_picks(saved, picks, basis)
    stored, _ = refstore.read(doc_key(saved))
    by_id = {c["id"]: c for c in stored["cuts"]}
    assert "ref_auto" not in by_id[ids[0]] and by_id[ids[1]]["ref_auto"]["frame"] == a[0]
    assert by_id[ids[3]]["ref_auto"]["frame"] == a[0]                        # 고르지 않은 장면은 그대로
    assert result["skipped"] == 1


def test_pick_used_skips_pinned_scenes(home, fake):
    a = describe(home, "a00", ["하나", "둘", None, None])
    cuts = scene_cuts(121)
    cuts[0]["ref_frame"] = "workspace/frames/pin.jpg"          # 고정한 장면의 고른 사진은 화면에 안 보인다

    def answer(payload):
        return {"picks": [{"id": s["id"], "frame": key(payload, "하나")} for s in payload["scenes"]
                          if s["id"] == "c0000000"]}

    fake.answer = answer
    assert conti_refs.pick(cuts, "mukbang").picks == {"c0000000": a[0]}
    assert fake.calls[1][1]["used"] == []
