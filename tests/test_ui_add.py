"""Task 5: 레퍼런스 추가 작업(job) + API. 네트워크·claude 호출은 모두 가짜로 바꾼다."""
import json
import threading
import time

import pytest

from scripts import claude_cli, llm, cuts, fetch, refstore, shots, stt, videomd
from scripts.common import ScriptError
from scripts.ui import api_add, jobs
from scripts.ui import __main__ as ui_main
from scripts.ui import server
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture

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
        self.cut_status = "analyzed"
        self.cut_hook = None        # callable(progress): cut_stats 안에서 진행 콜백을 부른다
        self.progress = {}          # "fetch"/"cuts"/"stt" -> 받은 progress 콜백
        self.raw_cached = True      # False면 fetch를 부르기 전에는 raw 캐시가 없다
        self.step_sec = 0.02        # 가짜 단계도 시간이 걸려야 기록된다(걸린 시간 0은 기록하지 않음)
        self.asks = []              # (rules, payload, task, kw)
        self.ask_errors = []        # 차례대로 낼 오류(None이면 정상)
        self.writes = []            # dict(args..., analysis=tmp 내용)
        self.write_errors = []      # 차례대로 낼 오류(None이면 정상)
        self.shot_ctx_calls = []
        self.applied = []           # (vid, labels)
        self.pending = []

    # fetch / cuts / stt
    def fetch(self, link, category=None, role="ref", force=False, reanalyze=False, progress=None):
        vid = link[-11:]
        self.progress["fetch"] = progress
        time.sleep(self.step_sec)
        self.fetch_calls.append({"vid": vid, "category": category, "role": role,
                                 "force": force, "reanalyze": reanalyze})
        if vid in self.fetch_errors:
            raise self.fetch_errors[vid]
        status = self.fetch_status.get(vid, "fetched")
        return {"id": vid, "status": status, "title": f"제목 {vid}", "sparse": False, "duration_sec": 60}

    def cut_stats(self, vid, force=False, progress=None):
        self.cut_calls.append((vid, force))
        self.progress["cuts"] = progress
        time.sleep(self.step_sec)
        if self.cut_hook:
            self.cut_hook(progress)
        return {"id": vid, "status": self.cut_status, "duration_sec": 60,
                "frames": [f"workspace/frames/{vid}/scene001_0.5s.jpg"]}

    def transcribe(self, vid, force=False, progress=None):
        self.stt_calls.append(vid)
        self.progress["stt"] = progress
        time.sleep(self.step_sec)
        if self.stt_error:
            raise self.stt_error
        return {"id": vid, "status": "transcribed"}

    # videomd
    def load_raw(self, vid):
        if not self.raw_cached and vid not in [c["vid"] for c in self.fetch_calls]:
            raise ScriptError("raw_not_found", f"수집 데이터가 없습니다: {vid} (먼저 fetch)")
        return {"id": vid, "meta": {"duration": 60}}

    def load_cuts(self, vid):
        return None

    def context(self, vid, category):
        return {"id": vid, "title": f"제목 {vid}", "sparse": False, "duration_sec": 60,
                "examples": ["refs/ref_food/videos/ex.md"]}

    def write(self, vid, category, role, analysis_path, force=False):
        with open(analysis_path, encoding="utf-8") as f:
            content = json.load(f)
        if self.write_errors:
            err = self.write_errors.pop(0)
            if err is not None:
                raise err
        self.writes.append({"vid": vid, "category": category, "role": role, "path": str(analysis_path),
                            "force": force, "analysis": content})
        return {"id": vid, "status": "written", "stage_text": f"단계 {len(self.writes)}"}

    # claude
    def ask_validated(self, rules, payload, task, validate, **kw):
        self.asks.append((rules, payload, task, kw))
        time.sleep(self.step_sec)
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
        with open(labels_path, encoding="utf-8") as f:
            labels = json.load(f)
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
    monkeypatch.setattr(llm, "ask_validated", f.ask_validated)
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
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


def review(client, job_id, item_id, **body):
    return client.json("POST", f"/api/add/{job_id}/review/{item_id}", body)


def save_all(client, job_id):
    return client.json("POST", f"/api/add/{job_id}/save-all", {})


def in_review(i):
    return lambda job: job["items"][i]["state"] == "awaiting" and job["items"][i]["ask"] == "review"


def gate(fakes, vid):
    """vid의 컷 단계에서 멈추게 한다. (들어옴, 풀기) Event를 돌려준다."""
    entered, release = threading.Event(), threading.Event()
    real = fakes.cut_hook

    def hook(progress):
        if fakes.cut_calls[-1][0] == vid:
            entered.set()
            assert release.wait(5)
        if real:
            real(progress)

    fakes.cut_hook = hook
    return entered, release


# ---- 흐름 -------------------------------------------------------------------------

def test_add_happy_path_awaits_then_saves(client, fakes, tmp_path):
    job = start_add(client, [url(A)])
    assert job["kind"] == "collect" and job["category"] == CAT
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    item = job["items"][0]
    assert job["status"] == "awaiting"
    assert item["id"] == A and item["title"] == f"제목 {A}" and item["step"] == "confirm"
    assert item["preview"]["summary"] == "한 줄 요약" and item["ask"] == "review"
    assert item["frames"] == [f"workspace/frames/{A}/scene001_0.5s.jpg"]
    assert item["opts"] == {"category": CAT, "role": "ref", "shots": False, "force": False, "refetch": False,
                            "no_transcript": "stt"}
    rules, payload, _, kw = fakes.asks[0]
    assert rules == [claude_cli.SKILLS_DIR / "yt-analyze" / "SKILL.md"]
    assert payload["id"] == A
    assert kw["files"] == [tmp_path / "refs/ref_food/videos/ex.md"]   # examples(사용자 교정 md)를 읽어야 한다
    assert api_add.SCREEN_HINT not in fakes.asks[0][2]

    status, body = review(client, job["id"], A, action="save")
    assert status == 200 and body["job"]["id"] == job["id"]
    assert body["job"]["items"][0]["state"] == "saved" and body["job"]["items"][0]["step"] == "done"
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
    wait_for(client, job["id"], in_review(0))
    edited = analysis(label="intro")
    status, _ = review(client, job["id"], A, action="save", analysis=edited)
    assert status == 200
    wait_for(client, job["id"], finished)
    assert fakes.writes[0]["analysis"]["sections"][0]["label"] == "intro"


