"""Task 6: 콘티 API. claude 호출은 가짜로 바꾼다."""
import os
import shutil
from urllib.parse import quote

import pytest

from scripts import claude_cli, llm, conti
from scripts.common import ScriptError
from scripts.ui import api_conti
from tests.test_conti import CONTI, DRAFT, conti_file, draft_md, home, pin_frame  # noqa: F401 - fixtures
from tests.test_cuts import sample_video  # noqa: F401 - fixture
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture


class Fakes:
    def __init__(self):
        self.asks = []
        self.alt_result = {"alternatives": [{"line": "짧다", "intent": "짧게"}]}
        self.alt_error = None

    def ask_validated(self, rules, payload, task, validate, **kw):
        self.asks.append((rules, payload, task, kw))
        return CONTI, validate(CONTI)

    def ask(self, rules, payload, task, **kw):
        self.asks.append((rules, payload, task, kw))
        if self.alt_error:
            raise self.alt_error
        return self.alt_result


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(llm, "ask_validated", f.ask_validated)
    monkeypatch.setattr(llm, "ask", f.ask)
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    return f


def _no_claude(monkeypatch):
    def missing():
        raise ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")
    monkeypatch.setattr(claude_cli, "find_claude", missing)


@pytest.fixture
def client(home, fakes, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def rel(home, path):
    return path.relative_to(home).as_posix()


def saved_conti(home, draft_md):  # noqa: F811
    return home / conti.save(draft_md, conti_file(home))["path"]


def q(value):
    return quote(value, safe="")


def test_list_drafts_with_conti_flag(client, home, draft_md):
    status, body = client.json("GET", "/api/drafts")
    assert status == 200
    [d] = body["drafts"]
    assert d["path"] == rel(home, draft_md) and d["title"] == DRAFT["title"]
    assert d["conti"] is None and d["html"] is None and d["category"]
    saved = saved_conti(home, draft_md)
    drafts = client.json("GET", "/api/drafts")[1]["drafts"]
    [d] = drafts
    assert d["conti"] == rel(home, saved) and d["html"].endswith(".html")


def test_create_job_previews_then_save(client, home, draft_md, fakes):
    draft = rel(home, draft_md)
    status, body = client.json("POST", "/api/conti/create", {"draft": draft, "force": False})
    assert status == 200 and body["job"]["kind"] == "conti"
    job = wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")
    item = job["items"][0]
    assert item["state"] == "awaiting" and item["step"] == "preview" and item["id"] == draft
    assert len(item["preview"]["cuts"]) == 4 and item["preview"]["conti"] == CONTI
    assert "warnings" in item["preview"] and len(item["preview"]["script"]) == 3
    rules, payload, _, _ = fakes.asks[0]
    assert rules == [claude_cli.SKILLS_DIR / "yt-conti" / "SKILL.md"] and payload["draft"] == draft

    status, saved = client.json("POST", "/api/conti/save", {"draft": draft, "conti": CONTI, "force": False})
    assert status == 200, saved
    assert (home / saved["path"]).exists() and len(saved["hash"]) == 64
    assert saved["hash"] == conti.content_hash(saved["path"])


def test_create_rejects_existing_without_force(client, home, draft_md, fakes):
    saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/create", {"draft": rel(home, draft_md)})
    assert status == 400 and body["error"] == "conti_exists" and not fakes.asks


def test_save_existing_requires_force(client, home, draft_md):
    saved_conti(home, draft_md)
    body = {"draft": rel(home, draft_md), "conti": CONTI, "force": False}
    status, resp = client.json("POST", "/api/conti/save", body)
    assert status == 400 and resp["error"] == "conti_exists"
    status, resp = client.json("POST", "/api/conti/save", {**body, "force": True})
    assert status == 200 and not (home / "workspace" / "tmp" / "conti_ui.json").exists()


@pytest.mark.parametrize("bad", ["../secret.md", "refs/x.md", "drafts/a.txt"])
def test_paths_outside_drafts_rejected(client, bad):
    status, resp = client.json("GET", f"/api/conti?path={q(bad)}")
    assert status == 400 and resp["error"] == "bad_path"


def test_get_conti_korean_path(client, home, draft_md):
    src = saved_conti(home, draft_md)
    korean = home / "drafts" / "라면 콘티 test-conti.md"
    shutil.copy(src, korean)
    status, body = client.json("GET", f"/api/conti?path={q(rel(home, korean))}")
    assert status == 200, body
    assert body["hash"] == conti.content_hash(korean) and body["fm"]["title"] == DRAFT["title"]
    assert len(body["cuts"]) == 4 and body["cuts"][0]["sketch"].startswith("<svg")
    c0 = body["cuts"][0]
    assert c0["label"] == "hook" and c0["max_chars"] == int(body["fm"]["speech_cps"] * c0["sec"])
    assert c0["ref"] is None and body["html"].endswith(".html") and isinstance(body["warnings"], list)


def test_get_conti_marks_pinned_ref(client, home, draft_md):
    src = saved_conti(home, draft_md)
    frame = pin_frame(home)
    conti.update_cut(src, 2, {"ref_frame": frame})
    cuts = client.json("GET", f"/api/conti?path={q(rel(home, src))}")[1]["cuts"]
    assert cuts[1]["ref"]["match"] == "pinned" and cuts[1]["ref"]["frame"] == frame
    assert cuts[0]["ref"] is None or cuts[0]["ref"]["match"] != "pinned"


def test_scenes_route(client, home, draft_md):
    src = saved_conti(home, draft_md)
    status, body = client.json("GET", f"/api/conti/scenes?path={q(rel(home, src))}&n=2")
    assert status == 200 and body["scenes"] == conti.scenes(src, 2)
    status, body = client.json("GET", f"/api/conti/scenes?path={q(rel(home, src))}&n=x")
    assert status == 400 and body["error"] == "bad_request"


def test_alternatives_counts_and_over_flag(client, home, draft_md, fakes):
    src = saved_conti(home, draft_md)
    long_line = "아주아주아주 긴 대사" * 3
    fakes.alt_result = {"alternatives": [{"line": "짧다", "intent": "a"}, {"line": long_line, "intent": "b"}]}
    status, body = client.json("POST", "/api/conti/alternatives",
                               {"path": rel(home, src), "n": 2, "count": 3, "exclude": ["이전 안"]})
    assert status == 200, body
    first, second = body["alternatives"]
    cps = conti.cut_context(src, 2)["speech_cps"]
    assert body["max_chars"] == int(cps * 2)
    assert first == {"line": "짧다", "intent": "a", "chars": 2, "over": False}
    assert second["over"] is True and second["chars"] == len(conti._norm(long_line))
    rules, payload, task, kw = fakes.asks[0]
    assert rules == [claude_cli.SKILLS_DIR / "yt-conti" / "SKILL.md"]
    assert payload["exclude"] == ["이전 안"] and payload["count"] == 3
    assert payload["prev_line"] == "4단계 다 먹을 수 있을까?"
    assert task == "7절 규칙으로 컷 대사 대안을 3개 만드세요"
    assert kw["schema"] == api_conti.ALT_SCHEMA and not kw.get("files")


def test_alternatives_claude_missing_returns_400(client, home, draft_md, fakes):
    src = saved_conti(home, draft_md)
    fakes.alt_error = ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")
    status, body = client.json("POST", "/api/conti/alternatives", {"path": rel(home, src), "n": 1})
    assert status == 400 and body["error"] == "claude_missing" and "찾을 수 없습니다" in body["message"]


def test_alternatives_checks_claude_before_calling(client, home, draft_md, fakes, monkeypatch):
    src = saved_conti(home, draft_md)
    _no_claude(monkeypatch)
    status, body = client.json("POST", "/api/conti/alternatives", {"path": rel(home, src), "n": 1})
    assert status == 400 and body["error"] == "claude_missing"
    assert fakes.asks == []


def test_create_checks_claude_before_job(client, home, draft_md, fakes, monkeypatch):
    _no_claude(monkeypatch)
    status, body = client.json("POST", "/api/conti/create", {"draft": rel(home, draft_md)})
    assert status == 400 and body["error"] == "claude_missing"
    assert fakes.asks == []
    status, body = client.json("GET", "/api/jobs/current?kind=conti")
    assert body["job"] is None


def test_alternatives_malformed_answer(client, home, draft_md, fakes):
    src = saved_conti(home, draft_md)
    fakes.alt_result = {"alternatives": [{"intent": "line 없음"}]}
    status, body = client.json("POST", "/api/conti/alternatives", {"path": rel(home, src), "n": 1})
    assert status == 400 and body["error"] == "claude_failed"


def test_cut_update_and_conflict(client, home, draft_md):
    src = saved_conti(home, draft_md)
    old = conti.content_hash(src)
    body = {"path": rel(home, src), "n": 1, "fields": {"guide": "새 가이드"}, "hash": old}
    status, resp = client.json("POST", "/api/conti/cut", body)
    assert status == 200 and resp["hash"] != old and resp["cut"]["guide"] == "새 가이드"
    status, resp = client.json("POST", "/api/conti/cut", body)
    assert status == 400 and resp["error"] == "conti_changed"


@pytest.mark.parametrize("hash_body", [{}, {"hash": ""}, {"hash": None}, {"hash": 5}])
def test_cut_requires_hash(client, home, draft_md, hash_body):
    src = saved_conti(home, draft_md)
    before = src.read_bytes()
    body = {"path": rel(home, src), "n": 1, "fields": {"guide": "새 가이드"}, **hash_body}
    status, resp = client.json("POST", "/api/conti/cut", body)
    assert status == 400 and resp["error"] == "bad_request" and "hash" in resp["message"]
    assert src.read_bytes() == before


def test_srt_export_returns_url_and_downloads(client, home, draft_md):
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/srt", {"path": rel(home, saved)})
    assert status == 200 and body["count"] == 3 and body["track"] == "caption"
    assert body["url"] == "/files/" + quote(body["path"])        # 한글 파일 이름도 그대로 쓸 수 있게 인코딩
    status, headers, raw = client.call("GET", body["url"])
    assert status == 200
    assert raw == (home / body["path"]).read_bytes() and raw.decode("utf-8").startswith("1\r\n00:00:00,000")
    assert "charset=utf-8" in headers["Content-Type"].lower()
    status, body = client.json("POST", "/api/conti/srt", {"path": rel(home, saved), "track": "line"})
    assert status == 200 and body["path"].endswith("-lines.srt")


def test_srt_export_rejects_bad_input(client, home, draft_md):
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/srt", {"path": rel(home, saved), "track": "sfx"})
    assert status == 400 and body["error"] == "bad_request"
    status, body = client.json("POST", "/api/conti/srt", {"path": rel(home, draft_md)})
    assert body["error"] == "bad_path"


# --- srt-fit ------------------------------------------------------------------------------

def test_srt_fit_job_writes_srt_with_preview_url(client, home, draft_md, sample_video):
    saved = saved_conti(home, draft_md)
    before = (sample_video.read_bytes(), sample_video.stat().st_mtime_ns)
    status, body = client.json("POST", "/api/conti/srt-fit", {"path": rel(home, saved), "video": str(sample_video)})
    assert status == 200 and body["job"]["kind"] == "srtfit"
    assert body["job"]["items"][0]["id"] == rel(home, saved)
    job = wait_for(client, body["job"]["id"], lambda j: j["status"] != "running")
    item = job["items"][0]
    assert job["status"] == "done" and item["state"] == "done" and item["ask"] is None
    preview = item["preview"]
    assert preview["url"] == "/files/" + quote(preview["path"]) and preview["retime"]["video"] == "sample.mp4"
    assert (home / preview["path"]).read_bytes().decode("utf-8").startswith("1\r\n00:00:00,000")
    assert (sample_video.read_bytes(), sample_video.stat().st_mtime_ns) == before


def test_srt_fit_rejects_bad_input_without_job(client, home, draft_md, tmp_path):
    saved = saved_conti(home, draft_md)
    for video in (str(tmp_path / "nope.mp4"), "", None, 3):
        status, body = client.json("POST", "/api/conti/srt-fit", {"path": rel(home, saved), "video": video})
        assert status == 400 and body["error"] == "bad_video"
    status, body = client.json("POST", "/api/conti/srt-fit", {"path": rel(home, draft_md), "video": "x"})
    assert status == 400 and body["error"] == "bad_path"
    status, body = client.json("POST", "/api/conti/srt-fit",
                               {"path": rel(home, saved), "video": str(tmp_path), "track": "sfx"})
    assert status == 400 and body["error"] == "bad_request"
    assert client.app.jobs.current("srtfit") is None


def test_srt_fit_rejects_second_run_while_running(client, home, draft_md, sample_video, monkeypatch):
    import threading
    gate = threading.Event()
    real = conti.export_srt

    def slow(*args, **kwargs):
        gate.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(conti, "export_srt", slow)
    saved = saved_conti(home, draft_md)
    req = {"path": rel(home, saved), "video": str(sample_video)}
    status, first = client.json("POST", "/api/conti/srt-fit", req)
    assert status == 200
    status, again = client.json("POST", "/api/conti/srt-fit", req)
    assert status == 400 and again["error"] == "job_running" and "자막 시간" in again["message"]
    gate.set()
    wait_for(client, first["job"]["id"], lambda j: j["status"] == "done")


def test_srt_fit_job_fails_with_message_when_video_cannot_be_read(client, home, draft_md, tmp_path):
    saved = saved_conti(home, draft_md)
    broken = tmp_path / "broken.mp4"
    broken.write_text("not a video", encoding="utf-8")
    status, body = client.json("POST", "/api/conti/srt-fit", {"path": rel(home, saved), "video": str(broken)})
    assert status == 200
    job = wait_for(client, body["job"]["id"], lambda j: j["status"] != "running")
    assert job["status"] == "failed" and job["message"] and job["items"][0]["state"] == "failed"


def test_srt_fit_rejects_non_video_and_empty_files_without_job(client, home, draft_md, tmp_path):
    saved = saved_conti(home, draft_md)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "empty.mp4").write_bytes(b"")
    for name in ("notes.txt", "empty.mp4"):
        status, body = client.json("POST", "/api/conti/srt-fit",
                                   {"path": rel(home, saved), "video": str(tmp_path / name)})
        assert status == 400 and body["error"] == "bad_video"
    assert client.app.jobs.current("srtfit") is None


