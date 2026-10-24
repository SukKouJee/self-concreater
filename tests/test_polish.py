"""다듬기(polish): 한국어 기준 점수·지적, 레퍼런스 근거, 기획안 반영."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts import conti, polish
from scripts.common import ScriptError
from scripts.videomd import HOOK_TYPES
from tests.test_conti import CONTI, conti_file, draft_md, home  # noqa: F401 - fixtures
from tests.typify_fixtures import CAT

ROOT = Path(__file__).resolve().parents[1]

THUMB = {"words": ["끝판왕", "도전"], "expression": "입 벌린 놀란 얼굴", "framing": "바스트, 오른쪽에 컵라면",
         "background": "노랑 단색, 글자는 검정 굵게"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def body_of(path: Path) -> str:
    return path.read_bytes().decode("utf-8").split("\n---", 1)[1]


def hooks_data(n: int = 5) -> list[dict]:
    return [{"script": f"말: 후보 {i} 일단 먹어 봅니다", "formula": "the-question", "hook_type": "question",
             "why": f"이유 {i}"} for i in range(n)]


def titles_data(n: int = 10) -> list[dict]:
    return [{"title": f"컵라면 {i}단계 도전", "why": f"이유 {i}"} for i in range(n)]


def llm_output() -> dict:
    return {"hooks": hooks_data(), "titles": titles_data(), "thumbnail": dict(THUMB)}


def choice(**patch) -> dict:
    base = {"title": "컵라면 4단계 전부 먹어 봤다", "hook_script": "말: 4단계 다 먹을 수 있을까?\n자막: 맵기 4단계",
            "hook_formula": "the-question", "thumbnail": dict(THUMB)}
    base.update(patch)
    return base


# --- 점수 · 지적 -------------------------------------------------------------------

def test_display_width():
    assert polish.display_width("라면") == 4
    assert polish.display_width("abc") == 3
    assert polish.display_width("라면 5개!") == 9
    assert polish.display_width("🔥") == 2


def test_lint_title_cuts_and_issues():
    result = polish.lint_title("가" * 30)
    assert result["width"] == 60
    assert result["mobile_cut"] is True and result["desktop_cut"] is False
    assert "숫자가 없음" in result["issues"]
    assert result["score"] < 100
    clean = polish.lint_title("라면 5개 먹방")
    assert clean["issues"] == [] and clean["score"] == 100 and clean["has_number"] is True
    assert clean["patterns"] == ["number"]


def test_lint_title_desktop_cut_and_punctuation_and_overlap():
    long = polish.lint_title("가" * 51 + "1")
    assert long["desktop_cut"] is True and long["mobile_cut"] is True
    assert polish.lint_title("라면 5개!!?")["issues"] == ["느낌표·물음표가 3개 — 많으면 스팸처럼 보임"]
    over = polish.lint_title("라면 5개 도전", thumb_words=["라면", "대박"])
    assert any("라면" in i and "썸네일" in i for i in over["issues"])


def test_lint_thumbnail_limits():
    assert polish.lint_thumbnail(["하나", "둘", "셋"], "제목")["over_limit"] is False
    result = polish.lint_thumbnail(["하나", "둘", "셋", "넷"], "라면 도전")
    assert result["over_limit"] is True and result["count"] == 4
    assert any("3단어" in i for i in result["issues"])
    repeat = polish.lint_thumbnail(["라면 대박"], "라면 5개 도전")
    assert repeat["overlap"] == ["라면"] and repeat["count"] == 2
    assert repeat["score"] < 100


def test_score_hook_fits_hook_section():
    script = "말: " + "가" * 20
    long = polish.score_hook(script, 2.0, 7.0)
    assert long["chars"] == 20 and long["speak_sec"] == pytest.approx(2.86, abs=0.01)
    assert long["fits"] is False
    assert polish.score_hook(script, 3.0, 7.0)["fits"] is True      # 0.5초 여유
    assert polish.score_hook(script, None, 7.0)["fits"] is True
    greeting = polish.score_hook("말: 안녕하세요 여러분\n자막: 라면 5개", 5.0, 7.0)
    assert greeting["opens_with_greeting"] is True and greeting["has_number"] is True
    assert greeting["score"] < polish.score_hook("말: 라면 5개 먹어 봅니다", 5.0, 7.0)["score"]


# --- 레퍼런스 근거 ------------------------------------------------------------------

def test_ref_stats_and_evidence(home):
    stats = polish.ref_stats(CAT)
    assert stats["ref"] == 10
    assert stats["hook_type"] == {"result_first": 8, "question": 1, "situation": 1}
    assert stats["title_pattern"] == {"number+exclaim": 7, "question+bracket": 3}
    assert 1 <= len(stats["examples"]) <= 8
    assert polish.evidence_hook("result_first", stats) == "이 훅 유형은 내 레퍼런스 10개 중 8개"
    assert polish.evidence_hook("shock_line", stats) == "내 레퍼런스에는 없는 훅 유형"
    assert "7개" in polish.evidence_title("라면 5개 먹방!", stats)
    assert "0개" in polish.evidence_title("그냥 제목", stats)


def test_ref_stats_excludes_mine(home):
    from tests.typify_fixtures import write_video
    write_video(home, "mine01", role="mine", hook="shock_line")
    assert polish.ref_stats(CAT)["ref"] == 10
    assert "shock_line" not in polish.ref_stats(CAT)["hook_type"]


def test_ref_stats_without_category():
    assert polish.ref_stats(None) == {"ref": 0, "hook_type": {}, "title_pattern": {}, "examples": []}


# --- context · evaluate ------------------------------------------------------------

def test_context_shape(draft_md, home):
    ctx = polish.context(draft_md.relative_to(home).as_posix())
    assert ctx["draft"] == draft_md.relative_to(home).as_posix()
    assert ctx["category"] == CAT and ctx["title"] == "컵라면 4단계 | 도전"
    assert ctx["hook"] == {"label": "hook", "start": 0.0, "end": 2.0, "script": "자막: 4단계 다 먹을 수 있을까?"}
    assert ctx["sections"] == ["hook", "body", "outro"]
    assert len(ctx["formulas"]) == 21 and "match" not in ctx["formulas"][0]
    assert ctx["hash"] == sha(draft_md) and ctx["conti_exists"] is False
    assert ctx["refs"]["ref"] == 10 and ctx["cps"] == 7.0


def test_context_rejects_paths_outside_drafts(draft_md, home):
    (home / "profile.md").write_text("x", encoding="utf-8")
    with pytest.raises(ScriptError) as e:
        polish.context("profile.md")
    assert e.value.code == "bad_path"
    with pytest.raises(ScriptError) as e:
        polish.context(draft_md.relative_to(home).as_posix().replace(".md", "-conti.md"))
    assert e.value.code == "bad_path"


def test_evaluate_validates_and_ranks(draft_md, home):
    ctx = polish.context(draft_md.relative_to(home).as_posix())
    bad = llm_output()
    bad["hooks"][0]["formula"] = "no-such-formula"
    with pytest.raises(ScriptError) as e:
        polish.evaluate(ctx, bad)
    assert e.value.code == "invalid_polish" and "no-such-formula" in e.value.message

    data = llm_output()
    data["hooks"][0]["script"] = "말: 안녕하세요 여러분 오늘은 라면을 먹습니다 아주 많이 먹습니다 정말로"
    data["titles"][0]["title"] = "가" * 30
    result = polish.evaluate(ctx, data)
    scores = [h["lint"]["score"] for h in result["hooks"]]
    assert scores == sorted(scores, reverse=True) and result["hooks"][-1]["script"].startswith("말: 안녕하세요")
    first = result["hooks"][0]
    assert first["formula_name"] and first["evidence"] == polish.evidence_hook("question", ctx["refs"])
    title_scores = [t["lint"]["score"] for t in result["titles"]]
    assert title_scores == sorted(title_scores, reverse=True) and result["titles"][-1]["title"] == "가" * 30
    assert result["titles"][0]["evidence"]
    assert result["thumbnail"]["lint"]["count"] == 2 and result["thumbnail"]["words"] == THUMB["words"]


@pytest.mark.parametrize("patch", [
    lambda d: d["hooks"].__delitem__(slice(2, None)),               # 훅 2개
    lambda d: d["titles"].__delitem__(slice(4, None)),              # 제목 4개
    lambda d: d["hooks"][0].update(hook_type="nope"),
    lambda d: d["hooks"][0].update(script="  "),
    lambda d: d["titles"][0].update(title=""),
    lambda d: d["thumbnail"].update(words=[]),
    lambda d: d["thumbnail"].update(expression=""),
])
def test_evaluate_rejects_bad_shapes(draft_md, home, patch):
    ctx = polish.context(draft_md.relative_to(home).as_posix())
    data = llm_output()
    patch(data)
    with pytest.raises(ScriptError) as e:
        polish.evaluate(ctx, data)
    assert e.value.code == "invalid_polish"


def test_evaluate_folds_one_line_fields(draft_md, home):
    ctx = polish.context(draft_md.relative_to(home).as_posix())
    data = llm_output()
    data["titles"][0]["title"] = "라면\n5개  도전"
    data["hooks"][0]["why"] = "첫 줄\n둘째 줄"
    result = polish.evaluate(ctx, data)
    assert "라면 5개 도전" in [t["title"] for t in result["titles"]]
    assert all("\n" not in h["why"] for h in result["hooks"])


# --- apply ------------------------------------------------------------------------

def test_apply_replaces_title_hook_and_adds_packaging(draft_md, home):
    before = draft_md.read_bytes().decode("utf-8")
    h = sha(draft_md)
    result = polish.apply(draft_md.relative_to(home).as_posix(), choice(), h)
    after = draft_md.read_bytes().decode("utf-8")
    assert result["path"] == draft_md.relative_to(home).as_posix() and result["hash"] == sha(draft_md)
    assert {"title", "hook", "thumbnail"} <= set(result["changed"]) and result["warnings"] == []
    fm, body = conti.refstore.split_frontmatter(after)
    assert fm["title"] == "컵라면 4단계 전부 먹어 봤다" and fm["title_pattern"] == ["number"]
    assert body.splitlines()[0] == "# 컵라면 4단계 전부 먹어 봤다"
    d = conti.parse_draft(draft_md)
    assert d["sections"][0]["script"] == "말: 4단계 다 먹을 수 있을까?\n자막: 맵기 4단계"
    # 다른 곳은 그대로
    old_body = before.split("\n---", 1)[1]
    assert d["sections"][1]["script"] == "말: 1단계부터 갑니다"
    for table in ("| 1 | 탑뷰 | 탑뷰 | 2초 | 컵라면 4개 나란히 |", "| 3 | 익스트림 클로즈업 | - | 2초 | 면발 후루룩 |"):
        assert table in body
    memo_before = old_body[old_body.index("## 내 메모"):]
    assert body[body.index("## 내 메모"):] == memo_before
    assert body.index("## 패키징") < body.index("## 내 메모")
    pack = body[body.index("## 패키징"):body.index("## 내 메모")]
    assert "컵라면 4단계 | 도전" in pack and "자막: 4단계 다 먹을 수 있을까?" in pack      # 이전 제목·훅 대본
    assert "끝판왕 / 도전" in pack and "the-question" in pack and "입 벌린 놀란 얼굴" in pack
    shots_before = [line for line in old_body.splitlines() if line.startswith("|")]
    assert [line for line in body.splitlines() if line.startswith("|")] == shots_before


def test_apply_twice_keeps_one_packaging_with_history(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    first = polish.apply(rel, choice(), sha(draft_md))
    polish.apply(rel, choice(title="두 번째 제목 7개", hook_script="말: 두 번째 훅"), first["hash"])
    body = body_of(draft_md)
    assert body.count("## 패키징") == 1 and body.count("### 이력") == 1
    history = body[body.index("### 이력"):body.index("## 내 메모")]
    assert "컵라면 4단계 | 도전" in history and "컵라면 4단계 전부 먹어 봤다" in history
    assert "말: 4단계 다 먹을 수 있을까?" in history
    assert conti.parse_draft(draft_md)["sections"][0]["script"] == "말: 두 번째 훅"


def test_apply_without_changes_adds_no_history(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    first = polish.apply(rel, choice(), sha(draft_md))
    second = polish.apply(rel, choice(), first["hash"])
    assert "title" not in second["changed"] and "hook" not in second["changed"]
    assert body_of(draft_md).count("이전 제목") == 1


def test_apply_hand_edited_crlf(draft_md, home):
    text = draft_md.read_bytes().decode("utf-8")
    text = text.replace("자막: 4단계 다 먹을 수 있을까?\n\n| #", "말: 먼저 말\n자막: 먼저 자막\n| #")
    assert "먼저 말" in text
    draft_md.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    raw_before = draft_md.read_bytes()
    polish.apply(draft_md.relative_to(home).as_posix(), choice(hook_script="말: 새 훅"), sha(draft_md))
    raw = draft_md.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")                    # CRLF 유지
    lines = raw.decode("utf-8").split("\r\n")
    i = lines.index("말: 새 훅")
    assert lines[i - 1].startswith("### 0:00") and lines[i + 1].startswith("| #")   # 빈 줄을 새로 만들지 않음
    text_after = raw.decode("utf-8")
    assert "먼저 말" not in text_after.split("## 패키징")[0] and "먼저 말" in text_after.split("## 패키징")[1]   # 이력으로 이동
    assert raw_before.count(b"| 3 | ") == raw.count(b"| 3 | ") == 1


def test_apply_escapes_heading_like_hook_lines(draft_md, home):
    polish.apply(draft_md.relative_to(home).as_posix(), choice(hook_script="말: 첫 줄\n# 제목처럼 보이는 줄"), sha(draft_md))
    assert "\n\\# 제목처럼 보이는 줄\n" in body_of(draft_md)
    assert conti.parse_draft(draft_md)["sections"][0]["script"] == "말: 첫 줄\n# 제목처럼 보이는 줄"


def test_apply_without_hook_section(home):
    from tests.test_conti import DRAFT
    from scripts import draft as draft_mod
    data = json.loads(json.dumps(DRAFT))
    data["sections"] = data["sections"][1:]
    data["sections"][0]["start"], data["sections"][0]["end"] = 0, 4
    data["sections"][1]["start"], data["sections"][1]["end"] = 4, 6
    path = home / "workspace" / "tmp" / "nohook.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    draft_file = home / draft_mod.save(CAT, path)["path"]
    ctx = polish.context(draft_file.relative_to(home).as_posix())
    assert ctx["hook"] is None
    result = polish.apply(draft_file.relative_to(home).as_posix(), choice(), ctx["hash"])
    assert "title" in result["changed"] and "hook" not in result["changed"]
    assert any("훅" in w for w in result["warnings"])
    assert conti.refstore.read_md(draft_file)[0]["title"] == "컵라면 4단계 전부 먹어 봤다"


def test_apply_stale_hash(draft_md, home):
    before = draft_md.read_bytes()
    with pytest.raises(ScriptError) as e:
        polish.apply(draft_md.relative_to(home).as_posix(), choice(), "0" * 64)
    assert e.value.code == "draft_changed"
    assert e.value.message == "화면을 연 뒤 기획안이 바뀌었습니다. 다시 불러와 주세요"
    assert draft_md.read_bytes() == before


def test_apply_warns_when_conti_exists(draft_md, home):
    conti.save(draft_md, conti_file(home))
    result = polish.apply(draft_md.relative_to(home).as_posix(), choice(), sha(draft_md))
    assert any("콘티" in w for w in result["warnings"])


def test_apply_rejects_invalid_choice(draft_md, home):
    rel, h = draft_md.relative_to(home).as_posix(), sha(draft_md)
    for bad in (choice(title="  "), choice(hook_formula="nope"), choice(thumbnail={**THUMB, "words": []})):
        with pytest.raises(ScriptError) as e:
            polish.apply(rel, bad, h)
        assert e.value.code == "invalid_polish"
    assert sha(draft_md) == h


# --- 자료 · CLI --------------------------------------------------------------------

def test_hooks_json_has_21_and_source():
    data = json.loads((ROOT / "skills/yt-polish/hooks.json").read_text("utf-8"))
    ids = [h["id"] for h in data["hooks"]]
    assert len(ids) == 21 == len(set(ids)) and len(data["rules"]) == 5
    assert all(h["hook_type"] in HOOK_TYPES for h in data["hooks"])
    assert all(h["name"] and h["shape"] and h["example"] and h["fails_when"] and "match" not in h for h in data["hooks"])
    assert "MIT" in data["source"] and "Jakeschincariol/youtube-agent-skill" in data["source"]
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text("utf-8")
    assert "MIT License" in notices and "https://github.com/Jakeschincariol/youtube-agent-skill" in notices


def test_thresholds_polish_keys():
    t = json.loads((ROOT / "templates/thresholds.json").read_text("utf-8"))["polish"]
    assert t == {"title_width_mobile": 56, "title_width_desktop": 100, "thumb_max_words": 3, "hook_default_cps": 7.0}


def test_cli_routes(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    ctx = polish.main(["context", rel])
    assert ctx["hash"] == sha(draft_md)
    data = home / "workspace" / "tmp" / "polish.json"
    data.write_text(json.dumps(llm_output(), ensure_ascii=False), encoding="utf-8")
    assert len(polish.main(["evaluate", rel, "--data", "workspace/tmp/polish.json"])["titles"]) == 10
    pick = home / "workspace" / "tmp" / "choice.json"
    pick.write_text(json.dumps(choice(), ensure_ascii=False), encoding="utf-8")
    result = polish.main(["apply", rel, "--choice", "workspace/tmp/choice.json", "--hash", ctx["hash"]])
    assert "title" in result["changed"]
    with pytest.raises(ScriptError) as e:
        polish.main(["evaluate", rel, "--data", "workspace/tmp/missing.json"])
    assert e.value.code == "invalid_polish"
