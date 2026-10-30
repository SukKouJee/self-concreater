"""motif·draft·polish·report·status의 반환 JSON과 파일 바이트를 고정한다(S3 Task 5 특성 테스트).

저장소로 옮기기 전 코드에서 통과해야 하고, 옮긴 뒤에도 같아야 한다. 골든은 옛 코드로 한 번 만든 값이다
(`GOLDEN_WRITE=1`로 다시 만들 수 있다. 평소에는 비교만 한다).
"""
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import draft, mission, motif, polish, report, status
from scripts.common import ScriptError
from tests.helpers.tree_snapshot import changes, copy_workfolder, tree
from tests.test_draft import DRAFT, home as dhome  # noqa: F401 - fixture
from tests.typify_fixtures import CAT

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "work_views"
REPO = Path(__file__).resolve().parents[1]
DRAFT_KEY = "drafts/2026-10-30-제주-한달살기.md"
BOM = b"\xef\xbb\xbf"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def golden(name: str, value, root: Path | None = None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=1)
    if root is not None:
        text = text.replace(json.dumps(str(root))[1:-1], "<ROOT>")
    path = GOLDEN / f"{name}.json"
    if os.environ.get("GOLDEN_WRITE") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    assert text == path.read_bytes().decode("utf-8")


class _Today:
    @staticmethod
    def today() -> datetime.date:
        return datetime.date(2026, 10, 31)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """골든에 날짜가 섞이지 않게 오늘을 2026-10-31로 고정한다."""
    for module in (motif, polish, draft):
        monkeypatch.setattr(module, "date", _Today)
    monkeypatch.setattr(mission, "_today", lambda value: value or "2026-10-31")


@pytest.fixture
def wf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = copy_workfolder(tmp_path / "w")
    monkeypatch.setenv("YT_COACH_HOME", str(root))
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for i, p in enumerate(files):                      # 복사본의 mtime을 고정해 status 결과를 재현한다
        os.utime(p, (1_700_000_000 + i * 10, 1_700_000_000 + i * 10))
    return root


