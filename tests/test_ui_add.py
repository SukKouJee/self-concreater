"""Task 5: 레퍼런스 추가 작업(job) + API. 네트워크·claude 호출은 모두 가짜로 바꾼다."""
import json
import time

import pytest

from scripts import claude_cli, cuts, fetch, refstore, shots, stt, videomd
from scripts.common import ScriptError
from scripts.ui import api_add, jobs
from tests.test_ui_server import Client, _start, _stop

CAT = "food"
A, B, C = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"


def url(vid):
    return f"https://youtu.be/{vid}"


def analysis(label="hook"):
    return {
        "summary": "한 줄 요약",
        "synopsis": "줄거리",
        "hook_type": "question",
        "narrative_type": "list",
        "sections": [
            {"label": label, "start": 0, "end": 3, "content": "첫 장면"},
            {"label": "body", "sub": "list_item", "start": 3, "end": 60, "content": "본론"},
        ],
        "techniques": [{"kind": "훅", "name": "결과 선공개", "desc": "", "at": 0}],
    }


class Fakes:
    """모듈 함수 대신 들어가는 가짜들. 영상별 동작은 dict로 바꾼다."""

    def __init__(self):
        self.fetch_calls = []
        self.fetch_errors = {}      # vid -> ScriptError
        self.fetch_status = {}      # vid -> "exists" 등
        self.cut_calls = []
        self.stt_calls = []
        self.stt_error = None
        self.asks = []              # (rules, payload, task, kw)
        self.ask_errors = []        # 차례대로 낼 오류(None이면 정상)
        self.writes = []            # dict(args..., analysis=tmp 내용)
        self.shot_ctx_calls = []
        self.applied = []           # (vid, labels)
        self.pending = []

    # fetch / cuts / stt
    def fetch(self, link, category=None, role="ref", force=False, reanalyze=False):
        vid = link[-11:]
        self.fetch_calls.append({"vid": vid, "category": category, "role": role,
                                 "force": force, "reanalyze": reanalyze})
        if vid in self.fetch_errors:
            raise self.fetch_errors[vid]
        status = self.fetch_status.get(vid, "fetched")
        return {"id": vid, "status": status, "title": f"제목 {vid}", "sparse": False}

    def cut_stats(self, vid, force=False):
        self.cut_calls.append((vid, force))
        return {"id": vid, "status": "analyzed", "frames": [f"workspace/frames/{vid}/scene001_0.5s.jpg"]}

    def transcribe(self, vid, force=False):
        self.stt_calls.append(vid)
        if self.stt_error:
            raise self.stt_error
        return {"id": vid, "status": "transcribed"}

    # videomd
    def load_raw(self, vid):
        return {"id": vid, "meta": {"duration": 60}}

    def load_cuts(self, vid):
        return None

    def context(self, vid, category):
        return {"id": vid, "title": f"제목 {vid}", "sparse": False, "duration_sec": 60}

    def write(self, vid, category, role, analysis_path, force=False):
        content = json.loads(open(analysis_path, encoding="utf-8").read())
        self.writes.append({"vid": vid, "category": category, "role": role, "path": str(analysis_path),
                            "force": force, "analysis": content})
        return {"id": vid, "status": "written", "stage_text": f"단계 {len(self.writes)}"}

    # claude
    def ask_validated(self, rules, payload, task, validate, **kw):
        self.asks.append((rules, payload, task, kw))
        if self.ask_errors:
            err = self.ask_errors.pop(0)
            if err is not None:
                raise err
        if "yt-shots" in str(rules[0]):
            data = {"labels": [{"frame": f["frame"], "size": "바스트", "angle": "정면", "face": True,
                                "hands_only": False, "broll": False, "text_overlay": False}
                               for f in payload["frames"]]}
        else:
            data = analysis()
        return data, validate(data)

    # shots
    def shots_context(self, vid, category):
        self.shot_ctx_calls.append(vid)
        return {"id": vid, "frames": [{"frame": f"workspace/frames/{vid}/scene001_0.5s.jpg"}]}

    def shots_apply(self, vid, category, labels_path):
        labels = json.loads(open(labels_path, encoding="utf-8").read())
        self.applied.append((vid, labels))
        return {"status": "applied", "id": vid, "frames": len(labels)}

    def shots_pending(self, category):
        return {"category": category, "pending": list(self.pending), "relabel": []}

    def analyze_asks(self):
        return [a for a in self.asks if "yt-analyze" in str(a[0][0])]


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(fetch, "fetch", f.fetch)
    monkeypatch.setattr(fetch, "load_raw", f.load_raw)
    monkeypatch.setattr(cuts, "cut_stats", f.cut_stats)
    monkeypatch.setattr(stt, "transcribe", f.transcribe)
    monkeypatch.setattr(videomd, "load_cuts", f.load_cuts)
    monkeypatch.setattr(videomd, "context", f.context)
    monkeypatch.setattr(videomd, "write", f.write)
    monkeypatch.setattr(claude_cli, "ask_validated", f.ask_validated)
    monkeypatch.setattr(shots, "context", f.shots_context)
    monkeypatch.setattr(shots, "apply", f.shots_apply)
    monkeypatch.setattr(shots, "pending", f.shots_pending)
    return f


