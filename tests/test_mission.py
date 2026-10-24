import json

import pytest

from scripts import cluster, mission, motif, refstore, similarity, draft
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set


def _json(home, name, data):
    path = home / "workspace" / "tmp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


PROFILE = ("---\ncreated: 2026-10-01\nupdated: 2026-10-01\nschema_version: 1\n---\n# 크리에이터 프로필\n\n"
           "## 관심 주제\n- 자취 요리\n\n## 포맷\n쇼츠\n\n## 롤모델\n\n## 하루 시간\n20분\n\n"
           "## 피하고 싶은 스타일\n- 얼굴 출연\n\n## 강점\n\n## 약점\n\n## 성장 단계\n관찰\n")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    (tmp_path / "profile.md").write_text(PROFILE, encoding="utf-8")
    return tmp_path


def mission_json(home, title="탑뷰 손 클로즈업 15초 찍기", kind="촬영", minutes=20, source="M-mukbang-001"):
    return _json(home, "mission.json", {
        "title": title, "kind": kind, "minutes": minutes, "source": source,
        "why": "★카드의 탑뷰 기법을 연습", "steps": ["폰을 탑뷰로 고정", "면 들기 15초 촬영"],
        "deliverable": "15초 클립 1개",
    })


# --- 난이도 조정 ------------------------------------------------------------------

@pytest.mark.parametrize("history, base, expected", [
    ([], 20, (20, "same")),
    ([("submitted", "hard"), ("submitted", "hard"), ("submitted", "right")], 20, (15, "easier")),
    ([("skipped", None), ("submitted", "hard"), ("submitted", "easy")], 20, (15, "easier")),
    ([("submitted", "easy"), ("submitted", "easy"), ("submitted", "right")], 20, (25, "harder")),
    ([("submitted", "easy"), ("submitted", "easy")], 30, (30, "harder")),        # 30분 상한
    ([("submitted", "hard"), ("submitted", "hard")], 15, (15, "easier")),        # 15분 하한
    ([("submitted", "right"), ("submitted", "easy"), ("submitted", "hard")], 20, (20, "same")),
])
def test_recommend_minutes(history, base, expected):
    recent = [{"status": s, "difficulty": d, "minutes": base} for s, d in history]
    assert mission.recommend(recent, base) == {"minutes": expected[0], "adjust": expected[1]}


# --- 3일 시뮬레이션 (완료 조건) ------------------------------------------------------

def test_three_day_simulation_adapts_difficulty(home):
    days = ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert mission.context(date=days[0])["recommended"] == {"minutes": 20, "adjust": "same"}
    for day in days[:2]:
        mission.save(mission_json(home, minutes=mission.context(date=day)["recommended"]["minutes"]), date=day)
        mission.submit("clip.mp4", "hard", "손이 화면 밖으로 나감", date=day)
    ctx = mission.context(date=days[2])
    assert ctx["recommended"] == {"minutes": 15, "adjust": "easier"}
    assert [h["difficulty"] for h in ctx["history"]] == ["hard", "hard"]


# --- context ----------------------------------------------------------------------

def test_context_collects_materials(home):
    standard_set(home)
    cluster.run(CAT)
    cluster.apply(CAT, _json(home, "n.json", {
        "axes": {a: "근거" for a in ["structure", "hook", "narrative", "title", "pacing"]},
        "archetypes": [{"id": "A1", "name": "결과 먼저"}, {"id": "A2", "name": "리스트"}]}))
    motif.add(CAT, _json(home, "c.json", [{"type": "technique", "title": "탑뷰 손", "idea": "위에서 손만",
                                           "evidence": ["a00"]}]))
    motif.star(CAT, "M-mukbang-001")
    d = draft.save(CAT, _json(home, "d.json", {
        "title": "라면 이야기", "hook_type": "situation", "narrative_type": "problem_solution", "script_only": True,
        "sections": [{"label": "intro", "start": 0, "end": 20, "script": "x"},
                     {"label": "outro", "start": 20, "end": 60, "script": "y"}]}))
    target = home / d["path"]
    gaps = similarity.compare(CAT, target)["gaps"]
    similarity.save(CAT, target, _json(home, "dn.json", {"summary": "s", "actions": ["a"],
                                                          "gaps": {g["key"]: "x" for g in gaps}}))
    similarity.intentional(CAT, target.stem, gaps[0]["key"])

    ctx = mission.context(date="2026-10-20")
    assert ctx["profile"]["성장 단계"] == "관찰"
    assert ctx["base_minutes"] == 20
    assert ctx["starred"] == [{"category": CAT, "id": "M-mukbang-001", "type": "기법", "title": "탑뷰 손"}]
    assert ctx["drafts"][0]["path"] == d["path"]
    diag = ctx["gaps"][0]
    assert diag["name"] == target.stem
    assert gaps[0]["key"] not in diag["gaps"]                 # intentional은 재료에서 빠진다
    assert ctx["today"] is None


