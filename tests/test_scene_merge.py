from scripts import scene_merge


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
    assert merged["sec"] == sum(m["sec"] for m in members)
    assert round(merged["sec"], 1) == 7.2
    assert merged["guide"] == "a / b"
    assert merged["caption"] == "x"
    assert merged["sfx"] == "s / t"
    assert merged["id"] == "id1" and merged["line"] == ""
    assert merged["merged_ids"] == ["id1", "id2", "id3"]


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
    assert out["cuts"] == {"keep": {"kind": "pinned", "user": True, "choice": 1},
                           "keep2": {"kind": "pinned", "user": True, "choice": 4}}
    assert out["folders"] == ["x"] and out["suggestions"] == []
