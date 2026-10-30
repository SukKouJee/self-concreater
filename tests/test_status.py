import datetime
import json
import os

import pytest

from scripts import cluster, conti, draft, mission, refstore, status
from tests.test_draft import DRAFT
from tests.test_cluster import NOTES, notes
from tests.typify_fixtures import CAT, standard_set, write_video


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    return tmp_path


def _save_draft(home, category=CAT):
    path = home / "workspace" / "tmp" / "draft.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    return home / draft.save(category, path)["path"]


def _typify(home):
    """유형화를 실행하고 저장(apply)까지 한다. 홈 화면은 저장된 것만 완료로 본다."""
    cluster.run(CAT)
    cluster.apply(CAT, notes(home, NOTES))


def _cat(result, category=CAT):
    return next(c for c in result["categories"] if c["category"] == category)


def test_uninitialized_folder(home):
    out = status.overview()
    assert out["initialized"] is False
    assert out["categories"] == []
    assert out["work_root"] == str(home)


def test_collect_stage_and_next_text(home):
    refstore.init_category(CAT)
    for i in range(3):
        write_video(home, f"v{i:02d}")
    preliminary, _ = refstore._stage_limits()
    c = _cat(status.overview())
    assert c["current"] == "collect"
    assert [s["key"] for s in c["steps"]] == ["collect", "typify", "draft", "conti", "diagnose"]
    assert f"{preliminary - 3}개 더" in c["next"]["text"]
    assert c["next"]["menu"] == "add"
    assert c["steps"][0]["detail"] == "ref 3개 / mine 0개"


def test_typify_then_draft_then_conti(home):
    standard_set(home)
    assert _cat(status.overview())["current"] == "typify"
    assert _cat(status.overview())["next"]["menu"] == "typify"

    _typify(home)
    assert _cat(status.overview())["current"] == "draft"

    draft_path = _save_draft(home)
    c = _cat(status.overview())
    assert c["current"] == "conti"
    assert c["steps"][2]["detail"] == "기획안 1개"
    assert c["next"]["menu"] == "conti"

    draft_path.with_name(f"{draft_path.stem}-conti.md").write_text("x", encoding="utf-8")
    c = _cat(status.overview())
    assert c["current"] == "diagnose"
    assert c["next"]["menu"] == "diagnose"


def test_typify_done_only_after_apply(home):
    standard_set(home)
    cluster.run(CAT)
    assert _cat(status.overview())["current"] == "typify"
    cluster.apply(CAT, notes(home, NOTES))
    assert _cat(status.overview())["current"] == "draft"


def test_diagnose_done_reads_mine_category(home):
    standard_set(home)
    _typify(home)
    _save_draft(home)
    (home / "drafts" / f"{next((home / 'drafts').glob('*.md')).stem}-conti.md").write_text("x", encoding="utf-8")
    (home / "mine").mkdir()
    (home / "mine" / "x.md").write_bytes(refstore.join_frontmatter({"category": CAT}, "본문\n").encode("utf-8"))
    c = _cat(status.overview())
    assert c["current"] is None
    assert c["next"] is None
    assert all(s["done"] for s in c["steps"])


def test_other_category_draft_not_counted(home):
    standard_set(home)
    _typify(home)
    (home / "drafts").mkdir()
    (home / "drafts" / "other.md").write_bytes(
        refstore.join_frontmatter({"category": "other"}, "본문\n").encode("utf-8"))
    c = _cat(status.overview())
    assert c["current"] == "draft"
    assert c["steps"][2]["done"] is False


def test_mission_card(home):
    refstore.scaffold()
    day = mission._today(None)
    assert status.overview()["mission"]["exists"] is False
    assert status.overview()["mission"]["status"] is None
    path = mission._mission_path(day)
    path.write_bytes(refstore.join_frontmatter({"date": day, "kind": "촬영", "status": "open"}, "# m\n").encode("utf-8"))
    m = status.overview()["mission"]
    assert m["exists"] is True
    assert m["status"] == "open"
    assert m["kind"] == "촬영"
    assert m["date"] == day
    assert m["path"] == f"missions/{day}.md"