def test_srt_fit_rejects_conti_without_cuts(client, home, draft_md, sample_video, monkeypatch):
    saved = saved_conti(home, draft_md)
    monkeypatch.setattr(conti, "_load_conti", lambda path: {"cuts": [], "script": []})
    status, body = client.json("POST", "/api/conti/srt-fit", {"path": rel(home, saved), "video": str(sample_video)})
    assert status == 400 and "콘티에 컷이 없습니다" in body["message"]
    assert client.app.jobs.current("srtfit") is None


# --- caption-video ------------------------------------------------------------------------

def _fake_render(monkeypatch, **extra):
    from scripts import captions
    calls = []

    def render(conti_path, video=None, track="caption", progress=None, cancel=None, timeout=900, step=None):
        calls.append({"conti": str(conti_path), "video": video, "track": track})
        if progress:
            progress(2, 4)
        if extra.get("wait"):
            extra["wait"].wait(5)
        if cancel is not None and cancel():
            raise captions.RenderCancelled()
        return {"path": "drafts/x-captions.mp4", "count": 3, "duration_sec": 8.0, "width": 1080, "height": 1920}

    monkeypatch.setattr(captions, "check_setup", lambda: {"ready": True})
    monkeypatch.setattr(captions, "render", render)
    return calls


def test_captions_check_reports_ready_and_missing(client, monkeypatch):
    from scripts import captions
    monkeypatch.setattr(captions, "check_setup", lambda: {"ready": True})
    assert client.json("GET", "/api/captions/check") == (200, {"ready": True})

    def missing():
        raise ScriptError("remotion_missing", captions.MISSING_MESSAGE)

    monkeypatch.setattr(captions, "check_setup", missing)
    status, body = client.json("GET", "/api/captions/check")
    assert status == 200 and body["ready"] is False and "ytc captions setup" in body["message"]


