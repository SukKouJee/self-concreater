"""다듬기 API: 실행(작업) → 미리보기, 저장. claude 호출(ask)만 가짜로 바꿔 실제 재시도 경로를 지난다."""
import copy
import threading

import pytest

from scripts import claude_cli
from scripts.common import ScriptError
from tests.test_conti import draft_md, home  # noqa: F401 - fixtures
from tests.test_polish import body_of, choice, llm_output, sha
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture


class Fakes:
    def __init__(self):
        self.asks = []
        self.outputs = [llm_output()]

    def ask(self, rules, payload, task, **kw):
        self.asks.append((rules, payload, task, kw))
        return copy.deepcopy(self.outputs[min(len(self.asks), len(self.outputs)) - 1])


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(claude_cli, "ask", f.ask)
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    return f


@pytest.fixture
def client(home, fakes, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def _rel(draft_md, home):
    return draft_md.relative_to(home).as_posix()


def _run(client, path):
    status, resp = client.json("POST", "/api/polish/run", {"draft": path})
    assert status == 200, resp
    return wait_for(client, resp["job"]["id"], lambda j: j["status"] in ("done", "failed"))


def _current(client):
    return client.json("GET", "/api/jobs/current?kind=polish")[1].get("job")


def test_run_builds_preview_with_ranked_candidates(client, draft_md, home, fakes):
    path = _rel(draft_md, home)
    before = draft_md.read_bytes()
    job = _run(client, path)
    assert job["status"] == "done" and job["kind"] == "polish" and job["category"]
    item = job["items"][0]
    assert item["id"] == path and item["state"] == "awaiting" and item["step"] == "preview"
    assert item["ask"] == "save" and item["title"]
    preview = item["preview"]
    assert set(preview["context"]) == {"draft", "title", "hook", "hash", "conti_exists", "refs"}
    assert set(preview["context"]["refs"]) == {"ref", "examples"}
    assert preview["context"]["hash"] == sha(draft_md) and preview["context"]["draft"] == path
    result = preview["result"]
    scores = [t["lint"]["score"] for t in result["titles"]]
    assert scores == sorted(scores, reverse=True) and result["hooks"][0]["evidence"]
    assert "score" in result["thumbnail"]["lint"]
    rules, payload, task, _ = fakes.asks[0]
    assert rules[0].parts[-2:] == ("yt-polish", "SKILL.md") and payload["draft"] == path
    assert task.startswith("yt-polish 규칙대로")
    assert draft_md.read_bytes() == before


@pytest.mark.parametrize("bad", ["../x.md", "drafts/x-conti.md", "refs/a.md", "drafts/x.txt", "", None, 3])
def test_run_rejects_bad_path_without_job(client, bad):
    status, body = client.json("POST", "/api/polish/run", {"draft": bad})
    assert status == 400 and body["error"]
    assert _current(client) is None


def test_run_without_claude_makes_no_job(client, draft_md, home, monkeypatch):
    def missing():
        raise ScriptError("claude_not_found", "claude가 없습니다")

    monkeypatch.setattr(claude_cli, "find_claude", missing)
    status, body = client.json("POST", "/api/polish/run", {"draft": _rel(draft_md, home)})
    assert status == 400 and body["error"] == "claude_not_found"
    assert _current(client) is None


def test_invalid_twice_fails_and_keeps_file(client, draft_md, home, fakes):
    fakes.outputs = [{"hooks": [], "titles": [], "thumbnail": {}}]
    before = draft_md.read_bytes()
    job = _run(client, _rel(draft_md, home))
    assert job["status"] == "failed" and len(fakes.asks) == 2
    assert "이전 출력이 거절됐습니다" in fakes.asks[1][2]
    assert job["items"][0]["state"] == "failed"
    assert draft_md.read_bytes() == before


def test_duplicate_run_while_running(client, draft_md, home, fakes, monkeypatch):
    gate = threading.Event()
    original = fakes.ask

    def slow(*a, **kw):
        gate.wait(5)
        return original(*a, **kw)

    monkeypatch.setattr(claude_cli, "ask", slow)
    path = _rel(draft_md, home)
    status, resp = client.json("POST", "/api/polish/run", {"draft": path})
    assert status == 200
    status, body = client.json("POST", "/api/polish/run", {"draft": path})
    assert status == 400 and body["error"] == "job_running" and "다듬는 중" in body["message"]
    gate.set()
    wait_for(client, resp["job"]["id"], lambda j: j["status"] == "done")


def test_save_applies_and_marks_item_saved(client, draft_md, home):
    path = _rel(draft_md, home)
    job = _run(client, path)
    ctx = job["items"][0]["preview"]["context"]
    status, body = client.json("POST", "/api/polish/save", {
        "draft": path, "hash": ctx["hash"], "choice": choice(hook_type="question"), "job_id": job["id"]})
    assert status == 200, body
    assert body["path"] == path and "title" in body["changed"] and body["hash"] == sha(draft_md)
    assert "컵라면 4단계 전부 먹어 봤다" in body_of(draft_md) and "## 패키징" in body_of(draft_md)
    snap = wait_for(client, job["id"], lambda j: j["items"][0]["state"] == "saved")
    assert snap["items"][0]["ask"] is None


def test_save_other_draft_does_not_mark_job(client, draft_md, home):
    job = _run(client, _rel(draft_md, home))
    other = draft_md.with_name("2026-01-01-other.md")
    other.write_bytes(draft_md.read_bytes())
    status, _ = client.json("POST", "/api/polish/save", {
        "draft": _rel(other, home), "hash": sha(other), "choice": choice(), "job_id": job["id"]})
    assert status == 200
    assert _current(client)["items"][0]["state"] == "awaiting"
    status, _ = client.json("POST", "/api/polish/save", {
        "draft": _rel(other, home), "hash": sha(other), "choice": choice(), "job_id": "nope"})
    assert status == 200


def test_save_stale_hash_and_missing_hash(client, draft_md, home):
    path = _rel(draft_md, home)
    stale = sha(draft_md)
    draft_md.write_bytes(draft_md.read_bytes() + "\n수정".encode("utf-8"))
    after = draft_md.read_bytes()
    status, body = client.json("POST", "/api/polish/save", {"draft": path, "hash": stale, "choice": choice()})
    assert status == 400 and body["error"] == "draft_changed" and draft_md.read_bytes() == after
    status, body = client.json("POST", "/api/polish/save", {"draft": path, "choice": choice()})
    assert status == 400 and body["error"] == "bad_request"
    status, _ = client.json("POST", "/api/polish/save", {"draft": "../x.md", "hash": "a", "choice": choice()})
    assert status == 400
