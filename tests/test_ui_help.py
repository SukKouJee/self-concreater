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
from tests.helpers.paths import doc_key

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
    assert set(cat) == {"category", "ref", "mine", "stage", "stage_text", "steps", "current", "next", "gates"}
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


# ---- 화면별 실제 문턱(gates) -------------------------------------------------------------

def _typified(home, n_a=8, n_b=3, run=True, typified_at=False, **json_over):
    """ref n_a+n_b개. run이면 유형화 결과 json을 만들고, typified_at이면 index.md에 저장 표시만 한다."""
    from scripts import cluster
    from tests.typify_fixtures import standard_set
    standard_set(home, n_a=n_a, n_b=n_b)
    if run:
        cluster.run(CAT)
        path = home / "workspace" / "typify" / f"{CAT}.json"
        if json_over:
            data = json.loads(path.read_text("utf-8"))
            data.update(json_over)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if typified_at:
        index = home / "refs" / f"ref_{CAT}" / "index.md"
        fm, body = refstore.read(doc_key(index))
        fm["typified_at"] = "2026-10-20T10:00:00"
        index.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))


def _gates(client, fake, page="motif"):
    _, payload = run_chat(client, fake, category=CAT, page=page)
    return payload["situation"]["categories"][0]["gates"]


def test_gates_saved_typify_without_result_allows_only_technique_angle(client, fake, home):
    _typified(home, run=False, typified_at=True)
    gates = _gates(client, fake)
    assert gates["motif_allowed"] == ["기법", "각도"]
    assert gates["typify_result"] is False
    assert gates["diagnose"] == {"available": False, "preliminary": None}


def test_gates_typify_result_without_save_allows_all_types(client, fake, home):
    _typified(home)
    gates = _gates(client, fake)
    assert gates["motif_allowed"] == ["흐름", "기법", "각도", "조합"]
    assert gates["typify_result"] is True


def test_gates_motif_matches_motif_screen(client, fake, home):
    _typified(home, run=False, typified_at=True)
    screen = client.json("GET", f"/api/motif?category={CAT}")[1]
    assert _gates(client, fake)["motif_allowed"] == screen["allowed_ko"]


def test_gates_diagnose_preliminary_follows_typify_result_not_ref_count(client, fake, home):
    _typified(home, status="preliminary")
    gates = _gates(client, fake, page="diagnose")
    assert gates["diagnose"] == {"available": True, "preliminary": True}
    screen = client.json("GET", f"/api/diagnose/targets?category={CAT}")[1]
    assert (screen["has_typify"], screen["preliminary"]) == (True, True)


def test_gates_diagnose_unavailable_without_archetypes(client, fake, home):
    _typified(home, archetypes=[])
    gates = _gates(client, fake, page="diagnose")
    assert gates["diagnose"] == {"available": False, "preliminary": None}
    assert gates["typify_result"] is True


def test_gates_draft_sources_counts(client, fake, home):
    from scripts import motif
    _typified(home)
    cards = [{"type": "technique", "title": "탑뷰", "idea": "위에서", "evidence": ["a00"]},
             {"type": "angle", "title": "각도", "idea": "옆에서", "evidence": ["a01"]}]
    path = home / "workspace" / "tmp" / "c.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    motif.add(CAT, path)
    motif.star(CAT, "M-mukbang-002")
    gates = _gates(client, fake, page="draft")
    screen = client.json("GET", f"/api/draft/sources?category={CAT}")[1]
    assert gates["draft_sources"] == {"stars": 1, "cards": 1, "archetypes": len(screen["archetypes"]),
                                      "library_templates": None}
    assert gates["draft_sources"]["archetypes"] >= 1


def test_gates_has_profile(client, fake, home):
    _typified(home, run=False)
    assert _gates(client, fake)["has_profile"] is False
    (home / "profile.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    assert _gates(client, fake)["has_profile"] is True


def test_situation_has_no_recent_counts(client, fake, home):
    add_refs(home, 1)
    _, payload = run_chat(client, fake, category=CAT)
    assert "recent_counts" not in payload["situation"]


def test_chat_payload_hides_draft_names_and_mission_path(client, fake, home):
    from datetime import date
    add_refs(home, 1)
    drafts = home / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "2026-10-25-SECRET_TITLE.md").write_text(
        f"---\ntitle: SECRET_TITLE\ncategory: {CAT}\n---\nSECRET_BODY\n", encoding="utf-8")
    missions = home / "missions"
    missions.mkdir(parents=True, exist_ok=True)
    (missions / f"{date.today().isoformat()}.md").write_text(
        "---\nstatus: assigned\nkind: hook\n---\nSECRET_MISSION\n", encoding="utf-8")
    _, payload = run_chat(client, fake, category=CAT)
    assert payload["situation"]["mission"]["exists"] is True
    dumped = json.dumps(payload, ensure_ascii=False)
    for secret in ("SECRET_TITLE", "SECRET_BODY", "SECRET_MISSION", "missions/", str(home)):
        assert secret not in dumped, secret
    situation = json.dumps(payload["situation"], ensure_ascii=False)     # help md may mention file names itself
    for path_like in ("drafts/", ".md", json.dumps(str(home))[1:-1]):
        assert path_like not in situation, path_like


def test_chat_checks_engine_before_building_situation(client, monkeypatch):
    def missing():
        raise ScriptError("claude_missing", "claude가 없습니다")
    calls = []
    monkeypatch.setattr(llm, "check", missing)
    monkeypatch.setattr(api_help, "situation", lambda *a: calls.append(a) or {})
    status, res = ask(client)
    assert status == 400 and res["error"] == "claude_missing"
    assert calls == []


@pytest.mark.parametrize("broken", ["{}", "{not json", '{"status": "formal", "archetypes": [{"id": "A1"}]}'],
                         ids=["empty_object", "invalid_json", "missing_axes"])
@pytest.mark.parametrize("page", ["motif", "draft"])
def test_chat_survives_broken_typify_json(client, fake, home, broken, page):
    _typified(home)
    (home / "workspace" / "typify" / f"{CAT}.json").write_text(broken, encoding="utf-8")
    job, payload = run_chat(client, fake, category=CAT, page=page)
    assert job["status"] == "done"
    gates = payload["situation"]["categories"][0]["gates"]
    assert gates["draft_sources"]["archetypes"] == 0
    assert gates["typify_result"] is True                       # the file exists, only its content is broken
    assert gates["diagnose"]["available"] is (broken.startswith('{"status"'))


def test_chat_survives_broken_index_md(client, fake, home):
    _typified(home)
    (home / "refs" / f"ref_{CAT}" / "index.md").write_text("---\ncards: [unclosed\n---\n", encoding="utf-8")
    job, payload = run_chat(client, fake, category=CAT)
    assert job["status"] == "done"
    assert payload["situation"]["categories"][0]["gates"]["draft_sources"] == {"stars": 0, "cards": 0, "archetypes": 0}