def jsonable(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


# --- status -----------------------------------------------------------------------

def test_status_overview_whole_json(wf: Path) -> None:
    out = jsonable(status.overview())
    assert out["work_root"] == str(wf)
    golden("status_overview", out, wf)


def test_status_overview_category_and_unknown(wf: Path) -> None:
    golden("status_travel", jsonable(status.overview("travel")), wf)
    with pytest.raises(ScriptError) as e:
        status.overview("nope")
    assert e.value.code == "category_not_found"


def test_status_empty_and_refs_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    day = mission._today(None)
    assert status.overview() == {"initialized": False, "work_root": str(tmp_path), "categories": [],
                                 "mission": {"date": day, "exists": False, "status": None, "kind": None, "path": None},
                                 "recent": []}
    (tmp_path / "refs").mkdir()
    out = status.overview()
    assert out["initialized"] is True and out["categories"] == []


def test_status_recent_order_ties_and_filters(wf: Path) -> None:
    drafts, reports = wf / "drafts", wf / "reports"
    (drafts / "b-draft.md").write_bytes(b"---\ncategory: travel\n---\n# b\n")
    (drafts / "c-draft-conti.md").write_bytes(b"---\ncategory: travel\n---\n# c\n")
    (drafts / "notes.txt").write_bytes(b"x")
    (reports / "other.html").write_bytes(b"<p>x</p>")
    (reports / "third.html").write_bytes(b"<p>y</p>")
    for p in sorted(drafts.glob("*-conti.html")):
        os.utime(p, (5000, 5000))
    for p, t in [(drafts / "b-draft.md", 5000), (reports / "other.html", 5000), (reports / "third.html", 4000),
                 (drafts / "2026-10-30-제주-한달살기.md", 3000), (reports / "travel-2026-10-30.html", 2000),
                 (drafts / "c-draft-conti.md", 9000)]:
        os.utime(p, (t, t))
    recent = status.overview()["recent"]
    assert [r["kind"] for r in recent[:3]] == ["draft", "conti", "report"]      # 같은 mtime은 draft, conti, report 순
    assert all(not r["path"].endswith("-conti.md") for r in recent)
    golden("status_recent", jsonable(recent))


def test_status_skips_unreadable_and_broken_mission(wf: Path) -> None:
    (wf / "mine" / "cp949.md").write_bytes("---\ncategory: travel\n---\n한글".encode("cp949"))
    (wf / "drafts" / "bad.md").write_bytes(b"\xff\xfe broken")
    day = mission._today(None)
    (wf / "missions" / f"{day}.md").write_bytes(b"---\n: [\n---\n")
    out = status.overview()
    assert out["mission"]["exists"] is True and out["mission"]["status"] is None
    assert out["mission"]["path"] == f"missions/{day}.md"
    assert all(v["path"] != "drafts/bad.md" for c in out["categories"] for v in c["videos"])


def test_status_cli_matches_overview(wf: Path) -> None:
    proc = subprocess.run([sys.executable, "-X", "utf8", "-m", "scripts.status"], cwd=REPO, capture_output=True,
                          env={**os.environ, "YT_COACH_HOME": str(wf)}, check=True)
    cli, direct = json.loads(proc.stdout.decode("utf-8")), jsonable(status.overview())
    cli["mission"] = direct["mission"] = None                      # 서브프로세스는 오늘 날짜를 고정하지 않는다
    assert cli == direct


# --- report -----------------------------------------------------------------------

def _fix_run_at(home: Path) -> None:
    path = home / "workspace" / "typify" / f"{CAT}.json"
    data = json.loads(path.read_text("utf-8"))
    data["run_at"] = "2026-10-31T00:00:00"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_report_html_bytes_and_result(dhome: Path) -> None:
    _fix_run_at(dhome)
    before = tree(dhome)
    out = report.build(CAT, "2026-10-31")
    target = dhome / "reports" / f"{CAT}-2026-10-31.html"
    raw = target.read_bytes()
    assert out == {"status": "built", "path": f"reports/{CAT}-2026-10-31.html", "abs_path": str(target)}
    assert changes(before, tree(dhome)) == {"added": [f"reports/{CAT}-2026-10-31.html"], "removed": [], "modified": []}
    assert b"\r" not in raw and raw.startswith(b"<!doctype html>")
    golden("report_html_sha", {"sha256": sha(raw), "size": len(raw)})
    report.build(CAT, "2026-10-31")                        # 다시 만들면 같은 바이트로 덮는다
    assert target.read_bytes() == raw


def test_report_mine_history_without_typify_and_unknown_category(wf: Path) -> None:
    (wf / "workspace" / "typify" / "travel.json").unlink()
    report.build("travel", "2026-10-31")
    html = (wf / "reports" / "travel-2026-10-31.html").read_text("utf-8")
    assert "아직 유형화 결과가 없습니다" in html
    golden("report_no_typify_sha", {"sha256": sha((wf / "reports" / "travel-2026-10-31.html").read_bytes())})
    with pytest.raises(ScriptError) as e:
        report.build("nope")
    assert e.value.code == "category_not_found"


# --- polish -----------------------------------------------------------------------

CHOICE = {"title": "제주 한 달 살기 4단계", "hook_script": "말: 한 달 살면 얼마?\n자막: 제주 한 달",
          "hook_formula": "the-question",
          "thumbnail": {"words": ["제주"], "expression": "놀란 얼굴", "framing": "바스트", "background": "노랑"}}


def _hook_draft(root: Path, bom: bool, crlf: bool) -> str:
    text = (root / DRAFT_KEY).read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    (root / DRAFT_KEY).write_bytes((BOM if bom else b"") + text.encode("utf-8"))
    return sha((root / DRAFT_KEY).read_bytes())


@pytest.mark.parametrize("bom,crlf", [(False, False), (False, True), (True, False), (True, True)])
def test_polish_apply_bytes_bom_and_newline(wf: Path, bom: bool, crlf: bool) -> None:
    expected = _hook_draft(wf, bom, crlf)
    out = polish.apply(DRAFT_KEY, CHOICE, expected)
    raw = (wf / DRAFT_KEY).read_bytes()
    assert raw.startswith(BOM) is bom
    body = raw.removeprefix(BOM)
    assert (b"\r\n" in body) is crlf
    assert out["path"] == DRAFT_KEY and out["hash"] == sha(raw)
    golden(f"polish_apply_{int(bom)}{int(crlf)}", {"out": out, "sha": sha(raw)})


def test_polish_apply_rejects_stale_hash_and_missing(wf: Path) -> None:
    before = (wf / DRAFT_KEY).read_bytes()
    with pytest.raises(ScriptError) as e:
        polish.apply(DRAFT_KEY, CHOICE, "0" * 64)
    assert e.value.code == "draft_changed" and (wf / DRAFT_KEY).read_bytes() == before
    with pytest.raises(ScriptError) as e:
        polish.apply("drafts/none.md", CHOICE, "0" * 64)
    assert e.value.code == "draft_not_found"


def test_polish_context_and_conti_warning(wf: Path) -> None:
    h = sha((wf / DRAFT_KEY).read_bytes())
    out = polish.apply(DRAFT_KEY, CHOICE, h)
    assert any("콘티가 이미 있습니다" in w for w in out["warnings"])
    (wf / "drafts" / "2026-10-30-제주-한달살기-conti.md").unlink()
    out = polish.apply(DRAFT_KEY, CHOICE, out["hash"])
    assert not any("콘티가 이미 있습니다" in w for w in out["warnings"])


def test_polish_context_json(dhome: Path) -> None:
    saved = draft.save(CAT, _tmp_json(dhome, DRAFT))
    ctx = polish.context(saved["path"])
    assert ctx["draft"] == saved["path"] and ctx["conti_exists"] is False
    assert ctx["hash"] == sha((dhome / saved["path"]).read_bytes())
    (dhome / saved["path"].replace(".md", "-conti.md")).write_bytes(b"x")
    assert polish.context(saved["path"])["conti_exists"] is True
    golden("polish_context", jsonable({k: v for k, v in ctx.items() if k != "formulas"}))


def test_polish_cli_reads_relative_json(wf: Path) -> None:
    (wf / "workspace" / "tmp").mkdir(parents=True, exist_ok=True)
    (wf / "workspace" / "tmp" / "choice.json").write_text(json.dumps(CHOICE, ensure_ascii=False), encoding="utf-8")
    h = sha((wf / DRAFT_KEY).read_bytes())
    proc = subprocess.run([sys.executable, "-X", "utf8", "-m", "scripts.polish", "apply", DRAFT_KEY, "--choice",
                           "workspace/tmp/choice.json", "--hash", h], cwd=REPO, capture_output=True,
                          env={**os.environ, "YT_COACH_HOME": str(wf)}, check=True)
    assert json.loads(proc.stdout.decode("utf-8"))["path"] == DRAFT_KEY


# --- draft ------------------------------------------------------------------------

def _tmp_json(root: Path, data) -> Path:
    p = root / "workspace" / "tmp" / "d.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_draft_save_bytes_tree_and_collisions(dhome: Path) -> None:
    outs = []
    for _ in range(3):
        src = _tmp_json(dhome, DRAFT)
        before = tree(dhome)
        outs.append(draft.save(CAT, src))
        diff = changes(before, tree(dhome))
        assert diff["removed"] == ["workspace/tmp/d.json"] and diff["modified"] == [] and len(diff["added"]) == 1
    assert [o["path"] for o in outs] == ["drafts/2026-10-31-편의점-라면-3단계-맵기-도전.md",
                                         "drafts/2026-10-31-편의점-라면-3단계-맵기-도전-2.md",
                                         "drafts/2026-10-31-편의점-라면-3단계-맵기-도전-3.md"]
    raw = (dhome / outs[0]["path"]).read_bytes()
    assert not raw.startswith(BOM) and b"\r" not in raw and "## 내 메모".encode() in raw
    assert (dhome / outs[1]["path"]).read_bytes() == raw
    golden("draft_save", {"outs": outs, "sha": sha(raw)})


def test_draft_save_empty_slug_and_bad_category_keeps_tmp(dhome: Path) -> None:
    out = draft.save(CAT, _tmp_json(dhome, {**DRAFT, "title": "!!!"}))
    assert out["path"] == "drafts/2026-10-31-draft.md"
    src = _tmp_json(dhome, DRAFT)
    with pytest.raises(ScriptError) as e:
        draft.save("nope", src)
    assert e.value.code == "category_not_found" and src.exists()


def test_draft_trash_moves_everything(wf: Path) -> None:
    before = tree(wf)
    out = draft.trash(DRAFT_KEY)
    stamp = out["trash"]
    assert re.fullmatch(r"workspace/trash/[^/]+", stamp)
    diff = changes(before, tree(wf))
    assert all(r.startswith(("drafts/", "workspace/conti_history/", "workspace/footage_match/")) for r in diff["removed"])
    assert sorted(f"{stamp}/{r}" for r in diff["removed"]) == diff["added"]
    assert diff["modified"] == [] and out["moved"][-1] == DRAFT_KEY
    golden("draft_trash", {"moved": out["moved"], "removed": diff["removed"]})


def test_draft_trash_errors(wf: Path) -> None:
    for value, code in [("drafts/none.md", "draft_not_found"), ("../x.md", "bad_path"),
                        ("drafts/2026-10-30-제주-한달살기-conti.md", "bad_path"), ("profile.md", "bad_path")]:
        with pytest.raises(ScriptError) as e:
            draft.trash(value)
        assert e.value.code == code


def test_draft_sources_context_and_archetype(dhome: Path) -> None:
    golden("draft_sources", jsonable(draft.sources(CAT)))
    idx = (dhome / "refs" / f"ref_{CAT}" / "index.md").read_text("utf-8")
    first = re.search(r"^### (A\d+)\. ", idx, re.M)
    if first:
        golden("draft_context_archetype", jsonable(draft.context(CAT, first.group(1))))
        assert draft.find_archetype(CAT, first.group(1))["id"] == first.group(1)
    assert draft.find_archetype(CAT, "Z9") is None


# --- motif ------------------------------------------------------------------------

CARD = {"type": "technique", "title": "영수증 클로즈업", "idea": "비용을 영수증으로 먼저 보여 준다.",
        "how": ["영수증", "합계"], "basis": "비용 공개", "evidence": ["a00"]}


def test_motif_reads(dhome: Path) -> None:
    golden("motif_context", jsonable(motif.context(CAT)))
    golden("motif_cards", jsonable(motif.card_details(CAT)))
    assert motif._profile() is None
    profile = "---" + chr(10) + "schema_version: 1" + chr(10) + "---" + chr(10) + "## 채널" + chr(10) + "- 먹방 채널" + chr(10) + chr(10) + "## 목표" + chr(10) + "구독자 늘리기" + chr(10)
    (dhome / "profile.md").write_bytes(profile.encode())
    golden("motif_profile", jsonable(motif._profile()))
    (dhome / "profile.md").unlink()
    allowed_with = motif.allowed_types(CAT)
    (dhome / "workspace" / "typify" / f"{CAT}.json").unlink()
    golden("motif_allowed", {"with": allowed_with, "without": motif.allowed_types(CAT)})


def _cards(root: Path, data) -> Path:
    p = root / "workspace" / "tmp" / "cards.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.mark.parametrize("variant", ["lf", "crlf", "bom_lf"])
def test_motif_add_star_bytes(dhome: Path, variant: str) -> None:
    path = dhome / "refs" / f"ref_{CAT}" / "index.md"
    text = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    shaped = text.replace("\n", "\r\n") if variant == "crlf" else text
    path.write_bytes((BOM if variant == "bom_lf" else b"") + shaped.encode("utf-8"))
    before = tree(dhome)
    src = _cards(dhome, [CARD])
    out = motif.add(CAT, src)
    assert not src.exists()
    star = motif.star(CAT, out["ids"][0])
    diff = changes(before, tree(dhome))
    assert diff["modified"] == [f"refs/ref_{CAT}/index.md"] and diff["removed"] == [] and diff["added"] == []
    raw = path.read_bytes()
    assert not raw.startswith(BOM)                                  # 쓰면 BOM은 떨어진다
    assert (b"\r\n" in raw) is (variant == "crlf")
    golden(f"motif_add_{variant}", {"out": out, "star": star, "sha": sha(raw)})


def test_motif_add_failure_keeps_file_and_error_order(dhome: Path) -> None:
    src = _cards(dhome, [{**CARD, "evidence": ["nope"]}])
    before = tree(dhome)
    with pytest.raises(ScriptError) as e:
        motif.add(CAT, src)
    assert e.value.code == "invalid_cards" and tree(dhome) == before
    with pytest.raises(ScriptError) as e:
        motif.add("nope", dhome / "workspace" / "tmp" / "missing.json")
    assert e.value.code == "category_not_found"                       # 카테고리를 먼저 본다
    with pytest.raises(ScriptError) as e:
        motif.add(CAT, dhome / "workspace" / "tmp" / "missing.json")
    assert e.value.code == "invalid_cards"
    with pytest.raises(ScriptError) as e:
        motif.star(CAT, "M-mukbang-999")
    assert e.value.code == "card_not_found"


def test_motif_merge_techniques_bytes(dhome: Path) -> None:
    tech_path = dhome / "refs" / f"ref_{CAT}" / "techniques.md"
    tech = tech_path.read_text("utf-8")
    names = re.findall(r"^- \*\*(.+?)\*\*: ", tech, re.M)
    kind = re.search(r"^## (.+)$", tech, re.M).group(1)
    plan = _cards(dhome, [{"kind": kind, "keep": names[0], "merge": [names[1]]}] if len(names) >= 2 else [])
    out = motif.merge_techniques(CAT, plan)
    golden("motif_merge", {"out": out, "sha": sha(tech_path.read_bytes())})
    with pytest.raises(ScriptError) as e:
        motif.merge_techniques(CAT, dhome / "workspace" / "tmp" / "none.json")
    assert e.value.code == "invalid_plan"
