import json

import pytest

from scripts import cluster, features, refstore, shots, similarity
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set, write_video

VID = "a00"


def scenes(n, length=60):
    step = length / n
    return [{"start": round(i * step, 2), "end": round((i + 1) * step, 2), "camera": "static",
             "frame": f"workspace/frames/{VID}/scene{i + 1:03d}.jpg"} for i in range(n)]


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    standard_set(tmp_path)
    cuts = tmp_path / "workspace" / "cuts" / f"{VID}.json"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text(json.dumps({"id": VID, "duration_sec": 60, "scenes": scenes(20),
                                "cuts": [s["start"] for s in scenes(20)[1:]]}), encoding="utf-8")
    return tmp_path


def labels_file(home, labels):
    path = home / "workspace" / "tmp" / "shots.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
    return path


def label(frame, size="클로즈업", angle="정면", face=False, hands=True, broll=False, text=True):
    return {"frame": frame, "size": size, "angle": angle, "face": face, "hands_only": hands,
            "broll": broll, "text_overlay": text}


# --- context ----------------------------------------------------------------------

def test_context_samples_frames_evenly_with_weights(home):
    ctx = shots.context(VID, CAT)
    assert len(ctx["frames"]) == 8
    assert ctx["frames"][0]["frame"].endswith("scene001.jpg")
    assert ctx["frames"][-1]["frame"].endswith("scene020.jpg")
    assert sum(f["weight"] for f in ctx["frames"]) == pytest.approx(60.0)
    assert ctx["frames"][0]["section"] == "hook"
    assert ctx["vocab"]["size"][0] == "익스트림 클로즈업"
    assert ctx["has_shots"] is False


def test_context_without_frames(home):
    (home / "workspace" / "cuts" / f"{VID}.json").unlink()
    with pytest.raises(ScriptError) as exc:
        shots.context(VID, CAT)
    assert exc.value.code == "no_frames"


def test_pending_lists_videos_without_shots(home):
    out = shots.pending(CAT)
    assert VID in out["pending"] and len(out["pending"]) == 10


# --- apply ------------------------------------------------------------------------

def test_apply_writes_weighted_distribution(home):
    frames = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    labels = [label(frames[0], size="탑뷰", angle="탑뷰", text=True)] + \
             [label(f, size="클로즈업", hands=True, text=False) for f in frames[1:6]] + \
             [label(f, size="바스트", face=True, hands=False, text=False) for f in frames[6:]]
    path = refstore.read_md(home / "refs" / f"ref_{CAT}" / "videos" / f"{VID}.md")
    body_before = path[1]

    out = shots.apply(VID, CAT, labels_file(home, labels))
    fm, body = refstore.read_md(home / "refs" / f"ref_{CAT}" / "videos" / f"{VID}.md")
    s = fm["shots"]
    assert out["status"] == "applied" and out["frames"] == 8
    assert sum(s["size"].values()) == pytest.approx(1.0, abs=0.02)
    assert max(s["size"], key=s["size"].get) == "클로즈업"
    assert 0 < s["face"] < 0.5 and s["hands_only"] > 0.5
    assert s["by_section"]["hook"] == "탑뷰"
    assert body == body_before                                    # 본문은 건드리지 않는다
    assert not (home / "workspace" / "tmp" / "shots.json").exists()
    kept = json.loads((home / "workspace" / "shots" / f"{VID}.json").read_text("utf-8"))["labels"]
    assert len(kept) == 8 and kept[0]["size"] == "탑뷰" and kept[0]["section"] == "hook"


@pytest.mark.parametrize("patch, word", [
    ({"size": "드론"}, "드론"),
    ({"angle": "대각선"}, "대각선"),
    ({"face": "yes"}, "face"),
])
def test_apply_validates(home, patch, word):
    frames = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    labels = [label(f) for f in frames]
    labels[0].update(patch)
    with pytest.raises(ScriptError) as exc:
        shots.apply(VID, CAT, labels_file(home, labels))
    assert exc.value.code == "invalid_shots" and word in exc.value.message


def test_apply_rejects_frames_not_in_sample(home):
    frames = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    labels = [label(f) for f in frames[:-1]] + [label("workspace/frames/x/other.jpg")]
    with pytest.raises(ScriptError) as exc:
        shots.apply(VID, CAT, labels_file(home, labels))
    assert "other.jpg" in exc.value.message


# --- 축 반영 ----------------------------------------------------------------------

def give_shots(home, vid, size, face):
    p = home / "refs" / f"ref_{CAT}" / "videos" / f"{vid}.md"
    dist = {s: 0.0 for s in features.SHOT_SIZES}
    dist[size] = 1.0
    refstore.update_frontmatter(p, {"shots": {"size": dist, "angle": {"정면": 1.0}, "face": face,
                                              "hands_only": 1 - face, "broll": 0.0, "text_overlay": 0.5,
                                              "by_section": {}}})