def test_invalid_edited_analysis_returns_to_review_with_reason(client, fakes):
    fakes.write_errors = [ScriptError("invalid_analysis", "섹션 라벨이 틀렸습니다")]
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    edited = analysis(label="intro")

    status, body = review(client, job["id"], A, action="save", analysis=edited)

    assert status == 400 and body["error"] == "invalid_analysis"
    item = snap(client, job["id"])["items"][0]
    assert (item["state"], item["ask"], item["step"]) == ("awaiting", "review", "confirm")
    assert item["message"] == "섹션 라벨이 틀렸습니다"
    assert item["preview"]["sections"][0]["label"] == "intro"     # 고친 내용을 그대로 다시 보여 준다
    status, body = review(client, job["id"], A, action="save")
    assert status == 200 and body["job"]["items"][0]["state"] == "saved"


def test_save_exists_is_skipped(client, fakes, monkeypatch):
    monkeypatch.setattr(videomd, "write", lambda *a, **kw: {"id": A, "status": "exists"})
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    status, body = review(client, job["id"], A, action="save")
    assert status == 200
    item = body["job"]["items"][0]
    assert (item["state"], item["message"]) == ("skipped", "이미 있음")


def test_next_video_runs_while_first_waits_for_review(client, fakes):
    job = start_add(client, [url(A), url(B)])
    job = wait_for(client, job["id"], lambda j: all(i["state"] == "awaiting" for i in j["items"]))
    assert all(i["ask"] == "review" for i in job["items"]) and job["status"] == "awaiting"
    assert [c["vid"] for c in fakes.fetch_calls] == [A, B]
    assert fakes.writes == []                       # 저장은 사용자가 누를 때만


