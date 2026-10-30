"""UI API B(draft·conti·diagnose·mission·report·footage)의 응답 JSON·경로 검사·mtime 의미를 저장소 이전 전후로 고정한다.

골든은 옛 코드로 한 번 만든 값이다(`GOLDEN_WRITE=1`로 다시 만든다. 평소에는 비교만 한다).
작업 폴더 절대 경로는 `<ROOT>`로 바꿔 적는다.
"""
import datetime
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from scripts import mission
from scripts.ui import api_mission
from tests.helpers.tree_snapshot import copy_workfolder
from tests.test_ui_server import Client, _start, _stop, no_browser  # noqa: F401 - fixture
from tests.test_similarity import LIKE_A1, _json, make_draft
from tests.typify_fixtures import CAT, write_video

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "ui_b"
DRAFT_KEY = "drafts/2026-10-30-제주-한달살기.md"
CONTI_KEY = "drafts/2026-10-30-제주-한달살기-conti.md"
FID = "0123456789ab-100-200"
BASE_MTIME = int(datetime.datetime(2023, 11, 15, 12, 0).timestamp())   # 지역 시각 정오: 날짜가 시간대와 무관


def golden(name: str, value, root: Path) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=1)
    for r in {str(root), str(root.resolve())}:
        text = text.replace(json.dumps(r)[1:-1], "<ROOT>")
    path = GOLDEN / f"{name}.json"
    if os.environ.get("GOLDEN_WRITE") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    assert text == path.read_bytes().decode("utf-8")


class _Day(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 31)


@pytest.fixture
def wf(monkeypatch, tmp_path):
    root = copy_workfolder(tmp_path / "w")
    monkeypatch.setenv("YT_COACH_HOME", str(root))
    monkeypatch.setattr(mission, "Date", _Day)
    monkeypatch.setattr(api_mission, "_server_today", lambda: datetime.date(2026, 10, 30))
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for i, p in enumerate(files):                      # mtime 고정(날짜·stale 판정 재현)
        os.utime(p, (BASE_MTIME + i * 10, BASE_MTIME + i * 10))
    return root


@pytest.fixture
def client(wf, no_browser):  # noqa: F811
    srv, app, thread = _start(wf)
    yield Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def _ok(client, method, path, body=None):
    code, data = client.json(method, path, body)
    assert code == 200, data
    return data


# ---- 응답 JSON 스냅숏 ------------------------------------------------------------------

@pytest.mark.parametrize("name, url", [
    ("drafts", "/api/drafts"),
    ("draft_list", "/api/draft/list?category=travel"),
    ("draft_detail", "/api/draft?path=" + quote(DRAFT_KEY)),
    ("report_list", "/api/report/list?category=travel"),
    ("review_weeks", "/api/review/weeks"),
    ("review_week", "/api/review?week=2026-W44"),
    ("mission_status", "/api/mission?date=2026-10-30"),
    ("diagnose_targets", "/api/diagnose/targets?category=travel"),
])
def test_get_response_snapshots(client, wf, name, url):
    code, data = client.json("GET", url)
    golden(name, {"status": code, "body": data}, wf)


def test_mission_status_snapshot_with_saved_mission(monkeypatch, tmp_path, no_browser):  # noqa: F811
    from tests.test_mission import PROFILE
    from tests.test_ui_mission import GOOD
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    monkeypatch.setattr(mission, "Date", _Day)
    monkeypatch.setattr(api_mission, "_server_today", lambda: datetime.date(2026, 10, 30))
    (tmp_path / "profile.md").write_bytes(PROFILE.encode("utf-8"))
    seed = tmp_path / "seed.json"
    for day, patch in (("2026-10-28", {}), ("2026-10-30", {"title": "오늘 미션"})):
        seed.write_text(json.dumps(GOOD | patch, ensure_ascii=False), encoding="utf-8")
        mission.save(seed, day)
    mission.skip("바빠서", "2026-10-28")
    srv, app, thread = _start(tmp_path)
    try:
        client = Client(srv, app)
        golden("mission_status_full", _ok(client, "GET", "/api/mission?date=2026-10-30"), tmp_path)
        golden("mission_status_next_day", _ok(client, "GET", "/api/mission?date=2026-10-31"), tmp_path)
    finally:
        app.jobs.shutdown(timeout=5)
        _stop(srv, thread)


