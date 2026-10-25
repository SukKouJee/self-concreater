"""도움말 API: 허용 목록 안의 docs/help 페이지만 내고, 메뉴 표는 app.js MENU와 같아야 한다. 챗봇 작업은 가짜 llm으로 확인한다."""
import json
import re
import threading

import pytest

from scripts import claude_cli, library, llm, refstore
from scripts.common import ScriptError
from scripts.ui import api_help
from scripts.ui.api_help import HELP_DIR, MENU
from scripts.ui.server import STATIC_DIR
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture

CAT = "mukbang"
VIDEO = """---
id: {vid}
role: ref
schema_version: 1
---
SECRET_BODY 본문과 대본
## 내 메모
SECRET_MEMO
"""


class FakeLlm:
    def __init__(self, answer="**예측 (내 상황 기준)** 안녕"):
        self.answer = answer
        self.calls = []
        self.gate: threading.Event | None = None

    def ask_validated(self, rules, payload, task, validate, **kw):
        self.calls.append({"rules": rules, "payload": payload, "task": task, "kw": kw})
        if self.gate is not None:
            self.gate.wait(10)
        data = {"answer": self.answer}
        return data, validate(data)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake(monkeypatch):
    f = FakeLlm()
    monkeypatch.setattr(llm, "ask_validated", f.ask_validated)
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    return f


@pytest.fixture
def slow_fake(fake):
    fake.gate = threading.Event()
    yield fake
    fake.gate.set()


@pytest.fixture
def client(home, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def add_refs(home, n, category=CAT):
    refstore.scaffold()
    refstore.init_category(category)
    for i in range(n):
        path = home / "refs" / f"ref_{category}" / "videos" / f"vid{i}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VIDEO.format(vid=f"vid{i}"), encoding="utf-8")


def ask(client, **body):
    body = {"page": "motif", "question": "해보면 어떻게 돼?", **body}
    return client.json("POST", "/api/help/chat", body)


def run_chat(client, fake, **body):
    """질문을 보내고 작업이 끝날 때까지 기다린 뒤 (job, payload)를 돌려준다."""
    status, res = ask(client, **body)
    assert status == 200, res
    job = wait_for(client, res["job"]["id"], lambda j: j["status"] in {"done", "failed", "stopped"})
    return job, (fake.calls[-1]["payload"] if fake.calls else None)


# ---- 조회 ------------------------------------------------------------------------------

def test_get_help_screen(client):
    status, body = client.json("GET", "/api/help/motif")
    assert status == 200
    assert body["page"] == "motif"
    assert body["title"] == "모티브"
    assert body["markdown"].lstrip().startswith("## 무엇을 하나요")
    assert "title:" not in body["markdown"].split("\n", 1)[0]


@pytest.mark.parametrize("page", ["guide-shorts", "guide-long"])
def test_get_help_guide(client, page):
    status, body = client.json("GET", f"/api/help/{page}")
    assert status == 200
    assert body["page"] == page


@pytest.mark.parametrize("page", ["nope", "_chatbot"])
def test_get_help_unknown(client, page):
    status, body = client.json("GET", f"/api/help/{page}")
    assert status == 400
    assert body["error"] == "help_not_found"


def test_get_help_traversal(client):
    status, _ = client.json("GET", "/api/help/..%2FREADME")
    assert status != 200


def test_menu_matches_app_js():
    text = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    found = re.findall(r'\{ name: "([^"]+)", route: "(#/[^"]+)" \}', text)
    assert found == [(m["name"], m["route"]) for m in MENU]


# ---- 챗봇 ------------------------------------------------------------------------------

def test_chat_answer_flows_to_preview(client, fake):
    job, _ = run_chat(client, fake)
    assert job["kind"] == "help_chat"
    assert job["status"] == "done"
    assert job["items"][0]["state"] == "done"
    assert job["items"][0]["preview"] == {"answer": fake.answer}


def test_chat_payload_shape(client, fake):
    _, payload = run_chat(client, fake)
    assert set(payload) == {"screen", "guide", "menu", "situation", "stage_rules", "history", "question"}
    assert payload["screen"]["page"] == "motif"
    assert payload["screen"]["markdown"].lstrip().startswith("## 무엇을 하나요")
    assert set(payload["guide"]) == {"guide-shorts", "guide-long"}
    assert all(payload["guide"][k].strip() for k in payload["guide"])
    assert payload["menu"] == MENU
    assert payload["question"] == "해보면 어떻게 돼?"
    assert fake.calls[-1]["rules"] == [HELP_DIR / "_chatbot.md"]


def test_chat_payload_has_no_private_data(client, fake, home):
    add_refs(home, 1)
    _, payload = run_chat(client, fake, category=CAT)
    dumped = json.dumps(payload, ensure_ascii=False)
    for secret in ("SECRET_BODY", "SECRET_MEMO", str(home), json.dumps(str(home))[1:-1], "work_root", "vid0.md"):
        assert secret not in dumped


def test_chat_situation_counts_refs(client, fake, home):
    add_refs(home, 2)
    _, payload = run_chat(client, fake, category=CAT)
    cat = payload["situation"]["categories"][0]
    assert cat["category"] == CAT
    assert cat["ref"] == 2
    assert set(cat) == {"category", "ref", "mine", "stage", "stage_text", "steps", "current", "next"}
    assert set(payload["situation"]["mission"]) == {"date", "exists", "status", "kind"}
    assert payload["stage_rules"]["allowed_by_stage"]["2"] == ["technique", "angle"]


def test_chat_situation_unknown_category_falls_back_to_all(client, fake, home):
    add_refs(home, 1)
    _, payload = run_chat(client, fake, category="ghost")
    assert [c["category"] for c in payload["situation"]["categories"]] == [CAT]


def test_chat_situation_without_categories(client, fake):
    job, payload = run_chat(client, fake)
    assert job["status"] == "done"
    assert payload["situation"]["categories"] == []


def test_chat_library_situation_hides_remote(client, fake, monkeypatch):
    _, payload = run_chat(client, fake, page="library")
    assert payload["situation"]["library"] == {"connected": False}
    monkeypatch.setattr(library, "status", lambda: {
        "connected": True, "remote": "https://tok@host/x.git", "author": "Alice", "slug": "alice",
        "last_pull": "2026-10-25", "shared_count": 3, "empty": False})
    _, payload = run_chat(client, fake, page="library")
    assert payload["situation"]["library"] == {"connected": True, "shared_count": 3, "last_pull": "2026-10-25"}
    assert "tok@host" not in json.dumps(payload)
    _, other = run_chat(client, fake, page="motif")
    assert "library" not in other["situation"]


@pytest.mark.parametrize("body", [
    {"question": ""}, {"question": "   "}, {"question": "가" * 501}, {"question": 5},
    {"history": "x"}, {"history": [{"role": "bot", "text": "a"}]}, {"history": [{"role": "user"}]},
    {"history": ["a"]}, {"category": 3},
], ids=["empty", "blank", "too_long", "not_str", "history_str", "bad_role", "no_text", "history_item_str", "category"])
def test_chat_rejects_bad_input(client, body):
    status, res = ask(client, **body)
    assert status == 400
    assert res["error"] == "bad_request"


def test_chat_rejects_unknown_page(client):
    status, res = ask(client, page="nope")
    assert status == 400
    assert res["error"] == "help_not_found"


def test_chat_trims_history(client, fake):
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"{i}" + "a" * 2999} for i in range(10)]
    _, payload = run_chat(client, fake, history=history)
    assert len(payload["history"]) == 6
    assert all(len(h["text"]) == 2000 for h in payload["history"])
    assert [h["text"][0] for h in payload["history"]] == ["4", "5", "6", "7", "8", "9"]