def test_review_save_writes_and_closes_job(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    status, data = review(client, job["id"], A, action="save")
    assert status == 200 and data["job"]["items"][0]["state"] == "saved"
    job = wait_for(client, job["id"], lambda j: j["status"] == "done")
    assert [w["vid"] for w in fakes.writes] == [A]


def test_review_skip_marks_skipped_without_writing(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    status, data = review(client, job["id"], A, action="skip")
    assert status == 200
    assert data["job"]["items"][0]["state"] == "skipped" and data["job"]["items"][0]["message"] == "건너뜀"
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "done" and fakes.writes == []


def test_double_save_writes_once(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    barrier = threading.Barrier(2)
    results = []

    def click():
        barrier.wait()
        results.append(review(client, job["id"], A, action="save"))

    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    statuses = sorted(s for s, _ in results)
    assert statuses == [200, 400]
    assert [b["error"] for s, b in results if s == 400] == ["not_awaiting"]
    assert len(fakes.writes) == 1
    status, body = review(client, job["id"], A, action="save")
    assert status == 400 and body["error"] == "not_awaiting" and len(fakes.writes) == 1


def test_status_leaves_awaiting_when_reviews_resolved_while_worker_busy(client, fakes):
    entered, release = gate(fakes, B)
    job = start_add(client, [url(A), url(B)])
    wait_for(client, job["id"], in_review(0))
    assert entered.wait(5)
    assert snap(client, job["id"])["status"] == "awaiting"

    status, body = review(client, job["id"], A, action="save")

    assert status == 200 and body["job"]["status"] == "running"
    release.set()
    wait_for(client, job["id"], lambda j: in_review(1)(j) and j["status"] == "awaiting")


def test_status_done_when_last_review_resolved_while_idle(client, fakes):
    job = start_add(client, [url(A), url(B)])
    wait_for(client, job["id"], lambda j: in_review(0)(j) and in_review(1)(j))
    time.sleep(0.6)                                   # 워커가 한가한 상태로 돈다
    review(client, job["id"], A, action="save")
    assert snap(client, job["id"])["status"] == "awaiting"   # B가 아직 확인 대기
    review(client, job["id"], B, action="skip")
    job = wait_for(client, job["id"], lambda j: j["status"] != "awaiting")
    assert job["status"] in {"running", "done"}
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "done"
    assert job["summary"] == {"saved": 1, "skipped": 1, "failed": 0, "stage_text": "단계 1"}


def test_save_all_saves_only_current_reviews_and_reports_each(client, fakes):
    entered, release = gate(fakes, C)
    job = start_add(client, [url(A), url(B), url(C)])
    wait_for(client, job["id"], lambda j: in_review(0)(j) and in_review(1)(j))
    assert entered.wait(5)

    status, data = save_all(client, job["id"])

    assert status == 200
    assert data["results"] == [{"id": A, "ok": True, "message": None}, {"id": B, "ok": True, "message": None}]
    assert [i["state"] for i in data["job"]["items"][:2]] == ["saved", "saved"]
    release.set()
    job = wait_for(client, job["id"], in_review(2))   # 그 뒤에 끝난 영상은 다시 확인을 받는다
    time.sleep(0.2)
    assert snap(client, job["id"])["items"][2]["state"] == "awaiting"
    assert [w["vid"] for w in fakes.writes] == [A, B]


def test_save_all_continues_after_one_failure(client, fakes):
    fakes.write_errors = [ScriptError("write_failed", "md를 쓰지 못했습니다")]
    job = start_add(client, [url(A), url(B)])
    wait_for(client, job["id"], lambda j: in_review(0)(j) and in_review(1)(j))

    status, data = save_all(client, job["id"])

    assert status == 200
    assert data["results"] == [{"id": A, "ok": False, "message": "md를 쓰지 못했습니다"},
                               {"id": B, "ok": True, "message": None}]
    a, b = data["job"]["items"]
    assert (a["state"], a["ask"], a["message"]) == ("awaiting", "review", "md를 쓰지 못했습니다")
    assert b["state"] == "saved"
    assert [w["vid"] for w in fakes.writes] == [B]


def test_save_all_reports_unexpected_error_and_keeps_review(client, fakes):
    fakes.write_errors = [OSError("디스크 가득 참")]
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    status, data = save_all(client, job["id"])
    assert status == 200
    assert data["results"][0]["ok"] is False and "OSError" in data["results"][0]["message"]
    assert in_review(0)(data["job"])


def test_save_all_with_nothing_to_review(client, fakes):
    fakes.fetch_status[A] = "exists"
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], finished)
    status, data = save_all(client, job["id"])
    assert status == 200 and data["results"] == []


def test_reviews_survive_cancel(client, fakes):
    entered, release = gate(fakes, B)
    job = start_add(client, [url(A), url(B)])
    wait_for(client, job["id"], in_review(0))
    assert entered.wait(5)

    status, _ = client.json("POST", f"/api/jobs/{job['id']}/cancel", {})
    assert status == 200
    release.set()
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "stopped"
    assert job["items"][1]["state"] == "skipped"
    assert in_review(0)(job)

    status, data = review(client, job["id"], A, action="save")
    assert status == 200 and data["job"]["items"][0]["state"] == "saved"
    assert data["job"]["status"] == "stopped"
    assert data["job"]["summary"]["saved"] == 1        # 멈춘 작업의 요약도 갱신된다
    assert [w["vid"] for w in fakes.writes] == [A]


def test_append_links_to_running_job_skips_duplicates(client, fakes):
    entered, release = gate(fakes, A)
    job = start_add(client, [url(A), url(B)])
    assert entered.wait(5)

    status, data = client.json("POST", "/api/add", {"category": CAT, "links": [url(B), f"https://www.youtube.com/watch?v={A}", url(C)]})

    assert status == 200
    assert data["appended"] == [url(C)]
    assert data["job"]["id"] == job["id"]
    assert [i["url"] for i in data["job"]["items"]] == [url(A), url(B), url(C)]
    release.set()
    job = wait_for(client, job["id"], lambda j: all(in_review(i)(j) for i in range(3)))
    assert [i["id"] for i in job["items"]] == [A, B, C]
    status, body = client.json("GET", "/api/jobs/current?kind=collect")
    assert body["job"]["id"] == job["id"]


def test_appended_items_carry_their_own_options(client, fakes, tmp_path):
    (tmp_path / "refs" / "ref_travel" / "videos").mkdir(parents=True)
    entered, release = gate(fakes, A)
    job = start_add(client, [url(A)])
    assert entered.wait(5)
    status, data = client.json("POST", "/api/add", {"category": "travel", "role": "mine", "links": [url(B)],
                                                    "refetch": True, "no_transcript": "skip"})
    assert status == 200 and data["appended"] == [url(B)]
    release.set()
    wait_for(client, job["id"], lambda j: in_review(0)(j) and in_review(1)(j))
    assert fakes.fetch_calls[1] == {"vid": B, "category": "travel", "role": "mine", "force": True, "reanalyze": False}
    review(client, job["id"], B, action="save")
    w = fakes.writes[-1]
    assert (w["vid"], w["category"], w["role"], w["force"]) == (B, "travel", "mine", True)


def test_add_while_shots_only_runs_is_refused(client, fakes, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_context(vid, category):
        entered.set()
        assert release.wait(5)
        return fakes.shots_context(vid, category)

    monkeypatch.setattr(shots, "context", slow_context)
    fakes.pending = [A]
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    assert status == 200
    assert entered.wait(5)
    status, resp = client.json("POST", "/api/add", {"category": CAT, "links": [url(B)]})
    release.set()
    assert status == 400 and resp["error"] == "job_running"
    assert len(snap(client, body["job"]["id"])["items"]) == 1


def test_new_job_adopts_leftover_reviews(client, fakes):
    fakes.fetch_errors[B] = ScriptError("blocked", "유튜브가 요청을 막았습니다")
    old = start_add(client, [url(A), url(B)], shots=True)
    old = wait_for(client, old["id"], finished)
    assert old["status"] == "stopped" and in_review(0)(old)

    new = start_add(client, [url(A), url(D)])

    assert new["id"] != old["id"]
    first = new["items"][0]
    assert (first["id"], first["state"], first["ask"]) == (A, "awaiting", "review")
    assert first["preview"]["summary"] == "한 줄 요약" and first["opts"]["shots"] is True
    assert first["timing"]["steps"]["analyze"]["state"] == "done"
    assert [i["url"] for i in new["items"]] == [url(A), url(D)]      # 같은 영상은 다시 넣지 않는다
    moved = snap(client, old["id"])["items"][0]
    assert (moved["state"], moved["message"]) == ("skipped", "새 작업으로 옮김")
    status, body = review(client, old["id"], A, action="save")
    assert status == 400 and body["error"] == "not_awaiting"

    status, body = review(client, new["id"], A, action="save")
    assert status == 200
    new = wait_for(client, new["id"], lambda j: j["items"][0]["state"] == "saved" and in_review(1)(j))
    assert new["items"][0]["timing"]["steps"]["shots"]["state"] == "done"
    assert fakes.shot_ctx_calls == [A]


def test_new_job_adopts_reviews_even_after_shots_only_job(client, fakes):
    fakes.fetch_errors[B] = ScriptError("blocked", "유튜브가 요청을 막았습니다")
    old = start_add(client, [url(A), url(B)])
    old = wait_for(client, old["id"], finished)
    assert old["status"] == "stopped" and in_review(0)(old)
    fakes.pending = [C]
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    assert status == 200
    wait_for(client, body["job"]["id"], finished)

    new = start_add(client, [url(D)])

    assert [i["id"] for i in new["items"]] == [A, url(D)]
    assert in_review(0)(new) and new["items"][0]["preview"]["summary"] == "한 줄 요약"
    moved = snap(client, old["id"])["items"][0]
    assert (moved["state"], moved["message"]) == ("skipped", "새 작업으로 옮김")


def test_add_while_cancelled_job_winds_down_says_stopping(client, fakes):
    entered, release = gate(fakes, A)
    job = start_add(client, [url(A)])
    assert entered.wait(5)
    status, _ = client.json("POST", f"/api/jobs/{job['id']}/cancel", {})
    assert status == 200

    status, body = client.json("POST", "/api/add", {"category": CAT, "links": [url(B)]})

    release.set()
    assert status == 400 and body["error"] == "job_stopping"
    assert body["message"] == "작업을 멈추는 중입니다. 잠시 뒤 다시 넣어 주세요"
    wait_for(client, job["id"], finished)


def test_broken_item_options_fail_only_that_video(client, fakes):
    entered, release = gate(fakes, A)
    job = start_add(client, [url(A), url(B)])
    assert entered.wait(5)
    live = client.app.jobs.get(job["id"])
    broken = dict(jobs.new_item(url(C), url=url(C)), opts={"category": CAT, "bogus": True})
    assert live.append_queued([broken], "collect", api_add._same_video) == [2]
    release.set()

    job = wait_for(client, job["id"], lambda j: j["items"][2]["state"] == "failed" and in_review(1)(j))

    assert in_review(0)(job) and job["status"] == "awaiting"
    assert job["items"][2]["message"].startswith("예상치 못한 오류: TypeError")


def test_reanalyze_queues_and_returns_to_review(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    status, data = review(client, job["id"], A, action="reanalyze", feedback="훅이 더 길어요")
    assert status == 200
    item = data["job"]["items"][0]
    assert item["state"] in {"waiting", "running"} and item["step"] == "analyze" and item["ask"] is None
    wait_for(client, job["id"], lambda j: in_review(0)(j) and len(fakes.analyze_asks()) == 2)
    first, second = fakes.analyze_asks()
    assert "사용자 의견" not in first[2]
    assert second[2] == first[2] + "\n사용자 의견: 훅이 더 길어요"
    review(client, job["id"], A, action="skip")
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "skipped" and fakes.writes == []


def test_reanalyze_failure_returns_to_review_with_old_preview(client, fakes):
    fakes.ask_errors = [None, ScriptError("claude_failed", "시간 초과")]
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    review(client, job["id"], A, action="reanalyze", feedback="")
    job = wait_for(client, job["id"], lambda j: in_review(0)(j) and len(fakes.analyze_asks()) == 2)
    item = job["items"][0]
    assert item["preview"]["summary"] == "한 줄 요약" and "시간 초과" in item["message"]
    assert steps(job)["analyze"]["state"] == "failed"


def test_reanalyze_refused_on_stopped_job(client, fakes):
    fakes.fetch_errors[B] = ScriptError("blocked", "유튜브가 요청을 막았습니다")
    job = start_add(client, [url(A), url(B)])
    job = wait_for(client, job["id"], finished)
    status, body = review(client, job["id"], A, action="reanalyze", feedback="다시")
    assert status == 400 and body["error"] == "job_stopped"
    assert body["message"] == "작업이 멈춰 다시 분석할 수 없습니다. 링크를 다시 넣어 주세요"
    assert in_review(0)(snap(client, job["id"]))
    assert len(fakes.analyze_asks()) == 1


def test_shots_after_save_on_stopped_job_is_skipped(client, fakes):
    fakes.fetch_errors[B] = ScriptError("blocked", "유튜브가 요청을 막았습니다")
    job = start_add(client, [url(A), url(B)], shots=True)
    job = wait_for(client, job["id"], finished)
    status, body = review(client, job["id"], A, action="save")
    assert status == 200
    item = body["job"]["items"][0]
    assert item["state"] == "saved" and "샷 분류는 작업이 멈춰 건너뛰었어요" in item["message"]
    assert item["timing"]["steps"]["shots"]["state"] == "skipped"
    assert fakes.shot_ctx_calls == [] and [w["vid"] for w in fakes.writes] == [A]


def test_review_errors(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    status, body = review(client, job["id"], A, action="explode")
    assert status == 400 and body["error"] == "bad_action"
    status, body = review(client, job["id"], A, action="save", analysis=[1])
    assert status == 400 and body["error"] == "bad_request"
    status, body = review(client, job["id"], "nope", action="save")
    assert status == 400 and body["error"] == "item_not_found"
    status, body = review(client, "missing", A, action="save")
    assert status == 400 and body["error"] == "job_not_found"
    assert in_review(0)(snap(client, job["id"])) and fakes.writes == []
    review(client, job["id"], A, action="save")
    wait_for(client, job["id"], finished)
    status, body = review(client, job["id"], A, action="skip")
    assert status == 400 and body["error"] == "not_awaiting"


def test_review_routes_refuse_non_collect_job(client, fakes):
    fakes.pending = [A]
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    job = wait_for(client, body["job"]["id"], finished)
    status, body = review(client, job["id"], A, action="save")
    assert status == 400 and body["error"] == "not_awaiting"
    status, body = save_all(client, job["id"])
    assert status == 200 and body["results"] == []


def test_old_decision_route_refuses_collect(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], in_review(0))
    status, body = decide(client, job["id"], A, action="save")
    assert status == 400 and body["error"] == "bad_action"
    assert "/review/" in body["message"]
    assert in_review(0)(snap(client, job["id"])) and fakes.writes == []


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
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "stopped" and job["message"] == "유튜브가 요청을 막았습니다"
    assert [i["state"] for i in job["items"]] == ["awaiting", "failed", "skipped"]   # 확인 대기는 남는다
    assert job["items"][1]["message"] == "유튜브가 요청을 막았습니다"
    assert job["items"][2]["message"] == "차단으로 중단"
    assert [c["vid"] for c in fakes.fetch_calls] == [A, B]
    assert job["summary"]["failed"] == 1 and job["summary"]["skipped"] == 1
    status, body = review(client, job["id"], A, action="save")
    assert status == 200 and body["job"]["items"][0]["state"] == "saved"


def test_no_transcript_default_runs_stt_without_asking(client, fakes):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], in_review(0))
    item = job["items"][0]
    assert item["id"] == A and item["title"] == "무자막" and item["preview"] is not None
    assert fakes.stt_calls == [A]
    assert len(fakes.analyze_asks()) == 1


def test_no_transcript_screen_uses_read_tool(client, fakes):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    job = start_add(client, [url(A)], no_transcript="screen")
    job = wait_for(client, job["id"], in_review(0))
    assert fakes.stt_calls == []
    assert fakes.analyze_asks()[0][3]["files"]
    assert api_add.SCREEN_HINT in fakes.analyze_asks()[0][2]
    assert job["items"][0]["message"] == "자막이 없어 화면만 보고 분석합니다"


def test_no_transcript_option_skips_without_asking(client, fakes):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    job = start_add(client, [url(A), url(B)], no_transcript="skip")
    job = wait_for(client, job["id"], in_review(1))
    item = job["items"][0]
    assert (item["state"], item["ask"], item["message"]) == ("skipped", None, "자막이 없어 건너뜀")
    assert fakes.stt_calls == [] and len(fakes.analyze_asks()) == 1


def test_stt_unavailable_falls_back_to_screen(client, fakes):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    fakes.stt_error = ScriptError("stt_unavailable", "faster-whisper가 없습니다")
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], in_review(0))
    assert "faster-whisper가 없습니다" in job["items"][0]["message"]
    assert api_add.SCREEN_HINT in fakes.analyze_asks()[0][2]