def test_recent_sorted_and_limited(home):
    (home / "drafts").mkdir()
    (home / "reports").mkdir()
    files = [
        (home / "drafts" / "d1.md", "draft", 100),
        (home / "drafts" / "d1-conti.md", None, 900),       # conti md는 제외
        (home / "drafts" / "d1-conti.html", "conti", 700),
        (home / "drafts" / "d2.md", "draft", 300),
        (home / "reports" / "r1.html", "report", 600),
        (home / "reports" / "r2.html", "report", 200),
        (home / "drafts" / "d3.md", "draft", 500),
        (home / "drafts" / "d4.md", "draft", 400),
    ]
    for path, _, mtime in files:
        path.write_text("x", encoding="utf-8")
        os.utime(path, (mtime, mtime))
    recent = status.overview()["recent"]
    assert [(r["path"], r["kind"]) for r in recent] == [
        ("drafts/d1-conti.html", "conti"), ("reports/r1.html", "report"),
        ("drafts/d3.md", "draft"), ("drafts/d4.md", "draft"), ("drafts/d2.md", "draft"),
    ]
    assert [r["mtime"] for r in recent] == [700, 600, 500, 400, 300]


def test_main_category_filter(home):
    standard_set(home)
    refstore.init_category("other")
    assert len(status.main([])["categories"]) == 2
    out = status.main([CAT])
    assert len(out["categories"]) == 1
    assert out["categories"][0]["category"] == CAT


def test_frontmatterless_notes_do_not_break_overview(home):
    standard_set(home)
    (home / "drafts").mkdir(exist_ok=True)
    (home / "drafts" / "notes.md").write_text("그냥 메모\n", encoding="utf-8")
    (home / "mine").mkdir(exist_ok=True)
    (home / "mine" / "scratch.md").write_text("# 메모\n", encoding="utf-8")
    c = _cat(status.overview())
    assert c["steps"][2]["detail"] == "기획안 0개"
    assert c["steps"][4]["detail"] == "내 영상 0개"


def test_crlf_draft_does_not_break_overview(home):
    standard_set(home)
    path = _save_draft(home)
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    out = status.overview()
    assert _cat(out)["category"] == CAT


def test_broken_mission_md_does_not_break_overview(home):
    day = mission._today(None)
    path = mission._mission_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("프런트매터 없음\n", encoding="utf-8")
    card = status.overview()["mission"]
    assert card["exists"] is True and card["status"] is None and card["kind"] is None


# --- 영상별 단계 / 다음 할 일 -----------------------------------------------------

def _md(home, rel, fm, body="본문\n"):
    path = home / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))
    return path


def _video(home, name, *, date=None, conti=False, filmed=False, diagnosed=False, **extra):
    fm = {"category": CAT, "title": name, **extra}
    if date:
        fm["date"] = date
    if filmed:
        fm["filmed_at"] = "2026-10-25"
    path = _md(home, f"drafts/{name}.md", fm)
    if conti:
        _md(home, f"drafts/{name}-conti.md", {"category": CAT})
    if diagnosed:
        _md(home, f"mine/{name}.md", {"category": CAT, "target": f"./drafts/{name}.md"})
    return path


def _videos(home):
    refstore.init_category(CAT)
    return _cat(status.overview())["videos"]