def test_shots_axis_in_typify(home):
    assert cluster.run(CAT)["axes"]["shots"]["verdict"] == "insufficient"
    for i in range(7):
        give_shots(home, f"a{i:02d}", "탑뷰", 0.0)
    for i in range(3):
        give_shots(home, f"b{i:02d}", "바스트", 1.0)
    out = cluster.run(CAT)
    assert out["axes"]["shots"]["verdict"] == "typable"
    big = max(out["axes"]["shots"]["clusters"], key=lambda c: c["size"])
    assert big["summary"]["top_sizes"][0] == ["탑뷰", 1.0]
    assert out["archetypes"][0]["shots"]["top_sizes"][0][0] == "탑뷰"


def test_shots_vector_none_without_shots(home):
    fm = refstore.read_md(home / "refs" / f"ref_{CAT}" / "videos" / "a00.md")[0]
    assert features.shots_vec(fm) is None


def test_similarity_includes_shots_axis(home):
    for i in range(7):
        give_shots(home, f"a{i:02d}", "탑뷰", 0.0)
    for i in range(3):
        give_shots(home, f"b{i:02d}", "바스트", 1.0)
    cluster.run(CAT)
    mine = write_video(home, "mine01", role="mine")
    give_shots(home, "mine01", "탑뷰", 0.0)
    out = similarity.compare(CAT, mine)
    assert out["axes"]["shots"]["proximity"] >= 0.7


def test_pending_includes_videos_without_frame_labels(home):
    frames = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    shots.apply(VID, CAT, labels_file(home, [label(f) for f in frames]))
    assert VID not in shots.pending(CAT)["pending"]
    (home / "workspace" / "shots" / f"{VID}.json").unlink()        # Phase 12 이전 분류
    out = shots.pending(CAT)
    assert VID in out["pending"] and out["relabel"] == [VID]


def test_main_routes(home):
    assert shots.main(["context", VID, "--category", CAT])["id"] == VID
    assert VID in shots.main(["pending", CAT])["pending"]


# --- 사진 설명(desc) ---------------------------------------------------------------

def frame_path(i):
    return f"workspace/frames/{VID}/scene{i + 1:03d}.jpg"


def make_frames(home, indexes=range(20)):
    """저장된 대표 프레임 파일을 만든다(describe는 파일이 있는 프레임만 센다)."""
    for i in indexes:
        path = home / frame_path(i)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")


def described(frame, desc="시장 골목, 사람 많음", **extra):
    return {"frame": frame, "desc": desc, **extra}


def full(frame, desc="시장 골목, 사람 많음"):
    return {**label(frame), "desc": desc}


def saved_labels(home):
    return json.loads((home / "workspace" / "shots" / f"{VID}.json").read_text("utf-8"))["labels"]


def test_describe_context_skips_described(home):
    make_frames(home)
    shots.apply_desc(VID, CAT, labels_file(home, [full(frame_path(i)) for i in (1, 2)]))

    ctx = shots.describe_context(VID, CAT)

    got = [f["frame"] for f in ctx["frames"]]
    assert len(got) == 8 and frame_path(1) not in got and frame_path(2) not in got
    assert got[0] == frame_path(0) and ctx["vocab"]["size"][0] == "익스트림 클로즈업"
    assert ctx["frames"][0]["section"] == "hook" and ctx["remaining"] == 18
    assert shots.describe_context(VID, CAT, limit=3)["frames"][2]["frame"] == frame_path(4)


def test_describe_context_includes_existing_size_and_angle_and_skips_missing_files(home):
    make_frames(home, range(1, 20))                                   # scene001 파일 없음
    sample = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    shots.apply(VID, CAT, labels_file(home, [label(f, size="와이드", angle="하이앵글") for f in sample]))

    by_frame = {f["frame"]: f for f in shots.describe_context(VID, CAT, limit=40)["frames"]}

    assert frame_path(0) not in by_frame                              # 파일이 없는 프레임은 건너뛴다
    assert (by_frame[sample[-1]]["size"], by_frame[sample[-1]]["angle"]) == ("와이드", "하이앵글")
    unlabelled = next(f for f in by_frame if f not in sample)
    assert "size" not in by_frame[unlabelled]


def test_describe_context_without_frames(home):
    (home / "workspace" / "cuts" / f"{VID}.json").unlink()
    with pytest.raises(ScriptError) as exc:
        shots.describe_context(VID, CAT)
    assert exc.value.code == "no_frames"


