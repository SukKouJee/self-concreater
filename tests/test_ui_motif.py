"""모티브 API: 상태, 실행(작업) → 미리보기, 저장, ★. claude 호출은 가짜로 바꾼다."""
import threading

import pytest

from scripts import claude_cli, cluster, refstore
from scripts.common import ScriptError
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture
from tests.typify_fixtures import CAT, standard_set, write_video

REAL_ASK_VALIDATED = claude_cli.ask_validated


def card(title, **over):
    return {"type": "technique", "title": title, "idea": f"{title} 아이디어", "how": ["하나", "둘"],
            "basis": "바탕", "evidence": ["a00", "a01"], **over}


CARDS = [card("첫째 카드"), card("둘째 카드"), card("셋째 카드")]


class Fakes:
    def __init__(self):
        self.asks = []
        self.data = {"cards": CARDS}

    def ask_validated(self, rules, payload, task, validate, **kw):
        self.asks.append((rules, payload, task, kw))
        return self.data, validate(self.data)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    standard_set(tmp_path)
    return tmp_path


@pytest.fixture
def home_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    refstore.init_category(CAT)
    return tmp_path


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(claude_cli, "ask_validated", f.ask_validated)
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    return f




@pytest.fixture
def client(home, fakes, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


@pytest.fixture
def client_empty(home_empty, fakes, no_browser):  # noqa: F811
    srv, app, thread = _start(home_empty)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def _index(home):
    return home / "refs" / f"ref_{CAT}" / "index.md"


def _run(client, **body):
    status, resp = client.json("POST", "/api/motif/run", {"category": CAT, **body})
    assert status == 200, resp
    return wait_for(client, resp["job"]["id"], lambda j: j["status"] in ("done", "failed"))


def test_status_flags(client, home):
    status, body = client.json("GET", f"/api/motif?category={CAT}")
    assert status == 200
    assert body["category"] == CAT and body["ref"] == 10 and body["stage"] == 3
    assert body["allowed_types"] == ["technique", "angle"]       # 3단계라도 유형화 전에는 흐름·조합 없음
    assert body["allowed_ko"] == ["기법", "각도"]
    assert body["has_profile"] is False and body["has_typify"] is False
    assert body["cards"] == []

    (home / "profile.md").write_text("---\ncreated: 2026-10-20\n---\n# 프로필\n", encoding="utf-8")
    typify = home / "workspace" / "typify" / f"{CAT}.json"
    typify.parent.mkdir(parents=True, exist_ok=True)
    cluster.run(CAT, out=typify)
    body = client.json("GET", f"/api/motif?category={CAT}")[1]
    assert body["has_profile"] is True and body["has_typify"] is True
    assert body["allowed_types"] == ["flow", "technique", "angle", "combo"]


def test_status_stage_one_and_bad_category(client_empty, home_empty):
    for i in range(3):
        write_video(home_empty, f"v{i:02d}")
    body = client_empty.json("GET", f"/api/motif?category={CAT}")[1]
    assert body["stage"] == 1 and body["allowed_ko"] == ["기법"]
    assert client_empty.json("GET", "/api/motif?category=nope")[1]["error"] == "category_not_found"
    assert client_empty.json("GET", "/api/motif")[1]["error"] == "bad_request"


def test_run_no_refs(client_empty, fakes):
    status, body = client_empty.json("POST", "/api/motif/run", {"category": CAT})
    assert status == 400 and body["error"] == "no_refs" and "레퍼런스" in body["message"]
    assert client_empty.app.jobs.current("motif") is None and not fakes.asks


def test_run_without_claude_makes_no_job(client, monkeypatch):
    def missing():
        raise ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")

    monkeypatch.setattr(claude_cli, "find_claude", missing)
    status, body = client.json("POST", "/api/motif/run", {"category": CAT})
    assert status == 400 and body["error"] == "claude_missing"
    assert client.app.jobs.current("motif") is None


@pytest.mark.parametrize("topic", ["가" * 101, 5])
def test_run_topic_validation(client, fakes, topic):
    status, body = client.json("POST", "/api/motif/run", {"category": CAT, "topic": topic})
    assert status == 400 and body["error"] == "bad_request"
    assert client.app.jobs.current("motif") is None


def test_run_reaches_preview(client, home, fakes):
    before = _index(home).read_bytes()
    job = _run(client, topic="  라면   먹방 ")
    assert job["status"] == "done" and job["kind"] == "motif"
    [item] = job["items"]
    assert item["id"] == CAT and item["state"] == "awaiting" and item["step"] == "preview"
    assert item["ask"] == "save"
    preview = item["preview"]
    assert [c["title"] for c in preview["cards"]] == ["첫째 카드", "둘째 카드", "셋째 카드"]
    assert preview["topic"] == "라면 먹방" and preview["stage"] == 3
    assert preview["allowed_types"] == ["technique", "angle"]
    assert preview["titles"]["a00"] == "라면 5봉지 먹방!" and len(preview["titles"]) == 10
    [(rules, payload, task, kw)] = fakes.asks
    assert rules == [claude_cli.SKILLS_DIR / "yt-inspire" / "SKILL.md"]
    assert payload["topic"] == "라면 먹방" and payload["category"] == CAT
    assert "기법 카탈로그 병합은 하지 마세요" in task
    assert kw["schema"]["required"] == ["cards"]
    assert _index(home).read_bytes() == before


def test_run_retries_then_fails(client, home, monkeypatch):
    calls = []

    def fake_ask(rules, payload, task, **kw):
        calls.append(task)
        return {"cards": [card("나쁜 카드", evidence=["nope"])]}

    monkeypatch.setattr(claude_cli, "ask_validated", REAL_ASK_VALIDATED)
    monkeypatch.setattr(claude_cli, "ask", fake_ask)
    before = _index(home).read_bytes()
    job = _run(client)
    assert job["status"] == "failed" and "nope" in job["items"][0]["message"]
    assert len(calls) == 2
    assert _index(home).read_bytes() == before


def _write_typify(home):
    typify = home / "workspace" / "typify" / f"{CAT}.json"
    typify.parent.mkdir(parents=True, exist_ok=True)
    cluster.run(CAT, out=typify)


FLOW = card("흐름 카드", type="flow")


def test_stage3_without_typify_rejects_flow(client, home, monkeypatch):
    calls = []

    def fake_ask(rules, payload, task, **kw):
        calls.append(payload["allowed_types"])
        return {"cards": [FLOW]}

    monkeypatch.setattr(claude_cli, "ask_validated", REAL_ASK_VALIDATED)
    monkeypatch.setattr(claude_cli, "ask", fake_ask)
    job = _run(client)
    assert job["status"] == "failed" and "flow" in job["items"][0]["message"]
    assert len(calls) == 2 and calls[0] == ["technique", "angle"]
    status = client.json("GET", f"/api/motif?category={CAT}")[1]
    assert status["allowed_types"] == ["technique", "angle"] and status["allowed_ko"] == ["기법", "각도"]


def test_stage3_without_typify_save_rejects_flow(client, home):
    before = _index(home).read_bytes()
    status, body = client.json("POST", "/api/motif/save", {"category": CAT, "cards": [FLOW], "star": []})
    assert status == 400 and body["error"] == "invalid_cards"
    assert _index(home).read_bytes() == before


def test_stage3_with_typify_allows_flow(client, home, fakes):
    _write_typify(home)
    fakes.data = {"cards": [FLOW]}
    job = _run(client)
    assert job["status"] == "done"
    assert job["items"][0]["preview"]["allowed_types"] == ["flow", "technique", "angle", "combo"]
    status = client.json("GET", f"/api/motif?category={CAT}")[1]
    assert status["allowed_types"] == ["flow", "technique", "angle", "combo"]
    assert client.json("POST", "/api/motif/save", {"category": CAT, "cards": [FLOW], "star": []})[0] == 200


def test_save_with_job_id_marks_item_saved(client, home):
    job = _run(client)
    assert job["items"][0]["state"] == "awaiting"
    status, _ = client.json("POST", "/api/motif/save",
                            {"category": CAT, "cards": CARDS[:1], "star": [], "job_id": job["id"]})
    assert status == 200
    item = client.json("GET", f"/api/jobs/{job['id']}")[1]["job"]["items"][0]
    assert item["state"] == "saved" and item["ask"] is None


def test_save_with_unknown_job_id_ignored(client, home):
    status, body = client.json("POST", "/api/motif/save",
                               {"category": CAT, "cards": CARDS[:1], "star": [], "job_id": "nope"})
    assert status == 200 and body["ids"] == ["M-mukbang-001"]


def test_index_writes_do_not_lose_updates(home):
    import json

    from scripts import motif
    errors = []
    barrier = threading.Barrier(2)

    def adder():
        try:
            barrier.wait()
            for i in range(15):
                path = home / "workspace" / f"c{i}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps([card(f"동시 카드 {i}")]), encoding="utf-8")
                motif.add(CAT, path)
        except Exception as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    def stager():
        try:
            barrier.wait()
            for _ in range(15):
                refstore.update_stage(CAT)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=adder), threading.Thread(target=stager)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(motif.card_details(CAT)) == 15
    assert "현재 단계: 3" in refstore.read_md(_index(home))[1]


