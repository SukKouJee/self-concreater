import json

import pytest

from scripts import cluster, refstore, report
from scripts.common import ScriptError
from tests.typify_fixtures import CAT, standard_set, write_video


def _json(home, name, data):
    path = home / "workspace" / "tmp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    standard_set(tmp_path)
    write_video(tmp_path, "a00", title="<script>alert(1)</script> 라면")
    cluster.run(CAT)
    cluster.apply(CAT, _json(tmp_path, "n.json", {
        "axes": {a: f"{a} 근거 문장" for a in ["structure", "hook", "narrative", "title", "pacing"]},
        "archetypes": [{"id": "A1", "name": "결과 먼저 몰아먹기"}, {"id": "A2", "name": "사이즈별 리스트 비교"}]}))
    return tmp_path


def test_report_writes_self_contained_html(home):
    out = report.build(CAT, date="2026-10-20")
    assert out["path"] == f"reports/{CAT}-2026-10-20.html"
    html = (home / out["path"]).read_text("utf-8")
    assert html.startswith("<!doctype html>")
    assert "<script src" not in html and "<link" not in html          # 외부 자원 없음
    assert "prefers-color-scheme: dark" in html
    for text in ["축별 판정", "아키타입 비교", "컷 길이 분포", "내 콘텐츠 근접도",
                 "structure 근거 문장", "결과 먼저 몰아먹기", "hook → body/story → cta"]:
        assert text in html, text


def test_report_escapes_titles(home):
    html = (home / report.build(CAT, date="2026-10-20")["path"]).read_text("utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_has_table_views_and_legends(home):
    html = (home / report.build(CAT, date="2026-10-20")["path"]).read_text("utf-8")
    assert html.count("<table") >= 3
    assert 'class="legend"' in html
    assert "<title>" in html                                         # 마크 hover 툴팁


def test_report_pacing_dots_per_ref(home):
    html = (home / report.build(CAT, date="2026-10-20")["path"]).read_text("utf-8")
    assert html.count('class="dot"') == 10


def test_report_mine_history(home):
    mine = home / "mine" / "draft-x.md"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(refstore.join_frontmatter({
        "target": "drafts/x.md", "category": CAT, "nearest": {"id": "A1", "name": "결과 먼저 몰아먹기"},
        "history": [{"date": "2026-10-20", "nearest": "A1", "structure": 0.3, "hook": 0.5},
                    {"date": "2026-10-22", "nearest": "A1", "structure": 0.6, "hook": 0.7}],
    }, "# 진단\n").encode("utf-8"))
    html = (home / report.build(CAT, date="2026-10-22")["path"]).read_text("utf-8")
    assert "draft-x" in html and "30% → 60%" in html


def test_report_without_typify(home, monkeypatch, tmp_path_factory):
    other = tmp_path_factory.mktemp("o")
    monkeypatch.setenv("YT_COACH_HOME", str(other))
    standard_set(other, n_a=3, n_b=0)
    out = report.build(CAT, date="2026-10-20")
    html = (other / out["path"]).read_text("utf-8")
    assert "아직 유형화 결과가 없습니다" in html


def test_report_unknown_category(home):
    with pytest.raises(ScriptError) as exc:
        report.build("nope", date="2026-10-20")
    assert exc.value.code == "category_not_found"


def test_main(home):
    assert report.main([CAT, "--date", "2026-10-20"])["path"].endswith(".html")


def test_rebuild_same_day_replaces_file_without_tmp(home):
    first = report.build(CAT, date="2026-10-20")
    path = home / first["path"]
    path.write_text("old", encoding="utf-8")
    again = report.build(CAT, date="2026-10-20")
    assert again["path"] == first["path"] and path.read_text("utf-8").startswith("<!doctype html>")
    assert list((home / "reports").glob("*.tmp")) == []


def test_report_written_through_write_atomic(home, monkeypatch):
    calls = []
    real = refstore.write_atomic
    monkeypatch.setattr(refstore, "write_atomic", lambda p, d: (calls.append(p.name), real(p, d))[1])
    report.build(CAT, date="2026-10-20")
    assert calls == [f"{CAT}-2026-10-20.html"]