# ---- 콘티·촬영 영상: 실제로 저장한 콘티(컷 id·날짜 고정) ---------------------------------------

@pytest.fixture
def cw(monkeypatch, tmp_path, no_browser):  # noqa: F811
    from scripts import conti
    from tests.test_conti import DRAFT, conti_file
    from tests.typify_fixtures import standard_set
    from scripts import draft
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    standard_set(tmp_path)
    ids = iter(f"id{i:06d}" for i in range(1000))
    monkeypatch.setattr(conti, "new_cut_id", lambda: next(ids))
    monkeypatch.setattr(conti, "date", _Day)
    monkeypatch.setattr(draft, "date", _Day)
    src = tmp_path / "workspace" / "tmp" / "draft.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    draft_key = draft.save(CAT, src)["path"]
    conti_key = conti.save(draft_key, conti_file(tmp_path))["path"]
    base = conti_key[: -len("-conti.md")]
    for suffix in ("-captions.srt", "-lines.srt", "-captions.mp4", "-remaining.html", "-footage.csv"):
        (tmp_path / f"{base}{suffix}").write_bytes(b"x")
    (tmp_path / f"{base}-cuts").mkdir()
    (tmp_path / f"{base}-cuts" / "01.mp4").write_bytes(b"x")
    srv, app, thread = _start(tmp_path)
    yield tmp_path, Client(srv, app), draft_key, conti_key
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def test_conti_and_footage_view_snapshots(cw):
    root, client, draft_key, conti_key = cw
    golden("conti_view", _ok(client, "GET", "/api/conti?path=" + quote(conti_key)), root)
    golden("footage_view", _ok(client, "GET", "/api/footage?path=" + quote(conti_key)), root)
    golden("drafts_with_conti", _ok(client, "GET", "/api/drafts"), root)
    golden("draft_list_with_conti", _ok(client, "GET", f"/api/draft/list?category={CAT}"), root)


def test_draft_detail_absolute_path_answers_like_key(client, wf):
    by_key = _ok(client, "GET", "/api/draft?path=" + quote(DRAFT_KEY))
    by_abs = _ok(client, "GET", "/api/draft?path=" + quote(str(wf / DRAFT_KEY)))
    detour = _ok(client, "GET", "/api/draft?path=" + quote("drafts/x/../2026-10-30-제주-한달살기.md"))
    assert by_key == by_abs == detour and by_key["path"] == DRAFT_KEY


def test_draft_filmed_round_trip_and_path_rules(client, wf):
    view = _ok(client, "GET", "/api/draft?path=" + quote(DRAFT_KEY))
    out = _ok(client, "POST", "/api/drafts/filmed",
              {"category": "travel", "path": DRAFT_KEY, "filmed": True, "hash": view["hash"]})
    assert out["path"] == DRAFT_KEY and out["filmed_at"] == datetime.date.today().isoformat()
    (wf / "drafts" / "sub").mkdir()
    (wf / "drafts" / "sub" / "a.md").write_bytes((wf / DRAFT_KEY).read_bytes())
    for raw, error, message in [
        (str(wf / DRAFT_KEY), "bad_path", f"작업 폴더 기준 상대 경로만 쓸 수 있습니다: {wf / DRAFT_KEY}"),
        ("drafts\\x.md", "bad_path", "작업 폴더 기준 상대 경로만 쓸 수 있습니다: drafts\\x.md"),
        ("drafts/sub/a.md", "bad_path", "drafts 폴더 바로 아래의 기획안만 체크할 수 있습니다"),
        ("drafts/none.md", "draft_not_found", "기획안을 찾을 수 없습니다: drafts/none.md"),
        ("../x.md", "bad_path", "drafts 폴더 안의 .md 파일만 쓸 수 있습니다: ../x.md"),
    ]:
        code, data = client.json("POST", "/api/drafts/filmed",
                                 {"category": "travel", "path": raw, "filmed": True, "hash": out["hash"]})
        assert (code, data) == (400, {"error": error, "message": message}), raw