def test_add_claude_failure_marks_video_and_continues(client, fakes):
    fakes.ask_errors = [ScriptError("claude_failed", "Claude 응답 시간 초과(600초)")]
    job = start_add(client, [url(A), url(B)])
    job = wait_for(client, job["id"], item_state(1, "awaiting"))
    assert job["items"][0]["state"] == "failed"
    assert job["items"][0]["message"] == "Claude 응답 시간 초과(600초)"
    assert job["items"][1]["preview"] is not None


@pytest.mark.parametrize("code", ["claude_missing", "claude_login"])
def test_claude_unusable_stops_remaining(client, fakes, code):
    fakes.ask_errors = [ScriptError(code, "Claude에 로그인되어 있지 않습니다")]
    job = start_add(client, [url(A), url(B), url(C)])
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "stopped"
    assert [i["state"] for i in job["items"]] == ["failed", "skipped", "skipped"]
    assert job["items"][1]["message"] == api_add.MSG_CLAUDE_STOP
    assert [c["vid"] for c in fakes.fetch_calls] == [A]       # 남은 영상은 받지 않는다


def test_unexpected_error_fails_only_that_video(client, fakes, monkeypatch):
    real_context = fakes.context

    def flaky(vid, category):
        if vid == A:
            raise KeyError("meta")
        return real_context(vid, category)

    monkeypatch.setattr(videomd, "context", flaky)
    job = start_add(client, [url(A), url(B)])
    job = wait_for(client, job["id"], item_state(1, "awaiting"))
    assert job["items"][0]["state"] == "failed"
    assert job["items"][0]["message"] == "예상치 못한 오류: KeyError: 'meta'"
    assert job["items"][1]["preview"] is not None


