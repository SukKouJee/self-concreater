"""미션·회고·프로필 쓰기의 바이트와 조회 순서를 고정한다(S3 Task 6 특성 테스트).

저장소로 옮기기 전 코드에서 통과해야 하고, 옮긴 뒤에도 같은 바이트·같은 순서여야 한다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import mission, motif, refstore, status
from scripts.common import ScriptError
from tests.helpers.tree_snapshot import changes, tree
from tests.test_mission import PROFILE, mission_json
from tests.helpers.paths import doc_key

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-10-31"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    (tmp_path / "profile.md").write_bytes(PROFILE.encode("utf-8"))
    return tmp_path


def put(home, rel, data):
    path = home / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def tmp_json(home, name, data):
    return put(home, f"workspace/tmp/{name}", json.dumps(data, ensure_ascii=False))


def fm_md(**fm):
    lines = "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in fm.items())
    return f"---\n{lines}---\n# t\n"


MISSION_MD = ("---\ndate: 2026-10-31\nstage: 관찰\nkind: 촬영\nstatus: open\nsource: M-1\nminutes: 20\n"
              "difficulty: null\nskip_reason: null\nschema_version: 1\n---\n# 2026-10-31 미션\n\n## 미션\n"
              "### 탑뷰 (20분 · 촬영)\n\n1. 찍기\n\n## 제출\n\n## 피드백\n")


# --- give / swap ----------------------------------------------------------------

def test_save_new_bytes_and_tree(home):
    src = mission_json(home)
    before = tree(home)
    result = mission.save(src, DAY)
    assert result == {"status": "saved", "path": f"missions/{DAY}.md", "swapped_from": None}
    assert changes(before, tree(home)) == {
        "added": [f"missions/{DAY}.md"], "removed": ["workspace/tmp/mission.json"], "modified": []}
    data = (home / "missions" / f"{DAY}.md").read_bytes()
    assert b"\r" not in data and not data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8")
    assert text.startswith("---\ndate: '2026-10-31'\nstage: 관찰\nkind: 촬영\nstatus: open\n")
    assert "### 탑뷰 손 클로즈업 15초 찍기 (20분 · 촬영)\n- 왜: ★카드의 탑뷰 기법을 연습\n- 재료: M-mukbang-001\n" in text
    assert text.endswith("- 결과물: 15초 클립 1개\n\n## 제출\n\n## 피드백\n")


def test_save_keeps_handoff_when_invalid(home):
    src = mission_json(home, kind="없는종류")
    with pytest.raises(ScriptError) as e:
        mission.save(src, DAY)
    assert e.value.code == "invalid_mission"
    assert src.exists()
    assert not (home / "missions").exists()


def test_save_exists_and_swap(home):
    mission.save(mission_json(home), DAY)
    first = (home / "missions" / f"{DAY}.md").read_bytes()
    src = mission_json(home, title="다른 미션")
    with pytest.raises(ScriptError) as e:
        mission.save(src, DAY)
    assert (e.value.code, e.value.message) == ("mission_exists", f"{DAY} 미션이 이미 있습니다. 바꾸려면 swap(--replace)")
    assert src.exists() and (home / "missions" / f"{DAY}.md").read_bytes() == first
    result = mission.save(src, DAY, replace=True)
    assert result == {"status": "saved", "path": f"missions/{DAY}.md", "swapped_from": "탑뷰 손 클로즈업 15초 찍기"}
    fm, body = refstore.read(doc_key(home / "missions" / f"{DAY}.md"))
    assert fm["swapped_from"] == "탑뷰 손 클로즈업 15초 찍기" and "### 다른 미션 (20분" in body
    assert not src.exists()


def test_save_accepts_relative_and_absolute_handoff(home, monkeypatch):
    mission_json(home)
    monkeypatch.chdir(home)
    mission.save(Path("workspace/tmp/mission.json"), DAY)       # 상대 경로는 현재 폴더 기준(테스트에서는 절대로 바꿔 준다)
    assert (home / "missions" / f"{DAY}.md").exists()


def test_save_absolute_handoff_removed(home):
    src = mission_json(home, title="절대 경로")
    mission.save(src.resolve(), "2026-11-01")
    assert not src.exists()


# --- submit / skip --------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    MISSION_MD.encode("utf-8"),
    MISSION_MD.replace("\n", "\r\n").encode("utf-8"),
    b"\xef\xbb\xbf" + MISSION_MD.encode("utf-8"),
], ids=["lf", "crlf", "bom"])
def test_submit_bytes_always_lf_without_bom(home, raw):
    put(home, f"missions/{DAY}.md", raw)
    before = tree(home)
    result = mission.submit("C:\\clips\\a.mp4", "right", "잘 됨", DAY, "손 촬영")
    assert result == {"status": "submitted", "date": DAY, "difficulty": "right"}
    assert changes(before, tree(home)) == {"added": [], "removed": [], "modified": [f"missions/{DAY}.md"]}
    expected = (MISSION_MD.replace("status: open", "status: submitted").replace("difficulty: null", "difficulty: right")
                .replace("## 제출\n\n## 피드백\n",
                         "## 제출\n- 한 일: 손 촬영\n- 파일: a.mp4\n- 난이도: right\n\n## 피드백\n잘 됨\n"))
    if raw.replace(b"\xef\xbb\xbf", b"").startswith(b"---\r\n"):       # 프런트매터는 LF로 다시 쓰고 본문은 원래 줄바꿈을 따른다
        head, body = expected.split("---\n# ", 1)
        expected = head + "---\n# " + body.replace("\n", "\r\n")
    assert (home / "missions" / f"{DAY}.md").read_bytes() == expected.encode("utf-8")


def test_submit_errors(home):
    with pytest.raises(ScriptError) as e:
        mission.submit("", "right", "x", DAY)
    assert (e.value.code, e.value.message) == ("no_mission", f"{DAY} 미션이 없습니다. 먼저 /yt-mission today")
    with pytest.raises(ScriptError) as e:
        mission.submit("", "bad", "x", DAY)
    assert e.value.code == "usage"


def test_skip_keeps_crlf_and_clears(home):
    put(home, f"missions/{DAY}.md", MISSION_MD.replace("\n", "\r\n"))
    assert mission.skip("  바빠서\n오늘은 ", DAY) == {"status": "skipped", "date": DAY}
    data = (home / "missions" / f"{DAY}.md").read_bytes()
    assert "status: skipped\r\n".encode() in data and "skip_reason: 바빠서 오늘은\r\n".encode() in data
    assert data.endswith("## 피드백\r\n".encode("utf-8"))
    mission.skip("", DAY)
    assert b"skip_reason: null" in (home / "missions" / f"{DAY}.md").read_bytes()


def test_skip_without_mission(home):
    with pytest.raises(ScriptError) as e:
        mission.skip("x", DAY)
    assert e.value.code == "no_mission"


# --- review / profile -----------------------------------------------------------

REVIEW = {"summary": "좋은 주", "repeated_weakness": ["훅"], "next_week": ["쇼츠"],
          "profile_proposal": {"약점": "훅 약함"}}


def test_review_save_new_then_resave_keeps_memo_and_newline(home):
    src = tmp_json(home, "review.json", REVIEW)
    before = tree(home)
    result = mission.review_save("2026-W44", src)
    assert result == {"status": "saved", "path": "reviews/2026-W44.md"}
    assert changes(before, tree(home))["added"] == ["reviews/2026-W44.md"]
    path = home / "reviews" / "2026-W44.md"
    data = path.read_bytes()
    assert b"\r" not in data and data.endswith("## 내 메모\n".encode("utf-8"))
    # 사용자가 CRLF로 바꾸고 메모를 쓴 뒤 다시 저장
    crlf = data.replace(b"\n", b"\r\n") + "내 생각입니다\r\n\r\n## 그 뒤\r\n끝\r\n".encode("utf-8")
    crlf = crlf.replace(b"schema_version: 1", b"schema_version: 1\r\nmine: keep")
    path.write_bytes(crlf)
    mission.review_save("2026-W44", tmp_json(home, "review.json", {**REVIEW, "summary": "수정"}))
    out = path.read_bytes()
    assert b"\n" not in out.replace(b"\r\n", b"")
    assert out.endswith("## 내 메모\r\n내 생각입니다\r\n\r\n## 그 뒤\r\n끝\r\n".encode("utf-8"))
    assert b"mine: keep" in out and b"approved: false" in out and "수정".encode() in out


def test_review_save_broken_old_review_message(home):
    put(home, "reviews/2026-W44.md", "---\n: [bad\n---\nbody")
    src = tmp_json(home, "review.json", REVIEW)
    with pytest.raises(ScriptError) as e:
        mission.review_save("2026-W44", src)
    assert e.value.code == "bad_frontmatter"
    assert "기존 회고 파일을 읽을 수 없습니다(reviews/2026-W44.md): " in e.value.message
    assert e.value.message.endswith("파일을 고치거나 삭제한 뒤 다시 저장해 주세요")
    assert src.exists()


@pytest.mark.parametrize("prefix,nl", [(b"", "\n"), (b"", "\r\n"), (b"\xef\xbb\xbf", "\n")], ids=["lf", "crlf", "bom"])
def test_profile_update_bytes(home, prefix, nl):
    put(home, "profile.md", prefix + PROFILE.replace("\n", nl).encode("utf-8"))
    src = tmp_json(home, "upd.json", {"하루 시간": "30분", "성장 단계": "모방"})
    result = mission.profile_update(src, "2026-10-31")
    assert result == {"status": "updated", "sections": ["하루 시간", "성장 단계"], "updated": "2026-10-31", "reviews": []}
    expected = (PROFILE.replace("updated: 2026-10-01", "updated: '2026-10-31'")
                .replace("## 하루 시간\n20분", "## 하루 시간\n30분")
                .replace("## 성장 단계\n관찰", "## 성장 단계\n모방").replace("\n", nl))
    assert (home / "profile.md").read_bytes() == expected.encode("utf-8")     # BOM은 되살리지 않는다
    assert not src.exists()


def test_profile_update_errors_keep_handoff(home):
    src = tmp_json(home, "upd.json", {"없는": "x"})
    with pytest.raises(ScriptError) as e:
        mission.profile_update(src)
    assert e.value.code == "invalid_profile_update" and src.exists()
    (home / "profile.md").unlink()
    src = tmp_json(home, "upd.json", {"강점": "x"})
    with pytest.raises(ScriptError) as e:
        mission.profile_update(src)
    assert (e.value.code, e.value.message) == ("no_profile", "profile.md가 없습니다. 먼저 /yt-init")
    assert src.exists()


def test_profile_update_lists_review_stems(home):
    for name in ("2026-W02.md", "2026-W01.md", "notes.md", "memo.txt"):
        put(home, f"reviews/{name}", "---\nweek: x\n---\n")
    put(home, "reviews/sub/inner.md", "x")
    src = tmp_json(home, "upd.json", {"강점": "끈기"})
    assert mission.profile_update(src)["reviews"] == ["2026-W01", "2026-W02", "notes"]


# --- 조회 순서 ------------------------------------------------------------------

def test_drafts_name_descending_including_conti(home):
    for name in ("b", "A-2", "a", "B-conti", "c-conti"):
        put(home, f"drafts/{name}.md", fm_md(title=name, status="draft", category="travel"))
    put(home, "drafts/z.html", "x")
    expected = [p.relative_to(home).as_posix() for p in sorted((home / "drafts").glob("*.md"), reverse=True)[:3]]
    got = mission._drafts()
    assert [d["path"] for d in got] == expected and len(got) == 3
    assert any(d["path"].endswith("-conti.md") for d in mission._drafts(limit=10))
    assert got[0] == {"path": expected[0], "title": Path(expected[0]).stem, "status": "draft", "category": "travel"}
    assert len(mission._drafts(limit=10)) == 5


def test_absent_folders_give_empty_lists(home):
    assert mission._drafts() == [] and mission._diagnosis_gaps() == [] and mission._starred() == []
    assert mission._missions_between("2026-01-01", "2026-12-31") == []


def test_diagnosis_gaps_newest_first_and_filtering(home):
    rows = [("old", ["훅"], [], 0), ("mid", ["훅", "길이"], ["훅"], 100), ("new", ["톤"], [], 300),
            ("none", ["훅"], ["훅"], 400), ("tie", ["톤"], [], 300)]
    for i, (name, gaps, intentional, offset) in enumerate(rows):
        p = put(home, f"mine/{name}.md", fm_md(category="travel", date=f"2026-10-{20 + i}", nearest="A",
                                              gaps=gaps, intentional=intentional))
        os.utime(p, (1_700_000_000 + offset, 1_700_000_000 + offset))
    got = [g["name"] for g in mission._diagnosis_gaps()]
    glob_order = [p.stem for p in sorted((home / "mine").glob("*.md"))]
    assert got == [n for n in glob_order if n in ("new", "tie")] + ["mid", "old"]      # 같은 mtime은 glob 순서 유지
    assert mission._diagnosis_gaps()[-2] == {
        "name": "mid", "category": "travel", "date": "2026-10-21", "nearest": "A", "gaps": ["길이"]}


def test_missions_between_string_compare_and_skip_broken(home):
    names = ["2026-10-24", "2026-10-25", "2026-10-30", "2026-10-31", "notes", "2026-10-3", "2026-10-30-extra", "20261030"]
    for n in names:
        put(home, f"missions/{n}.md", fm_md(date=n, status="submitted", kind="촬영", minutes=20))
    put(home, "missions/2026-10-27.md", "---\n: [bad\n---\nx")
    got = mission._missions_between("2026-10-25", "2026-10-30")
    assert [r["date"] for r in got] == [n for n in sorted(names) if "2026-10-25" <= n <= "2026-10-30"]
    assert "2026-10-30-extra" not in [r["date"] for r in got]      # 문자열 비교: 접미사가 붙으면 끝 경계를 넘는다
    assert mission._missions_between("2027-01-01", "2027-01-02") == []


CARDS = ("---\nschema_version: 1\n---\n# idx\n\n## 모티브 카드\n\n### ★ M-travel-001 · 기법 · 탑뷰 손\n- 만든 날: 2026-10-28\n\n"
         "### M-travel-002 · 흐름 · 롱테이크\n- 만든 날: 2026-10-20\n")


def test_starred_across_categories_in_name_order(home, monkeypatch):
    cards = [{"id": "M-1", "type": "기법", "title": "탑뷰", "starred": True},
             {"id": "M-2", "type": "흐름", "title": "롱", "starred": False}]
    monkeypatch.setattr(motif, "parse_cards", lambda body: cards)
    put(home, "refs/ref_b/index.md", "---\nschema_version: 1\n---\n")
    put(home, "refs/ref_a/index.md", "---\nschema_version: 1\n---\n")
    assert mission._starred() == [{"category": "a", "id": "M-1", "type": "기법", "title": "탑뷰"},
                                  {"category": "b", "id": "M-1", "type": "기법", "title": "탑뷰"}]


def test_starred_category_without_index_propagates(home):
    (home / "refs" / "ref_x").mkdir(parents=True)
    with pytest.raises(OSError):
        mission._starred()


def test_week_json(home):
    put(home, "refs/ref_a/index.md", CARDS)
    put(home, "mine/m.md", fm_md(category="a", date="2026-10-28", nearest="A", gaps=["훅"], intentional=[]))
    put(home, "mine/old.md", fm_md(category="a", date="2026-09-01", nearest="A", gaps=["훅"], intentional=[]))
    put(home, "missions/2026-10-26.md",
        fm_md(date="2026-10-26", status="submitted", kind="촬영", difficulty="hard", minutes=20))
    put(home, "missions/2026-10-27.md",
        fm_md(date="2026-10-27", status="skipped", kind="관찰", skip_reason="바빠서", minutes=15))
    got = mission.week("2026-W44")
    assert got["range"] == ["2026-10-26", "2026-11-01"]
    assert got["missions"] == {"total": 2, "submitted": 1, "skipped": 1, "open": 0, "completion": 0.5}
    assert got["cards_created"] == 1 and [d["name"] for d in got["diagnosis_gaps"]] == ["m"]
    assert got["skip_reasons"] == ["바빠서"] and got["difficulty"] == {"hard": 1}
    assert got["kinds"] == {"촬영": 1, "관찰": 1}
    assert [i["date"] for i in got["items"]] == ["2026-10-26", "2026-10-27"]
    assert [r["category"] for r in got["refs"]] == ["a"]


def test_context_whole_json(home):
    put(home, "refs/ref_a/index.md", CARDS)
    put(home, "drafts/d1.md", fm_md(title="D1", status="draft", category="a"))
    put(home, "mine/m.md", fm_md(category="a", date="2026-10-28", nearest="A", gaps=["훅"], intentional=[]))
    put(home, "missions/2026-10-30.md", fm_md(date="2026-10-30", status="skipped", kind="촬영", minutes=25,
                                              skip_reason="피곤"))
    put(home, f"missions/{DAY}.md", MISSION_MD)
    got = mission.context(DAY)
    assert got["date"] == DAY and got["stage"] == "관찰" and got["base_minutes"] == 20 and got["kinds"] == mission.KINDS
    assert got["recommended"] == {"minutes": 25, "adjust": "same"}
    assert got["starred"] == [{"category": "a", "id": "M-travel-001", "type": "기법", "title": "탑뷰 손"}]
    assert got["drafts"] == [{"path": "drafts/d1.md", "title": "D1", "status": "draft", "category": "a"}]
    assert got["gaps"] == [{"name": "m", "category": "a", "date": "2026-10-28", "nearest": "A", "gaps": ["훅"]}]
    assert [h["date"] for h in got["history"]] == ["2026-10-30"]
    assert got["today"]["title"] == "탑뷰" and got["today"]["status"] == "open"
    assert got["profile"]["하루 시간"] == "20분"


# --- 호출부 ---------------------------------------------------------------------

def test_mission_key_helper_and_record(home):
    assert mission._mission_key(DAY) == f"missions/{DAY}.md"
    put(home, f"missions/{DAY}.md", MISSION_MD)
    record = mission._mission_record(mission._mission_key(DAY))
    assert record["title"] == "탑뷰" and record == mission.context(DAY)["today"]


def test_status_card_after_save(home):
    day = mission._today(None)
    mission.save(mission_json(home), day)
    assert status.overview()["mission"]["path"] == f"missions/{day}.md"


def run_cli(home, *args):
    env = {**os.environ, "YT_COACH_HOME": str(home), "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "scripts.mission", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8")


def test_cli_flow_and_error_shape(home):
    put(home, "workspace/tmp/m.json", json.dumps({"title": "T", "kind": "촬영", "minutes": 20, "steps": ["a"]}))
    r = run_cli(home, "save", "--mission", "workspace/tmp/m.json", "--date", DAY)
    assert r.returncode == 0
    assert json.loads(r.stdout) == {"status": "saved", "path": f"missions/{DAY}.md", "swapped_from": None}
    r = run_cli(home, "submit", "x.mp4", "--difficulty", "easy", "--feedback", "ok", "--date", DAY)
    assert json.loads(r.stdout) == {"status": "submitted", "date": DAY, "difficulty": "easy"}
    r = run_cli(home, "skip", "--reason", "x", "--date", "2020-01-01")
    assert r.returncode != 0 and json.loads(r.stdout)["error"] == "no_mission"
    r = run_cli(home, "week", "2026-W44")
    assert json.loads(r.stdout)["week"] == "2026-W44"
