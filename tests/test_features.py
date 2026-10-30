import math

import pytest

from scripts import features
from tests.typify_fixtures import CAT, SKELETON_A, SKELETON_B, standard_set, write_video


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


def key_of(home, path):
    """write_video가 돌려준 경로 -> 작업 폴더 기준 문서 키."""
    return path.relative_to(home).as_posix()


def test_load_refs_excludes_mine(home):
    standard_set(home, n_a=3, n_b=0)
    write_video(home, "mine1", role="mine")
    records = features.load_videos(CAT)
    assert [r["id"] for r in records] == ["a00", "a01", "a02"]


def test_sequence_collapses_and_marks_sub():
    fm = {"sections": [{"label": "hook", "start": 0, "end": 3},
                       {"label": "body", "sub": "story", "start": 3, "end": 30},
                       {"label": "body", "sub": "story", "start": 30, "end": 50},
                       {"label": "cta", "start": 50, "end": 60}]}
    assert features.sequence(fm) == ["hook", "body/story", "cta"]


def test_structure_vector_shares(home):
    path = write_video(home, "v1", skeleton=SKELETON_B)
    rec = features.record(key_of(home, path))
    vec = dict(zip(features.STRUCTURE_FIELDS, features.structure_vec(rec["fm"])))
    assert vec["share:hook"] == pytest.approx(5 / 60)
    assert vec["share:intro"] == pytest.approx(10 / 60)
    assert vec["share:body"] == pytest.approx(35 / 60)
    assert vec["share:cta"] == 0
    assert vec["sub:list_item"] == pytest.approx(35 / 60)
    assert vec["sections"] == pytest.approx(0.4)


def test_hook_vector_onehot_plus_length(home):
    rec = features.record(key_of(home, write_video(home, "v1", skeleton=SKELETON_A, hook="question")))
    vec = features.hook_vec(rec["fm"])
    assert vec[: len(features.HOOK_TYPES)] == [1.0, 0.0, 0.0, 0.0]
    assert vec[-1] == pytest.approx(0.3)      # 훅 3초 / 10


def test_title_vector(home):
    rec = features.record(key_of(home, write_video(home, "v1", title="[ASMR] 버블티 비교?")))
    vec = dict(zip(["number", "question", "bracket", "exclaim", "length"], features.title_vec(rec["fm"])))
    assert vec["question"] == 1.0 and vec["bracket"] == 1.0 and vec["number"] == 0.0


def test_title_vector_recomputes_patterns_from_title(home):
    # title_pattern을 손으로 지워도 제목에서 다시 계산한다(사용자가 제목을 고친 경우 반영)
    rec = features.record(key_of(home, write_video(home, "v1", title="라면 5봉지!")))
    assert features.title_vec(rec["fm"])[0] == 1.0


def test_pacing_vector_and_missing(home):
    rec = features.record(key_of(home, write_video(home, "v1", avg_cut=2.0)))
    assert features.pacing_vec(rec["fm"]) == pytest.approx([math.log(2.0), math.log(1.0), math.log(2.0)])
    rec["fm"]["pacing"] = {"avg_cut_sec": None, "by_section": {}}
    assert features.pacing_vec(rec["fm"]) is None


def test_axis_matrix_skips_missing(home):
    standard_set(home, n_a=3, n_b=0)
    records = features.load_videos(CAT)
    records[1]["fm"]["pacing"] = {"avg_cut_sec": None, "by_section": {}}
    ids, matrix = features.axis_matrix(records, "pacing")
    assert ids == ["a00", "a02"]
    assert len(matrix) == 2