def test_conti_view_outputs_stale_follows_md_vs_output_mtime(cw):
    wf, client, _, conti_key = cw
    base = conti_key[: -len("-conti.md")]
    md = wf / conti_key
    srt = wf / f"{base}-captions.srt"
    mp4 = wf / f"{base}-captions.mp4"
    url = "/api/conti?path=" + quote(conti_key)
    _ok(client, "GET", url)                           # 기준선(이력)을 먼저 남긴다

    def stale(md_t, srt_t, mp4_t):
        os.utime(md, (md_t, md_t))
        os.utime(srt, (srt_t, srt_t))
        os.utime(mp4, (mp4_t, mp4_t))
        return _ok(client, "GET", url)["outputs_stale"]

    assert stale(BASE_MTIME + 100, BASE_MTIME, BASE_MTIME + 200) == {"srt": True, "video": False}
    assert stale(BASE_MTIME + 100, BASE_MTIME + 100, BASE_MTIME + 99) == {"srt": False, "video": True}
    os.utime(wf / f"{base}-lines.srt", (BASE_MTIME + 200, BASE_MTIME + 200))
    assert stale(BASE_MTIME + 100, BASE_MTIME + 200, BASE_MTIME + 200) == {"srt": False, "video": False}
    os.utime(wf / f"{base}-lines.srt", (BASE_MTIME, BASE_MTIME))
    assert stale(BASE_MTIME + 100, BASE_MTIME + 200, BASE_MTIME + 200) == {"srt": True, "video": False}
    srt.unlink()
    (wf / f"{base}-lines.srt").unlink()
    mp4.unlink()
    assert _ok(client, "GET", url)["outputs_stale"] == {"srt": False, "video": False}


def test_footage_outputs_follow_files(cw):
    wf, client, _, conti_key = cw
    base = conti_key[: -len("-conti.md")]
    url = "/api/footage?path=" + quote(conti_key)
    assert _ok(client, "GET", url)["outputs"] == {
        "remaining_html": f"{base}-remaining.html", "csv": f"{base}-footage.csv", "clips_dir": f"{base}-cuts"}
    (wf / f"{base}-footage.csv").unlink()
    (wf / f"{base}-remaining.html").unlink()
    (wf / f"{base}-remaining.html").mkdir()                 # 같은 이름의 폴더는 파일이 아니다
    for p in (wf / f"{base}-cuts").iterdir():
        p.unlink()
    (wf / f"{base}-cuts").rmdir()
    (wf / f"{base}-cuts").write_bytes(b"not a folder")
    (wf / f"{base}-footage.csv").mkdir()
    assert _ok(client, "GET", url)["outputs"] == {"remaining_html": None, "csv": None, "clips_dir": None}


def test_footage_frame_serving_stays_inside_the_fid_folder(cw):
    wf, client, _, _ = cw
    base = wf / "workspace" / "footage" / FID
    (base / "frames").mkdir(parents=True)
    (base / "frames" / "0001.jpg").write_bytes(b"\xff\xd8one")
    (wf / "workspace" / "footage" / "secret.jpg").write_bytes(b"\xff\xd8secret")
    (wf / "workspace" / "outside.jpg").write_bytes(b"\xff\xd8out")
    code, headers, body = client.call("GET", f"/api/footage/frame/{FID}/frames/0001.jpg")
    assert (code, body) == (200, b"\xff\xd8one") and headers["Content-Type"] == "image/jpeg"
    code, _, body = client.call("GET", f"/api/footage/frame/{FID}/frames/x/../0001.jpg")
    assert (code, body) == (200, b"\xff\xd8one")
    for rel in ["../secret.jpg", "frames/../../secret.jpg", "..%2Fsecret.jpg", "frames%2F..%2F..%2Fsecret.jpg",
                "frames/none.jpg", "frames", "frames/0001.png", quote(str(wf / "workspace" / "outside.jpg"))]:
        code, _, _ = client.call("GET", f"/api/footage/frame/{FID}/{rel}")
        assert code == 404, rel
    code, _, _ = client.call("GET", f"/api/footage/frame/bad-fid/frames/0001.jpg")
    assert code == 404
    code, _, _ = client.call("GET", f"/api/footage/frame/{FID}/frames/%000001.jpg")
    assert code == 404


