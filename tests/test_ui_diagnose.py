"""진단 API: 대상 목록, 비교, 설명(작업) → 미리보기, 저장, 의도한 차이, 기록. claude 호출은 가짜로 바꾼다."""
import json
import threading
from urllib.parse import quote

import pytest

from scripts import claude_cli, cluster, refstore, similarity
from scripts.common import ScriptError
from tests.test_similarity import LIKE_A1, UNLIKE, _json, make_draft
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture
from tests.typify_fixtures import CAT, standard_set, write_video


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    standard_set(tmp_path)
    cluster.run(CAT)
    cluster.apply(CAT, _json(tmp_path, "n.json", {
        "axes": {a: "근거" for a in ["structure", "hook", "narrative", "title", "pacing"]},
        "archetypes": [{"id": "A1", "name": "결과 먼저 몰아먹기"}, {"id": "A2", "name": "사이즈별 리스트 비교"}],
    }))
    return tmp_path


class FakeAsk:
    """claude_cli.ask를 대신한다(ask_validated의 재시도 경로는 진짜). 응답은 data가 있으면 그것, 없으면 reply()."""

    def __init__(self):
        self.calls = []
        self.data = None

    def reply(self, payload):
        return {"summary": "  구조가   달라요  ",
                "gaps": {g["key"]: f"{g['key']} 설명" for g in payload["gaps"]},
                "actions": ["본론을 늘리기", "훅을 바꾸기"]}

    def __call__(self, rules, payload, task, **kw):
        self.calls.append((rules, payload, task, kw))
        return self.data if self.data is not None else self.reply(payload)


@pytest.fixture
def fake_ask(monkeypatch):
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


def _rel(home, path):
    return path.relative_to(home).as_posix()


def _explain(client, target):
    status, resp = client.json("POST", "/api/diagnose/explain", {"category": CAT, "target": target})
    assert status == 200, resp
    return wait_for(client, resp["job"]["id"], lambda j: j["status"] in ("done", "failed"))


def _notes(client, target):
    result = client.json("POST", "/api/diagnose/compare", {"category": CAT, "target": target})[1]
    return {"summary": "요약", "gaps": {g["key"]: f"{g['key']} 설명" for g in result["gaps"]}, "actions": ["하나"]}


def _save(client, target, notes=None, **extra):
    notes = _notes(client, target) if notes is None else notes
    return client.json("POST", "/api/diagnose/save", {"category": CAT, "target": target, "notes": notes, **extra})


def _tmp_files(home):
    tmp = home / "workspace" / "tmp"
    return list(tmp.glob("diag_notes_*")) if tmp.exists() else []


# ---- targets ------------------------------------------------------------------------

def test_targets_lists_mine_videos_and_drafts(client, home):
    write_video(home, "mine01", role="mine", title="내 영상")
    draft = make_draft(home, LIKE_A1, title="기획안 A")
    body = client.json("GET", f"/api/diagnose/targets?category={CAT}")[1]
    assert body["has_typify"] is True and body["preliminary"] is False
    [video] = body["videos"]
    assert video["name"] == "mine01" and video["title"] == "내 영상" and video["diagnosed"] is None
    assert video["path"] == f"refs/ref_{CAT}/videos/mine01.md" and video["date"]
    [d] = body["drafts"]
    assert d["path"] == _rel(home, draft) and d["title"] == "기획안 A" and d["name"] == draft.stem


def test_targets_excludes_conti_and_other_category(client, home):
    draft = make_draft(home, LIKE_A1)
    (home / "drafts" / f"{draft.stem}-conti.md").write_bytes(draft.read_bytes())
    other = home / "drafts" / "2026-01-01-other.md"
    other.write_bytes(draft.read_bytes().replace(f"category: {CAT}".encode(), b"category: other"))
    (home / "drafts" / "broken.md").write_text("---\n: [oops\n---\nx", encoding="utf-8")
    body = client.json("GET", f"/api/diagnose/targets?category={CAT}")[1]
    assert [d["name"] for d in body["drafts"]] == [draft.stem]


def test_targets_skips_broken_video_md(client, home):
    write_video(home, "mine01", role="mine")
    (home / "refs" / f"ref_{CAT}" / "videos" / "broken.md").write_text("---\n: [oops\n---\nx", encoding="utf-8")
    body = client.json("GET", f"/api/diagnose/targets?category={CAT}")[1]
    assert [v["name"] for v in body["videos"]] == ["mine01"]