def test_context_without_profile(home):
    (home / "profile.md").unlink()
    ctx = mission.context(date="2026-10-20")
    assert ctx["profile"] is None and ctx["base_minutes"] == 20


# --- save / submit / skip / swap -------------------------------------------------

def test_save_writes_mission_md(home):
    out = mission.save(mission_json(home), date="2026-10-20")
    assert out["path"] == "missions/2026-10-20.md"
    fm, body = refstore.read_md(home / out["path"])
    assert fm["status"] == "open" and fm["minutes"] == 20 and fm["kind"] == "촬영"
    assert fm["stage"] == "관찰" and fm["source"] == "M-mukbang-001"
    assert "### 탑뷰 손 클로즈업 15초 찍기 (20분 · 촬영)" in body
    assert "1. 폰을 탑뷰로 고정" in body and "- 결과물: 15초 클립 1개" in body


def test_save_twice_needs_replace(home):
    mission.save(mission_json(home), date="2026-10-20")
    with pytest.raises(ScriptError) as exc:
        mission.save(mission_json(home, title="다른 미션"), date="2026-10-20")
    assert exc.value.code == "mission_exists"
    mission.save(mission_json(home, title="다른 미션"), date="2026-10-20", replace=True)
    fm, body = refstore.read_md(home / "missions" / "2026-10-20.md")
    assert "다른 미션" in body and fm["swapped_from"] == "탑뷰 손 클로즈업 15초 찍기"


def test_save_validates(home):
    with pytest.raises(ScriptError) as exc:
        mission.save(mission_json(home, minutes=60), date="2026-10-20")
    assert exc.value.code == "invalid_mission" and "minutes" in exc.value.message
    with pytest.raises(ScriptError) as exc:
        mission.save(mission_json(home, kind="운동"), date="2026-10-20")
    assert "kind" in exc.value.message


def test_submit_and_skip(home):
    mission.save(mission_json(home), date="2026-10-20")
    mission.submit("clips/a.mp4", "right", "탑뷰 각도가 안정적이었다. 다음엔 손 위치를 화면 중앙에.", date="2026-10-20")
    fm, body = refstore.read_md(home / "missions" / "2026-10-20.md")
    assert fm["status"] == "submitted" and fm["difficulty"] == "right"
    assert "a.mp4" in body.split("## 제출")[1] and "탑뷰 각도가" in body.split("## 피드백")[1]

    mission.save(mission_json(home), date="2026-10-21")
    mission.skip("야근", date="2026-10-21")
    fm, _ = refstore.read_md(home / "missions" / "2026-10-21.md")
    assert fm["status"] == "skipped" and fm["skip_reason"] == "야근"


def test_submit_without_mission(home):
    with pytest.raises(ScriptError) as exc:
        mission.submit("x", "easy", "f", date="2026-10-20")
    assert exc.value.code == "no_mission"


def test_submit_rejects_bad_difficulty(home):
    mission.save(mission_json(home), date="2026-10-20")
    with pytest.raises(ScriptError) as exc:
        mission.submit("x", "medium", "f", date="2026-10-20")
    assert exc.value.code == "usage"


# --- 주간 회고 ---------------------------------------------------------------------

def test_week_stats(home):
    for day, status, diff in [("2026-10-19", "submitted", "easy"), ("2026-10-20", "submitted", "hard"),
                              ("2026-10-21", "skipped", None), ("2026-10-27", "submitted", "right")]:
        mission.save(mission_json(home, kind="촬영" if day != "2026-10-20" else "편집"), date=day)
        if status == "submitted":
            mission.submit("x", diff, "f", date=day)
        else:
            mission.skip("피곤", date=day)
    week = mission.week("2026-W43")                  # 10/19(월) ~ 10/25(일)
    assert week["range"] == ["2026-10-19", "2026-10-25"]
    assert week["missions"] == {"total": 3, "submitted": 2, "skipped": 1, "open": 0, "completion": 0.67}
    assert week["difficulty"] == {"easy": 1, "hard": 1}
    assert week["skip_reasons"] == ["피곤"]
    assert week["kinds"] == {"촬영": 2, "편집": 1}