def test_second_run_while_running(client, fakes, monkeypatch):
    gate = threading.Event()

    def slow(*args, **kw):
        gate.wait(5)
        return fakes.ask_validated(*args, **kw)

    monkeypatch.setattr(claude_cli, "ask_validated", slow)
    status, body = client.json("POST", "/api/motif/run", {"category": CAT})
    assert status == 200
    status, again = client.json("POST", "/api/motif/run", {"category": CAT})
    gate.set()
    assert status == 400 and again["error"] == "job_running" and "카드를 뽑는 중" in again["message"]
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")


def _tmp_files(home):
    tmp = home / "workspace" / "tmp"
    return list(tmp.glob("motif_cards_*")) if tmp.exists() else []


def test_save_selected_and_star(client, home):
    status, body = client.json("POST", "/api/motif/save", {"category": CAT, "cards": CARDS[:2], "star": [1]})
    assert status == 200, body
    assert body["ids"] == ["M-mukbang-001", "M-mukbang-002"] and body["starred"] == ["M-mukbang-002"]
    assert [(c["id"], c["starred"]) for c in body["cards"]] == [("M-mukbang-001", False), ("M-mukbang-002", True)]
    assert "### ★ M-mukbang-002 · 기법 · 둘째 카드" in refstore.read_md(_index(home))[1]
    assert not _tmp_files(home)