def test_targets_marks_diagnosed(client, home):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    rel = _rel(home, draft)
    assert _save(client, rel)[0] == 200
    [d] = client.json("GET", f"/api/diagnose/targets?category={CAT}")[1]["drafts"]
    assert d["diagnosed"]["nearest"]["id"] == "A2" and d["diagnosed"]["date"]
    mine = home / "mine" / f"{draft.stem}.md"                 # 다른 카테고리의 기록은 진단함이 아니다
    mine.write_bytes(mine.read_bytes().replace(f"category: {CAT}".encode(), b"category: other"))
    [d] = client.json("GET", f"/api/diagnose/targets?category={CAT}")[1]["drafts"]
    assert d["diagnosed"] is None


def test_targets_without_typify(client, home):
    (home / "workspace" / "typify" / f"{CAT}.json").unlink()
    body = client.json("GET", f"/api/diagnose/targets?category={CAT}")[1]
    assert body["has_typify"] is False and body["preliminary"] is None
    assert client.json("GET", "/api/diagnose/targets?category=nope")[1]["error"] == "category_not_found"
    assert client.json("GET", "/api/diagnose/targets")[1]["error"] == "bad_request"


# ---- compare ------------------------------------------------------------------------

def test_compare_returns_result_with_korean_axes(client, home):
    draft = make_draft(home, LIKE_A1)
    status, body = client.json("POST", "/api/diagnose/compare", {"category": CAT, "target": _rel(home, draft)})
    assert status == 200 and body["nearest"]["id"] == "A1"
    assert body["axis_ko"] == {"structure": "구조", "hook": "훅", "narrative": "서사", "title": "제목",
                               "pacing": "컷 속도", "shots": "샷"}
    assert {k: v for k, v in body.items() if k != "axis_ko"} == similarity.compare(CAT, draft)


@pytest.mark.parametrize("target", [
    "../outside.md", "profile.md", f"refs/ref_{CAT}/index.md", "drafts/x-conti.md", "drafts/sub/x.md",
    "C:/Windows/win.ini", "refs/ref_other/videos/a.md", "", 5,
])
def test_compare_bad_path(client, home, target):
    (home / "profile.md").write_text("x", encoding="utf-8")
    (home / "drafts").mkdir(exist_ok=True)
    (home / "drafts" / "x-conti.md").write_text("---\nx: 1\n---\n", encoding="utf-8")
    status, body = client.json("POST", "/api/diagnose/compare", {"category": CAT, "target": target})
    assert status == 400 and body["error"] == "bad_path"


def test_compare_ref_video_is_not_mine(client, home):
    body = client.json("POST", "/api/diagnose/compare",
                       {"category": CAT, "target": f"refs/ref_{CAT}/videos/a00.md"})[1]
    assert body["error"] == "not_mine"


def test_compare_without_typify(client, home):
    draft = make_draft(home, LIKE_A1)
    (home / "workspace" / "typify" / f"{CAT}.json").unlink()
    body = client.json("POST", "/api/diagnose/compare", {"category": CAT, "target": _rel(home, draft)})[1]
    assert body["error"] == "no_typify_result"


# ---- explain ------------------------------------------------------------------------

def test_explain_reaches_preview(client, home, fake_ask):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    rel = _rel(home, draft)
    job = _explain(client, rel)
    assert job["status"] == "done" and job["kind"] == "diagnose"
    [item] = job["items"]
    assert item["id"] == rel and item["state"] == "awaiting" and item["step"] == "preview" and item["ask"] == "save"
    result = similarity.compare(CAT, draft)
    assert item["preview"]["result"] == {**result, "axis_ko": item["preview"]["result"]["axis_ko"]}
    assert item["preview"]["notes"]["summary"] == "구조가 달라요"
    assert set(item["preview"]["notes"]["gaps"]) == {g["key"] for g in result["gaps"]}
    [(rules, payload, task, kw)] = fake_ask.calls
    assert rules == [claude_cli.SKILLS_DIR / "yt-diagnose" / "SKILL.md"] and payload == result
    assert task.startswith("yt-diagnose 규칙대로") and kw["schema"]["required"] == ["summary", "gaps", "actions"]
    assert not (home / "mine").exists()                       # 미리보기는 파일을 쓰지 않는다