def test_caption_video_job_done_has_preview_url(client, home, draft_md, monkeypatch):
    calls = _fake_render(monkeypatch)
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/caption-video", {"path": rel(home, saved)})
    assert status == 200 and body["job"]["kind"] == "caption_video"
    assert body["job"]["items"][0]["id"] == rel(home, saved) and body["job"]["items"][0]["step"] == "render"
    job = wait_for(client, body["job"]["id"], lambda j: j["status"] != "running")
    item = job["items"][0]
    assert job["status"] == "done" and item["state"] == "done" and item["ask"] is None
    assert item["preview"]["url"] == "/files/drafts/x-captions.mp4" and item["preview"]["count"] == 3
    assert calls == [{"conti": str(saved), "video": None, "track": "caption"}]


def test_caption_video_progress_message_and_video_passed(client, home, draft_md, sample_video, monkeypatch):
    import threading
    gate = threading.Event()
    calls = _fake_render(monkeypatch, wait=gate)
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/caption-video",
                               {"path": rel(home, saved), "video": str(sample_video)})
    assert status == 200
    job = wait_for(client, body["job"]["id"], lambda j: j["items"][0]["message"] == "2/4 프레임")
    assert job["status"] == "running" and job["items"][0]["state"] == "running"
    gate.set()
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")
    assert calls[0]["video"] == sample_video