def test_video_steps_from_files_and_filmed(home):
    standard_set(home)
    d = _save_draft(home)
    stem = d.stem
    (d.parent / f"{stem}-conti.md").write_text("---\ncategory: mukbang\n---\n", encoding="utf-8")
    v = _cat(status.overview())["videos"][0]
    assert [s["key"] for s in v["steps"]] == ["draft", "polish", "conti", "filmed", "captions", "diagnose"]
    assert {s["key"]: s["done"] for s in v["steps"]} == {
        "draft": True, "polish": False, "conti": True, "filmed": False, "captions": False, "diagnose": False}
    assert [s["optional"] for s in v["steps"]] == [False, True, False, False, True, False]
    assert v["next"] == "filmed" and v["progress"] == 2 and v["filmed_at"] is None
    refstore.update_frontmatter(d, {"filmed_at": "2026-10-25"})
    d.write_text(d.read_text(encoding="utf-8") + "\n## 패키징\n- x\n", encoding="utf-8")
    (d.parent / f"{stem}-captions.mp4").write_bytes(b"")
    (home / "mine").mkdir(exist_ok=True)
    (home / "mine" / "x.md").write_text(f"---\ncategory: {CAT}\ntarget: drafts\\{d.name}\n---\n", encoding="utf-8")
    v = _cat(status.overview())["videos"][0]
    assert all(s["done"] for s in v["steps"]) and v["next"] is None and v["progress"] == 4
    assert v["filmed_at"] == "2026-10-25"
    assert v["path"] == f"drafts/{d.name}"
    assert v["hash"] == conti.content_hash(d)


@pytest.mark.parametrize("value, expected", [
    ("2026-10-25", "2026-10-25"), (datetime.date(2026, 10, 25), "2026-10-25"),
    ("2026-13-40", None), ("yes", None), ("", None), (None, None), (True, None), (20261025, None)])
def test_filmed_date(value, expected):
    assert status.filmed_date(value) == expected


def test_videos_sorted_by_progress_then_date_then_name(home):
    _video(home, "a", date="2026-10-20", conti=True)
    _video(home, "b", date="2026-10-25", conti=True)
    _video(home, "c", date="2026-10-26")
    _video(home, "z", date="2026-10-01", conti=True, filmed=True, diagnosed=True)
    expected = ["drafts/b.md", "drafts/a.md", "drafts/c.md", "drafts/z.md"]
    assert [v["path"] for v in _videos(home)] == expected
    for i, name in enumerate(["a", "b", "c", "z"]):
        os.utime(home / "drafts" / f"{name}.md", (1000 * (4 - i), 1000 * (4 - i)))
    assert [v["path"] for v in _videos(home)] == expected


def test_videos_tie_breaks_by_name_descending(home):
    _video(home, "a", date="2026-10-20")
    _video(home, "b", date="2026-10-20")
    assert [v["path"] for v in _videos(home)] == ["drafts/b.md", "drafts/a.md"]


def test_broken_draft_is_left_out_of_videos(home):
    standard_set(home)
    _video(home, "ok")
    (home / "drafts" / "broken.md").write_text("---\n: [\n---\n", encoding="utf-8")
    assert [v["path"] for v in _videos(home)] == ["drafts/ok.md"]


def test_cp949_draft_is_left_out_and_overview_still_works(home):
    standard_set(home)
    _video(home, "ok")
    (home / "drafts" / "notepad.md").write_bytes(f"---\ncategory: {CAT}\ntitle: 메모장\n---\n본문\n".encode("cp949"))
    assert [v["path"] for v in _videos(home)] == ["drafts/ok.md"]


def test_cp949_mine_is_left_out_and_overview_still_works(home):
    _video(home, "a")
    (home / "mine").mkdir(exist_ok=True)
    (home / "mine" / "notepad.md").write_bytes(
        f"---\ncategory: {CAT}\ntarget: drafts/a.md\ntitle: 메모장\n---\n".encode("cp949"))
    videos = _videos(home)
    assert videos[0]["steps"][-1]["done"] is False
    c = _cat(status.overview())
    assert c["steps"][-1]["done"] is False


def test_draft_deleted_before_reading_is_left_out(home, monkeypatch):
    """목록을 만든 뒤 읽기 전에 지워진 기획안은 건너뛰고 나머지를 보여 준다."""
    from scripts.filestore import FileDocStore

    standard_set(home)
    _video(home, "ok")
    _video(home, "gone")

    real = FileDocStore.read

    def flaky(self, key):
        if key.endswith("gone.md"):
            raise FileNotFoundError(key)
        return real(self, key)

    monkeypatch.setattr(FileDocStore, "read", flaky)
    assert [v["path"] for v in _videos(home)] == ["drafts/ok.md"]
