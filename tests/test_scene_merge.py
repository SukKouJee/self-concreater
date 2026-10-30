import json

import pytest

from scripts import conti, conti_history, footage_match, refstore, scene_merge
from scripts.common import ScriptError
from tests.test_conti import CONTI, conti_file, draft_md, home  # noqa: F401 - fixtures


def cut(n, section, action, **kw):
    base = {"n": n, "id": f"id{n}", "section": section, "size": "미디엄", "angle": "정면", "sec": 2.4,
            "action": action, "line": f"대사{n}", "caption": "", "sfx": "", "guide": ""}
    return {**base, **kw}


def cycle():
    return [cut(1, 1, "A"), cut(2, 1, "B"), cut(3, 1, "C"), cut(4, 1, "A"), cut(5, 1, "B"), cut(6, 1, "C"),
            cut(7, 2, "A")]


def test_groups_cycle_within_section():
    assert scene_merge.groups(cycle()) == [[1, 4], [2, 5], [3, 6], [7]]
    assert scene_merge.has_repeats(cycle()) is True
    assert scene_merge.has_repeats([cut(1, 1, "A"), cut(2, 1, "B")]) is False


def test_groups_normalizes_action():
    cuts = [cut(1, 1, "컵라면 뜯기"), cut(2, 1, "다른 것"), cut(3, 1, "컵라면, 뜯기!")]
    assert scene_merge.groups(cuts) == [[1, 3], [2]]


def test_merge_fields_sums_and_dedups():
    members = [cut(1, 1, "A", guide="a", caption="x", sfx="s"), cut(2, 1, "A", guide="a", caption="", sfx="t"),
               cut(3, 1, "A", guide="b", caption="x", sfx="")]
    merged = scene_merge.merge_fields(members)
    assert merged["sec"] == 7.2
    assert merged["guide"] == "a / b"
    assert merged["caption"] == "x"
    assert merged["sfx"] == "s / t"
    assert merged["id"] == "id1" and merged["line"] == ""
    assert merged["merged_ids"] == ["id1", "id2", "id3"]


@pytest.mark.parametrize("sec,total", [(2.4, 7.2), (0.1, 0.3), (3.6, 10.8)])
def test_merge_fields_rounds_sec_sum(sec, total):
    members = [cut(n, 1, "A", sec=sec) for n in (1, 2, 3)]
    assert scene_merge.merge_fields(members)["sec"] == total


def test_merge_keeps_first_pinned_frame():
    members = [cut(1, 1, "A"), cut(2, 1, "A", ref_frame="f2.jpg"), cut(3, 1, "A", ref_frame="f3.jpg")]
    assert scene_merge.merge_fields(members)["ref_frame"] == "f2.jpg"
    assert "ref_frame" not in scene_merge.merge_fields([cut(1, 1, "A")])


def test_joined_lines_order():
    assert scene_merge.joined_lines(cycle(), 1) == "대사1 대사2 대사3 대사4 대사5 대사6"
    assert scene_merge.joined_lines(cycle(), 2) == "대사7"


def test_transfer_footage_moves_user_only():
    state = {"folders": ["x"], "cuts": {
        "gone1": {"kind": "pinned", "user": True, "choice": 1},
        "gone2": {"kind": "matched", "user": False, "choice": 2},
        "keep": {"kind": "matched", "user": False, "choice": 3},
        "keep2": {"kind": "pinned", "user": True, "choice": 4},
        "gone3": {"kind": "pinned", "user": True, "choice": 5}}, "suggestions": []}
    out = scene_merge.transfer_footage(state, {"gone1": "keep", "gone2": "keep", "gone3": "keep2"})
    assert out["cuts"] == {"gone1": {"kind": "pinned", "user": True, "choice": 1},     # 되돌리면 다시 보이게 남긴다
                           "keep": {"kind": "pinned", "user": True, "choice": 1},
                           "keep2": {"kind": "pinned", "user": True, "choice": 4},
                           "gone3": {"kind": "pinned", "user": True, "choice": 5}}
    assert out["folders"] == ["x"] and out["suggestions"] == []


