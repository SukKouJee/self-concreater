"""Task 4: 장면에 사진 고르기 — 설명(desc)이 있는 프레임만 후보, Claude는 글로만 고르고 결과는 컷 ref_auto."""
import json

import pytest

from scripts import claude_cli, conti, conti_history, conti_refs, llm, refstore
from scripts.common import ScriptError
from tests.test_conti import conti_file, draft_md, home, make_frames, saved  # noqa: F401 - fixtures


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
    refstore.update_frontmatter(home / "refs" / "ref_mukbang" / "videos" / "a01.md",
                                {"shots": {"by_section": {"body": "클로즈업"}}})
    got = conti_refs.candidates("mukbang")
    assert [c["frame"] for c in got] == [a[0], a[2]]
    assert got[0] == {"frame": a[0], "desc": "시장 골목, 사람 많음", "size": "클로즈업", "angle": "눈높이"}


def test_candidates_skip_missing_files_and_mine(home):
    a = describe(home, "a00", ["하나", "둘", None, None])
    (home / a[1]).unlink()
    refstore.update_frontmatter(home / "refs" / "ref_mukbang" / "videos" / "a01.md", {"role": "mine"})
    describe(home, "a01", ["내 영상", None, None, None])
    assert [c["frame"] for c in conti_refs.candidates("mukbang")] == [a[0]]
    assert conti_refs.candidates(None) == []


def scene_cuts(n):
    return [{"id": f"c{i:07d}", "action": f"장면 {i}", "guide": "가이드", "size": "클로즈업", "line": "말"}
            for i in range(n)]


def test_pick_rejects_unknown_frame(home, fake):
    a = describe(home, "a00", ["시장 골목", "국물", None, None])
    other = describe(home, "zz9", ["다른 카테고리 사진", None, None, None])   # 이 카테고리 md가 없는 영상
    fake.answer = {"picks": [
        {"id": "c0000000", "frame": a[0]},
        {"id": "c0000001", "frame": "workspace/frames/zzz/scene001.jpg"},   # 없는 프레임
        {"id": "c0000004", "frame": other[0]},                 # 다른 카테고리 프레임
        {"id": "c0000002", "frame": a[2]},                     # 설명 없는 프레임
        {"id": "nope0000", "frame": a[1]},                     # 없는 장면
        {"id": "c0000003", "frame": None},
        {"id": ["c0000001"], "frame": a[1]},                  # id가 글이 아니면 버린다
    ]}
    picks = conti_refs.pick(scene_cuts(5), "mukbang")
    assert picks == {"c0000000": a[0]}
    rules, payload, _ = fake.calls[0]
    assert rules == [claude_cli.SKILLS_DIR / "yt-conti" / "pick-refs.md"]
    assert payload["scenes"][0] == {"id": "c0000000", "action": "장면 0", "guide": "가이드", "size": "클로즈업"}
    assert {c["frame"] for c in payload["candidates"]} == {a[0], a[1]}


def test_pick_bad_shape_is_invalid(home, fake):
    describe(home, "a00", ["시장 골목", None, None, None])
    fake.answer = {"pick": []}
    with pytest.raises(ScriptError) as exc:
        conti_refs.pick(scene_cuts(1), "mukbang")
    assert exc.value.code == "invalid_refs"


def test_pick_batches_40(home, fake):
    a = describe(home, "a00", ["하나", "둘", "셋", "넷"])

    def answer(payload):
        return {"picks": [{"id": s["id"], "frame": a[0]} for s in payload["scenes"]]}

    fake.answer = answer
    picks = conti_refs.pick(scene_cuts(85), "mukbang")
    assert [len(p["scenes"]) for _, p, _ in fake.calls] == [40, 40, 5]
    assert len(picks) == 85
    assert fake.calls[0][1]["used"] == [] and fake.calls[1][1]["used"] == [a[0]]   # 앞 묶음에서 쓴 사진을 알려 준다


def test_pick_without_candidates_does_not_call(home, fake):
    make_frames(home, "a00")
    assert conti_refs.pick(scene_cuts(3), "mukbang") == {} and not fake.calls


def test_apply_picks_sets_and_clears():
    cuts = [{"id": "a", "ref_auto": {"frame": "workspace/frames/x.jpg", "match": "content"}},
            {"id": "b"}, {"id": "c", "ref_frame": "workspace/frames/pin.jpg"}]
    out = conti_refs.apply_picks(cuts, {"b": "workspace/frames/y.jpg", "c": "workspace/frames/z.jpg"})
    assert "ref_auto" not in out[0]
    assert out[1]["ref_auto"] == {"frame": "workspace/frames/y.jpg", "match": "content"}
    assert out[2]["ref_frame"] == "workspace/frames/pin.jpg" and out[2]["ref_auto"]["frame"] == "workspace/frames/z.jpg"
    assert "ref_auto" in cuts[0] and "ref_auto" not in cuts[1]          # 원본은 그대로


def test_save_picks_writes_ref_auto_and_history(saved, home):
    a = describe(home, "a00", ["하나", None, None, None])
    digest = conti.content_hash(saved)
    fm = conti._load_conti(saved)
    picks = {fm["cuts"][1]["id"]: a[0]}
    result = conti_refs.save_picks(saved, picks, digest)
    stored, body = refstore.read_md(saved)
    assert stored["cuts"][1]["ref_auto"] == {"frame": a[0], "match": "content"}
    assert all("ref_auto" not in c for i, c in enumerate(stored["cuts"]) if i != 1)
    assert result["hash"] == conti.content_hash(saved) and result["picked"] == 1 and result["scenes"] == 4
    assert "## 내 메모" in body
    [latest, *_] = conti_history.list_versions(saved)
    assert latest["kind"] == "auto"
    state = conti_history.get_version(saved, latest["n"])["state"]
    assert state["cuts"][1]["ref_auto"] == {"frame": a[0], "match": "content"}


def test_save_picks_conflict(saved, home):
    with pytest.raises(ScriptError) as exc:
        conti_refs.save_picks(saved, {}, "0" * 64)
    assert exc.value.code == "conti_changed"