@pytest.mark.parametrize("over", [{"cards": []}, {"cards": "x"}, {"star": "1"}, {"star": [5]}, {"star": [True]}])
def test_save_bad_request(client, home, over):
    before = _index(home).read_bytes()
    status, body = client.json("POST", "/api/motif/save", {"category": CAT, "cards": CARDS[:1], "star": [], **over})
    assert status == 400 and body["error"] == "bad_request"
    assert _index(home).read_bytes() == before and not _tmp_files(home)


def test_save_duplicate_title_rejected(client, home):
    one = [card("이미 있는 카드")]
    assert client.json("POST", "/api/motif/save", {"category": CAT, "cards": one, "star": []})[0] == 200
    before = _index(home).read_bytes()
    status, body = client.json("POST", "/api/motif/save", {"category": CAT, "cards": one, "star": []})
    assert status == 400 and body["error"] == "invalid_cards"
    assert _index(home).read_bytes() == before and not _tmp_files(home)


def test_star_toggle(client, home):
    client.json("POST", "/api/motif/save", {"category": CAT, "cards": CARDS[:1], "star": []})
    cid = "M-mukbang-001"
    status, body = client.json("POST", "/api/motif/star", {"category": CAT, "id": cid, "on": True})
    assert status == 200 and body["cards"][0]["starred"] is True
    body = client.json("POST", "/api/motif/star", {"category": CAT, "id": cid, "on": False})[1]
    assert body["cards"][0]["starred"] is False
    assert client.json("POST", "/api/motif/star", {"category": CAT, "id": "bad id", "on": True})[1]["error"] == "bad_request"
    assert client.json("POST", "/api/motif/star", {"category": CAT, "id": cid, "on": "yes"})[1]["error"] == "bad_request"
    err = client.json("POST", "/api/motif/star", {"category": CAT, "id": "M-mukbang-009", "on": True})[1]
    assert err["error"] == "card_not_found"


def test_concurrent_saves_of_same_card(client, home):
    results = []
    barrier = threading.Barrier(2)

    def save():
        barrier.wait()
        results.append(client.json("POST", "/api/motif/save", {"category": CAT, "cards": CARDS[:1], "star": [0]}))

    threads = [threading.Thread(target=save) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    codes = sorted(status for status, _ in results)
    assert codes == [200, 400]
    assert [b["error"] for s, b in results if s == 400] == ["invalid_cards"]
    ids = [c["id"] for c in motif_cards(home)]
    assert ids == ["M-mukbang-001"]


def motif_cards(home):
    from scripts import motif
    return motif.card_details(CAT)


def test_star_failure_after_add_returns_warning(client, home, monkeypatch):
    from scripts import motif

    def boom(*args, **kw):
        raise ScriptError("card_not_found", "카드를 찾을 수 없습니다")

    monkeypatch.setattr(motif, "star", boom)
    status, body = client.json("POST", "/api/motif/save", {"category": CAT, "cards": CARDS[:1], "star": [0]})
    assert status == 200 and body["ids"] == ["M-mukbang-001"] and body["starred"] == []
    assert "★" in body["warning"] and len(body["cards"]) == 1


def test_mark_saved_ignores_other_category_job():
    from types import SimpleNamespace

    from scripts.ui import api_motif
    updates = []
    job = SimpleNamespace(kind="motif", category="other", item=lambda i: {"state": "awaiting"},
                          update=lambda *a, **k: updates.append((a, k)))
    req = SimpleNamespace(app=SimpleNamespace(jobs=SimpleNamespace(get=lambda _id: job)))
    api_motif._mark_saved(req, "j1", CAT)
    assert updates == []
    job.category = CAT
    api_motif._mark_saved(req, "j1", CAT)
    assert updates and updates[0][1]["state"] == "saved"