def test_review_save_and_profile_update(home):
    out = mission.review_save("2026-W43", _json(home, "r.json", {
        "summary": "촬영 미션은 꾸준했고 편집은 어려워했다.",
        "repeated_weakness": ["손이 화면 밖으로 나감"],
        "next_week": ["탑뷰 고정 촬영 3번", "컷 편집 1번"],
        "profile_proposal": {"약점": "- 프레이밍(손 위치)", "성장 단계": "모방"},
    }))
    assert out["path"] == "reviews/2026-W43.md"
    _, body = refstore.read_md(home / out["path"])
    assert "탑뷰 고정 촬영 3번" in body and "성장 단계" in body

    mission.profile_update(_json(home, "p.json", {"약점": "- 프레이밍(손 위치)", "성장 단계": "모방"}), date="2026-10-26")
    fm, body = refstore.read_md(home / "profile.md")
    assert fm["updated"] == "2026-10-26"
    assert body.split("## 성장 단계")[1].strip() == "모방"
    assert "- 프레이밍(손 위치)" in body.split("## 약점")[1].split("## 성장 단계")[0]
    assert "- 자취 요리" in body                                  # 다른 섹션은 그대로


def test_profile_update_rejects_unknown_section_and_stage(home):
    with pytest.raises(ScriptError) as exc:
        mission.profile_update(_json(home, "p.json", {"취미": "x"}), date="2026-10-26")
    assert exc.value.code == "invalid_profile_update"
    with pytest.raises(ScriptError) as exc:
        mission.profile_update(_json(home, "p.json", {"성장 단계": "고수"}), date="2026-10-26")
    assert "성장 단계" in exc.value.message


def test_main_routes(home):
    mission_json(home)
    assert mission.main(["save", "--mission", "workspace/tmp/mission.json", "--date", "2026-10-20"])["status"] == "saved"
    assert mission.main(["submit", "x.mp4", "--difficulty", "easy", "--feedback", "좋음", "--date", "2026-10-20"])["status"] == "submitted"
    assert mission.main(["context", "--date", "2026-10-21"])["history"][0]["difficulty"] == "easy"
    assert mission.main(["week", "2026-W43"])["missions"]["total"] == 1


# --- 특성 테스트: 검증 메시지·submit 출력(변경 전후 동일해야 하는 것) ---------------------------

@pytest.mark.parametrize("patch, expected", [
    ({"title": "  "}, "미션이 형식에 맞지 않습니다: title이 비어 있습니다"),
    ({"kind": "운동"}, "미션이 형식에 맞지 않습니다: kind='운동' (허용: 관찰, 분석, 기획, 촬영, 편집, 업로드)"),
    ({"minutes": 60}, "미션이 형식에 맞지 않습니다: minutes=60 (10~30분)"),
    ({"minutes": "20"}, "미션이 형식에 맞지 않습니다: minutes='20' (10~30분)"),
    ({"steps": []}, "미션이 형식에 맞지 않습니다: steps가 비어 있습니다"),
    ({"steps": ["  ", ""]}, "미션이 형식에 맞지 않습니다: steps가 비어 있습니다"),
])
def test_save_validation_messages_are_stable(home, patch, expected):
    data = json.loads(mission_json(home).read_text("utf-8")) | patch
    with pytest.raises(ScriptError) as exc:
        mission.save(_json(home, "m2.json", data), date="2026-10-20")
    assert exc.value.code == "invalid_mission" and exc.value.message == expected
    assert not (home / "missions" / "2026-10-20.md").exists()


def test_submit_output_and_sections_are_stable(home):
    mission.save(mission_json(home), date="2026-10-20")
    out = mission.submit("a.mp4", "right", " 좋았다 ", date="2026-10-20")
    assert out == {"status": "submitted", "date": "2026-10-20", "difficulty": "right"}
    _, body = refstore.read_md(home / "missions" / "2026-10-20.md")
    assert "## 제출\n- 파일: a.mp4\n- 난이도: right\n\n## 피드백\n좋았다\n" in body.replace("\r\n", "\n")


# --- validate_mission ------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, [], "미션", 3])
def test_validate_mission_rejects_non_dict(bad):
    with pytest.raises(ScriptError) as exc:
        mission.validate_mission(bad)
    assert exc.value.code == "invalid_mission"


