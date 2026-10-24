"""리포트·회고 API: 리포트 목록·만들기, 주 목록, 한 주 조회, 회고 쓰기(작업) → 저장, 프로필 반영. claude 호출은 가짜로 바꾼다."""
import json
import threading

import pytest

from scripts import claude_cli, mission, refstore
from scripts.common import ScriptError
from scripts.ui import api_report
from tests.test_mission import PROFILE
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture
from tests.typify_fixtures import CAT, standard_set

WEEK = "2026-W43"                                     # 2026-10-19(월) ~ 10-25(일)
GOOD = {"summary": "촬영 미션은 꾸준했다.", "repeated_weakness": ["손이 화면 밖으로 나감"],
        "next_week": ["탑뷰 고정 촬영 3번"], "profile_proposal": {"약점": "- 프레이밍", "성장 단계": "모방"}}
MISSION = {"title": "탑뷰 손 클로즈업", "kind": "촬영", "minutes": 20, "source": "M-1", "steps": ["찍기"]}
LF_PROFILE = PROFILE.replace("\r\n", "\n")


class FakeAsk:
    """claude_cli.ask를 대신한다(ask_validated의 재시도 경로는 진짜). replies가 있으면 차례로 쓴다."""

    def __init__(self):
        self.calls = []
        self.replies = []
        self.gate = None

    def __call__(self, rules, payload, task, **kw):
        self.calls.append({"rules": rules, "payload": payload, "task": task, **kw})
        if self.gate is not None:
            assert self.gate.wait(5)
        reply = self.replies.pop(0) if self.replies else dict(GOOD)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    (tmp_path / "profile.md").write_bytes(LF_PROFILE.encode("utf-8"))
    return tmp_path


@pytest.fixture
def fake_ask(monkeypatch, home):
    f = FakeAsk()
    monkeypatch.setattr(claude_cli, "ask", f)
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    return f