def test_transfer_footage_first_user_wins():
    state = {"cuts": {"g1": {"kind": "pinned", "user": True, "choice": 1},
                      "g2": {"kind": "not_filmed", "user": True, "choice": None}}}
    out = scene_merge.transfer_footage(state, {"g1": "keep", "g2": "keep"})
    assert out["cuts"]["keep"] == {"kind": "pinned", "user": True, "choice": 1}
    assert set(out["cuts"]) == {"g1", "g2", "keep"}


def test_transfer_footage_tolerates_state_without_cuts():
    out = scene_merge.transfer_footage({"folders": []}, {"g1": "keep"})
    assert out == {"folders": [], "cuts": {}}


# --- 파일에 쓰는 합치기 (계획 → 검증 → 적용) -------------------------------------------------

# body 섹션(4초)이 A B A B로 돈다. hook·outro는 반복 없음.
REPEAT = {
    "script": [{"text": "4단계 다 먹을 수 있을까?"}, {"text": "하나 둘 셋 넷"}, {"text": ""}],
    "cuts": [
        {"section": 0, "size": "탑뷰", "angle": "탑뷰", "sec": 2, "action": "컵라면 4개", "line": "4단계 다 먹을 수 있을까?",
         "caption": "4단계 도전", "sfx": "띵", "guide": "카메라 고정"},
        {"section": 1, "size": "클로즈업", "sec": 1, "action": "뚜껑 뜯기", "line": "하나", "caption": "", "sfx": "",
         "guide": "소리 크게"},
        {"section": 1, "size": "익스트림 클로즈업", "sec": 1, "action": "면발", "line": "둘", "caption": "순한맛", "sfx": "",
         "guide": "입은 프레임 밖"},
        {"section": 1, "size": "클로즈업", "sec": 1, "action": "뚜껑, 뜯기!", "line": "셋", "caption": "", "sfx": "찍",
         "guide": "천천히"},
        {"section": 1, "size": "익스트림 클로즈업", "sec": 1, "action": "면발", "line": "넷", "caption": "", "sfx": "",
         "guide": "입은 프레임 밖"},
        {"section": 2, "size": "인서트", "angle": "탑뷰", "sec": 2, "action": "빈 컵", "line": "", "caption": "다음은 불닭",
         "sfx": "", "guide": "3초 정지"},
    ],
}


@pytest.fixture
def repeat_conti(home, draft_md):  # noqa: F811
    return home / conti.save(draft_md, conti_file(home, REPEAT))["path"]


def ids_of(path):
    fm, _ = refstore.read_md(path)
    return [c["id"] for c in fm["cuts"]]


def split(plan, order, lines):
    """body 섹션 장면을 order(장면 위치 0/1)대로 놓고 lines로 나눈 Claude 출력."""
    scenes = plan["sections"][1]["scenes"]
    return {"sections": [{"idx": 1, "order": [scenes[i]["id"] for i in order], "lines": lines}]}


def test_plan_lists_scenes_per_section(repeat_conti):
    ids = ids_of(repeat_conti)
    plan = scene_merge.plan(repeat_conti)
    assert plan["before"] == 6 and plan["after"] == 4
    assert plan["hash"] == conti.content_hash(repeat_conti)
    body = plan["sections"][1]
    assert body["idx"] == 1 and body["text"] == "하나 둘 셋 넷" and body["split"] is True
    assert [s["merged_ids"] for s in body["scenes"]] == [[ids[1], ids[3]], [ids[2], ids[4]]]
    assert body["scenes"][0]["sec"] == 2 and body["scenes"][0]["guide"] == "소리 크게 / 천천히"
    hook = plan["sections"][0]
    assert hook["split"] is False and [s["line"] for s in hook["scenes"]] == ["4단계 다 먹을 수 있을까?"]


def test_merge_nothing_to_merge(home, draft_md):  # noqa: F811
    path = home / conti.save(draft_md, conti_file(home, CONTI))["path"]
    before = path.read_bytes()
    with pytest.raises(ScriptError) as exc:
        scene_merge.plan(path)
    assert exc.value.code == "nothing_to_merge" and exc.value.message == "합칠 장면이 없어요"
    assert path.read_bytes() == before