@pytest.mark.parametrize("path,body", [
    ("/api/add", {"category": CAT, "links": [url(A)]}),
    ("/api/add/shots-only", {"category": CAT}),
])
def test_add_routes_require_claude_before_job(client, fakes, monkeypatch, path, body):
    def missing():
        raise ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")

    monkeypatch.setattr(claude_cli, "find_claude", missing)
    fakes.pending = [A]
    status, resp = client.json("POST", path, body)
    assert status == 400 and resp["error"] == "claude_missing"
    assert fakes.fetch_calls == [] and fakes.asks == []
    status, resp = client.json("GET", "/api/jobs/current?kind=collect")
    assert resp["job"] is None


def test_cuts_failure_keeps_going(client, fakes, monkeypatch):
    def broken(vid, force=False, progress=None):
        raise ScriptError("download_failed", "영상 파일을 받지 못했습니다.")
    monkeypatch.setattr(cuts, "cut_stats", broken)
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    assert "영상 파일을 받지 못했습니다." in job["items"][0]["message"]
    assert job["items"][0]["frames"] == []


def test_force_and_refetch_flags(client, fakes):
    job = start_add(client, [url(A)], force=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    review(client, job["id"], A, action="save")
    wait_for(client, job["id"], finished)
    assert fakes.fetch_calls[-1]["reanalyze"] is True and fakes.fetch_calls[-1]["force"] is False
    assert fakes.cut_calls[-1] == (A, False) and fakes.writes[-1]["force"] is True

    job = start_add(client, [url(B)], refetch=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    review(client, job["id"], B, action="save")
    wait_for(client, job["id"], finished)
    assert fakes.fetch_calls[-1]["force"] is True
    assert fakes.cut_calls[-1] == (B, True) and fakes.writes[-1]["force"] is True


def test_shots_option_runs_after_save(client, fakes, tmp_path):
    job = start_add(client, [url(A)], shots=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    status, body = review(client, job["id"], A, action="save")
    assert status == 200 and body["job"]["items"][0]["step"] in {"shots", "done"}
    assert body["job"]["items"][0]["state"] in {"waiting", "running", "saved"}
    assert [w["vid"] for w in fakes.writes] == [A]          # md는 요청을 받은 쪽이 바로 쓴다
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "saved"
    assert fakes.shot_ctx_calls == [A]
    vid, labels = fakes.applied[0]
    assert vid == A and labels[0]["size"] == "바스트"
    rules, _, _, kw = fakes.asks[-1]
    assert rules == [claude_cli.SKILLS_DIR / "yt-shots" / "SKILL.md"]
    assert kw["files"] == [tmp_path / f"workspace/frames/{A}/scene001_0.5s.jpg"]
    assert not (tmp_path / "workspace" / "tmp" / f"shots_{A}.json").exists()


def test_shots_no_frames_keeps_saved(client, fakes, monkeypatch):
    def no_frames(vid, category):
        raise ScriptError("no_frames", "대표 프레임이 없습니다")
    monkeypatch.setattr(shots, "context", no_frames)
    job = start_add(client, [url(A)], shots=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    review(client, job["id"], A, action="save")
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


def test_shots_only_stops_when_claude_unusable(client, fakes):
    fakes.pending = [A, B]
    fakes.ask_errors = [ScriptError("claude_login", "로그인 필요")]
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    job = wait_for(client, body["job"]["id"], finished)
    assert job["status"] == "stopped"
    assert [i["state"] for i in job["items"]] == ["failed", "skipped"]
    assert len(fakes.asks) == 1


# ---- API / 잠금 -----------------------------------------------------------------

def test_shots_only_refused_while_collect_runs(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    assert status == 400 and body["error"] == "job_running"
    assert body["message"] == "이미 수집 중인 작업이 있습니다"


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
    ({"category": CAT, "links": ["x"], "no_transcript": "ask"}, "bad_request"),
])
def test_add_rejects_bad_request(client, body, code):
    status, resp = client.json("POST", "/api/add", body)
    assert status == 400 and resp["error"] == code


def test_missing_job(client, fakes):
    status, body = client.json("GET", "/api/jobs/missing")
    assert status == 400 and body["error"] == "job_not_found"


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


# ---- 수정 1차: 종료 시 작업 정리 · 결정 검증 -------------------------------------------

def test_cancel_during_cuts_stops_before_analysis(client, fakes, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_cuts(vid, force=False, progress=None):
        entered.set()
        assert release.wait(5)
        return {"id": vid, "status": "analyzed", "frames": []}
    monkeypatch.setattr(cuts, "cut_stats", slow_cuts)
    job = start_add(client, [url(A), url(B)])
    assert entered.wait(5)
    stopper = threading.Thread(target=client.app.jobs.shutdown, kwargs={"timeout": 5})
    stopper.start()
    release.set()
    stopper.join(5)
    job = snap(client, job["id"])
    assert job["status"] == "stopped"
    assert [i["state"] for i in job["items"]] == ["skipped", "skipped"]
    assert fakes.asks == [] and fakes.writes == []
    assert [c["vid"] for c in fakes.fetch_calls] == [A]


def test_serve_waits_for_jobs_on_exit(monkeypatch, tmp_path, no_browser):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    calls = []
    real = jobs.JobManager.shutdown

    def record(self, timeout=None):
        calls.append(timeout)
        real(self, timeout=timeout)
    monkeypatch.setattr(jobs.JobManager, "shutdown", record)
    fg = threading.Thread(target=lambda: ui_main.main(["--no-open"]), daemon=True)
    fg.start()
    lock = tmp_path / "workspace" / server.LOCK
    deadline = time.monotonic() + 5
    while not lock.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ui_main.main(["--stop"]) == {"status": "stopped"}
    fg.join(5)
    assert not fg.is_alive()
    assert calls[0] == 0                                   # /api/shutdown: 응답을 막지 않는다
    assert calls[-1] == ui_main.JOB_STOP_TIMEOUT_SEC       # _serve finally: 작업 정리를 기다린다


def _scenes(n):
    return {"cuts": [], "duration_sec": 60,
            "scenes": [{"start": i, "end": i + 1, "frame": f"workspace/frames/{A}/s{i:02d}.jpg"} for i in range(n)]}


def test_analyze_attaches_only_examples_when_speech_is_enough(client, fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(videomd, "load_cuts", lambda vid: _scenes(12))
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    assert fakes.analyze_asks()[0][3]["files"] == [tmp_path / "refs/ref_food/videos/ex.md"]
    assert api_add.SCREEN_HINT not in fakes.analyze_asks()[0][2]


def test_analyze_attaches_at_most_8_frames_with_first_and_last_when_screen(client, fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(videomd, "load_cuts", lambda vid: _scenes(30))
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    job = start_add(client, [url(A)], no_transcript="screen")
    wait_for(client, job["id"], in_review(0))
    files = fakes.analyze_asks()[0][3]["files"]
    frames = [f for f in files if f.suffix == ".jpg"]
    assert len(frames) == 8
    assert frames[0] == tmp_path / f"workspace/frames/{A}/s00.jpg" and frames[-1] == tmp_path / f"workspace/frames/{A}/s29.jpg"
    assert files[-1] == tmp_path / "refs/ref_food/videos/ex.md"


def test_analyze_attaches_frames_when_context_is_sparse(client, fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(videomd, "load_cuts", lambda vid: _scenes(3))
    monkeypatch.setattr(videomd, "context", lambda vid, cat: {**fakes.context(vid, cat), "sparse": True})
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    files = fakes.analyze_asks()[0][3]["files"]
    assert [f.name for f in files if f.suffix == ".jpg"] == ["s00.jpg", "s01.jpg", "s02.jpg"]
    assert api_add.SCREEN_HINT in fakes.analyze_asks()[0][2]


# ---- 단계별 진행 시간(timing) ---------------------------------------------------------

def records(tmp_path):
    path = tmp_path / "workspace" / "timings.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def steps(job, i=0):
    return job["items"][i]["timing"]["steps"]


def engine_key(step):
    return f"{step}:{llm.config()['engine']}"


def test_collect_item_has_timing_rows(client, fakes):
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    timing = job["items"][0]["timing"]
    assert list(timing["steps"]) == ["fetch", "cuts", "analyze"]
    assert timing["video_sec"] == 60
    assert [s["state"] for s in timing["steps"].values()] == ["done", "done", "done"]
    review(client, job["id"], A, action="save")
    job = wait_for(client, job["id"], finished)
    assert [s["state"] for s in steps(job).values()] == ["done", "done", "done"]


def test_timing_rows_include_shots_when_selected(client, fakes, tmp_path):
    job = start_add(client, [url(A)], shots=True)
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    assert list(steps(job)) == ["fetch", "cuts", "analyze", "shots"]
    assert steps(job)["shots"]["state"] == "pending"
    review(client, job["id"], A, action="save")
    job = wait_for(client, job["id"], finished)
    assert steps(job)["shots"]["state"] == "done"
    assert len(records(tmp_path)[engine_key("shots")]) == 1


def test_timing_records_written(client, fakes, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "load_raw", lambda vid: {
        "id": vid, "meta": {"duration": 60}, "transcript": {"segments": [{"text": "안녕 하세요"}, {"text": "네"}]}})
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    review(client, job["id"], A, action="save")
    wait_for(client, job["id"], finished)
    data = records(tmp_path)
    assert {"fetch", "cuts", engine_key("analyze")} <= set(data)
    assert data["cuts"][0]["video_sec"] == 60
    analyze = data[engine_key("analyze")][0]
    assert analyze["text_chars"] == len("안녕 하세요") + len("네")
    assert analyze["frames"] == 0            # 자막이 충분하면 장면 프레임은 첨부하지 않는다


def test_cached_cuts_not_recorded(client, fakes, tmp_path):
    fakes.cut_status = "cached"
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    cuts_row = steps(job)["cuts"]
    assert cuts_row["state"] == "done" and cuts_row["expected"] == 0
    assert "cuts" not in records(tmp_path)


def test_cached_fetch_not_recorded(client, fakes, tmp_path):
    fakes.fetch_status[A] = "cached"
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    assert steps(job)["fetch"]["state"] == "done" and steps(job)["fetch"]["expected"] == 0
    assert "fetch" not in records(tmp_path)


def test_reanalyze_restarts_analyze_row(client, fakes, tmp_path):
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    first = steps(job)["analyze"]["started_at"]
    time.sleep(0.4)                                  # 확인 대기
    review(client, job["id"], A, action="reanalyze", feedback="")
    job = wait_for(client, job["id"], lambda j: in_review(0)(j) and len(fakes.asks) == 2)
    row = steps(job)["analyze"]
    assert row["state"] == "done"
    assert row["started_at"] >= first + 0.4
    assert row["elapsed"] < 0.3                      # 확인 대기 시간은 넣지 않는다
    entries = records(tmp_path)[engine_key("analyze")]
    assert len(entries) == 2 and all(e["elapsed_sec"] < 0.3 for e in entries)


def test_shots_only_has_single_row(client, fakes, tmp_path):
    fakes.pending = [A]
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    job = wait_for(client, body["job"]["id"], finished)
    timing = job["items"][0]["timing"]
    assert list(timing["steps"]) == ["shots"]
    assert timing["video_sec"] == 60 and timing["steps"]["shots"]["state"] == "done"
    assert len(records(tmp_path)[engine_key("shots")]) == 1


def test_shots_only_failure_marks_row_failed(client, fakes, tmp_path):
    fakes.pending = [A]
    fakes.ask_errors = [ScriptError("claude_failed", "실패")]
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    job = wait_for(client, body["job"]["id"], finished)
    assert steps(job)["shots"]["state"] == "failed"
    assert engine_key("shots") not in records(tmp_path)


def test_failed_step_not_recorded(client, fakes, tmp_path, monkeypatch):
    def broken(vid, force=False, progress=None):
        raise ScriptError("download_failed", "영상 파일을 받지 못했습니다.")
    monkeypatch.setattr(cuts, "cut_stats", broken)
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    assert steps(job)["cuts"]["state"] == "failed"
    data = records(tmp_path)
    assert "cuts" not in data and "fetch" in data


def test_failed_analysis_marks_row_failed(client, fakes, tmp_path):
    fakes.ask_errors = [ScriptError("claude_failed", "Claude 응답 시간 초과(600초)")]
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], finished)
    assert steps(job)["analyze"]["state"] == "failed"
    assert engine_key("analyze") not in records(tmp_path)


def test_wait_phase_is_shown_but_excluded_from_elapsed(client, fakes, tmp_path):
    in_wait, release = threading.Event(), threading.Event()

    def hook(progress):
        progress("wait", None, "차단 방지 대기 1초")
        in_wait.set()
        assert release.wait(5)
        progress("download", 0.5, "영상 받는 중 50%")

    fakes.cut_hook = hook
    job = start_add(client, [url(A)])
    assert in_wait.wait(5)
    row = steps(snap(client, job["id"]))["cuts"]
    assert row["phase_text"] == "차단 방지 대기 1초" and "paused_at" in row
    time.sleep(0.4)
    release.set()
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    assert steps(job)["cuts"]["elapsed"] < 0.3
    assert records(tmp_path)["cuts"][0]["elapsed_sec"] < 0.3


def test_wait_still_paused_when_call_returns_is_resumed(client, fakes, tmp_path):
    fakes.cut_hook = lambda progress: progress("wait", None, "차단 방지 대기 1초")
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], item_state(0, "awaiting"))
    row = steps(job)["cuts"]
    assert row["state"] == "done" and "paused_at" not in row
    assert "cuts" in records(tmp_path)


def test_fetch_gets_progress_callback(client, fakes):
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    assert callable(fakes.progress["fetch"]) and callable(fakes.progress["cuts"])


def test_no_transcript_stt_is_new_fetch_run(client, fakes, tmp_path):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막",
                                        duration_sec=120, raw_status="fetched")
    fakes.raw_cached = False                         # 길이는 no_transcript 정보에서 얻는다
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], in_review(0))
    assert job["items"][0]["timing"]["video_sec"] == 120
    assert "prior_elapsed" in steps(job)["fetch"]  # 자막 받기 -> 음성 인식이 한 줄로 합쳐 보인다
    assert callable(fakes.progress["stt"])
    assert steps(job)["fetch"]["state"] == "done"
    data = records(tmp_path)
    assert data["fetch"][0]["elapsed_sec"] < 0.25 and data["stt"][0]["elapsed_sec"] < 0.25
    assert data["stt"][0]["video_sec"] == 120