def test_caption_video_rejects_before_creating_job(client, home, draft_md, tmp_path, monkeypatch):
    from scripts import captions
    saved = saved_conti(home, draft_md)
    req = {"path": rel(home, saved)}

    def missing():
        raise ScriptError("remotion_missing", captions.MISSING_MESSAGE)

    monkeypatch.setattr(captions, "check_setup", missing)
    status, body = client.json("POST", "/api/conti/caption-video", req)
    assert status == 400 and body["error"] == "remotion_missing"

    _fake_render(monkeypatch)
    status, body = client.json("POST", "/api/conti/caption-video", {**req, "video": str(tmp_path / "nope.mp4")})
    assert status == 400 and body["error"] == "bad_video"
    status, body = client.json("POST", "/api/conti/caption-video", {**req, "track": "sfx"})
    assert status == 400 and body["error"] == "bad_request"
    status, body = client.json("POST", "/api/conti/caption-video", {"path": rel(home, draft_md)})
    assert status == 400 and body["error"] == "bad_path"
    assert client.app.jobs.current("caption_video") is None


def test_caption_video_rejects_conti_without_captions(client, home, draft_md, monkeypatch):
    from tests.test_conti import with_cuts
    _fake_render(monkeypatch)
    data = with_cuts(c0={"caption": ""}, c2={"caption": ""}, c3={"caption": ""})
    path = home / conti.save(draft_md, conti_file(home, data))["path"]
    status, body = client.json("POST", "/api/conti/caption-video", {"path": rel(home, path)})
    assert status == 400 and body["error"] == "no_captions" and body["message"] == "내보낼 자막이 없습니다"
    assert client.app.jobs.current("caption_video") is None


def test_caption_video_rejects_second_run_while_running(client, home, draft_md, monkeypatch):
    import threading
    gate = threading.Event()
    _fake_render(monkeypatch, wait=gate)
    saved = saved_conti(home, draft_md)
    req = {"path": rel(home, saved)}
    status, first = client.json("POST", "/api/conti/caption-video", req)
    assert status == 200
    status, again = client.json("POST", "/api/conti/caption-video", req)
    assert status == 400 and again["error"] == "job_running" and "자막 영상" in again["message"]
    gate.set()
    wait_for(client, first["job"]["id"], lambda j: j["status"] == "done")