def test_validate_split_accepts_permutation(repeat_conti):
    ids = ids_of(repeat_conti)
    plan = scene_merge.plan(repeat_conti)
    cuts = scene_merge.validate_split(plan, split(plan, [1, 0], ["하나 둘", "셋 넷"]))
    assert [c["n"] for c in cuts] == [1, 2, 3, 4]
    assert [c["id"] for c in cuts] == [ids[0], ids[2], ids[1], ids[5]]
    assert [c["line"] for c in cuts] == ["4단계 다 먹을 수 있을까?", "하나 둘", "셋 넷", ""]
    assert [c["section"] for c in cuts] == [0, 1, 1, 2] and cuts[1]["sec"] == 2
    assert all("merged_ids" not in c for c in cuts)


@pytest.mark.parametrize("data", [
    {"sections": [{"idx": 1, "order": "AB", "lines": ["하나 둘", "셋 녯"]}]},       # 글자 하나 바뀜
    {"sections": [{"idx": 1, "order": "AB", "lines": ["하나 둘 셋", "넷 다섯"]}]},  # 글자 더함
    {"sections": [{"idx": 1, "order": "AB", "lines": ["하나, 둘", "셋 넷"]}]},      # 문장부호 더함
    {"sections": [{"idx": 1, "order": "AB", "lines": ["하나 둘 셋 넷"]}]},           # 장면 수와 다름
    {"sections": [{"idx": 1, "order": "AA", "lines": ["하나 둘", "셋 넷"]}]},        # 순열 아님
    {"sections": [{"idx": 1, "order": "A", "lines": ["하나 둘 셋 넷"]}]},            # 장면 빠짐
    {"sections": [{"idx": 1, "order": "AB", "lines": ["하나 둘", 3]}]},              # 글이 아님
    {"sections": []},                                                                  # 섹션 빠짐
    {"sections": [{"idx": 1, "order": "AB", "lines": ["하나 둘", "셋 넷"]},
                  {"idx": 0, "order": "H", "lines": ["4단계 다 먹을 수 있을까?"]}]},  # 반복 없는 섹션
    {"sections": [{"idx": 1, "order": "AB", "lines": ["하나 둘", "셋 넷"]},
                  {"idx": 1, "order": "AB", "lines": ["하나 둘", "셋 넷"]}]},          # 같은 섹션 두 번
    {"sections": [{"idx": 1, "order": [["x"], "B"], "lines": ["하나 둘", "셋 넷"]}]},    # id가 글이 아님
    {"sections": [{"idx": True, "order": "AB", "lines": ["하나 둘", "셋 넷"]}]},
    {"cuts": []},
])
def test_validate_split_rejects_changed_text(repeat_conti, data):
    plan = scene_merge.plan(repeat_conti)
    hook_id, (a, b) = plan["sections"][0]["scenes"][0]["id"], plan["sections"][1]["scenes"]
    names = {"A": a["id"], "B": b["id"], "H": hook_id}
    data = json.loads(json.dumps(data))
    for section in data.get("sections", []):
        section["order"] = [names.get(k, k) if isinstance(k, str) else k for k in section["order"]]
    with pytest.raises(ScriptError) as exc:
        scene_merge.validate_split(plan, data)
    assert exc.value.code == "invalid_merge"


def test_merge_keeps_edited_lines(repeat_conti):
    conti.update_cut(repeat_conti, 3, {"line": "둘이요, 진짜!"})
    plan = scene_merge.plan(repeat_conti)
    assert plan["sections"][1]["text"] == "하나 둘이요, 진짜! 셋 넷"
    cuts = scene_merge.validate_split(plan, split(plan, [0, 1], ["하나 둘이요, 진짜!", "셋 넷"]))
    scene_merge.apply(repeat_conti, cuts, plan["hash"])
    fm, _ = refstore.read_md(repeat_conti)
    assert fm["script"][1]["text"] == "하나 둘이요, 진짜! 셋 넷"
    assert [c["line"] for c in fm["cuts"] if c["section"] == 1] == ["하나 둘이요, 진짜!", "셋 넷"]


def _applied(path, order=(1, 0), lines=("하나 둘", "셋 넷"), **kw):
    plan = scene_merge.plan(path)
    cuts = scene_merge.validate_split(plan, split(plan, list(order), list(lines)))
    return plan, scene_merge.apply(path, cuts, plan["hash"], **kw)


