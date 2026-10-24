"""유형화 API: 상태, 실행(작업) → 미리보기, 저장. claude 호출은 가짜로 바꾼다."""
import json
import threading

import pytest

from scripts import claude_cli, cluster, features, refstore
from scripts.common import ScriptError
from tests.test_cluster import NOTES
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture
from tests.typify_fixtures import CAT, standard_set, write_video


class Fakes:
    def __init__(self):
        self.asks = []
        self.notes = NOTES

    def ask_validated(self, rules, payload, task, validate, **kw):
        self.asks.append((rules, payload, task, kw))
        return self.notes, validate(self.notes)


class RetryFakes(Fakes):
    """실제 ask_validated처럼 검증 실패 시 한 번 더 묻고, 또 실패하면 그대로 올린다."""

    def ask_validated(self, rules, payload, task, validate, **kw):
        for _ in range(2):
            self.asks.append((rules, payload, task, kw))
            try:
                return self.notes, validate(self.notes)
            except ScriptError as e:
                last = e
        raise last


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
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


def _index(home):
    return home / "refs" / f"ref_{CAT}" / "index.md"


def _run_preview(client):
    status, body = client.json("POST", "/api/typify/run", {"category": CAT})
    assert status == 200, body
    job = wait_for(client, body["job"]["id"], lambda j: j["status"] in ("done", "failed"))
    return job


def test_status_too_few_and_ready(client, home):
    refstore.init_category(CAT)
    for i in range(4):
        write_video(home, f"v{i:02d}")
    status, body = client.json("GET", f"/api/typify?category={CAT}")
    preliminary, formal = refstore._stage_limits()
    assert status == 200
    assert body["stage"] == "too_few" and body["need"] == preliminary - 4 and body["ref"] == 4
    assert body["typified_at"] is None and body["applied"] is None

    write_video(home, "v04")
    assert client.json("GET", f"/api/typify?category={CAT}")[1]["stage"] == "preliminary"
    for i in range(5, formal):
        write_video(home, f"v{i:02d}")
    body = client.json("GET", f"/api/typify?category={CAT}")[1]
    assert body["stage"] == "formal" and body["need"] == 0 and body["applied"] is None


def test_status_counts_mine_separately(client, home):
    standard_set(home)
    write_video(home, "m01", role="mine")
    body = client.json("GET", f"/api/typify?category={CAT}")[1]
    assert body["ref"] == 10 and body["mine"] == 1


def test_run_too_few_makes_no_job_and_no_claude(client, home, fakes):
    refstore.init_category(CAT)
    write_video(home, "v00")
    status, body = client.json("POST", "/api/typify/run", {"category": CAT})
    assert status == 400 and body["error"] == "too_few" and "1개" in body["message"]
    assert client.app.jobs.current("typify") is None and not fakes.asks


def test_run_without_claude_makes_no_job(client, home, monkeypatch):
    standard_set(home)

    def missing():
        raise ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")

    monkeypatch.setattr(claude_cli, "find_claude", missing)
    status, body = client.json("POST", "/api/typify/run", {"category": CAT})
    assert status == 400 and body["error"] == "claude_missing"
    assert client.app.jobs.current("typify") is None


def test_run_job_reaches_preview(client, home, fakes):
    standard_set(home)
    before = _index(home).read_bytes()
    job = _run_preview(client)
    assert job["status"] == "done" and job["kind"] == "typify"
    [item] = job["items"]
    assert item["id"] == CAT and item["state"] == "awaiting" and item["step"] == "preview" and item["ask"] == "save"
    assert list(item["preview"]["result"]["axes"]) == features.AXES
    assert item["preview"]["notes"] == NOTES
    titles = item["preview"]["titles"]
    ids = {v for a in item["preview"]["result"]["archetypes"] for v in a["members"]}
    ids |= {o["id"] for o in item["preview"]["result"]["outliers"]}
    ids |= {v for res in item["preview"]["result"]["axes"].values() for c in res["clusters"] for v in c["members"]}
    assert ids and ids <= set(titles) and all(isinstance(t, str) for t in titles.values())
    [(rules, payload, task, kw)] = fakes.asks
    assert rules == [claude_cli.SKILLS_DIR / "yt-typify" / "SKILL.md"]
    assert payload["run_at"] == item["preview"]["result"]["run_at"]
    assert "notes JSON" in task and kw["schema"]["required"] == ["axes", "archetypes"]
    assert _index(home).read_bytes() == before            # 미리보기는 파일을 바꾸지 않는다