@pytest.mark.parametrize("steps", ["한 줄 문자열", {"a": 1}, 5])
def test_validate_mission_rejects_non_list_steps(steps):
    with pytest.raises(ScriptError) as exc:
        mission.validate_mission({"title": "t", "kind": "촬영", "minutes": 20, "steps": steps})
    assert exc.value.code == "invalid_mission" and "steps" in exc.value.message


def test_validate_mission_rejects_bool_minutes():
    with pytest.raises(ScriptError) as exc:
        mission.validate_mission({"title": "t", "kind": "촬영", "minutes": True, "steps": ["a"]})
    assert exc.value.code == "invalid_mission"


def test_validate_mission_cleans_one_line_fields():
    out = mission.validate_mission({
        "title": "  탑뷰 \n 촬영 ", "kind": "촬영", "minutes": 20.0, "source": "  M-x-001 ", "why": "왜\n그래",
        "steps": [" 하나 ", "", "둘\n셋"], "deliverable": "  클립  ", "extra": "버림"})
    assert out == {"title": "탑뷰 촬영", "kind": "촬영", "minutes": 20, "source": "M-x-001", "why": "왜 그래",
                   "steps": ["하나", "둘 셋"], "deliverable": "클립"}


def test_validate_mission_blank_optionals_become_none():
    out = mission.validate_mission({"title": "t", "kind": "관찰", "minutes": 15, "steps": ["a"], "source": " "})
    assert out["source"] is None and out["why"] is None and out["deliverable"] is None


def test_save_writes_cleaned_title(home):
    data = json.loads(mission_json(home).read_text("utf-8")) | {"title": "  제목 \n 둘  "}
    mission.save(_json(home, "m3.json", data), date="2026-10-20")
    _, body = refstore.read_md(home / "missions" / "2026-10-20.md")
    assert "### 제목 둘 (20분 · 촬영)" in body


# --- submit note / 파일 줄 ---------------------------------------------------------

def _submission(home, day):
    _, body = refstore.read_md(home / "missions" / f"{day}.md")
    return body.replace("\r\n", "\n").split("## 제출\n")[1].split("\n## 피드백")[0].strip()


def test_submit_records_note_and_file_name_only(home):
    mission.save(mission_json(home), date="2026-10-20")
    mission.submit(r"C:\Users\me\clips\a.mp4", "easy", "f", date="2026-10-20", note=" 탑뷰로 \n 찍었다 ")
    assert _submission(home, "2026-10-20") == "- 한 일: 탑뷰로 찍었다\n- 파일: a.mp4\n- 난이도: easy"


def test_submit_with_empty_file_omits_file_line(home):
    mission.save(mission_json(home), date="2026-10-20")
    mission.submit("", "right", "f", date="2026-10-20", note="찍음")
    assert _submission(home, "2026-10-20") == "- 한 일: 찍음\n- 난이도: right"


def test_resubmit_replaces_submission(home):
    mission.save(mission_json(home), date="2026-10-20")
    mission.submit("", "hard", "첫", date="2026-10-20", note="처음")
    mission.submit("", "easy", "둘째", date="2026-10-20", note="다시")
    fm, body = refstore.read_md(home / "missions" / "2026-10-20.md")
    assert fm["difficulty"] == "easy" and _submission(home, "2026-10-20") == "- 한 일: 다시\n- 난이도: easy"
    assert "첫" not in body


def test_cli_submit_accepts_note(home, capsys):
    mission.save(mission_json(home), date="2026-10-20")
    mission.main(["submit", "x.png", "--difficulty", "right", "--feedback", "f", "--note", "했다", "--date", "2026-10-20"])
    assert "- 한 일: 했다" in _submission(home, "2026-10-20")


# --- 쓰기 원자화 ---------------------------------------------------------------------

def test_save_and_submit_write_through_write_atomic(home, monkeypatch):
    calls = []
    real = refstore.write_atomic
    monkeypatch.setattr(refstore, "write_atomic", lambda p, d: (calls.append(p.name), real(p, d))[1])
    mission.save(mission_json(home), date="2026-10-20")
    mission.submit("", "right", "f", date="2026-10-20")
    assert calls == ["2026-10-20.md", "2026-10-20.md"]
    assert not list((home / "missions").glob("*.tmp"))


