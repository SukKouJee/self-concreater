"""기획안 API: 출처, 목록·상세, 실행(작업) → 미리보기, 저장. claude 호출은 가짜로 바꾼다."""
import copy
import json
import threading
from urllib.parse import quote

import pytest

from scripts import claude_cli, llm, conti, motif, refstore
from scripts.common import ScriptError
from tests.test_draft import DRAFT, home  # noqa: F401 - fixture
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture
from tests.typify_fixtures import CAT

REAL_ASK_VALIDATED = llm.ask_validated
CARD_ID = "M-mukbang-001"


class Fakes:
    def __init__(self):
        self.asks = []
        self.data = copy.deepcopy(DRAFT)

    def ask_validated(self, rules, payload, task, validate, **kw):
        self.asks.append((rules, payload, task, kw))
        return self.data, validate(self.data)


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(llm, "ask_validated", f.ask_validated)
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    return f


@pytest.fixture
def client(home, fakes, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def _run(client, **body):
    status, resp = client.json("POST", "/api/draft/run", {"category": CAT, "source": CARD_ID, **body})
    assert status == 200, resp
    return wait_for(client, resp["job"]["id"], lambda j: j["status"] in ("done", "failed"))


def _drafts(home):
    folder = home / "drafts"
    return sorted(folder.glob("*.md")) if folder.exists() else []


def _tmp_files(home):
    tmp = home / "workspace" / "tmp"
    return list(tmp.glob("draft_ui_*")) if tmp.exists() else []


def _save(client, data=None, **extra):
    return client.json("POST", "/api/draft/save",
                       {"category": CAT, "draft": copy.deepcopy(DRAFT if data is None else data), **extra})


# ---- 출처 -------------------------------------------------------------------------------

def test_sources_groups(client, home):
    card = {"type": "angle", "title": "탑뷰 각도", "idea": "위에서 찍는다", "evidence": ["a00"]}
    path = home / "workspace" / "tmp" / "c2.json"
    path.write_text(json.dumps([card], ensure_ascii=False), encoding="utf-8")
    motif.add(CAT, path)
    motif.star(CAT, "M-mukbang-002")
    status, body = client.json("GET", f"/api/draft/sources?category={CAT}")
    assert status == 200
    assert [c["id"] for c in body["stars"]] == ["M-mukbang-002"]
    assert [c["id"] for c in body["cards"]] == [CARD_ID]
    assert set(body["cards"][0]) == {"id", "title", "type", "type_ko", "idea"}
    assert body["cards"][0]["type"] == "flow" and body["cards"][0]["type_ko"] == "흐름"
    assert body["has_typify"] is True
    assert [a["id"] for a in body["archetypes"]] == ["A1", "A2"]
    first = body["archetypes"][0]
    assert first["name"] == "결과 먼저 몰아먹기" and first["size"] == 7
    assert first["skeleton"] and first["mean_duration"] is not None
    assert set(first) == {"id", "name", "size", "skeleton", "mean_duration"}


def test_sources_without_typify(client, home):
    (home / "workspace" / "typify" / f"{CAT}.json").unlink()
    body = client.json("GET", f"/api/draft/sources?category={CAT}")[1]
    assert body["has_typify"] is False and body["archetypes"] == []


def test_category_validation(client):
    for method, path, payload in [("GET", "/api/draft/sources", None), ("GET", "/api/draft/list", None)]:
        assert client.json(method, path, payload)[1]["error"] == "bad_request"
        assert client.json(method, f"{path}?category=nope")[1]["error"] == "category_not_found"
    assert client.json("POST", "/api/draft/run", {"source": CARD_ID})[1]["error"] == "bad_request"
    assert client.json("POST", "/api/draft/save", {"category": "nope", "draft": DRAFT})[1]["error"] \
        == "category_not_found"


# ---- 목록 · 상세 ------------------------------------------------------------------------

def _md(home, name, **fm):
    folder = home / "drafts"
    folder.mkdir(exist_ok=True)
    meta = {"title": name, "category": CAT, "date": "2026-10-01", "status": "draft", **fm}
    (folder / f"{name}.md").write_bytes(
        refstore.join_frontmatter(meta, "# t\n\n## 개요\n- 주제: x\n\n## 내 메모\n메모\n").encode("utf-8"))


def test_list_filters_category_and_skips_conti(client, home):
    _md(home, "2026-10-01-a", date="2026-10-01", source=CARD_ID, archetype="A1", target_sec=60.0,
        sections=[{"label": "hook", "start": 0, "end": 3}])
    _md(home, "2026-10-03-b", date="2026-10-03")
    _md(home, "2026-10-05-other", category="other")
    _md(home, "2026-10-04-c-conti", date="2026-10-04")
    _md(home, "2026-10-02-d", date="2026-10-02")
    (home / "drafts" / "2026-10-02-d-conti.md").write_text("x", encoding="utf-8")
    (home / "drafts" / "2026-10-02-d-conti.html").write_text("x", encoding="utf-8")
    (home / "drafts" / "broken.md").write_text("---\n: [\n---\nbody", encoding="utf-8")
    status, body = client.json("GET", f"/api/draft/list?category={CAT}")
    assert status == 200
    paths = [d["path"] for d in body["drafts"]]
    assert paths == ["drafts/2026-10-03-b.md", "drafts/2026-10-02-d.md", "drafts/2026-10-01-a.md"]
    by = {d["path"]: d for d in body["drafts"]}
    a = by["drafts/2026-10-01-a.md"]
    assert a["source"] == CARD_ID and a["archetype"] == "A1" and a["target_sec"] == 60.0
    assert a["sections"] == [{"label": "hook", "start": 0, "end": 3}] and a["status"] == "draft"
    assert a["conti"] is None and a["html"] is None
    d = by["drafts/2026-10-02-d.md"]
    assert d["conti"] == "drafts/2026-10-02-d-conti.md" and d["html"] == "drafts/2026-10-02-d-conti.html"


def test_list_empty(client):
    assert client.json("GET", f"/api/draft/list?category={CAT}")[1] == {"drafts": []}


def test_detail_body_and_bad_paths(client, home):
    saved = _save(client)[1]["path"]
    status, body = client.json("GET", f"/api/draft?path={quote(saved)}")
    assert status == 200
    assert body["path"] == saved and body["category"] == CAT and body["source"] == CARD_ID
    assert body["hook_type"] == "result_first" and body["narrative_type"] == "journey"
    assert body["target_sec"] == 60.0 and body["pacing"]["avg_cut_sec"] == 2.61
    assert [p["heading"] for p in body["body"]] == ["", "개요", "대본", "아키타입 비교", "준비물", "내 메모"]
    assert body["conti"] is None and body["html"] is None
    assert body["hash"] == conti.content_hash(saved)
    assert body["hook"]["script"] and body["hook"]["end"] > body["hook"]["start"]
    for bad in ["../x.md", "drafts/x-conti.md", "refs/a.md", "drafts/x.txt",
                r"drafts\..\x.md", str(home / "refs" / "a.md")]:
        assert client.json("GET", f"/api/draft?path={bad}")[1]["error"] == "bad_path", bad
    assert client.json("GET", "/api/draft")[1]["error"] == "bad_request"
    assert client.json("GET", "/api/draft?path=drafts/none.md")[1]["error"] == "draft_not_found"


# ---- 실행(작업) -------------------------------------------------------------------------

def test_run_unknown_source(client, fakes):
    status, body = client.json("POST", "/api/draft/run", {"category": CAT, "source": "M-mukbang-099"})
    assert status == 400 and body["error"] == "source_not_found"
    assert client.app.jobs.current("draft") is None and not fakes.asks


def test_run_without_claude_makes_no_job(client, monkeypatch):
    def missing():
        raise ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")

    monkeypatch.setattr(claude_cli, "find_claude", missing)
    status, body = client.json("POST", "/api/draft/run", {"category": CAT, "source": CARD_ID})
    assert status == 400 and body["error"] == "claude_missing"
    assert client.app.jobs.current("draft") is None


@pytest.mark.parametrize("over", [
    {"topic": "가" * 101}, {"topic": 5}, {"target_sec": 4}, {"target_sec": 1801}, {"target_sec": "60"},
    {"target_sec": True}, {"source": ""}, {"source": 5},
])
def test_run_validation(client, fakes, over):
    status, body = client.json("POST", "/api/draft/run", {"category": CAT, "source": CARD_ID, **over})
    assert status == 400 and body["error"] == "bad_request"
    assert client.app.jobs.current("draft") is None and not fakes.asks


def test_run_reaches_preview(client, home, fakes):
    job = _run(client, topic="  편의점   라면 ", target_sec=60)
    assert job["status"] == "done" and job["kind"] == "draft"
    [item] = job["items"]
    assert item["id"] == CAT and item["title"] == "편의점 라면 3단계 맵기"
    assert item["state"] == "awaiting" and item["step"] == "preview" and item["ask"] == "save"
    preview = item["preview"]
    assert preview["draft"]["title"] == DRAFT["title"]
    assert preview["draft"]["sections"][1]["start"] == 3.0
    assert preview["pacing"]["avg_cut_sec"] == 2.61
    assert preview["compare"]["archetype"] == "A1" and preview["compare"]["max_gap"] < 0.1
    assert preview["source"]["id"] == CARD_ID and preview["source"]["kind"] == "motif"
    [(rules, payload, task, kw)] = fakes.asks
    assert rules == [claude_cli.SKILLS_DIR / "yt-draft" / "SKILL.md"]
    assert payload["topic"] == "편의점 라면" and payload["target_sec"] == 60 and payload["category"] == CAT
    assert "target_sec" in task and kw["schema"]["required"] == ["title", "hook_type", "narrative_type", "sections"]
    assert _drafts(home) == []


def test_run_ignores_script_only_flag(client, fakes):
    data = copy.deepcopy(DRAFT)
    data["script_only"] = True
    fakes.data = data
    job = _run(client)
    assert job["status"] == "done"
    assert "script_only" not in job["items"][0]["preview"]["draft"]


def test_run_rejects_script_only_draft_without_shots(client, fakes):
    data = copy.deepcopy(DRAFT)
    data["script_only"] = True
    data["sections"][0].pop("shots")
    fakes.data = data
    assert _run(client)["status"] == "failed"


def test_run_without_target_sec_omits_it(client, fakes):
    data = copy.deepcopy(DRAFT)
    data["archetype"] = None
    fakes.data = data
    job = _run(client)
    assert "target_sec" not in fakes.asks[0][1]
    assert job["items"][0]["preview"]["compare"] is None


def test_run_retries_then_fails(client, home, monkeypatch):
    calls = []
    bad = copy.deepcopy(DRAFT)
    bad["sections"][0]["shots"][0]["size"] = "드론"

    def fake_ask(rules, payload, task, **kw):
        calls.append(task)
        return bad

    monkeypatch.setattr(llm, "ask_validated", REAL_ASK_VALIDATED)
    monkeypatch.setattr(llm, "ask", fake_ask)
    job = _run(client)
    assert job["status"] == "failed" and "드론" in job["items"][0]["message"]
    assert len(calls) == 2 and "드론" in calls[1]
    assert _drafts(home) == []


def test_second_run_while_running(client, fakes, monkeypatch):
    gate = threading.Event()

    def slow(*args, **kw):
        gate.wait(5)
        return fakes.ask_validated(*args, **kw)

    monkeypatch.setattr(llm, "ask_validated", slow)
    status, body = client.json("POST", "/api/draft/run", {"category": CAT, "source": CARD_ID})
    assert status == 200
    status, again = client.json("POST", "/api/draft/run", {"category": CAT, "source": CARD_ID})
    gate.set()
    assert status == 400 and again["error"] == "job_running" and "기획안을 만드는 중" in again["message"]
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")


# ---- 저장 -------------------------------------------------------------------------------

def test_save_writes_and_marks_job(client, home):
    job = _run(client)
    status, body = _save(client, job["items"][0]["preview"]["draft"], job_id=job["id"])
    assert status == 200, body
    assert body["path"].startswith("drafts/") and body["pacing"]["avg_cut_sec"] == 2.61
    assert body["compare"]["archetype"] == "A1"
    item = client.json("GET", f"/api/jobs/{job['id']}")[1]["job"]["items"][0]
    assert item["state"] == "saved" and item["ask"] is None
    first = home / body["path"]
    first_bytes = first.read_bytes()
    second = _save(client)[1]["path"]
    assert second != body["path"] and second.endswith("-2.md")
    assert first.read_bytes() == first_bytes and len(_drafts(home)) == 2
    assert not _tmp_files(home)


def test_save_invalid_keeps_nothing(client, home):
    bad = copy.deepcopy(DRAFT)
    bad["sections"][0]["shots"][0]["sec"] = 0
    status, body = _save(client, bad)
    assert status == 400 and body["error"] == "invalid_draft"
    assert _drafts(home) == [] and not _tmp_files(home)
    status, body = _save(client, "x")
    assert status == 400 and body["error"] == "invalid_draft"


def test_save_removes_tmp_when_draft_save_fails(client, home, monkeypatch):
    from scripts import draft

    def boom(category, path):
        raise ScriptError("invalid_draft", "실패")

    monkeypatch.setattr(draft, "save", boom)
    assert _save(client)[0] == 400
    assert not _tmp_files(home) and _drafts(home) == []


def test_mark_saved_checks_kind_category_and_state():
    from types import SimpleNamespace

    from scripts.ui import api_draft
    updates = []
    job = SimpleNamespace(kind="draft", category="other", item=lambda i: {"state": "awaiting"},
                          update=lambda *a, **k: updates.append((a, k)))
    req = SimpleNamespace(app=SimpleNamespace(jobs=SimpleNamespace(get=lambda _id: job)))
    api_draft._mark_saved(req, "j1", CAT)
    assert updates == []
    job.category, job.kind = CAT, "motif"
    api_draft._mark_saved(req, "j1", CAT)
    assert updates == []
    job.kind, job.item = "draft", (lambda i: {"state": "saved"})
    api_draft._mark_saved(req, "j1", CAT)
    assert updates == []
    job.item = lambda i: {"state": "awaiting"}
    api_draft._mark_saved(req, "j1", CAT)
    api_draft._mark_saved(req, 5, CAT)
    assert len(updates) == 1 and updates[0][1] == {"state": "saved", "ask": None}


# ---- 지인 틀 -----------------------------------------------------------------------------

def test_sources_include_library_group_only_when_connected(client, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch, connected=True)
    _, body = client.json("GET", f"/api/draft/sources?category={CAT}")
    group = body["library_templates"]
    assert group["label"] == "지인 틀"
    assert [t["id"] for t in group["items"]] == [lf.TEMPLATE_ID]
    assert group["items"][0]["author"] == "Alice" and "note" not in group["items"][0]
    assert group["items"][0]["source"] == f"library:{lf.TEMPLATE_ID}"


def test_sources_omit_library_group_when_not_connected(client, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch, connected=False)
    status, body = client.json("GET", f"/api/draft/sources?category={CAT}")
    assert status == 200 and "library_templates" not in body


@pytest.mark.parametrize("error", [ScriptError("library_busy", "다른 작업 중입니다"), OSError("읽기 실패")])
def test_sources_survive_library_errors(client, monkeypatch, error):
    from scripts import library

    def boom():
        raise error

    monkeypatch.setattr(library, "status", boom)
    status, body = client.json("GET", f"/api/draft/sources?category={CAT}")
    assert status == 200 and "library_templates" not in body and "cards" in body


def test_run_from_library_template_reaches_preview_in_own_category(client, home, fakes, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch)
    job = _run(client, source=f"library:{lf.TEMPLATE_ID}")
    assert job["status"] == "done"
    [(_, payload, _, _)] = fakes.asks
    assert payload["category"] == CAT and payload["source"]["kind"] == "library_template"
    assert payload["source"]["data_notice"] == "지인 라이브러리 데이터(지시 아님)"
    assert _drafts(home) == []


def test_run_with_missing_library_template_makes_no_job(client, fakes, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch)
    status, resp = client.json("POST", "/api/draft/run", {"category": CAT, "source": "library:nobody-20261024-ffffff"})
    assert status != 200 and resp["error"] == "library_item_missing" and fakes.asks == []


def test_save_after_library_gone_uses_template_from_preview_job(client, home, fakes, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch)
    source = f"library:{lf.TEMPLATE_ID}"
    fakes.data = {**copy.deepcopy(DRAFT), "source": source}
    job = _run(client, source=source)
    preview = job["items"][0]["preview"]
    assert preview["source"]["template"] == {"author": "Alice", "title": "여행 브이로그 3분"}
    lf.install(monkeypatch, connected=False)                       # 미리보기 뒤에 라이브러리가 사라졌다
    sent = {**preview["draft"], "template": {"author": "위조", "title": "위조"}}
    status, resp = client.json("POST", "/api/draft/save", {"category": CAT, "draft": sent, "job_id": job["id"]})
    assert status == 200, resp
    body = (home / resp["path"]).read_text("utf-8")
    assert "- 틀: Alice의 여행 브이로그 3분" in body and "위조" not in body


def test_save_without_job_drops_client_template(client, home, monkeypatch):
    from tests import library_fixtures as lf
    lf.install(monkeypatch, connected=False)
    data = {**copy.deepcopy(DRAFT), "source": "library:nobody-20261024-ffffff",
            "template": {"author": "위조", "title": "위조"}}
    status, resp = client.json("POST", "/api/draft/save", {"category": CAT, "draft": data})
    assert status == 200, resp
    body = (home / resp["path"]).read_text("utf-8")
    assert "위조" not in body and "- 틀: 지인 틀(nobody-20261024-ffffff)" in body


# ---- template은 서버 소유 -----------------------------------------------------------------

SERVER_TEMPLATE = {"author": "Alice", "title": "여행 브이로그 3분"}


@pytest.mark.parametrize("claude_value", ["지수", None, {"author": "위조", "title": "위조"}, ["x"], "__omit__"])
def test_library_run_ignores_claudes_template_and_uses_the_servers(client, home, fakes, monkeypatch, claude_value):
    from tests import library_fixtures as lf
    lf.install(monkeypatch)
    source = f"library:{lf.TEMPLATE_ID}"
    fakes.data = {**copy.deepcopy(DRAFT), "source": source}
    if claude_value != "__omit__":
        fakes.data["template"] = claude_value
    job = _run(client, source=source)
    assert job["status"] == "done", job
    preview = job["items"][0]["preview"]
    assert preview["draft"]["template"] == SERVER_TEMPLATE
    [(_, payload, _, _)] = fakes.asks
    assert "template" not in payload["source"]                  # Claude는 이 필드를 보지도 쓰지도 않는다
    lf.install(monkeypatch, connected=False)
    status, resp = client.json("POST", "/api/draft/save", {"category": CAT, "draft": preview["draft"], "job_id": job["id"]})
    assert status == 200, resp
    assert "- 틀: Alice의 여행 브이로그 3분" in (home / resp["path"]).read_text("utf-8")


def test_sources_survive_any_library_failure_including_recursion(client, monkeypatch):
    from scripts import library
    monkeypatch.setattr(library, "status", lambda: {"connected": True})
    monkeypatch.setattr(library, "search", lambda **kw: (_ for _ in ()).throw(RecursionError()))
    status, body = client.json("GET", f"/api/draft/sources?category={CAT}")
    assert status == 200 and "library_templates" not in body and "cards" in body


# ---- 촬영함 체크 ------------------------------------------------------------------------

def _filmed(client, path, filmed, hash_, category=CAT):
    return client.json("POST", "/api/drafts/filmed",
                       {"category": category, "path": path, "filmed": filmed, "hash": hash_})


def test_filmed_toggle_round_trip(client, home):
    from datetime import date
    rel = _save(client)[1]["path"]
    path = home / rel
    fm0, body0 = refstore.read_md(path)

    status, on = _filmed(client, rel, True, conti.content_hash(rel))
    assert status == 200, on
    assert on["path"] == rel and on["filmed_at"] == date.today().isoformat()
    assert on["hash"] == conti.content_hash(rel)
    fm1, body1 = refstore.read_md(path)
    assert str(fm1["filmed_at"]) == on["filmed_at"] and body1 == body0
    assert {k: v for k, v in fm1.items() if k != "filmed_at"} == fm0

    status, off = _filmed(client, rel, False, on["hash"])
    assert status == 200, off
    assert off["filmed_at"] is None and off["hash"] == conti.content_hash(rel)
    fm2, body2 = refstore.read_md(path)
    assert "filmed_at" not in fm2 and fm2 == fm0 and body2 == body0
    assert "## 내 메모" in body2


def test_filmed_rejects_stale_hash(client, home):
    rel = _save(client)[1]["path"]
    before = (home / rel).read_bytes()
    status, resp = _filmed(client, rel, True, "0" * 64)
    assert status == 400 and resp["error"] == "draft_changed"
    assert resp["message"] == "화면을 연 뒤 기획안이 바뀌었습니다. 다시 불러와 주세요"
    assert (home / rel).read_bytes() == before


def test_filmed_rejects_bad_paths(client, home):
    rel = _save(client)[1]["path"]
    (home / "drafts" / "x-conti.md").write_text("---\ncategory: " + CAT + "\n---\n", encoding="utf-8")
    (home / "drafts" / "other.md").write_text("---\ncategory: other\ntitle: t\n---\nbody\n", encoding="utf-8")
    profile = home / "profile.md"
    snapshot = {p: p.read_bytes() for p in [home / rel, home / "drafts" / "x-conti.md",
                                            home / "drafts" / "other.md"] + ([profile] if profile.exists() else [])}
    (home / "drafts" / "sub").mkdir()
    (home / "drafts" / "sub" / "x.md").write_text("---\ncategory: " + CAT + "\n---\nbody\n", encoding="utf-8")
    snapshot[home / "drafts" / "sub" / "x.md"] = (home / "drafts" / "sub" / "x.md").read_bytes()
    for bad in ["drafts/../profile.md", "drafts/x-conti.md", "drafts/none.md", "drafts/other.md", "drafts/sub/x.md",
                str(home / rel), str(home / "drafts" / "other.md"), rel.replace("/", "\\")]:
        status, resp = _filmed(client, bad, True, conti.content_hash(rel))
        assert status == 400, (bad, resp)
        if bad in (str(home / rel), "drafts/sub/x.md", rel.replace("/", "\\")):
            assert resp["error"] == "bad_path", (bad, resp)
    assert {p: p.read_bytes() for p in snapshot} == snapshot


def test_filmed_requires_bool(client, home):
    rel = _save(client)[1]["path"]
    before = (home / rel).read_bytes()
    status, resp = _filmed(client, rel, "yes", conti.content_hash(rel))
    assert status == 400 and resp["error"] == "bad_request"
    assert (home / rel).read_bytes() == before