def test_run_rejects_while_running(client, home, fakes, monkeypatch):
    standard_set(home)
    gate = threading.Event()

    def slow(*args, **kw):
        gate.wait(5)
        return fakes.ask_validated(*args, **kw)

    monkeypatch.setattr(claude_cli, "ask_validated", slow)
    status, body = client.json("POST", "/api/typify/run", {"category": CAT})
    assert status == 200
    status, again = client.json("POST", "/api/typify/run", {"category": CAT})
    gate.set()
    assert status == 400 and again["error"] == "job_running" and "유형화" in again["message"]
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")


def test_run_retries_once_then_fails_on_missing_notes(client, home, monkeypatch):
    standard_set(home)
    f = RetryFakes()
    f.notes = {**NOTES, "axes": {k: v for k, v in NOTES["axes"].items() if k != "hook"}}
    monkeypatch.setattr(claude_cli, "ask_validated", f.ask_validated)
    before = _index(home).read_bytes()
    job = _run_preview(client)
    assert job["status"] == "failed" and "빠졌습니다" in job["items"][0]["message"]
    assert len(f.asks) == 2
    assert _index(home).read_bytes() == before


def _save_body(job, **over):
    result = job["items"][0]["preview"]["result"]
    return {"category": CAT, "run_at": result["run_at"], "notes": NOTES, **over}


def test_save_applies_and_returns_snapshot(client, home):
    standard_set(home)
    job = _run_preview(client)
    edited = {**NOTES, "archetypes": [{"id": "A1", "name": "고친 이름"}, {"id": "A2", "name": "둘째 이름"}]}
    status, body = client.json("POST", "/api/typify/save", _save_body(job, notes=edited))
    assert status == 200, body
    assert body["status"] == "applied" and body["applied"]["archetypes"][0]["name"] == "고친 이름"
    fm, _ = refstore.read_md(_index(home))
    assert [a["name"] for a in fm["archetypes"]] == ["고친 이름", "둘째 이름"]
    got = client.json("GET", f"/api/typify?category={CAT}")[1]
    assert got["applied"] is not None and got["typified_at"] == fm["typified_at"]
    assert not list((home / "workspace" / "tmp").glob("typify_notes_*"))


@pytest.mark.parametrize("bad_notes", [
    {"axes": [], "archetypes": []}, {"axes": {}, "archetypes": ["x"]}, {"axes": {}, "archetypes": [{"name": "a"}]},
    {"axes": "x", "archetypes": []}, {"axes": {}, "archetypes": {"A1": "a"}}, {"archetypes": [None]},
])
def test_save_malformed_notes_is_invalid_notes(client, home, bad_notes):
    standard_set(home)
    job = _run_preview(client)
    status, body = client.json("POST", "/api/typify/save", _save_body(job, notes=bad_notes))
    assert status == 400 and body["error"] == "invalid_notes" and "notes 형식이 맞지 않습니다" in body["message"]
    assert list((home / "workspace" / "tmp").glob("typify_notes*")) == []


def test_save_applied_has_titles(client, home):
    standard_set(home)
    job = _run_preview(client)
    status, body = client.json("POST", "/api/typify/save", _save_body(job))
    assert status == 200, body
    assert body["applied"]["titles"] == job["items"][0]["preview"]["titles"]
    assert not list(cluster.applied_path(CAT).parent.glob("*.tmp"))