def test_report_list_ignores_other_names_and_folders(client, wf):
    (wf / "reports" / "travel-2026-10-31.html").mkdir()
    (wf / "reports" / "food-2026-10-29.html").write_bytes(b"x")
    (wf / "reports" / "travel-latest.html").write_bytes(b"x")
    (wf / "reports" / "travel-2026-10-28.html").write_bytes(b"x")
    out = _ok(client, "GET", "/api/report/list?category=travel")
    assert [r["path"] for r in out["reports"]] == ["reports/travel-2026-10-30.html", "reports/travel-2026-10-28.html"]
    assert out["reports"][1]["url"] == "/files/reports/travel-2026-10-28.html"


# ---- 진단 대상 경로 검사(§7-6: resolve 기준, 우회 회귀) -----------------------------------------

def _compare(client, target):
    return client.json("POST", "/api/diagnose/compare", {"category": CAT, "target": target})


BAD = {"error": "bad_path", "message": "진단할 수 있는 파일이 아닙니다(이 카테고리의 내 영상 또는 기획안만 가능)"}


@pytest.fixture
def diag(monkeypatch, tmp_path, no_browser):  # noqa: F811
    from scripts import cluster
    from tests.typify_fixtures import standard_set
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("YT_COACH_HOME", str(home))
    standard_set(home)
    cluster.run(CAT)
    cluster.apply(CAT, _json(home, "n.json", {
        "axes": {a: "근거" for a in ["structure", "hook", "narrative", "title", "pacing"]},
        "archetypes": [{"id": "A1", "name": "결과 먼저 몰아먹기"}, {"id": "A2", "name": "사이즈별 리스트 비교"}],
    }))
    write_video(home, "m1", role="mine")
    draft = make_draft(home, LIKE_A1)
    draft.rename(home / "drafts" / "idea.md")
    (home / "refs" / "ref_other" / "videos").mkdir(parents=True)
    (home / "refs" / "ref_other" / "videos" / "m1.md").write_bytes((home / "refs" / f"ref_{CAT}" / "videos" / "m1.md").read_bytes())
    (home / "drafts" / "x-conti.md").write_bytes(b"---\ncategory: x\n---\n")
    (home / "drafts" / "up.MD").write_bytes((home / "drafts" / "idea.md").read_bytes())
    srv, app, thread = _start(home)
    yield home, Client(srv, app)
    app.jobs.shutdown(timeout=5)
    _stop(srv, thread)


def test_diagnose_target_accepted_spellings_resolve_to_the_key(diag):
    diag, dclient = diag
    video = f"refs/ref_{CAT}/videos/m1.md"
    spellings = [video, str(diag / video), f"refs/ref_{CAT}/videos/../videos/m1.md", f"refs/./ref_{CAT}/videos/m1.md",
                 "drafts/idea.md", "drafts/sub/../idea.md", str(diag / "drafts" / "idea.md")]
    if sys.platform == "win32":                     # 역슬래시·드라이브 상대 경로도 지금은 작업 폴더 기준으로 풀린다
        spellings += [video.replace("/", "\\"), "drafts\\idea.md", f"{diag.drive}drafts/idea.md"]
    for target in spellings:
        code, data = _compare(dclient, target)
        assert code == 200, (target, data)
        assert data["target"]["path"] in (video, "drafts/idea.md"), target


@pytest.mark.parametrize("target", [
    "../outside.md", "drafts/../../outside.md", "drafts/../profile.md", "profile.md", f"refs/ref_{CAT}/index.md",
    "drafts/x-conti.md", "drafts/sub/x.md", f"refs/ref_other/videos/m1.md", f"refs/ref_{CAT}/videos/m1.txt",
    f"refs/ref_{CAT}/m1.md", "drafts/up.MD", "drafts", ".", "", "   ", 5, None, "D:/drafts/x.md", "Z:\\drafts\\x.md",
    "//server/share/drafts/x.md",
])
def test_diagnose_target_bypass_is_bad_path(diag, target):
    _, dclient = diag
    code, data = _compare(dclient, target)
    assert (code, data) == (400, BAD), target


