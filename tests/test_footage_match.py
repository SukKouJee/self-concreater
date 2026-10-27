import json

import pytest

from scripts import conti, footage, llm
from scripts import conti_history as ch
from scripts import footage_match as fm
from scripts.common import ScriptError
from tests.test_conti import conti_file, draft_md, home  # noqa: F401 - fixtures

FID_A = "0123456789ab-100-1"
FID_B = "abcdef012345-200-2"
FID_C = "fedcba987654-300-3"


@pytest.fixture
def saved(draft_md, home):  # noqa: F811
    return home / conti.save(draft_md, conti_file(home))["path"]


def cut_ids(path):
    return [c["id"] for c in conti._load_conti(path)["cuts"]]


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def scene(i, start, end, fid):
    rel = f"montage/{i:03d}.jpg"
    path = footage.footage_dir(fid) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8jpeg")
    return {"i": i, "start": start, "end": end, "frames": [], "montage": rel}


def result(fid, scenes, transcript, name="clip.mp4"):
    return {"fid": fid, "name": name, "status": "done", "decoder": "pyav", "duration_sec": 60.0,
            "scenes": scenes, "transcript": transcript}


def talk_files():
    """FID_A: 컷 1·2의 대사, FID_B: 말 없는 장면 2개."""
    a = result(FID_A, [scene(0, 0.0, 10.0, FID_A), scene(1, 10.0, 20.0, FID_A)], [
        seg(1.0, 3.0, "4단계 다 먹을 수 있을까요"),
        seg(11.0, 13.0, "1단계부터 가볼게요"),
        seg(14.0, 16.0, "오늘 날씨 정말 좋네요"),
        seg(17.0, 19.0, "와 순하네요"),
    ])
    b = result(FID_B, [scene(0, 0.0, 5.0, FID_B), scene(1, 5.0, 9.0, FID_B)], [])
    return [a, b]


class FakeLLM:
    """llm.ask_validated 대역. respond(payload, files) -> Claude 응답 dict."""

    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def __call__(self, rules, payload, task, validate, **kw):
        files = kw.get("files") or []
        self.calls.append({"rules": rules, "payload": payload, "task": task, "files": files,
                           "schema": kw.get("schema")})
        data = self.respond(payload, files)
        return data, validate(data)


@pytest.fixture
def fake(monkeypatch):
    def install(respond):
        f = FakeLLM(respond)
        monkeypatch.setattr(llm, "ask_validated", f)
        return f
    return install


def answer(cuts=None, suggestions=None):
    return {"cuts": [{"id": k, "matches": v} for k, v in (cuts or {}).items()], "suggestions": suggestions or []}


def m(fid, start, end, confidence, why="이유"):
    return {"fid": fid, "start": start, "end": end, "confidence": confidence, "why": why}


def verdict_responder(ids):
    """컷0 high, 컷1 medium, 컷2 대답 없음, 컷3(말 없음) 사진으로 low."""
    def respond(payload, files):
        picks = {}
        offered = {c["id"] for c in payload["cuts"]}
        if ids[0] in offered:
            picks[ids[0]] = [m(FID_A, 1.0, 3.0, "high", "대사가 같아요")]
        if ids[1] in offered:
            picks[ids[1]] = [m(FID_A, 11.0, 13.0, "medium")]
        if ids[3] in offered:
            picks[ids[3]] = [m(FID_B, 0.0, 5.0, "low", "빈 컵이 보여요")]
        return answer(picks)
    return respond


# --- 글자 후보 ------------------------------------------------------------------------

def test_text_candidates_rank_by_bigram_similarity_when_line_spoken():
    cut = {"line": "1단계부터 갑니다."}
    transcripts = {FID_A: [seg(0, 2, "오늘 날씨 좋네요"), seg(2, 4, "1단계부터 가볼게요")]}
    got = fm.text_candidates(cut, transcripts)
    assert got[0]["fid"] == FID_A and (got[0]["start"], got[0]["end"]) == (2, 4)
    assert got[0]["text"] == "1단계부터 가볼게요"
    assert 0 < got[0]["score"] <= 1
    assert all(g["score"] > 0 for g in got)


def test_text_candidates_join_two_segments_when_line_spans_them():
    cut = {"line": "와 순하다 진짜 맛있다"}
    transcripts = {FID_A: [seg(0, 1, "와 순하다"), seg(1, 2, "진짜 맛있다"), seg(5, 6, "다른 말")]}
    got = fm.text_candidates(cut, transcripts)
    assert (got[0]["start"], got[0]["end"]) == (0, 2)
    assert got[0]["text"] == "와 순하다 진짜 맛있다"


def test_text_candidates_empty_when_cut_has_no_line():
    assert fm.text_candidates({"line": ""}, {FID_A: [seg(0, 1, "아무 말")]}) == []