def test_apply_writes_scenes_and_retimes(repeat_conti, home):
    ids = ids_of(repeat_conti)
    _, out = _applied(repeat_conti)
    fm, _ = refstore.read_md(repeat_conti)
    assert out["status"] == "merged" and out["before"] == 6 and out["after"] == 4
    assert out["hash"] == conti.content_hash(repeat_conti) and (home / out["html"]).exists()
    assert [c["id"] for c in fm["cuts"]] == [ids[0], ids[2], ids[1], ids[5]]
    assert [(c["start"], c["end"]) for c in fm["cuts"]] == [(0, 2), (2, 4), (4, 6), (6, 8)]
    assert fm["target_sec"] == 8 and fm["cuts"][2]["sfx"] == "찍" and fm["cuts"][2]["guide"] == "소리 크게 / 천천히"
    assert all("merged_ids" not in c for c in fm["cuts"])


def test_apply_records_before_merge_history(repeat_conti):
    _applied(repeat_conti, now=1_000_000.0)
    versions = conti_history.list_versions(repeat_conti)
    assert [v["kind"] for v in versions] == ["auto", "before_merge", "created"]
    assert conti_history.get_version(repeat_conti, versions[1]["n"])["summary"]["cuts"] == 6
    assert versions[0]["summary"]["cuts"] == 4


def test_apply_keeps_memo_and_body(repeat_conti):
    text = repeat_conti.read_text("utf-8").replace("## 내 메모\n", "## 내 메모\n소품 미리 사기\n")
    repeat_conti.write_text(text, encoding="utf-8", newline="\n")
    _applied(repeat_conti)
    _, body = refstore.read_md(repeat_conti)
    assert "## 내 메모\n소품 미리 사기" in body
    assert "하나 둘" in body and "셋 넷" in body


def test_apply_transfers_footage_user_verdicts(repeat_conti):
    ids = ids_of(repeat_conti)
    pinned = {"kind": "pinned", "user": True, "choice": {"fid": "f", "start": 1.0, "end": 2.0}}
    footage_match.save_state(repeat_conti, {"folders": ["x"], "suggestions": [], "cuts": {
        ids[3]: pinned, ids[1]: {"kind": "matched", "user": False}, ids[2]: {"kind": "matched", "user": False},
        ids[0]: {"kind": "matched", "user": False}}})
    _applied(repeat_conti)
    state = footage_match.load_state(repeat_conti)
    assert state["cuts"] == {ids[1]: pinned, ids[3]: pinned, ids[0]: {"kind": "matched", "user": False}}
    assert state["folders"] == ["x"]


def test_apply_warns_when_footage_state_cannot_be_saved(repeat_conti, monkeypatch):
    footage_match.save_state(repeat_conti, {"folders": [], "suggestions": [], "cuts": {}})

    def broken(path, state):
        raise OSError("disk full")

    monkeypatch.setattr(footage_match, "save_state", broken)
    _, out = _applied(repeat_conti)
    assert out["status"] == "merged" and len(ids_of(repeat_conti)) == 4
    [warning] = [w for w in out["warnings"] if w["kind"] == "footage_not_transferred"]
    assert "disk full" in warning["message"]


def test_plan_warns_scene_longer_than_max(repeat_conti, monkeypatch):
    assert scene_merge.plan(repeat_conti)["warnings"] == []
    monkeypatch.setattr(conti, "MAX_CUT_SEC", 1.5)
    warnings = scene_merge.plan(repeat_conti)["warnings"]
    assert [(w["kind"], w["sec"]) for w in warnings] == [("scene_too_long", 2.0), ("scene_too_long", 2.0)]
    assert {w["id"] for w in warnings} == {s["id"] for s in scene_merge.plan(repeat_conti)["sections"][1]["scenes"]}


def test_apply_keeps_pinned_frame_of_later_repeat(home, draft_md):  # noqa: F811
    data = json.loads(json.dumps(REPEAT))
    data["cuts"][3]["ref_frame"] = "workspace/frames/pin.jpg"
    path = home / conti.save(draft_md, conti_file(home, data))["path"]
    _applied(path)
    fm, _ = refstore.read_md(path)
    assert [c.get("ref_frame") for c in fm["cuts"]] == [None, None, "workspace/frames/pin.jpg", None]


def test_apply_without_footage_state_creates_none(repeat_conti):
    _applied(repeat_conti)
    assert not footage_match.state_path(repeat_conti).exists()