def test_stt_unavailable_marks_fetch_row_skipped(client, fakes, tmp_path):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    fakes.stt_error = ScriptError("stt_unavailable", "faster-whisper가 없습니다")
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], in_review(0))
    assert steps(job)["fetch"]["state"] == "skipped"
    assert "stt" not in records(tmp_path)


def test_timing_errors_never_fail_collection(client, fakes, monkeypatch):
    from scripts.ui import progress

    def boom(*a, **kw):
        raise RuntimeError("timing broke")

    monkeypatch.setattr(progress.ItemTiming, "done", boom)
    monkeypatch.setattr(progress.ItemTiming, "phase", boom)
    fakes.cut_hook = lambda p: p("download", 0.1, "영상 받는 중 10%")
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    review(client, job["id"], A, action="save")
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "saved"


def test_timing_constructor_error_never_fails_collection(client, fakes, monkeypatch):
    from scripts.ui import progress

    def boom(*a, **kw):
        raise RuntimeError("timing broke")

    monkeypatch.setattr(progress.ItemTiming, "__init__", boom)
    job = start_add(client, [url(A)])
    wait_for(client, job["id"], item_state(0, "awaiting"))
    review(client, job["id"], A, action="save")
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "saved"


def test_engine_falls_back_to_none_when_llm_config_broken(monkeypatch):
    def broken():
        raise ScriptError("llm_not_configured", "알 수 없는 LLM 엔진입니다")

    monkeypatch.setattr(llm, "config", broken)
    assert api_add._timing_engine() is None