def test_explain_retries_then_fails(client, home, fake_ask):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    fake_ask.data = {"summary": "", "gaps": {}, "actions": []}
    job = _explain(client, _rel(home, draft))
    assert job["status"] == "failed" and "필요합니다" in job["items"][0]["message"]
    assert len(fake_ask.calls) == 2 and not (home / "mine").exists()


def test_explain_retry_can_recover(client, home, fake_ask):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    replies = [{"summary": "", "gaps": {}, "actions": []}]
    original = fake_ask.reply
    fake_ask.reply = lambda payload: replies.pop(0) if replies else original(payload)
    job = _explain(client, _rel(home, draft))
    assert job["status"] == "done" and len(fake_ask.calls) == 2


def test_explain_checks_before_job(client, home, fake_ask, monkeypatch):
    draft = make_draft(home, LIKE_A1)
    rel = _rel(home, draft)

    def missing():
        raise ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")

    monkeypatch.setattr(claude_cli, "find_claude", missing)
    assert client.json("POST", "/api/diagnose/explain", {"category": CAT, "target": rel})[1]["error"] == "claude_missing"
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "claude")
    (home / "workspace" / "typify" / f"{CAT}.json").unlink()
    status, body = client.json("POST", "/api/diagnose/explain", {"category": CAT, "target": rel})
    assert status == 400 and body["error"] == "no_typify_result"
    assert client.app.jobs.current("diagnose") is None and not fake_ask.calls


def test_explain_bad_path(client, home):
    status, body = client.json("POST", "/api/diagnose/explain", {"category": CAT, "target": "../x.md"})
    assert status == 400 and body["error"] == "bad_path"


def test_second_explain_while_running(client, home, fake_ask, monkeypatch):
    rel = _rel(home, make_draft(home, LIKE_A1))
    gate = threading.Event()

    def slow(*args, **kw):
        gate.wait(5)
        return fake_ask(*args, **kw)

    monkeypatch.setattr(claude_cli, "ask", slow)
    status, body = client.json("POST", "/api/diagnose/explain", {"category": CAT, "target": rel})
    assert status == 200
    status, again = client.json("POST", "/api/diagnose/explain", {"category": CAT, "target": rel})
    gate.set()
    assert status == 400 and again["error"] == "job_running" and "진단 설명" in again["message"]
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")


# ---- save ---------------------------------------------------------------------------

def test_save_writes_record_and_marks_job(client, home):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    rel = _rel(home, draft)
    job = _explain(client, rel)
    notes = job["items"][0]["preview"]["notes"]
    status, body = _save(client, rel, notes, job_id=job["id"])
    assert status == 200, body
    assert body["status"] == "saved" and body["path"] == f"mine/{draft.stem}.md"
    assert body["record"] == client.json("GET", f"/api/diagnose/record?category={CAT}&name={quote(draft.stem)}")[1]
    assert "구조가 달라요" in refstore.read_md(home / body["path"])[1] and not _tmp_files(home)
    item = client.json("GET", f"/api/jobs/{job['id']}")[1]["job"]["items"][0]
    assert item["state"] == "saved" and item["ask"] is None


def test_save_keeps_memo_intentional_and_history(client, home):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    rel = _rel(home, draft)
    _save(client, rel)
    path = home / "mine" / f"{draft.stem}.md"
    path.write_text(path.read_text("utf-8") + "직접 쓴 메모\n", encoding="utf-8", newline="")
    assert client.json("POST", "/api/diagnose/intentional",
                       {"category": CAT, "name": draft.stem, "key": "hook", "on": True})[0] == 200
    status, _ = _save(client, rel)
    fm, body = refstore.read_md(path)
    assert status == 200 and fm["intentional"] == ["hook"] and len(fm["history"]) == 2
    assert body.endswith("## 내 메모\n직접 쓴 메모\n")


def test_save_ignores_vanished_gap_notes_and_rejects_missing(client, home):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    rel = _rel(home, draft)
    notes = _notes(client, rel)
    stale = {**notes, "gaps": {**notes["gaps"], "nonexistent:gap": "사라진 갭 설명"}}
    status, body = _save(client, rel, stale)
    assert status == 200 and "사라진 갭 설명" not in (home / body["path"]).read_text("utf-8")
    fewer = {**notes, "gaps": dict(list(notes["gaps"].items())[1:])}      # 새로 생긴 갭처럼 설명이 모자람
    status, body = _save(client, rel, fewer)
    assert status == 400 and body["error"] == "invalid_notes" and "빠진 갭" in body["message"]
    assert not _tmp_files(home)