def test_apply_desc_merges_existing_labels(home):
    make_frames(home)
    sample = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    shots.apply(VID, CAT, labels_file(home, [label(f, size="와이드", angle="로우앵글") for f in sample]))
    new = frame_path(1)
    assert new not in sample

    out = shots.apply_desc(VID, CAT, labels_file(home, [described(sample[0], "역 앞 광장"), full(new, "카페 내부")]))

    kept = {l["frame"]: l for l in saved_labels(home)}
    assert out["status"] == "applied" and out["described"] == 2
    assert kept[sample[0]]["desc"] == "역 앞 광장" and kept[sample[0]]["size"] == "와이드"
    assert kept[sample[0]]["angle"] == "로우앵글" and kept[sample[0]]["section"] == "hook"
    assert kept[new]["desc"] == "카페 내부" and kept[new]["size"] == "클로즈업" and kept[new]["section"] == "body"
    assert len(kept) == 9
    assert not (home / "workspace" / "tmp" / "shots.json").exists()


def test_apply_desc_new_frame_needs_full_label(home):
    make_frames(home)
    with pytest.raises(ScriptError) as exc:
        shots.apply_desc(VID, CAT, labels_file(home, [described(frame_path(1))]))
    assert exc.value.code == "invalid_shots" and "size" in exc.value.message


@pytest.mark.parametrize("desc", ["", "   ", "가" * 61, None, 5])
def test_apply_desc_rejects_empty_or_long_desc(home, desc):
    make_frames(home)
    with pytest.raises(ScriptError) as exc:
        shots.apply_desc(VID, CAT, labels_file(home, [full(frame_path(1), desc)]))
    assert exc.value.code == "invalid_shots" and "desc" in exc.value.message
    assert not (home / "workspace" / "shots" / f"{VID}.json").exists()


def test_apply_desc_accepts_boundary_lengths_and_strips(home):
    make_frames(home)
    shots.apply_desc(VID, CAT, labels_file(home, [full(frame_path(1), " 가 "), full(frame_path(2), "나" * 60)]))
    assert [l["desc"] for l in saved_labels(home)] == ["가", "나" * 60]


def test_apply_desc_rejects_empty_list_and_unknown_frame(home):
    for labels in ([], [described("workspace/frames/x/other.jpg")]):
        with pytest.raises(ScriptError) as exc:
            shots.apply_desc(VID, CAT, labels_file(home, labels))
        assert exc.value.code == "invalid_shots"


def test_desc_status_counts(home):
    make_frames(home, range(19))                                      # 마지막 프레임 파일은 없다
    assert shots.desc_status(CAT) == {"frames": 19, "described": 0, "videos_todo": [VID]}
    shots.apply_desc(VID, CAT, labels_file(home, [full(frame_path(i)) for i in range(3)]))
    assert shots.desc_status(CAT) == {"frames": 19, "described": 3, "videos_todo": [VID]}
    shots.apply_desc(VID, CAT, labels_file(home, [full(frame_path(i)) for i in range(3, 19)]))
    assert shots.desc_status(CAT) == {"frames": 19, "described": 19, "videos_todo": []}


def test_desc_status_ignores_mine_videos(home):
    make_frames(home)
    refstore.update_frontmatter(home / "refs" / f"ref_{CAT}" / "videos" / f"{VID}.md", {"role": "mine"})
    assert shots.desc_status(CAT) == {"frames": 0, "described": 0, "videos_todo": []}


def test_shots_ratio_unchanged_by_desc(home):
    make_frames(home)
    sample = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    shots.apply(VID, CAT, labels_file(home, [label(f) for f in sample]))
    md = home / "refs" / f"ref_{CAT}" / "videos" / f"{VID}.md"
    before = refstore.read_md(md)

    shots.apply_desc(VID, CAT, labels_file(home, [full(frame_path(1)), described(sample[0])]))

    assert refstore.read_md(md) == before


def test_apply_keeps_existing_desc_and_other_frames(home):
    make_frames(home)
    sample = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    shots.apply_desc(VID, CAT, labels_file(home, [full(sample[0], "첫 장면"), full(frame_path(1), "다른 장면")]))

    shots.apply(VID, CAT, labels_file(home, [label(f) for f in sample]))

    kept = {l["frame"]: l for l in saved_labels(home)}
    assert kept[sample[0]]["desc"] == "첫 장면" and kept[frame_path(1)]["desc"] == "다른 장면"


def test_apply_validates_desc_when_given(home):
    sample = [f["frame"] for f in shots.context(VID, CAT)["frames"]]
    labels = [label(f) for f in sample]
    labels[0]["desc"] = "가" * 61
    with pytest.raises(ScriptError) as exc:
        shots.apply(VID, CAT, labels_file(home, labels))
    assert exc.value.code == "invalid_shots" and "desc" in exc.value.message


def test_main_describe_routes(home, capsys):
    make_frames(home)
    assert shots.main(["describe-context", VID, "--category", CAT])["id"] == VID
    path = labels_file(home, [full(frame_path(1))])
    out = shots.main(["apply-desc", VID, "--category", CAT, "--labels", str(path)])
    assert out["described"] == 1
    assert shots.main(["desc-status", CAT])["described"] == 1