def test_text_candidates_limit_k_and_no_overlap():
    segs = [seg(i, i + 1, "1단계부터 갑니다") for i in range(10)]
    got = fm.text_candidates({"line": "1단계부터 갑니다"}, {FID_A: segs}, k=3)
    assert len(got) == 3
    spans = sorted((g["start"], g["end"]) for g in got)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


# --- 판정 -----------------------------------------------------------------------------

def test_match_sets_auto_review_missing(saved, fake):
    ids = cut_ids(saved)
    f = fake(verdict_responder(ids))
    state = fm.match(saved, talk_files())
    cuts = state["cuts"]
    assert cuts[ids[0]]["kind"] == "auto"
    assert cuts[ids[0]]["choice"] == {"fid": FID_A, "start": 1.0, "end": 3.0}
    assert cuts[ids[0]]["user"] is False
    assert cuts[ids[1]]["kind"] == "review" and cuts[ids[1]]["choice"] is None
    assert cuts[ids[1]]["candidates"][0]["text"] == "1단계부터 가볼게요"
    assert cuts[ids[2]]["kind"] == "missing" and cuts[ids[2]]["candidates"] == []
    assert cuts[ids[3]]["kind"] == "review"
    assert cuts[ids[3]]["candidates"][0] == {"fid": FID_B, "start": 0.0, "end": 5.0, "score": "low",
                                              "why": "빈 컵이 보여요", "text": ""}
    assert fm.load_state(saved) == state
    # 대사 컷은 글자만, 말 없는 컷만 사진
    text_calls = [c for c in f.calls if not c["files"]]
    image_calls = [c for c in f.calls if c["files"]]
    assert {c["id"] for call in text_calls for c in call["payload"]["cuts"]} == set(ids[:3])
    assert {c["id"] for call in image_calls for c in call["payload"]["cuts"]} == {ids[3]}
    assert all(p.is_file() for call in image_calls for p in call["files"])
    assert state["images_used"] == sum(len(c["files"]) for c in f.calls)
    assert f.calls[0]["rules"][0].name == "footage-match.md"


def test_match_keeps_at_most_three_candidates_best_first(saved, fake):
    ids = cut_ids(saved)
    fake(lambda p, f: answer({ids[0]: [m(FID_A, 1.0, 3.0, "low"), m(FID_A, 11.0, 13.0, "medium"),
                                       m(FID_A, 14.0, 16.0, "low"), m(FID_A, 1.0, 3.0, "high")]}
                             if ids[0] in {c["id"] for c in p["cuts"]} else answer()))
    state = fm.match(saved, talk_files())
    cands = state["cuts"][ids[0]]["candidates"]
    assert [c["score"] for c in cands] == ["high", "medium", "low"]
    assert state["cuts"][ids[0]]["kind"] == "auto"


def test_match_drops_matches_outside_offered_footage(saved, fake):
    ids = cut_ids(saved)
    fake(lambda p, f: answer({ids[0]: [m(FID_C, 1.0, 3.0, "high")]}) if ids[0] in {c["id"] for c in p["cuts"]}
         else answer())
    state = fm.match(saved, talk_files())
    assert state["cuts"][ids[0]]["kind"] == "missing"


def test_match_retries_on_bad_confidence(saved, fake):
    ids = cut_ids(saved)
    with pytest.raises(ScriptError) as e:
        fake(lambda p, f: answer({ids[0]: [m(FID_A, 1.0, 3.0, "sure")]}) if ids[0] in {c["id"] for c in p["cuts"]}
             else answer())
        fm.match(saved, talk_files())
    assert e.value.code.startswith("invalid_")


def test_match_without_usable_files_marks_all_missing_and_skips_claude(saved, fake):
    f = fake(lambda p, files: pytest.fail("Claude를 부르면 안 됩니다"))
    broken = {"fid": FID_C, "name": "x.mov", "status": "unreadable", "reason": "열 수 없음",
              "duration_sec": None, "scenes": [], "transcript": None}
    state = fm.match(saved, [broken])
    assert {v["kind"] for v in state["cuts"].values()} == {"missing"}
    assert f.calls == []


def test_line_cut_uses_images_when_no_transcript(saved, fake):
    ids = cut_ids(saved)
    files = [result(FID_B, [scene(0, 0.0, 5.0, FID_B)], None)]
    f = fake(lambda p, fl: answer())
    fm.match(saved, files)
    assert {c["id"] for call in f.calls for c in call["payload"]["cuts"]} == set(ids)
    assert all(call["files"] for call in f.calls)


# --- 사용자 판정 보존 (Review Focus 2) ----------------------------------------------------

