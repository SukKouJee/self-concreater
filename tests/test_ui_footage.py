"""Task 3: 촬영 영상 API — 폴더 연결, 처리 작업(footage), 판정 조작, 새 컷 넣기, 미디어 서빙."""
import json
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

from scripts import claude_cli, conti, footage, llm
from scripts import conti_history as ch
from scripts import footage_match as fm
from scripts.common import ScriptError
from scripts.ui import api_footage
from scripts.ui.jobs import Job, new_item
from tests.test_conti import conti_file, draft_md, home  # noqa: F401 - fixtures
from tests.test_footage_match import FakeLLM, answer, m
from tests.test_ui_add import wait_for
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture

VIDEO_BYTES = bytes(range(256)) * 40          # 10240 bytes


@pytest.fixture
def saved(draft_md, home):  # noqa: F811
    return home / conti.save(draft_md, conti_file(home))["path"]


@pytest.fixture
def rel(saved, home):  # noqa: F811
    return saved.relative_to(home).as_posix()


@pytest.fixture
def claude_ok(monkeypatch):
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")


@pytest.fixture
def client(home, claude_ok, no_browser):  # noqa: F811
    srv, app, thread = _start(home)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def shoot(tmp_path, name="촬영 1일차", files=("a.mp4", "b.MOV")):
    """원본 촬영 폴더(작업 폴더 밖). 하위 폴더와 영상이 아닌 파일도 섞는다."""
    folder = tmp_path.parent / f"{tmp_path.name}-src" / name
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        (folder / f).write_bytes(VIDEO_BYTES)
    (folder / "memo.txt").write_text("x", encoding="utf-8")
    (folder / "sub").mkdir(exist_ok=True)
    (folder / "sub" / "c.mp4").write_bytes(VIDEO_BYTES)
    return folder


def q(value):
    return quote(value, safe="")


def media(client, rel, fid, **kw):
    return client.call("GET", f"/api/footage/media/{fid}?path={q(rel)}", **kw)


def link(client, rel, folder):
    status, body = client.json("POST", "/api/footage/folders", {"path": rel, "folder": str(folder)})
    assert status == 200, body
    return body