def test_caption_video_cancel_reaches_render(client, home, draft_md, monkeypatch):
    import threading
    gate = threading.Event()
    _fake_render(monkeypatch, wait=gate)
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/caption-video", {"path": rel(home, saved)})
    job = client.app.jobs.get(body["job"]["id"])
    wait_for(client, job.id, lambda j: j["items"][0]["message"] == "2/4 프레임")
    job.cancel()
    gate.set()
    final = wait_for(client, job.id, lambda j: j["status"] != "running")
    assert final["status"] == "stopped" and final["items"][0]["state"] == "skipped"


def test_cancel_endpoint_stops_running_job_and_rejects_finished_or_unknown(client, home, draft_md, monkeypatch):
    import threading
    gate = threading.Event()
    _fake_render(monkeypatch, wait=gate)
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/caption-video", {"path": rel(home, saved)})
    job_id = body["job"]["id"]
    wait_for(client, job_id, lambda j: j["items"][0]["message"] == "2/4 프레임")
    status, resp = client.json("POST", f"/api/jobs/{job_id}/cancel")
    assert status == 200 and resp["job"]["id"] == job_id
    gate.set()
    final = wait_for(client, job_id, lambda j: j["status"] != "running")
    assert final["status"] == "stopped" and final["items"][0]["state"] == "skipped"
    status, resp = client.json("POST", f"/api/jobs/{job_id}/cancel")
    assert status == 400 and resp["error"] == "not_running"
    status, resp = client.json("POST", "/api/jobs/nope/cancel")
    assert resp["error"] == "job_not_found" and status in (400, 404)


def test_caption_video_detect_step_is_reported(client, home, draft_md, sample_video, monkeypatch):
    import threading
    from scripts import captions
    gate = threading.Event()
    monkeypatch.setattr(captions, "check_setup", lambda: {"ready": True})

    def render(conti_path, video=None, track="caption", progress=None, cancel=None, timeout=900, step=None):
        step("detect")
        gate.wait(5)
        return {"path": "drafts/x-captions.mp4", "count": 1, "duration_sec": 1.0, "width": 1, "height": 1}

    monkeypatch.setattr(captions, "render", render)
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/caption-video",
                               {"path": rel(home, saved), "video": str(sample_video)})
    wait_for(client, body["job"]["id"], lambda j: j["items"][0]["step"] == "detect")
    gate.set()
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")


def test_caption_video_render_failure_fails_job_with_message(client, home, draft_md, monkeypatch):
    from scripts import captions
    monkeypatch.setattr(captions, "check_setup", lambda: {"ready": True})

    def boom(*a, **k):
        raise ScriptError("render_failed", "자막 영상을 만들지 못했습니다: boom")

    monkeypatch.setattr(captions, "render", boom)
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/caption-video", {"path": rel(home, saved)})
    job = wait_for(client, body["job"]["id"], lambda j: j["status"] != "running")
    assert job["status"] == "failed" and "boom" in job["message"] and job["items"][0]["state"] == "failed"


def test_trash_route_moves_files_and_list_updates(client, home, draft_md):
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/trash", {"path": rel(home, saved)})
    assert status == 200, body
    assert body["trash"].startswith("workspace/trash/") and len(body["moved"]) == 3   # md, html, 이력 폴더
    assert not saved.exists() and draft_md.exists()
    [d] = client.json("GET", "/api/drafts")[1]["drafts"]
    assert d["conti"] is None and d["html"] is None


@pytest.mark.parametrize("bad", ["../secret.md", "drafts/a.txt"])
def test_trash_route_rejects_bad_paths(client, home, draft_md, bad):
    status, resp = client.json("POST", "/api/conti/trash", {"path": bad})
    assert status == 400 and resp["error"] == "bad_path"


def test_trash_route_refuses_a_draft(client, home, draft_md):
    status, resp = client.json("POST", "/api/conti/trash", {"path": rel(home, draft_md)})
    assert status == 400 and resp["error"] == "bad_path" and draft_md.exists()


def test_trash_route_refuses_while_a_job_uses_the_conti(client, home, draft_md, monkeypatch):
    from scripts import captions
    import threading
    gate = threading.Event()
    monkeypatch.setattr(captions, "check_setup", lambda: {"ready": True})

    def render(conti_path, video=None, track="caption", progress=None, cancel=None, timeout=900, step=None):
        gate.wait(5)
        return {"path": "drafts/x-captions.mp4", "count": 1, "duration_sec": 1.0, "width": 1, "height": 1}

    monkeypatch.setattr(captions, "render", render)
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/caption-video", {"path": rel(home, saved)})
    assert status == 200, body
    try:
        status, resp = client.json("POST", "/api/conti/trash", {"path": rel(home, saved)})
        assert status == 400 and resp["error"] == "conti_busy" and saved.exists()
    finally:
        gate.set()
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")
    status, _ = client.json("POST", "/api/conti/trash", {"path": rel(home, saved)})
    assert status == 200 and not saved.exists()