@pytest.mark.parametrize("notes", [None, [], "x", {"summary": "s", "gaps": [], "actions": ["a"]}])
def test_save_bad_notes(client, home, notes):
    rel = _rel(home, make_draft(home, LIKE_A1))
    status, body = client.json("POST", "/api/diagnose/save", {"category": CAT, "target": rel, "notes": notes})
    assert status == 400 and body["error"] == "invalid_notes"
    assert not (home / "mine").exists() and not _tmp_files(home)


def test_save_job_of_other_target_not_marked(client, home):
    job = _explain(client, _rel(home, make_draft(home, UNLIKE, hook="shock_line")))
    rel = _rel(home, write_video(home, "mine01", role="mine"))
    assert _save(client, rel, job_id=job["id"])[0] == 200
    assert client.json("GET", f"/api/jobs/{job['id']}")[1]["job"]["items"][0]["state"] == "awaiting"


def test_save_failure_leaves_no_tmp(client, home, monkeypatch):
    rel = _rel(home, make_draft(home, LIKE_A1))
    notes = _notes(client, rel)

    def boom(*args, **kw):
        raise ScriptError("write_failed", "쓰기 실패")

    monkeypatch.setattr(similarity, "save", boom)
    assert _save(client, rel, notes)[0] == 400
    assert not _tmp_files(home)


# ---- intentional · record ------------------------------------------------------------

def test_intentional_toggle_updates_compare(client, home):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    _save(client, _rel(home, draft))
    status, body = client.json("POST", "/api/diagnose/intentional",
                               {"category": CAT, "name": draft.stem, "key": "hook", "on": True})
    assert status == 200 and body["intentional"] == ["hook"]
    assert "hook" not in [g["key"] for g in body["compare"]["gaps"]] and body["compare"]["intentional"] == ["hook"]
    body = client.json("POST", "/api/diagnose/intentional",
                       {"category": CAT, "name": draft.stem, "key": "hook", "on": False})[1]
    assert body["intentional"] == [] and "hook" in [g["key"] for g in body["compare"]["gaps"]]


@pytest.mark.parametrize("over,error", [
    ({}, "diagnosis_not_found"), ({"name": "../x"}, "bad_request"), ({"key": ""}, "bad_request"),
    ({"on": "yes"}, "bad_request"),
])
def test_intentional_errors(client, home, over, error):
    body = {"category": CAT, "name": "nope", "key": "hook", "on": True, **over}
    status, resp = client.json("POST", "/api/diagnose/intentional", body)
    assert status == 400 and resp["error"] == error


def test_record_returns_body_parts(client, home):
    draft = make_draft(home, UNLIKE, hook="shock_line")
    rel = _rel(home, draft)
    _save(client, rel)
    status, body = client.json("GET", f"/api/diagnose/record?category={CAT}&name={quote(draft.stem)}")
    assert status == 200 and body["name"] == draft.stem and body["path"] == f"mine/{draft.stem}.md"
    assert set(body["fm"]) == {"target", "date", "nearest", "proximity", "gaps", "intentional", "preliminary"}
    assert body["fm"]["target"] == rel and body["fm"]["nearest"]["id"] == "A2"
    headings = [p["heading"] for p in body["body"]]
    assert "요약" in headings and "다음 행동" in headings and "내 메모" in headings


def test_record_not_found(client, home):
    body = client.json("GET", f"/api/diagnose/record?category={CAT}&name=nope")[1]
    assert body["error"] == "diagnosis_not_found"


@pytest.mark.parametrize("name", ["../profile", "a/b", "a\\b", "..", ".", "a..b", "x" * 121, ""])
def test_record_bad_name(client, home, name):
    (home / "profile.md").write_text("---\nx: 1\n---\n", encoding="utf-8")
    status, body = client.json("GET", f"/api/diagnose/record?category={CAT}&name={name}")
    assert status == 400 and body["error"] == "bad_request"


def test_targets_preliminary_when_typify_not_formal(client, home):
    path = home / "workspace" / "typify" / f"{CAT}.json"
    data = json.loads(path.read_text("utf-8"))
    path.write_text(json.dumps({**data, "status": "preliminary"}), encoding="utf-8")
    assert client.json("GET", f"/api/diagnose/targets?category={CAT}")[1]["preliminary"] is True
