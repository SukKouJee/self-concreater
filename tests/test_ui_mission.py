"""미션 API: 조회, 뽑기(작업) → 미리보기, 저장, 피드백(작업, 파일 임시 복사), 제출, 건너뛰기. claude 호출은 가짜로 바꾼다."""
import json
import threading
from datetime import date as Date

import pytest

from scripts import claude_cli, mission, refstore
from scripts.common import ScriptError
from scripts.ui import api_mission
from tests.test_mission import PROFILE
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture

DAY = "2026-10-22"                                    # 목요일(그 주 월요일은 10-19)
GOOD = {"title": "탑뷰 손 클로즈업 15초 찍기", "kind": "촬영", "minutes": 20, "source": "M-mukbang-001",
        "why": "★카드의 탑뷰 기법을 연습", "steps": ["폰을 탑뷰로 고정", "면 들기 15초 촬영"], "deliverable": "15초 클립 1개"}
MB = 1024 * 1024
FEEDBACK = "탑뷰 각도가 좋았어요. 다음엔 손을 중앙에 두세요."


class FakeAsk:
    """claude_cli.ask를 대신한다(ask_validated의 재시도 경로는 진짜). replies가 있으면 차례로 쓴다."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.replies = []
        self.file_seen = None
        self.gate = None

    def __call__(self, rules, payload, task, **kw):
        self.calls.append({"rules": rules, "payload": payload, "task": task, **kw})
        if self.gate is not None:
            assert self.gate.wait(5)
        if payload.get("file"):
            path = self.root / payload["file"]
            self.file_seen = {"exists": path.exists(), "bytes": path.read_bytes() if path.exists() else None}
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        if reply is not None:
            return reply
        return {"feedback": FEEDBACK} if "mission" in payload else dict(GOOD)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(api_mission, "_server_today", lambda: Date.fromisoformat(DAY))


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    (tmp_path / "profile.md").write_text(PROFILE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake_ask(monkeypatch, home):
    f = FakeAsk(home)
    monkeypatch.setattr(claude_cli, "ask", f)
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    return f


@pytest.fixture
def client(home, fake_ask, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def _put(home, day=DAY, **patch):
    """미션 파일을 직접 만든다(API를 거치지 않는 준비용)."""
    path = home / "workspace" / "tmp" / "seed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(GOOD | patch, ensure_ascii=False), encoding="utf-8")
    mission.save(path, day)


def _get(client, day=DAY):
    status, body = client.json("GET", f"/api/mission?date={day}")
    assert status == 200, body
    return body


def _draft(client, **extra):
    status, resp = client.json("POST", "/api/mission/draft", {"date": DAY, **extra})
    assert status == 200, resp
    return wait_for(client, resp["job"]["id"], lambda j: j["status"] in ("done", "failed"))


def _feedback(client, **extra):
    status, resp = client.json("POST", "/api/mission/feedback",
                               {"date": DAY, "note": "탑뷰로 찍었어요", "difficulty": "right", **extra})
    assert status == 200, resp
    return wait_for(client, resp["job"]["id"], lambda j: j["status"] in ("done", "failed"))


def _tmp(home, pattern):
    tmp = home / "workspace" / "tmp"
    return list(tmp.glob(pattern)) if tmp.exists() else []


def _err(client, method, path, body=None):
    status, resp = client.json(method, path, body)
    assert status == 400, resp
    return resp["error"]


def _record_text(home, day=DAY):
    return (home / "missions" / f"{day}.md").read_bytes().decode("utf-8").replace("\r\n", "\n")


# ---- GET /api/mission ----------------------------------------------------------------

def test_get_without_mission(client):
    body = _get(client)
    assert body["date"] == DAY and body["today"] is None
    assert body["recommended"] == {"minutes": 20, "adjust": "same"} and body["stage"] == "관찰"
    assert body["has_profile"] is True and body["week"] == {"done": 0, "skipped": 0, "total": 0, "rate": 0.0}
    assert len(body["history"]) == 14 and all(h["status"] is None for h in body["history"])


def test_get_with_mission_has_record_and_body(client, home):
    _put(home)
    today = _get(client)["today"]
    assert today["title"] == GOOD["title"] and today["status"] == "open" and today["minutes"] == 20
    assert today["kind"] == "촬영" and today["date"] == DAY
    headings = [p["heading"] for p in today["body"]]
    assert "미션" in headings and "제출" in headings and "피드백" in headings


def test_get_defaults_to_local_today(client):
    assert client.json("GET", "/api/mission")[1]["date"] == DAY


def test_get_history_is_past_14_days_oldest_first_with_gaps(client, home):
    _put(home, "2026-10-21")
    mission.submit("", "hard", "f", "2026-10-21", "했다")
    _put(home, "2026-10-08")
    _put(home, DAY)                                   # 오늘은 history에 넣지 않는다
    history = _get(client)["history"]
    assert history[0]["date"] == "2026-10-08" and history[-1]["date"] == "2026-10-21"
    assert len(history) == 14
    assert history[-1] == {"date": "2026-10-21", "title": GOOD["title"], "kind": "촬영", "status": "submitted",
                           "difficulty": "hard", "skip_reason": None, "minutes": 20}
    assert history[0]["status"] == "open"
    assert history[1] == {"date": "2026-10-09", "status": None}


def test_get_history_survives_broken_record(client, home):
    _put(home, "2026-10-21")
    (home / "missions" / "2026-10-20.md").write_text("---\n: [oops\n---\nx", encoding="utf-8")
    history = _get(client)["history"]
    assert history[-2] == {"date": "2026-10-20", "status": None}
    assert history[-1]["status"] == "open"


def test_get_week_rate_counts_monday_through_date_only(client, home):
    _put(home, "2026-10-18")                          # 지난 주 일요일: 제외
    mission.submit("", "right", "f", "2026-10-18", "x")
    _put(home, "2026-10-19")
    mission.submit("", "right", "f", "2026-10-19", "x")
    _put(home, "2026-10-20")
    mission.skip("야근", "2026-10-20")
    _put(home, "2026-10-21")
    mission.submit("", "easy", "f", "2026-10-21", "x")
    _put(home, DAY)                                   # 진행 중도 분모에 든다
    _put(home, "2026-10-23")                          # 날짜 이후: 제외
    assert _get(client)["week"] == {"done": 2, "skipped": 1, "total": 4, "rate": 0.5}


def test_get_without_profile(client, home):
    (home / "profile.md").unlink()
    body = _get(client)
    assert body["has_profile"] is False and body["stage"] == "관찰" and body["today"] is None


@pytest.mark.parametrize("bad", ["2026-13-01", "20261022", "abc", "2026-10-2", "2026-10-22x"])
def test_get_rejects_bad_date(client, bad):
    assert _err(client, "GET", f"/api/mission?date={bad}") == "bad_request"


# ---- draft ----------------------------------------------------------------------------

def test_draft_runs_claude_and_waits_for_save(client, home, fake_ask):
    job = _draft(client)
    assert job["status"] == "done" and job["kind"] == "mission"
    [item] = job["items"]
    assert item["id"] == DAY and item["state"] == "awaiting" and item["step"] == "preview" and item["ask"] == "save"
    preview = item["preview"]
    assert preview["mission"]["title"] == GOOD["title"] and preview["date"] == DAY and preview["replace"] is False
    assert preview["recommended"] == {"minutes": 20, "adjust": "same"}
    assert not (home / "missions" / f"{DAY}.md").exists()          # 미리보기는 기록을 만들지 않는다
    [call] = fake_ask.calls
    assert call["rules"] == [claude_cli.SKILLS_DIR / "yt-mission" / "SKILL.md"]
    assert call["task"].startswith("yt-mission 규칙 1~4절대로") and "다른 재료" not in call["task"]
    assert call["payload"]["date"] == DAY and "swap_from" not in call["payload"]
    assert call["schema"]["required"] and call["schema"]["properties"]["kind"]["enum"] == mission.KINDS


def test_draft_preview_mission_is_cleaned(client, fake_ask):
    fake_ask.replies = [GOOD | {"title": "  앞뒤  공백 \n 제목 "}]
    assert _draft(client)["items"][0]["preview"]["mission"]["title"] == "앞뒤 공백 제목"


def test_draft_retries_once_then_fails(client, fake_ask):
    fake_ask.replies = [{"title": ""}, {"title": ""}]
    job = _draft(client)
    assert job["status"] == "failed" and "title" in job["message"] and len(fake_ask.calls) == 2
    assert "거절됐습니다" in fake_ask.calls[1]["task"]


def test_draft_retry_can_recover(client, fake_ask):
    fake_ask.replies = [{"title": ""}, dict(GOOD)]
    assert _draft(client)["items"][0]["state"] == "awaiting" and len(fake_ask.calls) == 2


def test_draft_rejects_when_today_exists_before_any_work(client, home, fake_ask):
    _put(home)
    assert _err(client, "POST", "/api/mission/draft", {"date": DAY}) == "mission_exists"
    assert fake_ask.calls == [] and client.app.jobs.current("mission") is None


def test_draft_replace_asks_for_something_different(client, home, fake_ask):
    _put(home)
    job = _draft(client, replace=True)
    assert job["items"][0]["preview"]["replace"] is True
    [call] = fake_ask.calls
    assert call["payload"]["swap_from"] == {"title": GOOD["title"], "source": "M-mukbang-001", "kind": "촬영"}
    assert "오늘 미션과 다른 재료·종류로" in call["task"]


def test_draft_replace_without_existing_mission_is_plain(client, fake_ask):
    job = _draft(client, replace=True)
    assert job["status"] == "done" and "swap_from" not in fake_ask.calls[0]["payload"]


def test_draft_needs_claude_before_job(client, monkeypatch):
    def missing():
        raise ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")
    monkeypatch.setattr(claude_cli, "find_claude", missing)
    assert _err(client, "POST", "/api/mission/draft", {"date": DAY}) == "claude_missing"
    assert client.app.jobs.current("mission") is None


def test_draft_rejects_bad_args(client):
    assert _err(client, "POST", "/api/mission/draft", {"date": "nope"}) == "bad_request"
    assert _err(client, "POST", "/api/mission/draft", {"date": DAY, "replace": "yes"}) == "bad_request"


def test_draft_second_run_while_active_is_job_running(client, fake_ask):
    fake_ask.gate = threading.Event()
    status, first = client.json("POST", "/api/mission/draft", {"date": DAY})
    assert status == 200
    assert _err(client, "POST", "/api/mission/draft", {"date": DAY, "replace": True}) == "job_running"
    fake_ask.gate.set()
    wait_for(client, first["job"]["id"], lambda j: j["status"] == "done")


# ---- save ------------------------------------------------------------------------------

def _save(client, mission_data=None, **extra):
    return client.json("POST", "/api/mission/save", {"date": DAY, "mission": mission_data or dict(GOOD), **extra})


def test_save_writes_record_and_returns_today(client, home):
    status, body = _save(client)
    assert status == 200 and body["status"] == "saved" and body["path"] == f"missions/{DAY}.md"
    assert body["today"]["title"] == GOOD["title"] and body["today"]["body"]
    assert refstore.read_md(home / "missions" / f"{DAY}.md")[0]["status"] == "open"
    assert _tmp(home, "mission_*") == []


def test_save_marks_preview_item_saved(client):
    job = _draft(client)
    status, _ = _save(client, job_id=job["id"])
    assert status == 200
    assert client.json("GET", f"/api/jobs/{job['id']}")[1]["job"]["items"][0]["state"] == "saved"


def test_save_ignores_unknown_or_foreign_job(client):
    assert _save(client, job_id="nope")[0] == 200
    item = {"id": DAY, "url": None, "title": None, "step": "x", "state": "awaiting", "message": None,
            "preview": None, "ask": "save", "frames": []}
    other = client.app.jobs.start("motif", lambda j: j.update(0, state="awaiting"), items=[item], category=None)
    other.join(2)
    client.json("POST", "/api/mission/save", {"date": "2026-10-23", "mission": dict(GOOD), "job_id": other.id})
    assert other.item(0)["state"] == "awaiting"


def test_save_rejects_when_saved_elsewhere_meanwhile_and_keeps_it(client, home):
    job = _draft(client)                              # 미리보기를 띄운다
    _put(home, title="다른 터미널에서 만든 미션")          # 그 사이 다른 곳이 저장
    assert _err(client, "POST", "/api/mission/save", {"date": DAY, "mission": dict(GOOD), "job_id": job["id"]}) == "mission_exists"
    assert "다른 터미널에서 만든 미션" in _record_text(home) and GOOD["title"] not in _record_text(home)
    assert _tmp(home, "mission_*") == []
    assert client.app.jobs.get(job["id"]).item(0)["state"] == "awaiting"      # 저장 실패면 그대로


def test_save_replace_swaps_explicitly(client, home):
    _put(home)
    status, body = _save(client, GOOD | {"title": "새 미션"}, replace=True)
    assert status == 200 and body["swapped_from"] == GOOD["title"] and body["today"]["title"] == "새 미션"


def test_save_invalid_mission_leaves_no_tmp(client, home):
    for bad in (GOOD | {"kind": "운동"}, "text", GOOD | {"steps": "a"}):
        assert _err(client, "POST", "/api/mission/save", {"date": DAY, "mission": bad}) == "invalid_mission"
    assert _tmp(home, "mission_*") == [] and not (home / "missions").exists()


def test_save_rejects_bad_args(client):
    assert _err(client, "POST", "/api/mission/save", {"mission": dict(GOOD)}) == "bad_request"
    assert _err(client, "POST", "/api/mission/save", {"date": DAY, "mission": dict(GOOD), "replace": 1}) == "bad_request"


# ---- feedback ---------------------------------------------------------------------------

def _user_file(home, name="shot.png", data=b"PNG-bytes"):
    path = home.parent / f"{home.name}-outside" / name              # 작업 폴더 밖의 사용자 파일
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_feedback_without_file_uses_no_tools(client, home, fake_ask):
    _put(home)
    job = _feedback(client)
    [item] = job["items"]
    assert job["kind"] == "mission_feedback" and job["status"] == "done"
    assert item["id"] == DAY and item["state"] == "awaiting" and item["ask"] == "save" and item["step"] == "preview"
    assert item["preview"] == {"feedback": FEEDBACK, "note": "탑뷰로 찍었어요", "difficulty": "right", "file_name": None}
    [call] = fake_ask.calls
    assert call["tools"] is None and "file" not in call["payload"]
    assert call["payload"]["mission"]["title"] == GOOD["title"] and call["payload"]["mission"]["body"]
    assert call["payload"]["note"] == "탑뷰로 찍었어요" and call["payload"]["difficulty"] == "right"
    assert call["task"].startswith("yt-mission 규칙 5절대로")
    assert call["rules"] == [claude_cli.SKILLS_DIR / "yt-mission" / "SKILL.md"]
    assert call["schema"]["required"] == ["feedback"]
    assert FEEDBACK not in _record_text(home)                       # 확인 전에는 저장하지 않는다


def test_feedback_with_image_copies_temp_then_deletes_and_keeps_original(client, home, fake_ask):
    _put(home)
    src = _user_file(home, data=b"PNG-original")
    job = _feedback(client, file=str(src))
    [call] = fake_ask.calls
    assert call["tools"] == ["Read"]
    assert call["payload"]["file"].startswith("workspace/tmp/mission_submit_") and call["payload"]["file"].endswith(".png")
    assert fake_ask.file_seen == {"exists": True, "bytes": b"PNG-original"}      # 복사본이 Claude 호출 중 존재
    assert _tmp(home, "mission_submit_*") == []                                   # 끝난 뒤 삭제
    assert src.read_bytes() == b"PNG-original"                                    # 원본 불변
    assert job["items"][0]["preview"]["file_name"] == "shot.png"


def test_feedback_failure_still_removes_temp_copy(client, home, fake_ask):
    _put(home)
    src = _user_file(home, "note.txt", b"hello")
    fake_ask.replies = [ScriptError("claude_failed", "boom")]
    job = _feedback(client, file=str(src))
    assert job["status"] == "failed" and "boom" in job["message"]
    assert _tmp(home, "mission_submit_*") == [] and src.read_bytes() == b"hello"


def test_feedback_empty_text_retries_once(client, home, fake_ask):
    _put(home)
    fake_ask.replies = [{"feedback": "   "}, {"feedback": "좋아요. 다음엔 중앙에."}]
    job = _feedback(client)
    assert job["items"][0]["preview"]["feedback"] == "좋아요. 다음엔 중앙에." and len(fake_ask.calls) == 2


def test_feedback_empty_twice_fails(client, home, fake_ask):
    _put(home)
    fake_ask.replies = [{"feedback": ""}, {"feedback": ""}]
    assert _feedback(client)["status"] == "failed"


@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".md", ".PNG"])
def test_feedback_accepts_allowed_extensions(client, home, ext):
    _put(home)
    assert _feedback(client, file=str(_user_file(home, f"a{ext}")))["status"] == "done"


def test_feedback_accepts_quoted_path(client, home):
    _put(home)
    assert _feedback(client, file=f'"{_user_file(home)}"')["status"] == "done"


def test_feedback_blank_file_means_no_file(client, home, fake_ask):
    _put(home)
    assert _feedback(client, file="  ")["status"] == "done"
    assert fake_ask.calls[0]["tools"] is None


def test_feedback_bad_file_is_rejected_before_any_work(client, home, fake_ask):
    _put(home)
    folder = home / "somedir.png"
    folder.mkdir()
    video = _user_file(home, "clip.mp4")
    big = _user_file(home, "big.png", b"x" * (10 * MB + 1))
    for bad in [str(home / "nope.png"), str(folder), str(video), str(big), 5, ["a.png"]]:
        status, resp = client.json("POST", "/api/mission/feedback",
                                   {"date": DAY, "note": "n", "difficulty": "right", "file": bad})
        assert (status, resp["error"]) == (400, "bad_file"), bad
    assert fake_ask.calls == [] and _tmp(home, "mission_submit_*") == []
    assert client.app.jobs.current("mission_feedback") is None
    assert big.stat().st_size == 10 * MB + 1 and video.exists()


def test_feedback_exactly_10mb_is_allowed(client, home):
    _put(home)
    assert _feedback(client, file=str(_user_file(home, "ok.png", b"x" * (10 * MB))))["status"] == "done"


def test_feedback_requires_mission_and_valid_input(client, home):
    body = {"date": DAY, "note": "n", "difficulty": "right"}
    assert _err(client, "POST", "/api/mission/feedback", body) == "no_mission"
    _put(home)
    assert _err(client, "POST", "/api/mission/feedback", body | {"difficulty": "medium"}) == "bad_request"
    assert _err(client, "POST", "/api/mission/feedback", body | {"note": "  "}) == "bad_request"
    assert _err(client, "POST", "/api/mission/feedback", body | {"note": "x" * 1001}) == "bad_request"
    assert _err(client, "POST", "/api/mission/feedback", body | {"date": "bad"}) == "bad_request"


def test_feedback_needs_claude_before_job(client, home, monkeypatch):
    _put(home)

    def missing():
        raise ScriptError("claude_missing", "없음")
    monkeypatch.setattr(claude_cli, "find_claude", missing)
    assert _err(client, "POST", "/api/mission/feedback", {"date": DAY, "note": "n", "difficulty": "right"}) == "claude_missing"
    assert client.app.jobs.current("mission_feedback") is None


def test_feedback_second_run_while_active_is_job_running(client, home, fake_ask):
    _put(home)
    fake_ask.gate = threading.Event()
    body = {"date": DAY, "note": "n", "difficulty": "right"}
    status, first = client.json("POST", "/api/mission/feedback", body)
    assert status == 200
    assert _err(client, "POST", "/api/mission/feedback", body) == "job_running"
    fake_ask.gate.set()
    wait_for(client, first["job"]["id"], lambda j: j["status"] == "done")


# ---- submit -----------------------------------------------------------------------------

def _submit(client, **extra):
    return client.json("POST", "/api/mission/submit", {
        "date": DAY, "note": "탑뷰로 찍었어요", "difficulty": "right", "feedback": "좋아요. 다음엔 중앙에.", **extra})


def test_submit_records_note_file_name_and_returns_today(client, home):
    _put(home)
    status, body = _submit(client, file_name="shot.png")
    assert status == 200 and body["status"] == "submitted" and body["today"]["status"] == "submitted"
    fm = refstore.read_md(home / "missions" / f"{DAY}.md")[0]
    assert fm["status"] == "submitted" and fm["difficulty"] == "right"
    text = _record_text(home)
    assert "- 한 일: 탑뷰로 찍었어요\n- 파일: shot.png\n- 난이도: right" in text and "좋아요. 다음엔 중앙에." in text


def test_submit_without_file_omits_file_line(client, home):
    _put(home)
    _submit(client)
    assert "- 파일:" not in _record_text(home)


def test_submit_file_name_is_reduced_to_name(client, home):
    _put(home)
    _submit(client, file_name="../../etc/passwd.png")
    text = _record_text(home)
    assert "- 파일: passwd.png" in text and "etc" not in text


def test_submit_allows_resubmit_of_completed_mission(client, home):
    _put(home)
    _submit(client)
    status, body = _submit(client, note="다시 했어요", difficulty="easy", feedback="두 번째 피드백")
    assert status == 200 and body["difficulty"] == "easy"
    text = _record_text(home)
    assert "다시 했어요" in text and "탑뷰로 찍었어요" not in text and "두 번째 피드백" in text


def test_submit_allowed_after_skip(client, home):
    _put(home)
    client.json("POST", "/api/mission/skip", {"date": DAY, "reason": "시간 부족"})
    assert _submit(client)[0] == 200
    assert refstore.read_md(home / "missions" / f"{DAY}.md")[0]["status"] == "submitted"


def test_submit_marks_feedback_item_saved(client, home):
    _put(home)
    job = _feedback(client)
    assert _submit(client, job_id=job["id"])[0] == 200
    assert client.json("GET", f"/api/jobs/{job['id']}")[1]["job"]["items"][0]["state"] == "saved"


def test_submit_applies_to_the_date_the_screen_saw(client, home):
    _put(home, "2026-10-21")                          # 어제 열어 둔 화면: 어제 미션에 적용
    assert client.json("POST", "/api/mission/submit", {
        "date": "2026-10-21", "note": "n", "difficulty": "right", "feedback": "f"})[0] == 200
    assert _err(client, "POST", "/api/mission/submit", {
        "date": DAY, "note": "n", "difficulty": "right", "feedback": "f"}) == "no_mission"


def test_submit_rejects_bad_input(client, home):
    _put(home)
    ok = {"date": DAY, "note": "n", "difficulty": "right", "feedback": "f"}
    for patch in ({"difficulty": "medium"}, {"note": ""}, {"feedback": " "}, {"feedback": 3}, {"date": "x"},
                  {"file_name": 5}, {"file_name": "a" * 300}):
        assert _err(client, "POST", "/api/mission/submit", ok | patch) == "bad_request", patch


# ---- skip -------------------------------------------------------------------------------

def test_skip_records_reason_and_returns_today(client, home):
    _put(home)
    status, body = client.json("POST", "/api/mission/skip", {"date": DAY, "reason": " 시간 부족 "})
    assert status == 200 and body["status"] == "skipped" and body["today"]["status"] == "skipped"
    assert body["today"]["skip_reason"] == "시간 부족"


def test_skip_validates_reason_and_date(client, home):
    _put(home)
    for reason in ("", "   ", "x" * 101, None, 5):
        assert _err(client, "POST", "/api/mission/skip", {"date": DAY, "reason": reason}) == "bad_request", reason
    assert _err(client, "POST", "/api/mission/skip", {"date": "bad", "reason": "a"}) == "bad_request"
    assert client.json("POST", "/api/mission/skip", {"date": DAY, "reason": "x" * 100})[0] == 200


def test_skip_without_mission(client):
    assert _err(client, "POST", "/api/mission/skip", {"date": DAY, "reason": "a"}) == "no_mission"


def test_skip_does_not_overwrite_completed_mission(client, home):
    _put(home)
    _submit(client)
    assert _err(client, "POST", "/api/mission/skip", {"date": DAY, "reason": "a"}) == "already_submitted"
    assert refstore.read_md(home / "missions" / f"{DAY}.md")[0]["status"] == "submitted"


# ---- fix round 1 ---------------------------------------------------------------------

def test_save_replace_refuses_completed_mission(client, home):
    _put(home)
    _submit(client)
    assert _err(client, "POST", "/api/mission/save", {"date": DAY, "mission": dict(GOOD) | {"title": "새"}, "replace": True}) == "already_submitted"
    assert GOOD["title"] in _record_text(home) and _tmp(home, "mission_*") == []


def test_draft_replace_refuses_completed_mission_before_work(client, home, fake_ask):
    _put(home)
    _submit(client)
    assert _err(client, "POST", "/api/mission/draft", {"date": DAY, "replace": True}) == "already_submitted"
    assert fake_ask.calls == [] and client.app.jobs.current("mission") is None


def test_save_replace_allowed_for_skipped_mission(client, home):
    _put(home)
    client.json("POST", "/api/mission/skip", {"date": DAY, "reason": "a"})
    assert _save(client, GOOD | {"title": "새"}, replace=True)[0] == 200


def test_submit_with_heading_in_feedback_keeps_sections(client, home):
    _put(home)
    _submit(client, feedback="좋았다\n## 가짜\n내용")
    _submit(client, feedback="좋았다\n## 가짜\n내용")
    assert [l for l in _record_text(home).split("\n") if l.startswith("## ")] == ["## 미션", "## 제출", "## 피드백"]


def test_skip_reason_is_folded_to_one_line(client, home):
    _put(home)
    status, body = client.json("POST", "/api/mission/skip", {"date": DAY, "reason": "시간\n## 부족"})
    assert status == 200 and body["today"]["skip_reason"] == "시간 ## 부족"


def test_feedback_uppercase_extension_keeps_original_name(client, home):
    _put(home)
    job = _feedback(client, file=str(_user_file(home, "SHOT.PNG")))
    assert job["items"][0]["preview"]["file_name"] == "SHOT.PNG"


def test_feedback_symlink_checks_target_extension(client, home):
    _put(home)
    target = _user_file(home, "secret.pem", b"k")
    link = target.parent / "innocent.png"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not permitted")
    status, resp = client.json("POST", "/api/mission/feedback",
                               {"date": DAY, "note": "n", "difficulty": "right", "file": str(link)})
    assert (status, resp["error"]) == (400, "bad_file")


def test_date_only_server_today_plus_minus_one(client):
    for day in ("2026-10-21", "2026-10-22", "2026-10-23"):
        assert client.json("GET", f"/api/mission?date={day}")[0] == 200
    for day in ("2026-10-20", "2026-10-24"):
        assert _err(client, "GET", f"/api/mission?date={day}") == "bad_request"
    assert _err(client, "POST", "/api/mission/skip", {"date": "2026-10-24", "reason": "a"}) == "bad_request"