def test_shots_no_frames_marks_shots_row_skipped(client, fakes, monkeypatch, tmp_path):
    def no_frames(vid, category):
        raise ScriptError("no_frames", "대표 프레임이 없습니다")
    monkeypatch.setattr(shots, "context", no_frames)
    job = start_add(client, [url(A)], shots=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    review(client, job["id"], A, action="save")
    job = wait_for(client, job["id"], finished)
    assert steps(job)["shots"]["state"] == "skipped" and steps(job)["shots"]["expected"] == 0


def test_shots_only_no_frames_marks_row_skipped(client, fakes, monkeypatch):
    def no_frames(vid, category):
        raise ScriptError("no_frames", "대표 프레임이 없습니다")
    monkeypatch.setattr(shots, "context", no_frames)
    fakes.pending = [A]
    status, body = client.json("POST", "/api/add/shots-only", {"category": CAT})
    job = wait_for(client, body["job"]["id"], finished)
    assert job["items"][0]["state"] == "skipped"
    assert steps(job)["shots"]["state"] == "skipped"


def test_exists_skips_remaining_rows(client, fakes):
    fakes.fetch_status[A] = "exists"
    job = start_add(client, [url(A)])
    job = wait_for(client, job["id"], finished)
    assert [s["state"] for s in steps(job).values()] == ["done", "skipped", "skipped"]


def test_skip_at_confirm_skips_shots_row(client, fakes):
    job = start_add(client, [url(A)], shots=True)
    wait_for(client, job["id"], item_state(0, "awaiting"))
    review(client, job["id"], A, action="skip")
    job = wait_for(client, job["id"], finished)
    assert steps(job)["shots"]["state"] == "skipped"


def test_failed_item_skips_pending_rows(client, fakes):
    fakes.ask_errors = [ScriptError("claude_failed", "실패")]
    job = start_add(client, [url(A)], shots=True)
    job = wait_for(client, job["id"], finished)
    assert steps(job)["analyze"]["state"] == "failed" and steps(job)["shots"]["state"] == "skipped"


def test_no_transcript_skip_skips_remaining_rows(client, fakes):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막")
    job = start_add(client, [url(A)], no_transcript="skip")
    job = wait_for(client, job["id"], finished)
    assert [s["state"] for s in steps(job).values()] == ["done", "skipped", "skipped"]


def test_no_transcript_raw_status_detail_skips_record(client, fakes, tmp_path):
    fakes.fetch_errors[A] = ScriptError("no_transcript", "자막이 없습니다", id=A, title="무자막", raw_status="cached")
    job = start_add(client, [url(A)], no_transcript="screen")
    job = wait_for(client, job["id"], in_review(0))
    assert steps(job)["fetch"]["expected"] == 0
    assert "fetch" not in records(tmp_path)


class _CountingJob:
    def __init__(self):
        self.updates = []

    def update(self, index, **fields):
        self.updates.append(fields)


def test_wait_phase_publishes_once_per_report(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    job = _CountingJob()
    clock = api_add._StepClock(job, 0, ["cuts"], None)
    clock.start("cuts")
    before = len(job.updates)
    with clock.progress() as report:
        report("wait", None, "차단 방지 대기 3초")
        assert len(job.updates) == before + 1           # pause와 phase가 한 번에 게시된다
        assert "paused_at" in job.updates[-1]["timing"]["steps"]["cuts"]
        report("download", None, "영상 받는 중")
        assert len(job.updates) == before + 2
        assert "paused_at" not in job.updates[-1]["timing"]["steps"]["cuts"]


# ---- 링크 미리 확인 · 샷 분류 보강 개수 -----------------------------------------------------

D, E, F = "DDDDDDDDDDD", "EEEEEEEEEEE", "FFFFFFFFFFF"


def put_md(home, vid, *, role="ref", category=CAT, title=None):
    path = home / "refs" / f"ref_{category}" / "videos" / f"{vid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": vid, "role": role, "title": title or f"제목 {vid}"}
    path.write_bytes(refstore.join_frontmatter(fm, "## 내 메모\n").encode("utf-8"))
    return path


def test_check_links_classifies_rows(client, fakes, tmp_path):
    put_md(tmp_path, A, role="ref")
    put_md(tmp_path, C, role="mine")
    links = f"{url(A)}\n{url(B)}\n{url(B)}\nhttps://www.youtube.com/@channel\n{url(C)}\n\n"
    status, data = client.json("POST", "/api/add/check", {"category": CAT, "role": "ref", "links": links})
    assert status == 200
    assert [r["status"] for r in data["rows"]] == ["exists", "new", "dup", "invalid", "other_role"]
    assert data["rows"][0]["title"] == f"제목 {A}" and data["rows"][4]["title"] == f"제목 {C}"
    assert data["rows"][1]["id"] == B and data["rows"][1]["title"] is None
    assert data["rows"][3]["id"] is None and data["rows"][2]["id"] == B
    assert data["rows"][0]["link"] == url(A)
    assert data["count"] == 1


def test_check_links_force_turns_exists_into_reanalyze(client, fakes, tmp_path):
    put_md(tmp_path, A, role="ref")
    put_md(tmp_path, C, role="mine")
    body = {"category": CAT, "role": "ref", "links": [url(A), url(C)], "force": True}
    status, data = client.json("POST", "/api/add/check", body)
    assert [r["status"] for r in data["rows"]] == ["reanalyze", "other_role"]
    assert data["count"] == 1


def test_check_links_other_category_counts_as_new(client, fakes, tmp_path):
    put_md(tmp_path, D, role="ref", category="travel")
    status, data = client.json("POST", "/api/add/check", {"category": CAT, "role": "ref", "links": [url(D)]})
    assert data["rows"][0]["status"] == "new" and data["count"] == 1


def test_check_links_md_without_title_gives_no_title(client, fakes, tmp_path):
    path = tmp_path / "refs" / f"ref_{CAT}" / "videos" / f"{E}.md"
    path.write_bytes(refstore.join_frontmatter({"id": E, "role": "ref"}, "## 내 메모\n").encode("utf-8"))
    status, data = client.json("POST", "/api/add/check", {"category": CAT, "role": "ref", "links": [url(E)]})
    assert status == 200
    assert data["rows"][0]["status"] == "exists" and data["rows"][0]["title"] is None


def test_check_links_rejects_bad_category(client):
    status, data = client.json("POST", "/api/add/check", {"category": "../x", "links": []})
    assert status == 400


def test_check_links_rejects_bad_role(client):
    status, data = client.json("POST", "/api/add/check", {"category": CAT, "role": "x", "links": []})
    assert status == 400


def test_shots_pending_count(client, fakes):
    fakes.pending = [A, B]
    status, data = client.json("GET", f"/api/add/shots-pending?category={CAT}")
    assert status == 200 and data == {"count": 2}


def test_shots_pending_rejects_bad_category(client):
    status, data = client.json("GET", "/api/add/shots-pending?category=nope")
    assert status == 400


def test_check_links_broken_md_does_not_fail_other_rows(client, fakes, tmp_path):
    same = tmp_path / "refs" / f"ref_{CAT}" / "videos" / f"{E}.md"
    same.write_text("프런트매터 없음\n", encoding="utf-8")
    other = tmp_path / "refs" / "ref_travel" / "videos" / f"{F}.md"
    other.parent.mkdir(parents=True)
    other.write_text("프런트매터 없음\n", encoding="utf-8")
    links = [url(E), url(F), url(B)]
    status, data = client.json("POST", "/api/add/check", {"category": CAT, "role": "ref", "links": links})
    assert status == 200
    assert [r["status"] for r in data["rows"]] == ["exists", "new", "new"]
    assert data["rows"][0]["title"] is None and data["count"] == 2


def test_check_links_missing_role_counts_as_exists(client, fakes, tmp_path):
    path = tmp_path / "refs" / f"ref_{CAT}" / "videos" / f"{E}.md"
    path.write_bytes(refstore.join_frontmatter({"id": E, "title": "역할 없음"}, "## 내 메모\n").encode("utf-8"))
    status, data = client.json("POST", "/api/add/check", {"category": CAT, "role": "mine", "links": [url(E)]})
    assert data["rows"][0]["status"] == "exists" and data["rows"][0]["title"] == "역할 없음"
