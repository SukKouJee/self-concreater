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


def _nohook_draft(home):
    import json

    from scripts import draft as draft_mod
    from tests.test_conti import DRAFT
    from tests.typify_fixtures import CAT
    data = json.loads(json.dumps(DRAFT))
    data["sections"] = data["sections"][1:]
    data["sections"][0]["start"], data["sections"][0]["end"] = 0, 4
    data["sections"][1]["start"], data["sections"][1]["end"] = 4, 6
    path = home / "workspace" / "tmp" / "nohook.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return home / draft_mod.save(CAT, path)["path"]


def test_save_with_job_of_other_kind_or_no_items_still_succeeds(client, draft_md, home):
    path = _rel(draft_md, home)
    job = client.app.jobs.start("typify", lambda j: None, items=None)
    job.join(5)
    status, body = client.json("POST", "/api/polish/save", {
        "draft": path, "hash": sha(draft_md), "choice": choice(), "job_id": job.id})
    assert status == 200, body
    assert "컵라면 4단계 전부 먹어 봤다" in body_of(draft_md)


def test_save_with_job_not_awaiting_keeps_item_state(client, draft_md, home):
    path = _rel(draft_md, home)
    job = _run(client, path)
    status, _ = client.json("POST", "/api/polish/save", {
        "draft": path, "hash": sha(draft_md), "choice": choice(), "job_id": job["id"]})
    assert status == 200
    wait_for(client, job["id"], lambda j: j["items"][0]["state"] == "saved")
    status, _ = client.json("POST", "/api/polish/save", {
        "draft": path, "hash": sha(draft_md), "choice": choice(title="컵라면 5단계 도전"), "job_id": job["id"]})
    assert status == 200
    assert _current(client)["items"][0]["state"] == "saved"


def test_save_invalid_choice_is_400_and_file_unchanged(client, draft_md, home):
    before = draft_md.read_bytes()
    status, body = client.json("POST", "/api/polish/save", {
        "draft": _rel(draft_md, home), "hash": sha(draft_md), "choice": {"title": ""}})
    assert status == 400 and body["error"] == "invalid_polish"
    assert draft_md.read_bytes() == before


def test_run_on_draft_without_hook_section(client, home):
    path = _rel(_nohook_draft(home), home)
    job = _run(client, path)
    assert job["status"] == "done"
    assert job["items"][0]["preview"]["context"]["hook"] is None
    assert job["items"][0]["preview"]["result"]["hooks"][0]["lint"]["fits"] is True


def test_schema_bounds_match_polish_rules():
    from scripts import polish
    from scripts.ui.api_polish import POLISH_SCHEMA
    props = POLISH_SCHEMA["properties"]
    assert (props["hooks"]["minItems"], props["hooks"]["maxItems"]) == polish.HOOK_COUNT
    assert (props["titles"]["minItems"], props["titles"]["maxItems"]) == polish.TITLE_COUNT
    ids = [h["id"] for h in polish._hooks_file()["hooks"]]
    assert props["hooks"]["items"]["properties"]["formula"]["enum"] == ids


def test_lint_scores_edits_and_never_calls_claude(client, draft_md, home, fakes):
    path = _rel(draft_md, home)
    before = draft_md.read_bytes()
    status, body = client.json("POST", "/api/polish/lint", {
        "draft": path, "hook_script": "말: 안녕하세요 3단계", "title": "라면 3단계", "thumb_words": ["라면"]})
    assert status == 200, body
    assert set(body) == {"hook", "title", "thumbnail"} and body["hook"]["opens_with_greeting"] is True
    assert body["thumbnail"]["overlap"] == ["라면"] and body["title"]["has_number"] is True
    status, body = client.json("POST", "/api/polish/lint", {"draft": path, "title": "제목"})
    assert status == 200 and set(body) == {"title"}
    assert fakes.asks == [] and draft_md.read_bytes() == before


@pytest.mark.parametrize("payload", [{"draft": "../x.md", "title": "a"}, {"draft": None}, {"title": "a"}])
def test_lint_rejects_bad_requests(client, draft_md, home, payload):
    status, body = client.json("POST", "/api/polish/lint", payload)
    assert status == 400 and body["error"]


def test_polish_schema_pins_hook_type_and_thumbnail_words():
    from scripts.ui import api_polish
    from scripts.videomd import HOOK_TYPES
    props = api_polish.POLISH_SCHEMA["properties"]
    assert props["hooks"]["items"]["properties"]["hook_type"]["enum"] == list(HOOK_TYPES)
    assert props["thumbnail"]["properties"]["words"]["minItems"] == 1
