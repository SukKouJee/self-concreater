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


def test_main_routes(home):
    assert shots.main(["context", VID, "--category", CAT])["id"] == VID
    assert VID in shots.main(["pending", CAT])["pending"]