@pytest.fixture
def client(home, fake_ask, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def _ok(client, method, path, body=None):
    status, resp = client.json(method, path, body)
    assert status == 200, resp
    return resp


def _err(client, method, path, body=None):
    status, resp = client.json(method, path, body)
    assert status == 400, resp
    return resp["error"]


def _put_mission(home, day, status="submitted", difficulty="right"):
    path = home / "workspace" / "tmp" / "seed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(MISSION, ensure_ascii=False), encoding="utf-8")
    mission.save(path, day)
    if status == "submitted":
        mission.submit("x.png", difficulty, "좋아요", date=day)
    elif status == "skipped":
        mission.skip("피곤", date=day)


def _write(client, week=WEEK):
    resp = _ok(client, "POST", "/api/review/write", {"week": week})
    return wait_for(client, resp["job"]["id"], lambda j: j["status"] in ("done", "failed"))


def _tmp(home, pattern):
    tmp = home / "workspace" / "tmp"
    return list(tmp.glob(pattern)) if tmp.exists() else []


def _review_file(home, week=WEEK):
    return home / "reviews" / f"{week}.md"


# ---- 리포트 ---------------------------------------------------------------------------

def test_report_list_empty_and_bad_category(client, home):
    standard_set(home, n_a=3, n_b=0)
    assert _ok(client, "GET", f"/api/report/list?category={CAT}") == {"category": CAT, "reports": [], "has_typify": False}
    assert _err(client, "GET", "/api/report/list?category=nope") == "category_not_found"
    assert _err(client, "GET", "/api/report/list") == "bad_request"


def test_report_list_filters_names_and_sorts_desc(client, home):
    standard_set(home, n_a=3, n_b=0)
    folder = home / "reports"
    folder.mkdir()
    for name in [f"{CAT}-2026-10-01.html", f"{CAT}-2026-10-20.html", f"{CAT}-x.html", f"{CAT}-2026-10-20.html.bak",
                 "other-2026-10-21.html", f"{CAT}-extra-2026-10-22.html", "notes.html"]:
        (folder / name).write_text("<p>x</p>", encoding="utf-8")
    (home / "workspace" / "typify").mkdir(parents=True)
    (home / "workspace" / "typify" / f"{CAT}.json").write_text("{}", encoding="utf-8")
    body = _ok(client, "GET", f"/api/report/list?category={CAT}")
    assert [r["date"] for r in body["reports"]] == ["2026-10-20", "2026-10-01"]
    assert body["reports"][0] == {"path": f"reports/{CAT}-2026-10-20.html", "date": "2026-10-20",
                                  "url": f"/files/reports/{CAT}-2026-10-20.html"}
    assert body["has_typify"] is True


def test_report_build_without_typify_works_and_url_is_served(client, home):
    standard_set(home, n_a=3, n_b=0)                  # 유형화 결과 없음: 기존 build는 안내 문구로 만든다
    out = _ok(client, "POST", "/api/report/build", {"category": CAT})
    assert out["status"] == "built" and out["path"].startswith(f"reports/{CAT}-") and out["url"] == "/files/" + out["path"]
    assert (home / out["path"]).exists()
    status, _, raw = client.call("GET", out["url"])
    assert status == 200 and "아직 유형화 결과가 없습니다" in raw.decode("utf-8")
    assert [r["path"] for r in _ok(client, "GET", f"/api/report/list?category={CAT}")["reports"]] == [out["path"]]


def test_file_url_is_percent_encoded():
    assert api_report._file_url("reports/먹방 모음-2026-10-20.html") == "/files/reports/%EB%A8%B9%EB%B0%A9%20%EB%AA%A8%EC%9D%8C-2026-10-20.html"


def test_report_build_errors(client):
    assert _err(client, "POST", "/api/report/build", {"category": "nope"}) == "category_not_found"
    assert _err(client, "POST", "/api/report/build", {}) == "bad_request"


# ---- 주 목록 · 한 주 조회 ----------------------------------------------------------------

def test_weeks_current_plus_saved_desc_without_duplicates(client, home):
    current = mission._week_range(None)[0]
    reviews = home / "reviews"
    reviews.mkdir()
    for name in ["2026-W43.md", "2026-W10.md", f"{current}.md", "memo.md", "2026-W54.md"]:
        (reviews / name).write_text("x", encoding="utf-8")
    body = _ok(client, "GET", "/api/review/weeks")
    names = [w["week"] for w in body["weeks"]]
    assert body["current"] == current and names == sorted(set(names), reverse=True)
    assert names.count(current) == 1 and "2026-W43" in names and "2026-W10" in names
    assert "memo" not in names and "2026-W54" not in names
    by_week = {w["week"]: w for w in body["weeks"]}
    assert by_week["2026-W43"] == {"week": "2026-W43", "range": ["2026-10-19", "2026-10-25"], "saved": True}
    assert by_week[current]["saved"] is True


def test_weeks_without_reviews_has_only_current(client):
    body = _ok(client, "GET", "/api/review/weeks")
    assert [w["week"] for w in body["weeks"]] == [body["current"]] and body["weeks"][0]["saved"] is False


def test_review_get_with_missions(client, home):
    _put_mission(home, "2026-10-19", "submitted", "easy")
    _put_mission(home, "2026-10-20", "skipped")
    body = _ok(client, "GET", f"/api/review?week={WEEK}")
    assert body["week"] == WEEK and body["range"] == ["2026-10-19", "2026-10-25"]
    stats = body["stats"]
    assert stats["missions"]["total"] == 2 and stats["missions"]["completion"] == 0.5
    assert stats["difficulty"] == {"easy": 1} and stats["skip_reasons"] == ["피곤"] and stats["kinds"] == {"촬영": 2}
    assert stats["cards_created"] == 0 and stats["diagnosis_gaps"] == [] and stats["diagnosis_gap_count"] == 0
    assert body["saved"] is None
    assert body["profile"]["exists"] is True and body["profile"]["sections"]["하루 시간"] == "20분"


def test_review_get_week_without_missions(client):
    body = _ok(client, "GET", f"/api/review?week={WEEK}")
    assert body["stats"]["missions"] == {"total": 0, "submitted": 0, "skipped": 0, "open": 0, "completion": 0.0}
    assert body["saved"] is None


def test_review_get_without_profile(client, home):
    (home / "profile.md").unlink()
    assert _ok(client, "GET", f"/api/review?week={WEEK}")["profile"] == {"exists": False, "sections": {}}


def test_review_get_defaults_to_current_week(client):
    assert _ok(client, "GET", "/api/review")["week"] == mission._week_range(None)[0]


@pytest.mark.parametrize("week", ["2026-W54", "2026-W00", "2026-43", "2026-W4", "26-W43", "2026-W43x", "..%2Fx", "",
                                  "%D9%A2%D9%A0%D9%A2%D9%A6-W43"])
def test_week_format_is_validated(client, week):
    assert _err(client, "GET", f"/api/review?week={week}") == "bad_request"
    plain = "../x" if week == "..%2Fx" else week
    assert _err(client, "POST", "/api/review/write", {"week": plain}) == "bad_request"
    assert _err(client, "POST", "/api/review/save", {"week": plain, "review": GOOD}) == "bad_request"
    assert _err(client, "POST", "/api/review/apply", {"week": plain, "sections": {"약점": "x"}}) == "bad_request"


def test_week_must_be_string_in_body(client):
    assert _err(client, "POST", "/api/review/write", {"week": 43}) == "bad_request"
    assert _err(client, "POST", "/api/review/write", {}) == "bad_request"


# ---- 회고 쓰기(작업) --------------------------------------------------------------------

def test_write_job_reaches_preview_and_writes_nothing(client, home, fake_ask):
    _put_mission(home, "2026-10-19")
    job = _write(client)
    assert job["status"] == "done" and job["kind"] == "review"
    item = job["items"][0]
    assert item["id"] == WEEK and item["state"] == "awaiting" and item["step"] == "preview" and item["ask"] == "save"
    assert item["preview"] == {"review": mission.validate_review(GOOD), "week": WEEK}
    call = fake_ask.calls[0]
    assert call["payload"]["week"] == WEEK and call["payload"]["missions"]["total"] == 1
    assert call["task"].startswith("yt-mission 규칙 6절대로") and call["schema"]["required"] == ["summary"]
    assert call["rules"][0].parts[-2:] == ("yt-mission", "SKILL.md") and len(fake_ask.calls) == 1
    assert not (home / "reviews").exists()


def test_write_job_retries_once_with_real_retry(client, fake_ask):
    fake_ask.replies = [{"summary": "  "}, dict(GOOD)]
    job = _write(client)
    assert job["status"] == "done" and len(fake_ask.calls) == 2
    assert "summary가 비어 있습니다" in fake_ask.calls[1]["task"]


def test_write_job_fails_after_two_bad_replies_without_files(client, home, fake_ask):
    fake_ask.replies = [{"summary": ""}, {"summary": "", "profile_proposal": {"취미": "x"}}]
    job = _write(client)
    assert job["status"] == "failed" and job["items"][0]["state"] == "failed"
    assert "summary" in job["message"] and len(fake_ask.calls) == 2
    assert not (home / "reviews").exists() and not _tmp(home, "review_*")


def test_write_requires_claude_and_creates_no_job(client, monkeypatch):
    def missing():
        raise ScriptError("claude_not_found", "claude 명령을 찾을 수 없습니다")

    monkeypatch.setattr(claude_cli, "find_claude", missing)
    assert _err(client, "POST", "/api/review/write", {"week": WEEK}) == "claude_not_found"
    assert client.app.jobs.current("review") is None


def test_write_twice_is_job_running(client, fake_ask):
    fake_ask.gate = threading.Event()
    try:
        _ok(client, "POST", "/api/review/write", {"week": WEEK})
        assert _err(client, "POST", "/api/review/write", {"week": WEEK}) == "job_running"
    finally:
        fake_ask.gate.set()


# ---- 저장 -------------------------------------------------------------------------------

def test_save_writes_review_marks_job_saved_and_cleans_tmp(client, home):
    job = _write(client)
    out = _ok(client, "POST", "/api/review/save",
              {"week": WEEK, "review": {**GOOD, "summary": "고친 요약"}, "job_id": job["id"]})
    assert out["status"] == "saved" and out["path"] == f"reviews/{WEEK}.md"
    saved = out["saved"]
    assert saved["fm"]["week"] == WEEK and saved["fm"]["approved"] is False
    assert saved["fm"]["proposal"] == {"약점": "- 프레이밍", "성장 단계": "모방"}
    by_heading = {p["heading"]: p["text"] for p in saved["body"]}
    assert by_heading["요약"] == "고친 요약" and "탑뷰 고정 촬영 3번" in by_heading["다음 주 방향"]
    assert "내 메모" in by_heading and not _tmp(home, "review_*")
    assert client.app.jobs.get(job["id"]).item(0)["state"] == "saved"
    assert _ok(client, "GET", f"/api/review?week={WEEK}")["saved"] == saved


def test_save_ignores_unknown_job_id(client):
    assert _ok(client, "POST", "/api/review/save", {"week": WEEK, "review": GOOD, "job_id": "nope"})["status"] == "saved"
    assert _ok(client, "POST", "/api/review/save", {"week": WEEK, "review": GOOD, "job_id": 7})["status"] == "saved"


def test_save_other_week_does_not_mark_job_saved(client):
    job = _write(client)
    _ok(client, "POST", "/api/review/save", {"week": "2026-W42", "review": GOOD, "job_id": job["id"]})
    assert client.app.jobs.get(job["id"]).item(0)["state"] == "awaiting"


def test_resave_keeps_memo(client, home):
    _ok(client, "POST", "/api/review/save", {"week": WEEK, "review": GOOD})
    path = _review_file(home)
    path.write_bytes(path.read_bytes() + "내 생각\n".encode("utf-8"))
    out = _ok(client, "POST", "/api/review/save", {"week": WEEK, "review": {**GOOD, "summary": "두 번째"}})
    memo = [p for p in out["saved"]["body"] if p["heading"] == "내 메모"][0]
    text = path.read_bytes().decode("utf-8")
    assert memo["text"] == "내 생각" and "두 번째" in text and text.count("## 내 메모") == 1


@pytest.mark.parametrize("review", [None, "text", {}, {"summary": " "}, {**GOOD, "profile_proposal": {"취미": "x"}},
                                    {**GOOD, "next_week": "a"}])
def test_save_rejects_invalid_review(client, home, review):
    assert _err(client, "POST", "/api/review/save", {"week": WEEK, "review": review}) == "invalid_review"
    assert not (home / "reviews").exists() and not _tmp(home, "review_*")


# ---- 프로필 반영 ------------------------------------------------------------------------

def test_apply_only_checked_sections_and_records_approved(client, home):
    _ok(client, "POST", "/api/review/save", {"week": WEEK, "review": GOOD})
    path = _review_file(home)
    path.write_bytes(path.read_bytes() + "메모\n".encode("utf-8"))
    out = _ok(client, "POST", "/api/review/apply", {"week": WEEK, "sections": {"약점": "- 프레이밍(손 위치)"}})
    assert out["sections"] == ["약점"] and out["updated"]
    sections = out["profile"]["sections"]
    assert sections["약점"] == "- 프레이밍(손 위치)" and sections["성장 단계"] == "관찰" and sections["강점"] == ""
    assert sections["관심 주제"] == "- 자취 요리"
    fm, body = refstore.read_md(path)
    assert fm["approved"] == ["약점"] and body.rstrip().endswith("메모")
    _ok(client, "POST", "/api/review/apply", {"week": WEEK, "sections": {"성장 단계": "모방", "약점": "- 다시"}})
    assert refstore.read_md(path)[0]["approved"] == ["약점", "성장 단계"]
    assert not _tmp(home, "profile_*")


def test_apply_keeps_profile_memo_and_other_sections(client, home):
    text = LF_PROFILE.rstrip("\n") + "\n\n## 내 메모\n건드리지 마\n"
    (home / "profile.md").write_bytes(text.encode("utf-8"))
    _ok(client, "POST", "/api/review/apply", {"week": WEEK, "sections": {"성장 단계": "모방"}})
    body = refstore.read_md(home / "profile.md")[1]
    assert "## 성장 단계\n모방" in body and "- 자취 요리" in body and body.endswith("## 내 메모\n건드리지 마\n")


def test_apply_empty_or_malformed_sections(client, home):
    for sections in [{}, None, [], "x", {"약점": 3}]:
        assert _err(client, "POST", "/api/review/apply", {"week": WEEK, "sections": sections}) == "bad_request"
    assert (home / "profile.md").read_text("utf-8") == LF_PROFILE


@pytest.mark.parametrize("sections", [{"취미": "x"}, {"성장 단계": "고수"}, {"내 메모": "x"}])
def test_apply_rejects_bad_section_or_stage(client, home, sections):
    assert _err(client, "POST", "/api/review/apply", {"week": WEEK, "sections": sections}) == "invalid_profile_update"
    assert (home / "profile.md").read_text("utf-8") == LF_PROFILE


def test_apply_without_profile(client, home):
    (home / "profile.md").unlink()
    assert _err(client, "POST", "/api/review/apply", {"week": WEEK, "sections": {"약점": "x"}}) == "no_profile"
    assert not (home / "profile.md").exists() and not _tmp(home, "profile_*")


def test_apply_without_saved_review_still_updates_profile(client, home):
    out = _ok(client, "POST", "/api/review/apply", {"week": WEEK, "sections": {"강점": "- 편집"}})
    assert out["sections"] == ["강점"] and not (home / "reviews").exists()


def test_apply_escapes_heading_lines(client, home):
    _ok(client, "POST", "/api/review/apply", {"week": WEEK, "sections": {"약점": "- a\n## 강점\n가짜"}})
    body = (home / "profile.md").read_text("utf-8").replace("\r\n", "\n")
    assert body.count("\n## 강점") == 1