def test_user_verdicts_survive_rematch_with_new_folder(saved, fake):
    ids = cut_ids(saved)
    fake(verdict_responder(ids))
    fm.match(saved, talk_files())
    fm.choose(saved, ids[1], FID_A, 11.0, 13.0)
    fm.mark_not_filmed(saved, ids[2])

    def everything_high(payload, files):
        offered = {c["id"] for c in payload["cuts"]}
        assert ids[1] not in offered and ids[2] not in offered
        return answer({i: [m(FID_C, 0.0, 4.0, "high")] for i in offered})
    fake(everything_high)
    more = [*talk_files(), result(FID_C, [scene(0, 0.0, 4.0, FID_C)],
                                   [seg(0.0, 4.0, "4단계 다 먹을 수 있을까 1단계부터 와 순하다")])]
    state = fm.match(saved, more)
    assert state["cuts"][ids[1]] == {**state["cuts"][ids[1]], "kind": "pinned", "user": True,
                                     "choice": {"fid": FID_A, "start": 11.0, "end": 13.0}}
    assert state["cuts"][ids[2]]["kind"] == "not_filmed" and state["cuts"][ids[2]]["user"] is True
    assert state["cuts"][ids[0]]["choice"]["fid"] == FID_C


def test_user_choice_made_during_match_is_not_overwritten(saved, fake):
    ids = cut_ids(saved)

    def respond(payload, files):
        if ids[0] in {c["id"] for c in payload["cuts"]}:
            fm.choose(saved, ids[0], FID_A, 11.0, 13.0)       # 사용자가 맞추는 도중에 고름
            return answer({ids[0]: [m(FID_A, 1.0, 3.0, "high")]})
        return answer()
    fake(respond)
    state = fm.match(saved, talk_files())
    assert state["cuts"][ids[0]]["kind"] == "pinned"
    assert state["cuts"][ids[0]]["choice"] == {"fid": FID_A, "start": 11.0, "end": 13.0}


def test_reset_returns_cut_to_ai_verdict(saved, fake):
    ids = cut_ids(saved)
    fake(verdict_responder(ids))
    fm.match(saved, talk_files())
    fm.mark_not_filmed(saved, ids[0])
    entry = fm.reset(saved, ids[0])
    assert entry["kind"] == "auto" and entry["user"] is False
    assert entry["choice"] == {"fid": FID_A, "start": 1.0, "end": 3.0}
    fm.choose(saved, ids[1], FID_A, 11.0, 13.0)
    assert fm.reset(saved, ids[1])["kind"] == "review"
    assert fm.reset(saved, ids[2])["kind"] == "missing"


@pytest.mark.parametrize("args, code", [
    (("../x", 0.0, 1.0), "bad_fid"),
    ((FID_A, 2.0, 1.0), "bad_request"),
    ((FID_A, -1.0, 1.0), "bad_request"),
    ((FID_A, "a", 1.0), "bad_request"),
])
def test_choose_rejects_bad_input(saved, args, code):
    with pytest.raises(ScriptError) as e:
        fm.choose(saved, cut_ids(saved)[0], *args)
    assert e.value.code == code


def test_user_ops_reject_unknown_cut(saved):
    for op in (lambda: fm.choose(saved, "zzzzzzzz", FID_A, 0.0, 1.0), lambda: fm.mark_not_filmed(saved, "zzzzzzzz"),
               lambda: fm.reset(saved, "zzzzzzzz")):
        with pytest.raises(ScriptError) as e:
            op()
        assert e.value.code == "cut_not_found"


# --- 이미지 상한 (Review Focus 5) -------------------------------------------------------

def test_image_cap_leaves_unseen_cut_in_review_with_reason(saved, fake):
    ids = cut_ids(saved)
    many = [result(FID_B, [scene(i, i * 2.0, i * 2.0 + 2.0, FID_B) for i in range(130)], [])]
    f = fake(lambda p, files: answer())
    state = fm.match(saved, many)
    assert all(len(c["files"]) <= fm.IMAGES_PER_CALL == 8 for c in f.calls)
    assert sum(len(c["files"]) for c in f.calls) == fm.IMAGES_PER_RUN == 120
    assert state["images_used"] == 120
    for i in ids:     # 글자가 없으니 모든 컷이 사진으로 간다
        assert state["cuts"][i]["kind"] == "review"
        assert state["cuts"][i]["reason"] == fm.CAP_REASON == "사진 상한으로 AI가 보지 못함"


def test_image_cap_keeps_high_match_found_before_cap(saved, fake):
    ids = cut_ids(saved)
    many = [result(FID_B, [scene(i, i * 2.0, i * 2.0 + 2.0, FID_B) for i in range(130)], [])]

    def respond(payload, files):
        if payload["scenes"][0]["start"] == 0.0:
            return answer({ids[3]: [m(FID_B, 0.0, 2.0, "high")]})
        return answer()
    fake(respond)
    state = fm.match(saved, many)
    assert state["cuts"][ids[3]]["kind"] == "auto" and "reason" not in state["cuts"][ids[3]]