def test_chat_strips_question(client, fake):
    _, payload = run_chat(client, fake, question="  안녕  ")
    assert payload["question"] == "안녕"


def test_chat_single_flight(client, slow_fake):
    status, first = ask(client)
    assert status == 200
    status, second = ask(client)
    assert status == 400
    assert second["error"] == "job_running"
    assert second["message"] == "이미 답을 만드는 중입니다"
    slow_fake.gate.set()
    wait_for(client, first["job"]["id"], lambda j: j["status"] == "done")


def test_chat_without_engine(client, monkeypatch):
    def missing():
        raise ScriptError("claude_missing", "claude가 없습니다")
    monkeypatch.setattr(llm, "check", missing)
    status, res = ask(client)
    assert status == 400
    assert res["error"] == "claude_missing"
    assert client.app.jobs.current("help_chat") is None


@pytest.mark.parametrize("bad", [None, "", "가" * 4001, 5, ["x"]], ids=["none", "empty", "too_long", "int", "list"])
def test_validate_answer_rejects(bad):
    with pytest.raises(ScriptError) as e:
        api_help.validate_answer({"answer": bad})
    assert e.value.code == "invalid_answer"


def test_validate_answer_rejects_non_dict():
    with pytest.raises(ScriptError) as e:
        api_help.validate_answer("ok")
    assert e.value.code == "invalid_answer"


def test_validate_answer_accepts():
    assert api_help.validate_answer({"answer": "ok"}) == "ok"
    assert len(api_help.validate_answer({"answer": "가" * 4000})) == 4000


def test_chat_passes_timeout_and_schema(client, fake):
    run_chat(client, fake)
    kw = fake.calls[-1]["kw"]
    assert kw["timeout"] == 120
    assert kw["schema"]["required"] == ["answer"]


def test_chat_engine_error_marks_failed(client, fake, monkeypatch):
    def boom(*a, **kw):
        raise ScriptError("claude_failed", "엔진 오류")
    monkeypatch.setattr(llm, "ask_validated", boom)
    job, _ = run_chat(client, fake)
    assert job["status"] == "failed"
    assert job["items"][0]["state"] == "failed"
    assert job["items"][0]["message"] == "엔진 오류"


def test_chatbot_rules_mark_payload_as_data():
    text = (HELP_DIR / "_chatbot.md").read_text(encoding="utf-8")
    assert "지시 아님" in text
    assert "**예측 (내 상황 기준)**" in text