def test_context_skips_record_with_broken_frontmatter(home):
    mission.save(mission_json(home), date="2026-10-21")
    (home / "missions" / "2026-10-20.md").write_text("---\n: [oops\n---\nx", encoding="utf-8")
    assert [m["date"] for m in mission.context(date="2026-10-22")["history"]] == ["2026-10-21"]


# --- fix round 1 ---------------------------------------------------------------------

def _real_sections(home, day):
    _, body = refstore.read_md(home / "missions" / f"{day}.md")
    return [line for line in body.replace("\r\n", "\n").split("\n") if line.startswith("## ")]


def test_refstore_escape_headings_exists():
    assert refstore.escape_headings("a\n## b\n#태그") == "a\n\## b\n#태그"


def test_submit_escapes_feedback_headings_across_resubmit(home):
    mission.save(mission_json(home), date="2026-10-20")
    for _ in range(2):
        mission.submit("", "right", "좋았다\n## 가짜\n내용", date="2026-10-20", note="n")
        assert _real_sections(home, "2026-10-20") == ["## 미션", "## 제출", "## 피드백"]
    _, body = refstore.read_md(home / "missions" / "2026-10-20.md")
    assert "\## 가짜" in body


def test_submit_folds_file_name_and_clears_skip_reason(home):
    mission.save(mission_json(home), date="2026-10-20")
    mission.skip("야근", date="2026-10-20")
    mission.submit("a\nb.png", "right", "f", date="2026-10-20", note="n")
    fm, _ = refstore.read_md(home / "missions" / "2026-10-20.md")
    assert fm["skip_reason"] is None and fm["status"] == "submitted"
    assert "- 파일: a b.png" in _submission(home, "2026-10-20")


# --- 회고 검증·메모 보존 (report-ui) -----------------------------------------------------

REVIEW = {"summary": "촬영 미션은 꾸준했다.", "repeated_weakness": ["손이 화면 밖으로 나감"],
          "next_week": ["탑뷰 고정 촬영 3번"], "profile_proposal": {"약점": "- 프레이밍", "성장 단계": "모방"}}


def test_validate_review_keeps_empty_summary_message():
    for bad in ({}, {"summary": "   "}, {"summary": None}):
        with pytest.raises(ScriptError) as exc:
            mission.validate_review(bad)
        assert (exc.value.code, exc.value.message) == ("invalid_review", "summary가 비어 있습니다")


def test_review_save_empty_summary_message_is_stable(home):
    with pytest.raises(ScriptError) as exc:
        mission.review_save("2026-W43", _json(home, "r.json", {"summary": " "}))
    assert (exc.value.code, exc.value.message) == ("invalid_review", "summary가 비어 있습니다")
    assert not (home / "reviews").exists()


def test_validate_review_cleans_and_defaults():
    out = mission.validate_review({"summary": "  요약  ", "repeated_weakness": [" 손 \n 위치 ", "", None],
                                   "next_week": None, "profile_proposal": None})
    assert out == {"summary": "요약", "repeated_weakness": ["손 위치"], "next_week": [], "profile_proposal": {}}


@pytest.mark.parametrize("patch", [
    {"repeated_weakness": "약점 하나"}, {"next_week": {"a": 1}}, {"repeated_weakness": [["x"]]},
    {"profile_proposal": ["약점"]}, {"profile_proposal": {"취미": "x"}}, {"profile_proposal": {"약점": 3}},
    {"profile_proposal": {"성장 단계": "고수"}}, {"summary": ["a"]},
])
def test_validate_review_rejects_bad_shapes(patch):
    with pytest.raises(ScriptError) as exc:
        mission.validate_review({**REVIEW, **patch})
    assert exc.value.code == "invalid_review"


def test_validate_review_rejects_non_dict():
    with pytest.raises(ScriptError) as exc:
        mission.validate_review(["summary"])
    assert exc.value.code == "invalid_review"


def test_review_save_rejects_bad_shape(home):
    with pytest.raises(ScriptError) as exc:
        mission.review_save("2026-W43", _json(home, "r.json", {**REVIEW, "profile_proposal": {"취미": "x"}}))
    assert exc.value.code == "invalid_review"
    assert not (home / "reviews" / "2026-W43.md").exists()