def test_apply_conflict_on_changed_hash(repeat_conti):
    plan = scene_merge.plan(repeat_conti)
    cuts = scene_merge.validate_split(plan, split(plan, [0, 1], ["하나 둘", "셋 넷"]))
    conti.update_cut(repeat_conti, 1, {"guide": "밖에서 고침"})
    before = repeat_conti.read_bytes()
    with pytest.raises(ScriptError) as exc:
        scene_merge.apply(repeat_conti, cuts, plan["hash"])
    assert exc.value.code == "conti_changed" and repeat_conti.read_bytes() == before


@pytest.mark.parametrize("tamper", ["line", "id", "list_id", "drop", "not_list", "section", "section_bool",
                                    "punct", "line_not_str", "nonsplit_line"])
def test_apply_rejects_cuts_that_lose_lines_or_ids(repeat_conti, tamper):
    plan = scene_merge.plan(repeat_conti)
    cuts = scene_merge.validate_split(plan, split(plan, [0, 1], ["하나 둘", "셋 넷"]))
    if tamper == "line":
        cuts[1]["line"] = "하나"
    elif tamper == "id":
        cuts[1]["id"] = "zzzzzzzz"
    elif tamper == "list_id":
        cuts[1]["id"] = ["x"]
    elif tamper == "section":
        cuts[1]["section"] = 0
    elif tamper == "section_bool":
        cuts[1]["section"] = True
    elif tamper == "punct":
        cuts[1]["line"] = "하나 둘!"
    elif tamper == "line_not_str":
        cuts[3]["line"] = None
    elif tamper == "nonsplit_line":
        cuts[0]["line"] = "4단계 다 먹을 수 있을까"
    elif tamper == "drop":
        cuts.pop(1)
    else:
        cuts = {"cuts": cuts}
    before = repeat_conti.read_bytes()
    with pytest.raises(ScriptError) as exc:
        scene_merge.apply(repeat_conti, cuts, plan["hash"])
    assert exc.value.code == "invalid_merge" and repeat_conti.read_bytes() == before


@pytest.mark.parametrize("tamper", ["drop_pin", "other_pin", "guide_changed", "sec_nan", "sec_big", "size_changed",
                                    "angle_changed", "extra_key"])
def test_apply_takes_only_order_and_lines_from_client(home, draft_md, tamper):  # noqa: F811
    data = json.loads(json.dumps(REPEAT))
    data["cuts"][3]["ref_frame"] = "workspace/frames/pin.jpg"
    path = home / conti.save(draft_md, conti_file(home, data))["path"]
    plan = scene_merge.plan(path)
    cuts = scene_merge.validate_split(plan, split(plan, [1, 0], ["하나 둘", "셋 넷"]))
    target = cuts[2]                                   # 뚜껑 뜯기 장면(고정 사진 있음)
    change = {"drop_pin": lambda: target.pop("ref_frame"),
              "other_pin": lambda: target.update(ref_frame="workspace/frames/other.jpg"),
              "guide_changed": lambda: target.update(guide="덮어쓴 가이드"),
              "sec_nan": lambda: target.update(sec="nan"), "sec_big": lambda: target.update(sec=9999),
              "size_changed": lambda: target.update(size="와이드"),
              "angle_changed": lambda: target.update(angle="로우"),
              "extra_key": lambda: target.update(merged_ids=["x"], start=99)}
    change[tamper]()
    scene_merge.apply(path, cuts, plan["hash"])
    fm, _ = refstore.read_md(path)
    scene = fm["cuts"][2]
    assert scene["ref_frame"] == "workspace/frames/pin.jpg" and scene["guide"] == "소리 크게 / 천천히"
    assert scene["sec"] == 2 and scene["size"] == "클로즈업" and scene.get("angle") is None
    assert "merged_ids" not in scene and scene["start"] == 4 and fm["target_sec"] == 8
    assert scene["line"] == "셋 넷"


def test_apply_puts_scenes_in_section_order_whatever_the_client_order(repeat_conti):
    ids = ids_of(repeat_conti)
    plan = scene_merge.plan(repeat_conti)
    cuts = scene_merge.validate_split(plan, split(plan, [1, 0], ["하나 둘", "셋 넷"]))
    scene_merge.apply(repeat_conti, [cuts[3], cuts[1], cuts[0], cuts[2]], plan["hash"])
    assert ids_of(repeat_conti) == [ids[0], ids[2], ids[1], ids[5]]