@pytest.fixture
def client(monkeypatch, tmp_path, fakes):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    (tmp_path / "refs" / f"ref_{CAT}" / "videos").mkdir(parents=True)
    srv, app, thread = _start(tmp_path)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def snap(client, job_id):
    status, body = client.json("GET", f"/api/jobs/{job_id}")
    assert status == 200, body
    return body["job"]


def wait_for(client, job_id, pred, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        job = snap(client, job_id)
        if pred(job):
            return job
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out: {json.dumps(job, ensure_ascii=False)}")
        time.sleep(0.02)


def item_state(i, state):
    return lambda job: job["items"][i]["state"] == state


def finished(job):
    return job["status"] in {"done", "stopped", "failed"}


def start_add(client, links, **opts):
    body = {"category": CAT, "role": "ref", "links": links, "shots": False, "force": False, "refetch": False, **opts}
    status, resp = client.json("POST", "/api/add", body)
    assert status == 200, resp
    return resp["job"]


def decide(client, job_id, item_id, **decision):
    return client.json("POST", f"/api/jobs/{job_id}/items/{item_id}/decision", decision)


# ---- 흐름 -------------------------------------------------------------------------

def test_add_happy_path_awaits_then_saves(client, fakes, tmp_path):
    job = start_add(client, [url(A)])
    assert job["kind"] == "collect" and job["category"] == CAT
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    item = job["items"][0]
    assert job["status"] == "awaiting"
    assert item["id"] == A and item["title"] == f"제목 {A}" and item["step"] == "confirm"
    assert item["preview"]["summary"] == "한 줄 요약" and item["ask"] is None
    assert item["frames"] == [f"workspace/frames/{A}/scene001_0.5s.jpg"]
    rules, payload, _, kw = fakes.asks[0]
    assert rules == [claude_cli.SKILLS_DIR / "yt-analyze" / "SKILL.md"]
    assert payload["id"] == A and kw["tools"] is None

    status, body = decide(client, job["id"], A, action="save")
    assert status == 200 and body["job"]["id"] == job["id"]
    job = wait_for(client, job["id"], finished)

    assert job["status"] == "done"
    assert job["items"][0]["state"] == "saved"
    assert job["summary"] == {"saved": 1, "skipped": 0, "failed": 0, "stage_text": "단계 1"}
    w = fakes.writes[0]
    assert (w["vid"], w["category"], w["role"], w["force"]) == (A, CAT, "ref", False)
    assert w["path"] == str(tmp_path / "workspace" / "tmp" / f"analysis_{A}.json")
    assert fakes.fetch_calls[0] == {"vid": A, "category": CAT, "role": "ref", "force": False, "reanalyze": False}
    assert fakes.cut_calls == [(A, False)]


def test_save_uses_edited_analysis(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    edited = analysis(label="intro")
    decide(client, job["id"], A, action="save", analysis=edited)
    wait_for(client, job["id"], finished)
    assert fakes.writes[0]["analysis"]["sections"][0]["label"] == "intro"


def test_save_all_skips_further_confirmation(client, fakes):
    job = start_add(client, [url(A), url(B), url(C)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], A, action="save_all")
    job = wait_for(client, job["id"], finished)
    assert [i["state"] for i in job["items"]] == ["saved", "saved", "saved"]
    assert [w["vid"] for w in fakes.writes] == [A, B, C]
    assert job["summary"]["saved"] == 3 and job["summary"]["stage_text"] == "단계 3"


def test_exists_is_skipped(client, fakes):
    fakes.fetch_status[A] = "exists"
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "skipped" and job["items"][0]["message"] == "이미 분석됨"
    assert job["summary"] == {"saved": 0, "skipped": 1, "failed": 0, "stage_text": None}
    assert fakes.cut_calls == [] and fakes.asks == []


def test_blocked_stops_remaining(client, fakes):
    fakes.fetch_errors[B] = ScriptError("blocked", "유튜브가 요청을 막았습니다")
    job = start_add(client, [url(A), url(B), url(C)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], A, action="save")
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "stopped"
    assert [i["state"] for i in job["items"]] == ["saved", "failed", "skipped"]
    assert job["items"][1]["message"] == "유튜브가 요청을 막았습니다"
    assert job["items"][2]["message"] == "차단으로 중단"
    assert [c["vid"] for c in fakes.fetch_calls] == [A, B]
    assert job["summary"]["failed"] == 1 and job["summary"]["skipped"] == 1


def test_no_transcript_asks_then_stt(client, fakes):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    item = job["items"][0]
    assert item["ask"] == "no_transcript" and item["preview"] is None
    assert item["id"] == A and item["title"] == "무자막"
    assert fakes.asks == []

    decide(client, job["id"], A, action="stt")
    job = wait_for(client, job["id"], lambda j: j["items"][0]["preview"] is not None)
    assert fakes.stt_calls == [A]
    assert job["items"][0]["ask"] is None
    assert len(fakes.analyze_asks()) == 1


def test_no_transcript_screen_uses_read_tool(client, fakes):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], A, action="screen")
    wait_for(client, job["id"], lambda j: j["items"][0]["preview"] is not None)
    assert fakes.stt_calls == []
    assert fakes.analyze_asks()[0][3]["tools"] == ["Read"]