def test_diagnose_target_with_nul_is_bad_path(diag):
    _, dclient = diag
    assert _compare(dclient, "drafts/\x00idea.md") == (400, BAD)       # 예전에는 resolve가 ValueError(500)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 폴더 이름은 대소문자를 가리지 않는다")
def test_diagnose_target_mixed_case_folders_on_windows(diag):
    home, dclient = diag
    (home / "drafts").rename(home / "Drafts")
    (home / "refs" / f"ref_{CAT}").rename(home / "refs" / f"ref_{CAT.capitalize()}")
    code, data = _compare(dclient, "drafts/idea.md")
    assert code == 200, data
    assert data["target"]["path"] == "Drafts/idea.md"
    code, data = _compare(dclient, f"refs/ref_{CAT}/videos/m1.md")
    assert code == 200, data
    assert data["target"]["path"] == f"refs/ref_{CAT.capitalize()}/videos/m1.md"


def test_diagnose_target_absolute_outside_is_bad_path(diag, tmp_path_factory):
    diag, dclient = diag
    outside = tmp_path_factory.mktemp("outside")
    (outside / "drafts").mkdir()
    (outside / "drafts" / "x.md").write_bytes((diag / "drafts" / "idea.md").read_bytes())
    assert _compare(dclient, str(outside / "drafts" / "x.md")) == (400, BAD)


def test_diagnose_target_symlink_escape_is_bad_path(diag, tmp_path_factory):
    diag, dclient = diag
    outside = tmp_path_factory.mktemp("outside") / "x.md"
    outside.write_bytes((diag / "drafts" / "idea.md").read_bytes())
    try:
        os.symlink(outside, diag / "drafts" / "link.md")
        os.symlink(diag / "drafts" / "idea.md", diag / "drafts" / "inner.md")
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다")
    assert _compare(dclient, "drafts/link.md") == (400, BAD)
    code, data = _compare(dclient, "drafts/inner.md")              # 안쪽을 가리키는 링크는 실제 파일 키로
    assert code == 200 and data["target"]["path"] == "drafts/idea.md"


def test_diagnose_targets_date_falls_back_to_mtime(diag):
    diag, dclient = diag
    path = diag / "drafts" / "idea.md"
    lines = path.read_bytes().decode("utf-8").split("\n")
    path.write_bytes("\n".join(ln for ln in lines if not ln.startswith("date:")).encode("utf-8"))
    os.utime(path, (BASE_MTIME, BASE_MTIME))
    code, data = dclient.json("GET", f"/api/diagnose/targets?category={CAT}")
    assert code == 200
    [d] = [d for d in data["drafts"] if d["path"] == "drafts/idea.md"]
    assert d["date"] == datetime.datetime.fromtimestamp(BASE_MTIME).date().isoformat()


# ---- 미션 임시 복사본 정리(mtime 기준) ------------------------------------------------------

def test_mission_stale_copies_removed_by_mtime_only(client, wf, monkeypatch):
    from scripts import claude_cli, llm
    monkeypatch.setattr(llm, "ask", lambda *a, **k: {"feedback": "좋아요"})
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    tmp = wf / "workspace" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    old, fresh, other = tmp / "mission_submit_old.png", tmp / "mission_submit_new.png", tmp / "other_old.json"
    folder = tmp / "mission_submit_dir"
    folder.mkdir()
    for p in (old, fresh, other):
        p.write_bytes(b"x")
    long_ago = time.time() - api_mission.STALE_COPY_SECONDS - 60
    for p in (old, other, folder):
        os.utime(p, (long_ago, long_ago))
    code, data = client.json("POST", "/api/mission/feedback", {"date": "2026-10-30", "note": "했어요", "difficulty": "right"})
    assert code == 200, data
    assert sorted(p.name for p in tmp.iterdir()) == ["mission_submit_dir", "mission_submit_new.png", "other_old.json"]


def test_footage_frame_key_rejects_nul_and_escape(cw):
    from scripts.ui import api_footage
    assert api_footage._frame_key(FID, "frames/\x000001.jpg") is None
    assert api_footage._frame_key(FID, "../x.jpg") is None
    assert api_footage._frame_key(FID, "frames/0001.jpg") == f"workspace/footage/{FID}/frames/0001.jpg"