def test_draft_trash_route(client, home, draft_md):
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/draft/trash", {"path": rel(home, draft_md)})
    assert status == 200, body
    assert body["trash"].startswith("workspace/trash/") and len(body["moved"]) == 4   # draft, md, html, 이력 폴더
    assert not draft_md.exists() and not saved.exists()
    assert client.json("GET", "/api/drafts")[1]["drafts"] == []
    status, resp = client.json("POST", "/api/draft/trash", {"path": rel(home, draft_md)})
    assert status == 400 and resp["error"] == "draft_not_found"


@pytest.mark.parametrize("bad", ["../secret.md", "drafts/a.txt", "refs/x.md"])
def test_draft_trash_route_rejects_bad_paths(client, home, draft_md, bad):
    status, resp = client.json("POST", "/api/draft/trash", {"path": bad})
    assert status == 400 and resp["error"] == "bad_path" and draft_md.exists()


def test_draft_trash_route_refuses_while_its_conti_renders(client, home, draft_md, monkeypatch):
    import threading
    from scripts import captions
    gate = threading.Event()
    monkeypatch.setattr(captions, "check_setup", lambda: {"ready": True})

    def render(conti_path, video=None, track="caption", progress=None, cancel=None, timeout=900, step=None):
        gate.wait(5)
        return {"path": "drafts/x-captions.mp4", "count": 1, "duration_sec": 1.0, "width": 1, "height": 1}

    monkeypatch.setattr(captions, "render", render)
    saved = saved_conti(home, draft_md)
    status, body = client.json("POST", "/api/conti/caption-video", {"path": rel(home, saved)})
    assert status == 200, body
    try:
        status, resp = client.json("POST", "/api/draft/trash", {"path": rel(home, draft_md)})
        assert status == 400 and resp["error"] == "draft_busy" and draft_md.exists()
    finally:
        gate.set()
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")


def test_draft_trash_route_refuses_while_conti_is_being_made(client, home, draft_md, fakes, monkeypatch):
    import threading
    gate = threading.Event()
    real = fakes.ask_validated

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(llm, "ask_validated", slow)
    status, body = client.json("POST", "/api/conti/create", {"draft": rel(home, draft_md)})
    assert status == 200, body
    try:
        status, resp = client.json("POST", "/api/draft/trash", {"path": rel(home, draft_md)})
        assert status == 400 and resp["error"] == "draft_busy" and draft_md.exists()
    finally:
        gate.set()
    wait_for(client, body["job"]["id"], lambda j: j["status"] == "done")


def test_draft_trash_sees_every_running_conti_job(client, home, draft_md, fakes, monkeypatch):
    # 기획안 A의 콘티를 만드는 동안 B의 콘티도 시작해도, A의 삭제는 막힌다(최근 작업만 보면 놓친다)
    import threading
    other = draft_md.with_name("2026-10-27-other.md")
    other.write_text(draft_md.read_text(encoding="utf-8"), encoding="utf-8")
    gate = threading.Event()
    real = fakes.ask_validated

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(llm, "ask_validated", slow)
    first = client.json("POST", "/api/conti/create", {"draft": rel(home, draft_md)})[1]["job"]["id"]
    second = client.json("POST", "/api/conti/create", {"draft": rel(home, other)})[1]["job"]["id"]
    try:
        status, resp = client.json("POST", "/api/draft/trash", {"path": rel(home, draft_md)})
        assert status == 400 and resp["error"] == "draft_busy" and draft_md.exists()
    finally:
        gate.set()
    for job_id in (first, second):
        wait_for(client, job_id, lambda j: j["status"] == "done")
    status, _ = client.json("POST", "/api/draft/trash", {"path": rel(home, draft_md)})
    assert status == 200 and other.exists()


# --- 컷 편집 · 이력 라우트 ------------------------------------------------------------------

def _view(client, home, src):
    status, body = client.json("GET", "/api/conti?path=" + q(rel(home, src)))
    assert status == 200
    return body


def test_get_conti_adds_totals_and_stale_flags(client, home, draft_md):
    import os
    src = saved_conti(home, draft_md)
    body = _view(client, home, src)
    assert body["total_sec"] == 8 and body["target_sec"] == 8 and body["outputs_stale"] == {"srt": False, "video": False}
    srt = src.with_name(src.stem[: -len("-conti")] + "-captions.srt")
    video = src.with_name(src.stem[: -len("-conti")] + "-captions.mp4")
    srt.write_text("x", encoding="utf-8")
    video.write_bytes(b"x")
    old = src.stat().st_mtime - 100
    os.utime(srt, (old, old))
    assert _view(client, home, src)["outputs_stale"] == {"srt": True, "video": False}
    os.utime(video, (old, old))
    assert _view(client, home, src)["outputs_stale"] == {"srt": True, "video": True}