def test_review_save_keeps_memo_on_resave(home):
    mission.review_save("2026-W43", _json(home, "r.json", REVIEW))
    path = home / "reviews" / "2026-W43.md"
    path.write_bytes(path.read_bytes() + "이번 주는 너무 바빴다.\n\n## 직접 추가한 절\n내용\n".encode("utf-8"))
    mission.review_save("2026-W43", _json(home, "r2.json", {**REVIEW, "summary": "새 요약"}))
    fm, body = refstore.read_md(path)
    assert "새 요약" in body and "촬영 미션은 꾸준했다" not in body
    assert body.count("## 내 메모") == 1
    assert "이번 주는 너무 바빴다." in body and "## 직접 추가한 절\n내용" in body.replace("\r\n", "\n")
    assert fm["approved"] is False and fm["week"] == "2026-W43"


def test_review_save_resets_approved_and_keeps_user_keys(home):
    mission.review_save("2026-W43", _json(home, "r.json", REVIEW))
    path = home / "reviews" / "2026-W43.md"
    refstore.update_frontmatter(path, {"approved": ["약점"], "tag": "mine"})
    mission.review_save("2026-W43", _json(home, "r2.json", REVIEW))
    fm, _ = refstore.read_md(path)
    assert fm["approved"] is False and fm["tag"] == "mine"


def test_review_save_keeps_crlf(home):
    mission.review_save("2026-W43", _json(home, "r.json", REVIEW))
    path = home / "reviews" / "2026-W43.md"
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes((text + "메모 한 줄\r\n").encode("utf-8"))
    mission.review_save("2026-W43", _json(home, "r2.json", {**REVIEW, "summary": "다시"}))
    raw = path.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n") > 5
    assert "메모 한 줄" in raw.decode("utf-8")


def test_review_save_writes_atomically_and_escapes_headings(home, monkeypatch):
    calls = []
    real = refstore.write_atomic
    monkeypatch.setattr(refstore, "write_atomic", lambda p, d: (calls.append(p.name), real(p, d))[1])
    mission.review_save("2026-W43", _json(home, "r.json", {**REVIEW, "summary": "요약\n## 내 메모\n가짜"}))
    assert calls == ["2026-W43.md"]
    _, body = refstore.read_md(home / "reviews" / "2026-W43.md")
    assert body.count("\n## 내 메모") == 1


def _crlf_profile(home):
    (home / "profile.md").write_bytes(PROFILE.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


def test_profile_update_escapes_heading_lines(home):
    mission.profile_update(_json(home, "p.json", {"약점": "- 손 위치\n## 강점\n가짜"}), date="2026-10-26")
    _, body = refstore.read_md(home / "profile.md")
    body = body.replace("\r\n", "\n")
    escaped = "\\" + "## 강점"
    assert body.count("\n## 강점") == 1 and escaped in body
    assert body.index("## 약점") < body.index(escaped) < body.index("## 성장 단계")


def test_profile_update_keeps_crlf_and_untouched_sections(home):
    _crlf_profile(home)
    mission.profile_update(_json(home, "p.json", {"약점": "- 프레이밍"}), date="2026-10-26")
    raw = (home / "profile.md").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n") > 5
    _, body = refstore.read_md(home / "profile.md")
    assert "- 얼굴 출연" in body and "- 프레이밍" in body and "## 성장 단계\r\n관찰" in body


def test_profile_update_lf_stays_lf_and_writes_atomically(home, monkeypatch):
    (home / "profile.md").write_bytes(PROFILE.replace("\r\n", "\n").encode("utf-8"))
    calls = []
    real = refstore.write_atomic
    monkeypatch.setattr(refstore, "write_atomic", lambda p, d: (calls.append(p.name), real(p, d))[1])
    mission.profile_update(_json(home, "p.json", {"강점": "- 편집"}), date="2026-10-26")
    assert calls == ["profile.md"] and b"\r\n" not in (home / "profile.md").read_bytes()


def test_profile_update_partial_sections_only_and_memo_untouched(home):
    memo = PROFILE.rstrip("\n") + "\n\n## 내 메모\n건드리지 마\n"
    (home / "profile.md").write_bytes(memo.replace("\r\n", "\n").encode("utf-8"))
    mission.profile_update(_json(home, "p.json", {"성장 단계": "모방"}), date="2026-10-26")
    _, body = refstore.read_md(home / "profile.md")
    assert "## 성장 단계\n모방" in body and "- 자취 요리" in body and body.endswith("## 내 메모\n건드리지 마\n")


def test_profile_update_cannot_target_memo_section(home):
    with pytest.raises(ScriptError) as exc:
        mission.profile_update(_json(home, "p.json", {"내 메모": "x"}), date="2026-10-26")
    assert exc.value.code == "invalid_profile_update"