class FakeProcess:
    """footage.process_file 대역: scenes.json·사진을 실제 캐시 자리에 쓰고 결과를 돌려준다."""

    def __init__(self, transcripts=None, unreadable=()):
        self.calls = []
        self.transcripts = transcripts or {}
        self.unreadable = set(unreadable)
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def __call__(self, path, progress=None, run_stt=True):
        path = Path(path)
        self.calls.append(path.name)
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        fid = footage.fingerprint(path)
        base = footage.footage_dir(fid)
        (base / "frames").mkdir(parents=True, exist_ok=True)
        (base / "montage").mkdir(parents=True, exist_ok=True)
        if progress:
            progress("scenes", 0.5, "장면 확인 중 0:05 / 0:10")
        name = path.name
        if name in self.unreadable:
            record = {"schema": 1, "fid": fid, "name": name, "status": "unreadable", "reason": "열 수 없음",
                      "duration_sec": None, "scenes": []}
        else:
            (base / "frames" / "s000_0.jpg").write_bytes(b"\xff\xd8frame")
            (base / "montage" / "s000.jpg").write_bytes(b"\xff\xd8montage")
            record = {"schema": 1, "fid": fid, "name": name, "status": "done", "decoder": "pyav",
                      "duration_sec": 10.0, "scenes": [{"i": 0, "start": 0.0, "end": 10.0,
                                                        "frames": ["frames/s000_0.jpg"],
                                                        "montage": "montage/s000.jpg"}]}
        (base / "scenes.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if record["status"] == "done":
            segs = self.transcripts.get(name, [])
            (base / "transcript.json").write_text(json.dumps({"segments": segs}, ensure_ascii=False),
                                                  encoding="utf-8")
        return footage.cached_result(fid)


@pytest.fixture
def fake_process(monkeypatch):
    def install(**kw):
        f = FakeProcess(**kw)
        monkeypatch.setattr(footage, "process_file", f)
        return f
    return install


@pytest.fixture
def fake_llm(monkeypatch):
    def install(respond):
        f = FakeLLM(respond)
        monkeypatch.setattr(llm, "ask_validated", f)
        return f
    return install


def finished(job):
    return job["status"] not in ("running", "awaiting")


# --- 폴더 연결 ---------------------------------------------------------------------------

def test_add_folder_lists_files_and_ignores_subfolders(client, rel, tmp_path):
    folder = shoot(tmp_path)
    body = link(client, rel, folder)
    [f] = body["folders"]
    assert f["path"] == str(folder.resolve())
    assert f["files"] == 2 and f["processed"] == 0 and f["unreadable"] == 0 and f["missing"] is False
    assert f["pending"] == 2
    assert fm.load_state(rel)["folders"] == [str(folder.resolve())]


def test_add_folder_accepts_quoted_path(client, rel, tmp_path):
    folder = shoot(tmp_path)
    body = link(client, rel, f'  "{folder}"  ')
    assert body["folders"][0]["path"] == str(folder.resolve())


@pytest.mark.parametrize("bad, code", [("", "bad_request"), ("relative/dir", "bad_request"),
                                       (None, "bad_request")])
def test_add_folder_rejects_bad_input(client, rel, bad, code):
    status, body = client.json("POST", "/api/footage/folders", {"path": rel, "folder": bad})
    assert status == 400 and body["error"] == code


def test_add_folder_rejects_missing_and_file(client, rel, tmp_path):
    status, body = client.json("POST", "/api/footage/folders", {"path": rel, "folder": str(tmp_path / "없음")})
    assert status == 400 and body["error"] == "folder_not_found"
    afile = tmp_path / "x.mp4"
    afile.write_bytes(b"x")
    status, body = client.json("POST", "/api/footage/folders", {"path": rel, "folder": str(afile)})
    assert status == 400 and body["error"] == "folder_not_found"


def test_add_same_folder_twice_rejected(client, rel, tmp_path):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    status, body = client.json("POST", "/api/footage/folders", {"path": rel, "folder": str(folder) + "\\"})
    assert status == 400 and body["error"] == "folder_exists"


def test_remove_folder_keeps_results(client, rel, tmp_path):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    status, body = client.json("POST", "/api/footage/folders/remove", {"path": rel, "folder": str(folder)})
    assert status == 200 and body["folders"] == []
    status, body = client.json("POST", "/api/footage/folders/remove", {"path": rel, "folder": str(folder)})
    assert status == 400 and body["error"] == "folder_not_linked"


def test_get_footage_rejects_non_conti_path(client):
    status, body = client.json("GET", f"/api/footage?path={q('refs/x.md')}")
    assert status == 400 and body["error"] == "bad_path"


def test_get_footage_empty_state(client, rel, saved):
    status, body = client.json("GET", f"/api/footage?path={q(rel)}")
    assert status == 200, body
    assert body["folders"] == [] and body["suggestions"] == [] and body["stt_missing"] is False
    assert [c["id"] for c in body["cuts"]] == [c["id"] for c in conti._load_conti(saved)["cuts"]]
    assert all(c["kind"] == "missing" for c in body["cuts"])
    assert body["summary"] == {"filmed": 0, "review": 0, "missing": 4, "total": 4}
    assert body["outputs"] == {"remaining_html": None, "csv": None, "clips_dir": None}
    assert body["job"] is None


def test_folder_marked_missing_when_drive_gone(client, rel, tmp_path):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    for f in folder.iterdir():
        if f.is_file():
            f.unlink()
    (folder / "sub" / "c.mp4").unlink()
    (folder / "sub").rmdir()
    folder.rmdir()
    body = client.json("GET", f"/api/footage?path={q(rel)}")[1]
    [f] = body["folders"]
    assert f["missing"] is True and f["files"] == 0


# --- 처리 작업 ---------------------------------------------------------------------------

def say_responder(ids):
    """컷0은 a.mp4 말로 high."""
    def respond(payload, files):
        picks = {}
        offered = {c["id"] for c in payload["cuts"]}
        for c in payload.get("cuts", []):
            for cand in c.get("candidates", []):
                if c["id"] == ids[0] and c["id"] in offered:
                    picks[ids[0]] = [m(cand["fid"], cand["start"], cand["end"], "high", "대사가 같아요")]
        return answer(picks)
    return respond


def test_process_job_processes_files_then_matches(client, rel, saved, tmp_path, fake_process, fake_llm):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    ids = [c["id"] for c in conti._load_conti(saved)["cuts"]]
    proc = fake_process(transcripts={"a.mp4": [{"start": 1.0, "end": 3.0, "text": "4단계 다 먹을 수 있을까요"}]},
                        unreadable={"b.MOV"})
    llm_fake = fake_llm(say_responder(ids))
    status, body = client.json("POST", "/api/footage/process", {"path": rel})
    assert status == 200, body
    job = body["job"]
    assert job["kind"] == "footage" and job["summary"]["path"] == rel
    assert [i["title"] for i in job["items"]] == ["a.mp4", "b.MOV", "맞추기"]
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "done", job
    a, b, match_item = job["items"]
    assert a["state"] == "done" and b["state"] == "failed" and "열 수 없음" in b["message"]
    assert match_item["state"] == "done"
    assert sorted(proc.calls) == ["a.mp4", "b.MOV"] and llm_fake.calls

    view = client.json("GET", f"/api/footage?path={q(rel)}")[1]
    cut0 = view["cuts"][0]
    fid_a = footage.fingerprint(folder / "a.mp4")
    assert cut0["kind"] == "auto" and cut0["choice"]["fid"] == fid_a
    assert cut0["choice"]["name"] == "a.mp4" and cut0["choice"]["missing_source"] is False
    assert view["summary"]["filmed"] == 1
    [f] = view["folders"]
    assert f["processed"] == 1 and f["unreadable"] == 1 and f["pending"] == 0 and f["duration_sec"] == 10.0
    assert view["job"]["id"] == job["id"]


def test_process_skips_cached_files_and_matches_all_linked(client, rel, saved, tmp_path, fake_process,
                                                          fake_llm, monkeypatch):
    day1 = shoot(tmp_path, "1일차", files=("a.mp4",))
    link(client, rel, day1)
    proc = fake_process()
    fake_llm(lambda payload, files: answer())
    job = client.json("POST", "/api/footage/process", {"path": rel})[1]["job"]
    wait_for(client, job["id"], finished)
    day2 = shoot(tmp_path, "2일차", files=("b.mp4",))
    link(client, rel, day2)
    seen = []
    real = fm.match

    def spy(path, results, now=None):
        seen.append(sorted(r["name"] for r in results))
        return real(path, results, now)
    monkeypatch.setattr(fm, "match", spy)
    job = client.json("POST", "/api/footage/process", {"path": rel})[1]["job"]
    assert job["items"][0]["state"] == "done" and job["items"][0]["message"] == "이미 처리함"
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "done"
    assert proc.calls == ["a.mp4", "b.mp4"]           # a는 두 번째에 다시 처리하지 않는다
    assert seen == [["a.mp4", "b.mp4"]]               # 맞추기에는 연결된 모든 파일을 넘긴다


def test_process_requires_linked_folder(client, rel):
    status, body = client.json("POST", "/api/footage/process", {"path": rel})
    assert status == 400 and body["error"] == "no_folders"


def test_process_checks_claude_before_work(client, rel, tmp_path, fake_process, monkeypatch):
    link(client, rel, shoot(tmp_path))
    proc = fake_process()

    def missing():
        raise ScriptError("claude_missing", "Claude Code CLI(claude)를 찾을 수 없습니다")
    monkeypatch.setattr(claude_cli, "find_claude", missing)
    status, body = client.json("POST", "/api/footage/process", {"path": rel})
    assert status == 400 and body["error"] == "claude_missing"
    assert proc.calls == [] and client.app.jobs.current("footage") is None


def test_process_cancel_between_files_skips_matching(client, rel, tmp_path, fake_process, fake_llm):
    link(client, rel, shoot(tmp_path))
    proc = fake_process()
    proc.gate = threading.Event()
    llm_fake = fake_llm(lambda payload, files: answer())
    job = client.json("POST", "/api/footage/process", {"path": rel})[1]["job"]
    assert proc.entered.wait(5)
    status, _ = client.json("POST", f"/api/jobs/{job['id']}/cancel")
    assert status == 200
    proc.gate.set()
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "stopped"
    assert proc.calls == ["a.mp4"] and llm_fake.calls == []
    assert job["items"][-1]["state"] == "skipped"


def test_process_match_failure_fails_job_with_message(client, rel, tmp_path, fake_process, fake_llm,
                                                      monkeypatch):
    link(client, rel, shoot(tmp_path, files=("a.mp4",)))
    fake_process(transcripts={"a.mp4": [{"start": 1.0, "end": 3.0, "text": "4단계 다 먹을 수 있을까요"}]})

    def boom(*a, **k):
        raise ScriptError("claude_failed", "Claude 호출 실패")
    monkeypatch.setattr(llm, "ask_validated", boom)
    job = client.json("POST", "/api/footage/process", {"path": rel})[1]["job"]
    job = wait_for(client, job["id"], finished)
    assert job["status"] == "failed" and job["message"] == "Claude 호출 실패"
    assert job["items"][0]["state"] == "done" and job["items"][-1]["state"] == "failed"


def test_second_footage_job_rejected_while_running(client, rel, tmp_path, fake_process, fake_llm):
    link(client, rel, shoot(tmp_path))
    proc = fake_process()
    proc.gate = threading.Event()
    fake_llm(lambda payload, files: answer())
    job = client.json("POST", "/api/footage/process", {"path": rel})[1]["job"]
    status, body = client.json("POST", "/api/footage/process", {"path": rel})
    assert status == 400 and body["error"] == "job_running"
    proc.gate.set()
    wait_for(client, job["id"], finished)


# --- collect와 동시 실행 금지 --------------------------------------------------------------

def test_collect_rejected_while_footage_runs(client, rel, tmp_path, fake_process, fake_llm):
    link(client, rel, shoot(tmp_path))
    proc = fake_process()
    proc.gate = threading.Event()
    fake_llm(lambda payload, files: answer())
    job = client.json("POST", "/api/footage/process", {"path": rel})[1]["job"]
    cat = conti._load_conti(conti.safe_draft_path(rel))["category"]
    status, body = client.json("POST", "/api/add", {"category": cat, "links": ["https://youtu.be/abcdefghijk"]})
    assert status == 400 and body["error"] == "busy"
    assert body["message"] == "촬영 영상 처리가 끝난 뒤 시작해 주세요"
    proc.gate.set()
    wait_for(client, job["id"], finished)


def test_footage_rejected_while_collect_runs(client, rel, tmp_path, fake_process):
    link(client, rel, shoot(tmp_path))
    fake_process()
    gate = threading.Event()

    def collect(job: Job) -> None:
        gate.wait(5)
    collect_job = client.app.jobs.start("collect", collect, items=[new_item("https://youtu.be/abcdefghijk")])
    status, body = client.json("POST", "/api/footage/process", {"path": rel})
    assert status == 400 and body["error"] == "busy"
    assert body["message"] == "레퍼런스 분석이 끝난 뒤 시작해 주세요"
    gate.set()
    collect_job.join(5)


# --- 판정 조작 ---------------------------------------------------------------------------

FID = "0123456789ab-100-1"


def test_choose_not_filmed_reset_roundtrip(client, rel, saved):
    cid = conti._load_conti(saved)["cuts"][1]["id"]
    status, body = client.json("POST", "/api/footage/choose",
                               {"path": rel, "cut": cid, "fid": FID, "start": 1.0, "end": 2.5})
    assert status == 200, body
    cut = next(c for c in body["cuts"] if c["id"] == cid)
    assert cut["kind"] == "pinned" and cut["user"] is True
    assert cut["choice"]["fid"] == FID and cut["choice"]["missing_source"] is True   # 연결 폴더에 없는 파일
    body = client.json("POST", "/api/footage/not-filmed", {"path": rel, "cut": cid})[1]
    cut = next(c for c in body["cuts"] if c["id"] == cid)
    assert cut["kind"] == "not_filmed" and body["summary"]["missing"] == 4
    body = client.json("POST", "/api/footage/reset", {"path": rel, "cut": cid})[1]
    cut = next(c for c in body["cuts"] if c["id"] == cid)
    assert cut["kind"] == "missing" and cut["user"] is False


@pytest.mark.parametrize("route, extra, code", [
    ("choose", {"cut": "nope", "fid": FID, "start": 0, "end": 1}, "cut_not_found"),
    ("choose", {"fid": "../x", "start": 0, "end": 1}, "bad_fid"),
    ("choose", {"fid": FID, "start": 2, "end": 1}, "bad_request"),
    ("not-filmed", {"cut": 3}, "bad_request"),
])
def test_verdict_actions_validate(client, rel, saved, route, extra, code):
    body = {"path": rel, "cut": conti._load_conti(saved)["cuts"][0]["id"], **extra}
    status, resp = client.json("POST", f"/api/footage/{route}", body)
    assert status == 400 and resp["error"] == code


def suggestion_state(rel, fid=FID):
    s = {"fid": fid, "start": 4.0, "end": 7.2, "line": "국물이 진짜 맛있네요", "guide": "그릇 들고 마시기",
         "size": "클로즈업", "text": "국물 진짜 맛있네"}
    with fm.STATE_LOCK:
        state = fm.load_state(rel)
        state["suggestions"] = [{**s, "key": fm.suggestion_key(s), "hidden": False}]
        fm.save_state(rel, state)
    return fm.suggestion_key(s)


def test_hide_suggestion_by_key(client, rel, saved):
    key = suggestion_state(rel)
    view = client.json("GET", f"/api/footage?path={q(rel)}")[1]
    [s] = view["suggestions"]
    assert s["key"] == key and s["missing_source"] is True and s["line"] == "국물이 진짜 맛있네요"
    status, body = client.json("POST", "/api/footage/suggestions/hide", {"path": rel, "key": key})
    assert status == 200 and body["suggestions"] == []
    status, body = client.json("POST", "/api/footage/suggestions/hide", {"path": rel, "key": "x"})
    assert status == 400 and body["error"] == "suggestion_not_found"


def test_insert_suggestion_adds_cut_pinned_and_records_history(client, rel, saved):
    key = suggestion_state(rel)
    before = conti._load_conti(saved)["cuts"]
    digest = client.json("GET", f"/api/conti?path={q(rel)}")[1]["hash"]
    status, body = client.json("POST", "/api/footage/suggestions/insert",
                               {"path": rel, "key": key, "after_cut": before[1]["id"], "hash": digest})
    assert status == 200, body
    after = conti._load_conti(saved)["cuts"]
    assert len(after) == len(before) + 1
    new = after[2]
    assert new["id"] == body["cut_id"] and new["line"] == "국물이 진짜 맛있네요"
    assert new["guide"] == "그릇 들고 마시기" and new["size"] == "클로즈업" and new["sec"] == 3.0
    assert new["section"] == before[1]["section"]
    assert body["hash"] == conti.content_hash(saved) and body["conti"]["hash"] == body["hash"]
    assert ch.list_versions(saved)[0]["kind"] == "auto"
    cut = next(c for c in body["footage"]["cuts"] if c["id"] == new["id"])
    assert cut["kind"] == "pinned" and cut["user"] is True
    assert (cut["choice"]["fid"], cut["choice"]["start"], cut["choice"]["end"]) == (FID, 4.0, 7.2)
    assert body["footage"]["suggestions"] == []


def test_insert_suggestion_stale_hash_changes_nothing(client, rel, saved):
    key = suggestion_state(rel)
    before = conti._load_conti(saved)["cuts"]
    status, body = client.json("POST", "/api/footage/suggestions/insert",
                               {"path": rel, "key": key, "after_cut": before[0]["id"], "hash": "0" * 64})
    assert status == 400
    assert len(conti._load_conti(saved)["cuts"]) == len(before)
    assert fm.load_state(rel)["suggestions"][0]["hidden"] is False


def test_insert_suggestion_unknown_after_cut(client, rel, saved):
    key = suggestion_state(rel)
    digest = conti.content_hash(saved)
    status, body = client.json("POST", "/api/footage/suggestions/insert",
                               {"path": rel, "key": key, "after_cut": "zzz", "hash": digest})
    assert status == 400 and body["error"] == "cut_not_found"


# --- 미디어 서빙 (Review Focus 3) -----------------------------------------------------------

def test_media_full_and_range(client, rel, tmp_path):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    fid = footage.fingerprint(folder / "a.mp4")
    status, headers, raw = media(client, rel, fid)
    assert status == 200 and raw == VIDEO_BYTES
    assert headers["Content-Type"] == "video/mp4" and headers["Accept-Ranges"] == "bytes"
    status, headers, raw = media(client, rel, fid, headers={"Range": "bytes=10-19"})
    assert status == 206 and raw == VIDEO_BYTES[10:20]
    assert headers["Content-Range"] == f"bytes 10-19/{len(VIDEO_BYTES)}" and headers["Content-Length"] == "10"
    status, headers, raw = media(client, rel, fid, headers={"Range": "bytes=10000-"})
    assert status == 206 and raw == VIDEO_BYTES[10000:]
    status, headers, raw = media(client, rel, fid, headers={"Range": "bytes=-5"})
    assert status == 206 and raw == VIDEO_BYTES[-5:]
    status, headers, raw = media(client, rel, fid, headers={"Range": "bytes=99999-"})
    assert status == 416 and headers["Content-Range"] == f"bytes */{len(VIDEO_BYTES)}"
    mov = footage.fingerprint(folder / "b.MOV")
    status, headers, _ = media(client, rel, mov)
    assert status == 200 and headers["Content-Type"] == "video/quicktime"


def test_media_streams_large_file_in_chunks(client, rel, tmp_path, monkeypatch):
    folder = shoot(tmp_path, files=())
    big = bytes(range(256)) * 5000                  # 1.28MB > chunk
    (folder / "big.mp4").write_bytes(big)
    link(client, rel, folder)
    monkeypatch.setattr(api_footage, "CHUNK", 64 * 1024)
    status, _, raw = media(client, rel, footage.fingerprint(folder / 'big.mp4'))
    assert status == 200 and raw == big


def test_media_refuses_files_outside_linked_folders(client, rel, tmp_path):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    other = shoot(tmp_path, "다른 폴더", files=("x.mp4",))
    status, _, _ = media(client, rel, footage.fingerprint(other / 'x.mp4'))
    assert status == 404
    sub = footage.fingerprint(folder / "sub" / "c.mp4")          # 하위 폴더 파일
    assert media(client, rel, sub)[0] == 404
    for bad in ("..%2F..%2Fsecret", "0123456789ab-1-1", "abc", "%2E%2E"):
        assert media(client, rel, bad)[0] == 404


def test_media_404_after_folder_removed(client, rel, tmp_path):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    fid = footage.fingerprint(folder / "a.mp4")
    assert media(client, rel, fid)[0] == 200
    client.json("POST", "/api/footage/folders/remove", {"path": rel, "folder": str(folder)})
    assert media(client, rel, fid)[0] == 404


def test_media_404_when_file_changed(client, rel, tmp_path):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    fid = footage.fingerprint(folder / "a.mp4")
    (folder / "a.mp4").write_bytes(VIDEO_BYTES + b"more")       # 지문이 바뀐다
    assert media(client, rel, fid)[0] == 404


def test_frame_serves_only_inside_footage_dir(client, home):  # noqa: F811
    base = footage.footage_dir(FID)
    (base / "frames").mkdir(parents=True)
    (base / "frames" / "s000_0.jpg").write_bytes(b"\xff\xd8x")
    (base / "scenes.json").write_text("{}", encoding="utf-8")
    status, headers, raw = client.call("GET", f"/api/footage/frame/{FID}/frames/s000_0.jpg")
    assert status == 200 and raw == b"\xff\xd8x" and headers["Content-Type"] == "image/jpeg"
    (home / "secret.jpg").write_bytes(b"secret")
    for bad in (f"{FID}/..%2F..%2F..%2Fsecret.jpg", f"{FID}/scenes.json", "..%2Fx/frames/s000_0.jpg",
                f"{FID}/frames/none.jpg"):
        assert client.call("GET", f"/api/footage/frame/{bad}")[0] == 404, bad


def test_media_requires_token(client, rel, tmp_path):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    fid = footage.fingerprint(folder / "a.mp4")
    assert media(client, rel, fid, token=False)[0] == 403


def test_clips_job_excludes_footage_and_collect():
    from scripts.ui.jobs import JobManager
    jobs = JobManager()
    gate = threading.Event()
    clips = jobs.start("footage_clips", lambda job: gate.wait(5), items=[new_item("clip")])
    for kind, message in (("footage", "컷 파일 만들기가 끝난 뒤 시작해 주세요"),
                          ("collect", "촬영 영상 처리가 끝난 뒤 시작해 주세요")):
        with pytest.raises(ScriptError) as e:
            jobs.start(kind, lambda job: None)
        assert e.value.code == "busy" and e.value.message == message
    gate.set()
    clips.join(5)
    jobs.start("footage", lambda job: None).join(5)


@pytest.mark.parametrize("header, expected", [
    (None, None), ("", None), ("bytes=0-0", (0, 0)), ("bytes=5-", (5, 99)), ("bytes=90-200", (90, 99)),
    ("bytes=-10", (90, 99)), ("bytes=-500", (0, 99)), ("bytes=0-1,5-6", None), ("items=0-5", None),
    ("bytes=-", None), ("bytes=a-b", None), ("bytes=5-2", None),
])
def test_byte_range(header, expected):
    assert api_footage.byte_range(header, 100) == expected


@pytest.mark.parametrize("header, size", [("bytes=100-", 100), ("bytes=-0", 100), ("bytes=100-200", 100),
                                          ("bytes=0-", 0), ("bytes=-1", 0)])
def test_byte_range_unsatisfiable(header, size):
    with pytest.raises(api_footage.RangeNotSatisfiable):
        api_footage.byte_range(header, size)


def test_suggested_sec_snaps_and_clamps():
    assert api_footage.suggested_sec(4.0, 7.2) == 3.0
    assert api_footage.suggested_sec(0.0, 0.1) == 0.5
    assert api_footage.suggested_sec(0.0, 500.0) == 120.0


# --- Fix round 1 ---------------------------------------------------------------------------

def test_failed_reprocess_keeps_cached_scenes_in_match(client, rel, tmp_path, fake_process, fake_llm,
                                                       monkeypatch):
    folder = shoot(tmp_path, files=("a.mp4",))
    link(client, rel, folder)
    proc = fake_process()
    proc(folder / "a.mp4")                                     # 장면 캐시는 있고
    (footage.footage_dir(footage.fingerprint(folder / "a.mp4")) / "transcript.json").unlink()   # 받아쓰기는 남음

    def fail(path, progress=None, run_stt=True):
        raise ScriptError("stt_failed", "받아쓰기 실패")
    monkeypatch.setattr(footage, "process_file", fail)
    fake_llm(lambda payload, files: answer())
    seen = []
    real = fm.match

    def spy(path, results, now=None):
        seen.append([(r["name"], r["status"]) for r in results])
        return real(path, results, now)
    monkeypatch.setattr(fm, "match", spy)
    job = client.json("POST", "/api/footage/process", {"path": rel})[1]["job"]
    job = wait_for(client, job["id"], finished)
    assert job["items"][0]["state"] == "failed" and job["items"][0]["message"] == "받아쓰기 실패"
    assert seen == [[("a.mp4", "done")]]                       # 앞서 뽑은 장면으로 계속 맞춘다


def test_insert_suggestion_without_size_uses_default(client, rel, saved):
    with fm.STATE_LOCK:
        state = fm.load_state(rel)
        s = {"fid": FID, "start": 1.0, "end": 3.0, "line": "", "guide": ""}
        state["suggestions"] = [{**s, "key": fm.suggestion_key(s), "hidden": False}]
        fm.save_state(rel, state)
    before = conti._load_conti(saved)["cuts"]
    status, body = client.json("POST", "/api/footage/suggestions/insert",
                               {"path": rel, "key": fm.suggestion_key(s), "after_cut": before[0]["id"],
                                "hash": conti.content_hash(saved)})
    assert status == 200, body
    new = conti._load_conti(saved)["cuts"][1]
    assert new["size"] == conti.CUT_DEFAULTS["size"] and new["guide"] == conti.CUT_DEFAULTS["guide"]


def test_insert_suggestion_pin_failure_reports_inserted_cut(client, rel, saved, monkeypatch):
    key = suggestion_state(rel)
    before = conti._load_conti(saved)["cuts"]

    def boom(*a, **k):
        raise ScriptError("bad_state", "판정 파일을 읽지 못했습니다")
    monkeypatch.setattr(fm, "choose", boom)
    status, body = client.json("POST", "/api/footage/suggestions/insert",
                               {"path": rel, "key": key, "after_cut": before[0]["id"],
                                "hash": conti.content_hash(saved)})
    assert status == 400 and body["error"] == "cut_not_linked"
    assert "넣었지만" in body["message"] and "판정 파일을 읽지 못했습니다" in body["message"]
    after = conti._load_conti(saved)["cuts"]
    assert len(after) == len(before) + 1 and body["cut_id"] == after[1]["id"]
    assert body["hash"] == conti.content_hash(saved)
    assert fm.load_state(rel)["suggestions"][0]["hidden"] is False   # 숨기지 않아 다시 고를 수 있다


def test_open_ended_range_is_capped(client, rel, tmp_path, monkeypatch):
    folder = shoot(tmp_path, files=("a.mp4",))
    link(client, rel, folder)
    monkeypatch.setattr(api_footage, "MAX_OPEN_RANGE", 1000)
    fid = footage.fingerprint(folder / "a.mp4")
    status, headers, raw = media(client, rel, fid, headers={"Range": "bytes=0-"})
    assert status == 206 and raw == VIDEO_BYTES[:1000]
    assert headers["Content-Range"] == f"bytes 0-999/{len(VIDEO_BYTES)}"
    status, headers, raw = media(client, rel, fid, headers={"Range": "bytes=10000-"})
    assert status == 206 and raw == VIDEO_BYTES[10000:]
    status, _, raw = media(client, rel, fid, headers={"Range": "bytes=0-4999"})   # 닫힌 범위는 그대로
    assert status == 206 and raw == VIDEO_BYTES[:5000]


def test_media_reuses_recent_lookup_without_rescanning(client, rel, tmp_path, monkeypatch):
    folder = shoot(tmp_path)
    link(client, rel, folder)
    fid = footage.fingerprint(folder / "a.mp4")
    assert media(client, rel, fid)[0] == 200
    scans = []
    real = footage.scan_folder
    monkeypatch.setattr(footage, "scan_folder", lambda f: scans.append(f) or real(f))
    for _ in range(3):
        assert media(client, rel, fid, headers={"Range": "bytes=0-9"})[0] == 206
    assert scans == []
    (folder / "a.mp4").write_bytes(VIDEO_BYTES + b"x")           # 바뀐 파일은 캐시가 있어도 내주지 않는다
    assert media(client, rel, fid)[0] == 404


def test_add_folder_rejects_unc_path(client, rel):
    status, body = client.json("POST", "/api/footage/folders", {"path": rel, "folder": r"\\nas\share\촬영"})
    assert status == 400 and body["error"] == "bad_folder" and "네트워크 폴더" in body["message"]


def test_add_folder_rejects_mapped_network_drive(client, rel, tmp_path, monkeypatch):
    folder = shoot(tmp_path)
    monkeypatch.setattr(api_footage, "_is_remote_drive", lambda path: True)
    status, body = client.json("POST", "/api/footage/folders", {"path": rel, "folder": str(folder)})
    assert status == 400 and body["error"] == "bad_folder"


def _review_only_collect(jobs):
    from scripts.ui import api_add
    item = {**api_add.new_item("https://youtu.be/abcdefghijk"), "state": "awaiting", "ask": "review"}
    return jobs.start("collect", api_add.Collector(), items=[item])


def test_footage_allowed_while_collect_only_waits_for_reviews(client, rel, tmp_path, fake_process, fake_llm):
    link(client, rel, shoot(tmp_path))
    proc = fake_process()
    proc.gate = threading.Event()
    fake_llm(lambda payload, files: answer())
    collect = _review_only_collect(client.app.jobs)
    status, body = client.json("POST", "/api/footage/process", {"path": rel})
    assert status == 200, body
    cat = conti._load_conti(saved_path(rel))["category"]
    status, resp = client.json("POST", "/api/add", {"category": cat, "links": ["https://youtu.be/bcdefghijkl"]})
    assert status == 400 and resp["error"] == "busy"        # 확인 대기 collect에 새 링크를 붙여도 안 된다
    assert resp["message"] == "촬영 영상 처리가 끝난 뒤 시작해 주세요"
    proc.gate.set()
    wait_for(client, body["job"]["id"], finished)
    collect.cancel()
    collect.join(5)


def test_footage_refused_while_collect_has_work(client, rel, tmp_path, fake_process):
    link(client, rel, shoot(tmp_path))
    fake_process()
    gate = threading.Event()

    def work(job: Job) -> None:
        job.update(0, state="running")
        gate.wait(5)
    collect = client.app.jobs.start("collect", work, items=[new_item("x")])
    status, body = client.json("POST", "/api/footage/process", {"path": rel})
    assert status == 400 and body["error"] == "busy"
    gate.set()
    collect.join(5)


def saved_path(rel):
    return conti.safe_draft_path(rel)


# --- 결과물: 남은 컷 HTML, CSV, 컷 파일(작업 footage_clips) -----------------------------------------

from tests.test_cuts import sample_video  # noqa: E402,F401 - fixture


def real_shoot(tmp_path, video, names=("a.mp4", "b.mp4")):
    """진짜 영상 사본이 든 촬영 폴더. -> (폴더, {이름: 지문})"""
    import shutil
    folder = tmp_path.parent / f"{tmp_path.name}-real" / "촬영"
    folder.mkdir(parents=True, exist_ok=True)
    fids = {}
    for n in names:
        shutil.copy(video, folder / n)
        fids[n] = fid = footage.fingerprint(folder / n)
        base = footage.footage_dir(fid)                           # 처리 캐시(원본이 사라져도 이름이 남는다)
        base.mkdir(parents=True, exist_ok=True)
        (base / "scenes.json").write_text(json.dumps(
            {"schema": 1, "fid": fid, "name": n, "status": "done", "duration_sec": 6.0, "scenes": []}),
            encoding="utf-8")
    return folder, fids


def pin_cuts(client, rel, saved, folder, picks):
    """picks: [(컷 위치, fid, 시작, 끝)] → 연결하고 고정."""
    link(client, rel, folder)
    ids = [c["id"] for c in conti._load_conti(saved)["cuts"]]
    for i, fid, start, end in picks:
        fm.choose(saved, ids[i], fid, start, end)


def test_remaining_and_csv_routes_write_files(client, rel, saved, tmp_path, sample_video):  # noqa: F811
    folder, fids = real_shoot(tmp_path, sample_video, names=("a.mp4",))
    pin_cuts(client, rel, saved, folder, [(0, fids["a.mp4"], 1.0, 3.0)])
    base = rel[: -len("-conti.md")]
    status, body = client.json("POST", "/api/footage/remaining", {"path": rel})
    assert status == 200, body
    assert body["path"] == f"{base}-remaining.html" and body["url"].startswith("/files/drafts/")
    assert body["footage"]["outputs"]["remaining_html"] == body["path"]
    status, headers, raw = client.call("GET", body["url"])
    assert status == 200 and "찍음 1/4" in raw.decode("utf-8")
    status, body = client.json("POST", "/api/footage/csv", {"path": rel})
    assert status == 200, body
    assert body["path"] == f"{base}-footage.csv" and body["footage"]["outputs"]["csv"] == body["path"]
    status, _, raw = client.call("GET", body["url"])
    assert status == 200 and raw.startswith(b"\xef\xbb\xbf")


def test_output_routes_reject_non_conti_path(client):
    for route in ("remaining", "csv", "clips"):
        status, body = client.json("POST", f"/api/footage/{route}", {"path": "refs/x.md"})
        assert status == 400 and body["error"] == "bad_path", route


def test_clips_job_makes_files_and_skips_missing_source(client, rel, saved, tmp_path, sample_video,  # noqa: F811
                                                        home):
    folder, fids = real_shoot(tmp_path, sample_video)
    pin_cuts(client, rel, saved, folder, [(0, fids["a.mp4"], 1.0, 3.0), (2, fids["b.mp4"], 0.0, 2.0)])
    (folder / "b.mp4").unlink()                                  # 원본 없음
    status, body = client.json("POST", "/api/footage/clips", {"path": rel})
    assert status == 200, body
    job = body["job"]
    assert job["kind"] == "footage_clips" and [i["title"] for i in job["items"]] == ["#1 a.mp4", "#3 b.mp4"]
    job = wait_for(client, job["id"], finished, timeout=30)
    assert job["status"] == "done", job
    made, skipped = job["items"]
    assert made["state"] == "done" and made["preview"]["reencoded"] is False
    assert made["preview"]["url"].startswith("/files/drafts/") and (home / made["preview"]["file"]).is_file()
    assert skipped["state"] == "skipped" and "원본 없음" in skipped["message"]
    assert job["summary"]["made"] == 1 and job["summary"]["skipped"] == 1 and job["summary"]["path"] == rel
    view = client.json("GET", f"/api/footage?path={q(rel)}")[1]
    assert view["outputs"]["clips_dir"] == made["preview"]["file"].rsplit("/", 1)[0]
    assert view["clips_job"]["id"] == job["id"]
    assert [c["choice"]["missing_source"] for c in view["cuts"] if c["choice"]] == [False, True]


def test_clips_requires_a_chosen_cut(client, rel):
    status, body = client.json("POST", "/api/footage/clips", {"path": rel})
    assert status == 400 and body["error"] == "no_clips"


def test_clips_cancel_between_cuts(client, rel, saved, tmp_path, sample_video, monkeypatch):  # noqa: F811
    from scripts import footage_out
    folder, fids = real_shoot(tmp_path, sample_video)
    pin_cuts(client, rel, saved, folder, [(0, fids["a.mp4"], 1.0, 3.0), (1, fids["b.mp4"], 1.0, 3.0)])
    gate, entered, calls = threading.Event(), threading.Event(), []
    real = footage_out.cut_clip

    def slow(target, out_dir):
        calls.append(target["n"])
        entered.set()
        assert gate.wait(5)
        return real(target, out_dir)

    monkeypatch.setattr(footage_out, "cut_clip", slow)
    job = client.json("POST", "/api/footage/clips", {"path": rel})[1]["job"]
    assert entered.wait(5)
    assert client.json("POST", f"/api/jobs/{job['id']}/cancel")[0] == 200
    gate.set()
    job = wait_for(client, job["id"], finished, timeout=30)
    assert job["status"] == "stopped" and calls == [1]
    assert [i["state"] for i in job["items"]] == ["done", "skipped"]


def test_clips_refused_while_footage_job_runs(client, rel, saved, tmp_path, sample_video):  # noqa: F811
    folder, fids = real_shoot(tmp_path, sample_video, names=("a.mp4",))
    pin_cuts(client, rel, saved, folder, [(0, fids["a.mp4"], 1.0, 3.0)])
    gate = threading.Event()
    running = client.app.jobs.start("footage", lambda job: gate.wait(5), items=[new_item("x")])
    try:
        status, body = client.json("POST", "/api/footage/clips", {"path": rel})
        assert status == 400 and body["error"] == "busy"
    finally:
        gate.set()
        running.join(5)


@pytest.mark.parametrize("kind", ["footage", "footage_clips"])
def test_trash_refused_while_footage_job_runs_on_that_conti(client, rel, saved, draft_md, kind):  # noqa: F811
    gate = threading.Event()
    running = client.app.jobs.start(kind, lambda job: gate.wait(5), items=[new_item("x")], summary={"path": rel})
    try:
        status, body = client.json("POST", "/api/conti/trash", {"path": rel})
        assert status == 400 and body["error"] == "conti_busy" and saved.exists()
        draft_rel = draft_md.relative_to(saved.parents[1]).as_posix()
        status, body = client.json("POST", "/api/draft/trash", {"path": draft_rel})
        assert status == 400 and body["error"] == "draft_busy" and draft_md.exists()
    finally:
        gate.set()
        running.join(5)


# --- 카드 사진 (Task 5: 확인 필요·새 컷 후보 카드의 대표 사진) ----------------------------------

def seed_scenes(fid, scenes):
    base = footage.footage_dir(fid)
    base.mkdir(parents=True, exist_ok=True)
    record = {"schema": 1, "fid": fid, "name": "a.mp4", "status": "done", "decoder": "pyav",
              "duration_sec": 30.0, "scenes": scenes}
    (base / "scenes.json").write_text(json.dumps(record), encoding="utf-8")


def test_view_items_carry_frames_of_overlapping_scenes(client, rel, saved):
    seed_scenes(FID, [
        {"i": 0, "start": 0.0, "end": 3.0, "frames": ["frames/s000_0.jpg", "frames/s000_1.jpg"], "montage": "m0.jpg"},
        {"i": 1, "start": 3.0, "end": 8.0, "frames": [f"frames/s001_{k}.jpg" for k in range(6)], "montage": "m1.jpg"},
        {"i": 2, "start": 8.0, "end": 30.0, "frames": ["frames/s002_0.jpg"], "montage": "m2.jpg"},
    ])
    cid = conti._load_conti(saved)["cuts"][0]["id"]
    body = client.json("POST", "/api/footage/choose", {"path": rel, "cut": cid, "fid": FID, "start": 1.0, "end": 2.0})[1]
    cut = next(c for c in body["cuts"] if c["id"] == cid)
    assert cut["choice"]["frames"] == ["frames/s000_0.jpg", "frames/s000_1.jpg"]
    suggestion_state(rel)                                      # 4.0–7.2: scene 1 only, capped to 4 evenly
    view = client.json("GET", f"/api/footage?path={q(rel)}")[1]
    frames = view["suggestions"][0]["frames"]
    assert len(frames) == api_footage.CARD_FRAMES == 4
    assert frames[0] == "frames/s001_0.jpg" and frames[-1] == "frames/s001_5.jpg"


def test_view_items_without_cache_have_no_frames(client, rel, saved):
    cid = conti._load_conti(saved)["cuts"][0]["id"]
    body = client.json("POST", "/api/footage/choose", {"path": rel, "cut": cid, "fid": FID, "start": 1.0, "end": 2.0})[1]
    cut = next(c for c in body["cuts"] if c["id"] == cid)
    assert cut["choice"]["frames"] == []