def test_cuts_route_ops_and_view_shape(client, home, draft_md):
    src = saved_conti(home, draft_md)
    view = _view(client, home, src)
    body = {"path": rel(home, src), "hash": view["hash"], "op": "insert", "at": 1, "cut": {"sec": 3}}
    status, resp = client.json("POST", "/api/conti/cuts", body)
    assert status == 200 and resp["cut_id"] and resp["hash"] != view["hash"]
    assert len(resp["cuts"]) == 5 and resp["total_sec"] == 11 and resp["target_sec"] == 8
    assert resp["cuts"][1]["id"] == resp["cut_id"] and "sketch" in resp["cuts"][0] and resp["fm"]["cuts"]
    status, stale = client.json("POST", "/api/conti/cuts", body)         # 낡은 hash
    assert status == 400 and stale["error"] == "conti_changed"
    status, resp = client.json("POST", "/api/conti/cuts", {"path": rel(home, src), "hash": resp["hash"], "op": "delete", "n": 5})
    assert status == 400 and resp["error"] == "last_cut"


@pytest.mark.parametrize("extra", [{}, {"hash": ""}, {"hash": 5}])
def test_cuts_route_requires_hash(client, home, draft_md, extra):
    src = saved_conti(home, draft_md)
    status, resp = client.json("POST", "/api/conti/cuts", {"path": rel(home, src), "op": "duplicate", "n": 1, **extra})
    assert status == 400 and resp["error"] == "bad_request" and "hash" in resp["message"]


def test_old_cut_route_still_edits_size_and_sec(client, home, draft_md):
    src = saved_conti(home, draft_md)
    body = {"path": rel(home, src), "n": 1, "fields": {"sec": 3}, "hash": conti.content_hash(src)}
    status, resp = client.json("POST", "/api/conti/cut", body)
    assert status == 200 and resp["cut"]["sec"] == 3


def test_history_routes_round_trip(client, home, draft_md):
    src = saved_conti(home, draft_md)
    path = q(rel(home, src))
    view = _view(client, home, src)
    status, resp = client.json("POST", "/api/conti/history/name", {"path": rel(home, src), "name": "기획 완료", "hash": view["hash"]})
    assert status == 200
    status, resp = client.json("POST", "/api/conti/cuts", {"path": rel(home, src), "hash": view["hash"], "op": "duplicate", "n": 2})
    assert status == 200
    status, listing = client.json("GET", "/api/conti/history?path=" + path)
    assert status == 200 and [v["kind"] for v in listing["versions"]][:2] == ["auto", "named"]
    named = next(v for v in listing["versions"] if v["kind"] == "named")
    status, one = client.json("GET", f"/api/conti/history/version?path={path}&n={named['n']}")
    assert status == 200 and one["version"]["state"]["cuts"] and len(one["version"]["state"]["cuts"]) == 4
    status, diff = client.json("GET", f"/api/conti/history/diff?path={path}&a={named['n']}")
    assert status == 200 and [c["kind"] for c in diff["changes"] if c["kind"] == "added"] == ["added"]
    status, restored = client.json("POST", "/api/conti/history/restore", {"path": rel(home, src), "n": named["n"], "hash": resp["hash"]})
    assert status == 200 and restored["hash"] == conti.content_hash(src)
    assert len(_view(client, home, src)["cuts"]) == 4
    status, bad = client.json("POST", "/api/conti/history/restore", {"path": rel(home, src), "n": named["n"], "hash": resp["hash"]})
    assert status == 400 and bad["error"] == "conti_changed"
    status, bad = client.json("GET", f"/api/conti/history/version?path={path}&n=99")
    assert status == 400 and bad["error"] == "version_not_found"


def test_history_name_validates_input(client, home, draft_md):
    src = saved_conti(home, draft_md)
    h = conti.content_hash(src)
    for body in ({"name": "", "hash": h}, {"name": "가" * 31, "hash": h}, {"name": 5, "hash": h}, {"name": "ok"}):
        status, resp = client.json("POST", "/api/conti/history/name", {"path": rel(home, src), **body})
        assert status == 400 and resp["error"] in ("invalid_name", "bad_request")


def test_get_conti_external_edit_adds_one_version(client, home, draft_md):
    src = saved_conti(home, draft_md)
    path = q(rel(home, src))
    _view(client, home, src)
    src.write_text(src.read_text("utf-8").replace("뚜껑 뜯기", "뚜껑 확 뜯기"), encoding="utf-8")
    _view(client, home, src)
    _view(client, home, src)
    status, listing = client.json("GET", "/api/conti/history?path=" + path)
    assert [v["kind"] for v in listing["versions"]] == ["external", "created"]


@pytest.mark.parametrize("extra", [{"op": "delete", "n": 1, "fields": {}}, {"op": "insert", "at": 0, "id": "x"},
                                   {"op": "bogus"}, {}])