def test_save_empty_name_is_invalid_notes(client, home):
    standard_set(home)
    job = _run_preview(client)
    bad = {**NOTES, "archetypes": [{"id": "A1", "name": ""}, {"id": "A2", "name": "b"}]}
    status, body = client.json("POST", "/api/typify/save", _save_body(job, notes=bad))
    assert status == 400 and body["error"] == "invalid_notes"
    assert not list((home / "workspace" / "tmp").glob("typify_notes_*"))


def test_save_stale_run_at(client, home):
    standard_set(home)
    job = _run_preview(client)
    first = _save_body(job)
    cluster.run(CAT, out=cluster.preview_path(CAT))       # 다른 실행이 미리보기를 대체
    status, body = client.json("POST", "/api/typify/save", first)
    assert status == 400 and body["error"] == "typify_changed"


@pytest.mark.parametrize("bad", [
    {"notes": "x"}, {"run_at": 5}, {"run_at": ""},
])
def test_save_bad_request(client, home, bad):
    standard_set(home)
    job = _run_preview(client)
    status, body = client.json("POST", "/api/typify/save", _save_body(job, **bad))
    assert status == 400 and body["error"] == "bad_request"


def test_mine_never_in_result(client, home):
    standard_set(home)
    write_video(home, "mine01", role="mine")
    job = _run_preview(client)
    assert "mine01" not in json.dumps(job["items"][0]["preview"]["result"], ensure_ascii=False)


def test_ui_run_leaves_saved_result_untouched_until_save(client, home):
    standard_set(home)
    job = _run_preview(client)
    assert job["status"] == "done"
    assert not cluster.result_path(CAT).exists()          # 저장 전에는 확정 결과가 없다
    assert cluster.preview_path(CAT).exists()
    assert not cluster.applied_path(CAT).exists()


def test_ui_run_does_not_overwrite_existing_saved_result(client, home):
    standard_set(home)
    cluster.run(CAT)
    saved = cluster.result_path(CAT).read_bytes()
    _run_preview(client)
    assert cluster.result_path(CAT).read_bytes() == saved


def test_save_promotes_preview_to_result(client, home):
    standard_set(home)
    job = _run_preview(client)
    preview = cluster.preview_path(CAT).read_bytes()
    status, body = client.json("POST", "/api/typify/save", _save_body(job))
    assert status == 200, body
    assert cluster.result_path(CAT).read_bytes() == preview
    assert not cluster.preview_path(CAT).exists()


def test_save_without_preview_file_is_typify_changed(client, home):
    standard_set(home)
    job = _run_preview(client)
    cluster.preview_path(CAT).unlink()
    status, body = client.json("POST", "/api/typify/save", _save_body(job))
    assert status == 400 and body["error"] == "typify_changed"


def test_status_overlays_index_names_onto_applied(client, home):
    standard_set(home)
    job = _run_preview(client)
    client.json("POST", "/api/typify/save", _save_body(job))
    index = _index(home)
    index.write_text(index.read_text("utf-8").replace("결과 먼저 몰아먹기", "손으로 고친 이름", 1), encoding="utf-8")
    got = client.json("GET", f"/api/typify?category={CAT}")[1]
    fm, _ = refstore.read_md(index)
    names = {a["id"]: a["name"] for a in got["applied"]["archetypes"]}
    assert names["A1"] == "손으로 고친 이름" == fm["archetypes"][0]["name"]


def test_typify_duplicate_start_is_rejected_by_job_manager(client):
    gate = threading.Event()
    jobs = client.app.jobs
    jobs.start("typify", lambda job: gate.wait(5), items=[])
    try:
        with pytest.raises(ScriptError) as err:
            jobs.start("typify", lambda job: None, items=[])
    finally:
        gate.set()
    assert err.value.code == "job_running" and err.value.message == "이미 유형화가 진행 중입니다"