def test_validate_split_ignores_whitespace_only(repeat_conti):
    plan = scene_merge.plan(repeat_conti)
    cuts = scene_merge.validate_split(plan, split(plan, [0, 1], ["하나둘 ", "  셋  넷"]))
    assert [c["line"] for c in cuts][1:3] == ["하나둘", "셋  넷"]


def test_cli_merge_plan_then_apply(repeat_conti, home):
    plan = conti.main(["merge", str(repeat_conti)])
    assert plan["before"] == 6 and plan["after"] == 4
    cuts = scene_merge.validate_split(plan, split(plan, [0, 1], ["하나 둘", "셋 넷"]))
    cuts_file = home / "workspace" / "tmp" / "merge.json"
    cuts_file.write_text(json.dumps(cuts, ensure_ascii=False), encoding="utf-8")
    out = conti.main(["merge", str(repeat_conti), "--cuts", str(cuts_file), "--hash", plan["hash"]])
    assert out["status"] == "merged" and len(ids_of(repeat_conti)) == 4


def test_cli_merge_apply_accepts_claude_split(repeat_conti, home):
    plan = conti.main(["merge", str(repeat_conti)])
    split_file = home / "workspace" / "tmp" / "split.json"
    split_file.write_text(json.dumps(split(plan, [1, 0], ["하나 둘", "셋 넷"]), ensure_ascii=False), encoding="utf-8")
    out = conti.main(["merge", str(repeat_conti), "--cuts", str(split_file), "--hash", plan["hash"]])
    assert out["status"] == "merged" and out["after"] == 4


def test_cli_merge_hash_requires_cuts(repeat_conti):
    with pytest.raises(ScriptError) as exc:
        conti.main(["merge", str(repeat_conti), "--hash", "abc"])
    assert exc.value.code == "bad_request"


def test_merge_fields_carries_first_ref_auto():
    first, later = {"frame": "workspace/frames/a.jpg", "match": "content"}, {"frame": "workspace/frames/b.jpg", "match": "content"}
    members = [cut(1, 1, "A"), cut(2, 1, "A", ref_auto=first), cut(3, 1, "A", ref_auto=later)]
    assert scene_merge.merge_fields(members)["ref_auto"] == first
    assert "ref_auto" not in scene_merge.merge_fields([cut(1, 1, "A")])


def test_apply_keeps_ref_auto_of_merged_and_single_scenes(home, draft_md):  # noqa: F811
    a, b = {"frame": "workspace/frames/a.jpg", "match": "content"}, {"frame": "workspace/frames/b.jpg", "match": "content"}
    path = home / conti.save(draft_md, conti_file(home, REPEAT))["path"]
    stored, _ = refstore.read_md(path)                    # 저장은 후보에 없는 ref_auto를 버리므로 파일에 직접 둔다
    stored["cuts"][0]["ref_auto"] = a                     # 혼자인 장면
    stored["cuts"][3]["ref_auto"] = b                     # 묶인 장면의 뒤 컷
    refstore.update_frontmatter(path, {"cuts": stored["cuts"]})
    _applied(path)
    fm, _ = refstore.read_md(path)
    assert [c.get("ref_auto") for c in fm["cuts"]] == [a, None, b, None]


def test_command_doc_describes_merge_flow():
    from scripts.common import PLUGIN_ROOT
    doc = (PLUGIN_ROOT / "commands" / "yt-conti.md").read_text("utf-8")
    assert "반복 컷 장면으로 합치기" in doc
    assert "ytc conti merge <파일>" in doc and "--cuts" in doc and "--hash" in doc
    assert "skills/yt-conti/merge.md" in doc and "저장 전 사용자 확인" in doc
    assert "참고 프레임은 같은 샷 크기의 레퍼런스 장면입니다(구도만 참고)" not in doc


def test_merge_carries_first_art():
    art1 = {"file": "workspace/conti-art/d/1.svg", "basis": "0123abcd"}
    art2 = {"file": "workspace/conti-art/d/2.svg", "basis": "0123abce"}
    members = [cut(1, 1, "A"), cut(2, 1, "A", art=art1), cut(3, 1, "A", art=art2)]
    assert scene_merge.merge_fields(members)["art"] == art1
    assert "art" not in scene_merge.merge_fields([cut(1, 1, "A")])
    assert "art" in scene_merge.CUT_FIELDS