def test_stt_unavailable_falls_back_to_screen(client, fakes):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    fakes.stt_error = ScriptError("stt_unavailable", "faster-whisper가 없습니다")
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], A, action="stt")
    job = wait_for(client, job["id"], lambda j: j["items"][0]["preview"] is not None)
    assert "faster-whisper가 없습니다" in job["items"][0]["message"]
    assert fakes.analyze_asks()[0][3]["tools"] == ["Read"]


def test_add_claude_failure_marks_video_and_continues(client, fakes):
    fakes.ask_errors = [ScriptError("claude_failed", "Claude 응답 시간 초과(600초)")]
    job = start_add(client, [url(A), url(B)])
    job = wait_for(client, job["id"], item_state(1, "awaiting"))
    assert job["items"][0]["state"] == "failed"
    assert job["items"][0]["message"] == "Claude 응답 시간 초과(600초)"
    assert job["items"][1]["preview"] is not None


def test_cuts_failure_keeps_going(client, fakes, monkeypatch):
    def broken(vid, force=False):
        raise ScriptError("download_failed", "영상 파일을 받지 못했습니다.")
    monkeypatch.setattr(cuts, "cut_stats", broken)
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    assert "영상 파일을 받지 못했습니다." in job["items"][0]["message"]
    assert job["items"][0]["frames"] == []


def test_reanalyze_passes_feedback(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], A, action="reanalyze", feedback="훅이 더 길어요")
    wait_for(client, job["id"], lambda j: j["items"][0]["state"] == "awaiting" and len(fakes.asks) == 2)
    first, second = fakes.analyze_asks()
    assert "사용자 의견" not in first[2]
    assert second[2] == first[2] + "\n사용자 의견: 훅이 더 길어요"
    decide(client, job["id"], A, action="skip")
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "skipped" and fakes.writes == []


def test_force_and_refetch_flags(client, fakes):
    job = start_add(client, [url(A)], force=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], A, action="save")
    wait_for(client, job["id"], finished)
    assert fakes.fetch_calls[-1]["reanalyze"] is True and fakes.fetch_calls[-1]["force"] is False
    assert fakes.cut_calls[-1] == (A, False) and fakes.writes[-1]["force"] is True

    job = start_add(client, [url(B)], refetch=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], B, action="save")
    wait_for(client, job["id"], finished)
    assert fakes.fetch_calls[-1]["force"] is True
    assert fakes.cut_calls[-1] == (B, True) and fakes.writes[-1]["force"] is True


def test_shots_option_runs_after_save(client, fakes, tmp_path):
    job = start_add(client, [url(A)], shots=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], A, action="save")
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "saved"
    assert fakes.shot_ctx_calls == [A]
    vid, labels = fakes.applied[0]
    assert vid == A and labels[0]["size"] == "바스트"
    rules, _, _, kw = fakes.asks[-1]
    assert rules == [claude_cli.SKILLS_DIR / "yt-shots" / "SKILL.md"] and kw["tools"] == ["Read"]
    assert not (tmp_path / "workspace" / "tmp" / f"shots_{A}.json").exists()