def test_no_cap_reason_when_every_scene_was_seen(saved, fake):
    ids = cut_ids(saved)
    fake(lambda p, f: answer())
    state = fm.match(saved, talk_files())
    assert state["cuts"][ids[3]]["kind"] == "missing" and "reason" not in state["cuts"][ids[3]]


# --- 지운 컷 숨김·복원 (F6) ---------------------------------------------------------------

def test_deleted_cut_verdict_hidden_and_returns_on_restore(saved, fake):
    ids = cut_ids(saved)
    fake(verdict_responder(ids))
    fm.match(saved, talk_files())
    conti.edit_cuts(saved, {"op": "delete", "id": ids[1]}, None)
    state = fm.load_state(saved)
    assert state["cuts"][ids[1]]["kind"] == "review"
    visible = fm.visible_cuts(saved, state)
    assert [c["id"] for c in visible] == [ids[0], ids[2], ids[3]]
    assert visible[0]["match"]["kind"] == "auto"
    ch.restore(saved, 1, None)
    back = fm.visible_cuts(saved, fm.load_state(saved))
    assert [c["id"] for c in back] == ids
    assert back[1]["match"]["kind"] == "review"


def test_visible_cuts_default_missing_before_match(saved):
    visible = fm.visible_cuts(saved, fm.load_state(saved))
    assert len(visible) == 4
    assert visible[0]["match"] == {"kind": "missing", "choice": None, "candidates": [], "user": False}
    assert {"id", "n", "section", "line", "guide", "action", "size", "sec"} <= set(visible[0])


# --- 새 컷 후보 (D) -------------------------------------------------------------------

def suggestion(fid=FID_A, start=14.0, end=16.0):
    return {"fid": fid, "start": start, "end": end, "line": "오늘 날씨 정말 좋네요", "guide": "하늘 보며 말하기",
            "size": "바스트"}


def test_suggestions_stored_with_text_and_hidden_flag(saved, fake):
    fake(lambda p, f: answer(suggestions=[suggestion()]))
    state = fm.match(saved, talk_files())
    assert state["suggestions"] == [{**suggestion(), "text": "오늘 날씨 정말 좋네요", "hidden": False}]


def test_suggestion_overlapping_chosen_cut_is_dropped(saved, fake):
    ids = cut_ids(saved)
    fake(lambda p, f: answer({ids[0]: [m(FID_A, 1.0, 3.0, "high")]} if ids[0] in {c["id"] for c in p["cuts"]}
                             else None, [suggestion(start=1.0, end=3.0)]))
    assert fm.match(saved, talk_files())["suggestions"] == []


def test_hidden_suggestion_stays_hidden_after_rematch(saved, fake):
    fake(lambda p, f: answer(suggestions=[suggestion()]))
    fm.match(saved, talk_files())
    fm.hide_suggestion(saved, 0)
    state = fm.match(saved, talk_files())
    assert state["suggestions"] == [{**suggestion(), "text": "오늘 날씨 정말 좋네요", "hidden": True}]


def test_suggestion_with_bad_size_is_rejected(saved, fake):
    fake(lambda p, f: answer(suggestions=[{**suggestion(), "size": "초광각"}]))
    with pytest.raises(ScriptError) as e:
        fm.match(saved, talk_files())
    assert e.value.code.startswith("invalid_")


def test_hide_suggestion_rejects_bad_index(saved):
    with pytest.raises(ScriptError) as e:
        fm.hide_suggestion(saved, 0)
    assert e.value.code == "bad_request"


# --- 저장 -----------------------------------------------------------------------------

def test_state_path_and_default(saved, home):  # noqa: F811
    assert fm.state_path(saved) == home / "workspace" / "footage_match" / f"{saved.stem}.json"
    assert fm.load_state(saved) == {"folders": [], "cuts": {}, "suggestions": [], "images_used": 0}


def test_save_state_is_atomic(saved, monkeypatch):
    fm.save_state(saved, {"folders": ["D:/a"], "cuts": {}, "suggestions": [], "images_used": 0})
    before = fm.state_path(saved).read_bytes()

    def boom(src, dst):
        raise OSError("disk gone")
    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        fm.save_state(saved, {"folders": ["D:/b"], "cuts": {}, "suggestions": [], "images_used": 0})
    assert fm.state_path(saved).read_bytes() == before
    assert not list(fm.state_path(saved).parent.glob("*.tmp"))
    assert b"\r\n" not in before and json.loads(before)["folders"] == ["D:/a"]


def test_load_state_rejects_broken_file(saved):
    path = fm.state_path(saved)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ScriptError) as e:
        fm.load_state(saved)
    assert e.value.code == "bad_state"