def test_cuts_route_rejects_unknown_keys(client, home, draft_md, extra):
    src = saved_conti(home, draft_md)
    before = src.read_bytes()
    status, resp = client.json("POST", "/api/conti/cuts", {"path": rel(home, src), "hash": conti.content_hash(src), **extra})
    assert status == 400 and resp["error"] == "invalid_edit" and src.read_bytes() == before


# --- 큰 콘티: 한 번만 읽고 빨리 ---------------------------------------------------------------

def test_cuts_route_and_get_read_the_conti_md_few_times(client, home, draft_md, monkeypatch):
    from tests.test_conti import count_reads
    src = saved_conti(home, draft_md)
    view = _view(client, home, src)
    seen = count_reads(monkeypatch, src)
    status, resp = client.json("POST", "/api/conti/cuts", {"path": rel(home, src), "hash": view["hash"],
                                                           "op": "update", "n": 2, "fields": {"sfx": "쿵"}})
    assert status == 200 and resp["hash"] == conti.content_hash(src)
    assert len(seen) - 1 <= 3, seen                       # content_hash 위 한 번은 테스트가 읽은 것
    seen.clear()
    _view(client, home, src)
    assert len(seen) <= 3, seen


def big_conti(home, draft_md, n_cuts=450, n_videos=10, frames=40):  # noqa: F811
    """n_cuts 컷 콘티 + 레퍼런스 프레임(프레임별 샷 라벨, 30KB짜리 jpg)."""
    import json
    from scripts import refstore
    from scripts.features import SHOT_SIZES
    from tests.typify_fixtures import CAT
    src = saved_conti(home, draft_md)
    per = n_cuts // 3
    cuts = [{"section": s, "size": SHOT_SIZES[i % len(SHOT_SIZES)], "angle": "45도" if i % 2 else None, "sec": 1,
             "action": f"컷 {s}-{i}: 컵라면 뚜껑을 천천히 뜯으며 김이 오르는 모습을 담는다",
             "line": f"{i}번째 대사, 이건 생각보다 훨씬 맵네요 🏆", "caption": f"자막 {i} | 맵기 단계",
             "sfx": "후루룩", "guide": "카메라는 고정하고 손만 프레임 안에, 소리는 크게 담기"}
            for s in range(3) for i in range(per)]
    fm, _ = refstore.read_md(src)
    script = [{"label": s["label"], "start": k * per, "end": (k + 1) * per, "text": f"섹션 {k} 전체 대사"}
              for k, s in enumerate(fm["script"])]
    refstore.update_frontmatter(src, {"cuts": cuts, "script": script, "target_sec": per * 3})
    for v in range(n_videos):
        vid = f"a{v:02d}"
        labels = []
        for i in range(frames):
            rel_frame = f"workspace/frames/{vid}/scene{i + 1:03d}.jpg"
            (home / rel_frame).parent.mkdir(parents=True, exist_ok=True)
            (home / rel_frame).write_bytes(b"\xff\xd8\xff\xe0" + bytes([v, i]) * 15_000)
            labels.append({"frame": rel_frame, "size": SHOT_SIZES[(v + i) % len(SHOT_SIZES)], "section": "body"})
        shots = home / "workspace" / "shots" / f"{vid}.json"
        shots.parent.mkdir(parents=True, exist_ok=True)
        shots.write_text(json.dumps({"labels": labels}), encoding="utf-8")
    assert refstore.read_md(home / "refs" / f"ref_{CAT}" / "videos" / "a00.md")[0]["role"] == "ref"
    return src


@pytest.mark.skipif(os.environ.get("YTC_SKIP_TIMING") == "1", reason="느린 CI에서는 YTC_SKIP_TIMING=1로 건너뛴다")
def test_big_conti_edit_and_get_are_fast(client, home, draft_md):
    import time
    src = big_conti(home, draft_md)
    # 처음 한 번(id 붙이기 · 이력 기준선 · 막 만든 프레임 파일 첫 열기)은 재지 않는다. 막 만든 파일을 처음 열 때
    # 백신 검사로 파일마다 수 ms가 걸려 HTTP 클라이언트의 5초 제한을 넘을 수 있어 서버를 거치지 않고 부른다.
    assert len(api_conti.conti_view(src)["cuts"]) == 450
    t0 = time.perf_counter()
    view = _view(client, home, src)
    get_sec = time.perf_counter() - t0
    t0 = time.perf_counter()
    status, resp = client.json("POST", "/api/conti/cuts", {"path": rel(home, src), "hash": view["hash"],
                                                           "op": "update", "id": view["cuts"][200]["id"],
                                                           "fields": {"caption": "고친 자막"}})
    edit_sec = time.perf_counter() - t0
    print(f"\n450 cuts: GET {get_sec:.2f}s, POST /api/conti/cuts {edit_sec:.2f}s")
    assert status == 200 and resp["cuts"][200]["caption"] == "고친 자막"
    assert get_sec < 4 and edit_sec < 4