def test_shots_no_frames_keeps_saved(client, fakes, monkeypatch):
    def no_frames(vid, category):
        raise ScriptError("no_frames", "대표 프레임이 없습니다")
    monkeypatch.setattr(shots, "context", no_frames)
    job = start_add(client, [url(A)], shots=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    decide(client, job["id"], A, action="save")
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "saved"
    assert job["items"][0]["message"] == "컷 통계가 없어 샷 분류를 건너뜁니다"


def test_shots_only_job(client, fakes):
    fakes.pending = [A, B]
    fakes.ask_errors = [None, ScriptError("claude_failed", "실패")]
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    assert status == 200
    job = wait_for(client, body["job"]["id"], finished)
    assert job["kind"] == "collect" and job["status"] == "done"
    assert [i["id"] for i in job["items"]] == [A, B]
    assert [i["state"] for i in job["items"]] == ["saved", "failed"]
    assert [v for v, _ in fakes.applied] == [A]
    assert job["summary"] == {"saved": 1, "skipped": 0, "failed": 1, "stage_text": None}


# ---- API / 잠금 -----------------------------------------------------------------

def test_only_one_collect_job(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    status, body = client.json("POST", "/api/add", {"category": CAT, "links": [url(B)]})
    assert status == 400 and body["error"] == "job_running"
    assert body["message"] == "이미 수집 중인 작업이 있습니다"
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    assert status == 400 and body["error"] == "job_running"


def test_current_job_snapshot_keeps_awaiting_preview(client, fakes):
    status, body = client.json("GET", "/api/jobs/current?kind=collect")
    assert status == 200 and body == {"job": None}
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    status, body = client.json("GET", "/api/jobs/current?kind=collect")
    assert status == 200
    assert body["job"]["id"] == job["id"]
    assert body["job"]["items"][0]["preview"] == job["items"][0]["preview"]
    assert body["job"]["items"][0]["state"] == "awaiting"


def test_links_parsing_dedup(client, fakes):
    assert api_add.parse_links(["a\n\n a  b\na"]) == ["a", "b"]
    assert api_add.parse_links("x y\nx") == ["x", "y"]
    fakes.fetch_status.update({A: "exists", B: "exists"})
    job = start_add(client, [f"{url(A)}\n\n {url(A)}  {url(B)}\n{url(A)}"])
    assert [i["url"] for i in job["items"]] == [url(A), url(B)]


@pytest.mark.parametrize("body, code", [
    ({"category": CAT, "links": []}, "no_links"),
    ({"category": CAT, "links": ["  \n "]}, "no_links"),
    ({"category": CAT, "role": "other", "links": ["x"]}, "bad_request"),
    ({"links": ["x"]}, "bad_request"),
    ({"category": "nope", "links": ["x"]}, "category_not_found"),
])
def test_add_rejects_bad_request(client, body, code):
    status, resp = client.json("POST", "/api/add", body)
    assert status == 400 and resp["error"] == code


def test_decision_errors(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    status, body = decide(client, job["id"], A, action="explode")
    assert status == 400 and body["error"] == "bad_action"
    status, body = decide(client, job["id"], "nope", action="save")
    assert status == 400 and body["error"] == "item_not_found"
    status, body = decide(client, job["id"], A, action="stt")
    assert status == 400 and body["error"] == "bad_action"
    status, body = client.json("GET", "/api/jobs/missing")
    assert status == 400 and body["error"] == "job_not_found"
    decide(client, job["id"], A, action="save")
    wait_for(client, job["id"], finished)
    status, body = decide(client, job["id"], A, action="save")
    assert status == 400 and body["error"] == "not_awaiting"


def test_categories_route(client, fakes):
    status, body = client.json("GET", "/api/categories")
    assert status == 200 and body == {"categories": refstore.list_categories()}
    assert body["categories"][0]["category"] == CAT


def test_app_has_job_manager(client):
    assert isinstance(client.app.jobs, jobs.JobManager)


# ---- JobManager 단위 ---------------------------------------------------------------

def test_job_manager_snapshot_is_copy_and_shutdown_unblocks():
    mgr = jobs.JobManager()
    seen = {}

    def target(job):
        job.update(0, state="awaiting")
        seen["decision"] = job.wait_decision("x")

    job = mgr.start("collect", target, items=[jobs.new_item("x")])
    deadline = time.monotonic() + 5
    while job.snapshot()["items"][0]["state"] != "awaiting" and time.monotonic() < deadline:
        time.sleep(0.01)
    s = job.snapshot()
    s["items"][0]["state"] = "tampered"
    assert job.snapshot()["items"][0]["state"] == "awaiting"
    assert mgr.current("collect") is job and mgr.get(job.id) is job
    with pytest.raises(ScriptError) as err:
        mgr.start("collect", lambda j: None)
    assert err.value.code == "job_running"
    other = mgr.start("conti", lambda j: None)   # 다른 kind는 동시에 돈다
    mgr.shutdown(timeout=5)
    assert job.status == "stopped" and "decision" not in seen
    assert job.snapshot()["items"][0]["state"] == "skipped"
    assert other.status == "done"


def test_job_unexpected_error_marks_failed():
    mgr = jobs.JobManager()

    def target(job):
        job.update(0, state="running")
        raise RuntimeError("boom")

    job = mgr.start("collect", target, items=[jobs.new_item("x")])
    job.join(5)
    assert job.status == "failed" and "boom" in job.snapshot()["message"]
    assert job.snapshot()["items"][0]["state"] == "failed"
    mgr.start("collect", lambda j: None).join(5)   # 끝난 작업은 새 작업을 막지 않는다
