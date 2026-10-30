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
from tests.helpers.paths import doc_key

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
    assert ctx["refs"]["ref"] == 10 and ctx["cps"] == conti.DEFAULT_CPS


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
    assert conti.refstore.read(doc_key(draft_file))[0]["title"] == "컵라면 4단계 전부 먹어 봤다"


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
    assert t == {"title_width_mobile": 56, "title_width_desktop": 100, "thumb_max_words": 3,
                 "caption_read_cps": 12.0}


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


# --- fix round 1 -----------------------------------------------------------------------

def test_display_width_emoji_sequences():
    assert polish.display_width("👍🏽") == 2                 # 피부색 수정자는 폭 0
    assert polish.display_width("🇰🇷") == 2                 # 국기(지역 표시 문자 한 쌍)
    assert polish.display_width("🇰") == 2
    assert polish.display_width("❤") == 1 and polish.display_width("❤️") == 2     # VS16은 앞 글자를 2로
    assert polish.display_width("👨‍👩‍👧") == 2   # ZWJ 시퀀스는 한 글자
    assert polish.display_width("한") == 2  # 분해된 한글은 NFC로 합쳐 센다


def test_speech_rate_default_is_shared(draft_md, home):
    t = json.loads((ROOT / "templates/thresholds.json").read_text("utf-8"))
    assert t["speech_default_cps"] == 4.5 == conti.DEFAULT_CPS and "hook_default_cps" not in t["polish"]
    assert polish.context(draft_md.relative_to(home).as_posix())["cps"] == conti.DEFAULT_CPS


def test_draft_path_rule_lives_in_conti(home):
    from scripts.ui import api_conti
    assert api_conti._safe_key is conti.safe_draft_key and api_conti.CONTI_SUFFIX == conti.CONTI_SUFFIX == "-conti"
    for bad, flag in (("profile.md", None), ("drafts/a-conti.md", False), ("drafts/a.md", True)):
        with pytest.raises(ScriptError) as e:
            conti.safe_draft_key(bad, conti_file=flag)
        assert e.value.code == "bad_path"
    with pytest.raises(ScriptError) as e:
        polish.context("drafts/a-conti.md")                      # 다듬기도 같은 규칙(기획안만)
    assert e.value.code == "bad_path"
    assert "scripts.ui" not in (ROOT / "scripts/polish.py").read_text("utf-8")


def overview(path: Path) -> str:
    return next(line for line in body_of(path).replace("\r\n", "\n").split("\n") if line.startswith("- 훅:"))


def test_apply_syncs_hook_type(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    assert overview(draft_md) == "- 훅: question · 서사: list"
    result = polish.apply(rel, choice(hook_type="shock_line"), sha(draft_md))
    fm = conti.refstore.read(doc_key(draft_md))[0]
    assert fm["hook_type"] == "shock_line" and overview(draft_md) == "- 훅: shock_line · 서사: list"
    assert "hook" in result["changed"]
    assert "이전 훅 유형: question" in body_of(draft_md)
    assert body_of(draft_md).count("- 훅:") == 1


def test_apply_hook_type_never_touches_memo(draft_md, home):
    # 개요에 '- 훅:' 줄이 없어도 내 메모의 같은 모양 줄은 바꾸지 않는다
    text = draft_md.read_text(encoding="utf-8").replace("- 훅: question · 서사: list\n", "")
    text = text.replace("## 내 메모\n", "## 내 메모\n- 훅: 내가 적어 둔 메모\n")
    draft_md.write_text(text, encoding="utf-8")
    rel = draft_md.relative_to(home).as_posix()
    polish.apply(rel, choice(hook_type="shock_line"), sha(draft_md))
    body = body_of(draft_md)
    assert "- 훅: 내가 적어 둔 메모" in body
    assert conti.refstore.read(doc_key(draft_md))[0]["hook_type"] == "shock_line"


def test_apply_hook_type_defaults_to_formula(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    polish.apply(rel, choice(hook_formula="the-teardown"), sha(draft_md))          # situation
    assert conti.refstore.read(doc_key(draft_md))[0]["hook_type"] == "situation"
    assert overview(draft_md).startswith("- 훅: situation")


def test_apply_keeps_hook_type_without_formula_or_hook_change(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    polish.apply(rel, choice(hook_formula=None), sha(draft_md))
    assert conti.refstore.read(doc_key(draft_md))[0]["hook_type"] == "question"
    same = conti.parse_draft(draft_md)["sections"][0]["script"]
    polish.apply(rel, choice(hook_script=same, hook_type="shock_line"), sha(draft_md))     # 훅이 그대로면 유형도 그대로
    assert conti.refstore.read(doc_key(draft_md))[0]["hook_type"] == "question"
    with pytest.raises(ScriptError) as e:
        polish.apply(rel, choice(hook_type="nope"), sha(draft_md))
    assert e.value.code == "invalid_polish"


def test_apply_hook_type_crlf(draft_md, home):
    draft_md.write_bytes(draft_md.read_bytes().decode("utf-8").replace("\n", "\r\n").encode("utf-8"))
    polish.apply(draft_md.relative_to(home).as_posix(), choice(hook_type="result_first"), sha(draft_md))
    raw = draft_md.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") and overview(draft_md) == "- 훅: result_first · 서사: list"


def test_apply_blank_only_hook_region(draft_md, home):
    text = draft_md.read_bytes().decode("utf-8").replace("자막: 4단계 다 먹을 수 있을까?\n\n| #", "\n\n\n| #")
    draft_md.write_bytes(text.encode("utf-8"))
    polish.apply(draft_md.relative_to(home).as_posix(), choice(hook_script="말: 새 훅"), sha(draft_md))
    lines = body_of(draft_md).split("\n")
    i = lines.index("말: 새 훅")
    assert lines[i - 1].startswith("### 0:00") and lines[i + 1] == "" and lines[i + 2].startswith("| #")


def test_apply_escapes_pipe_lines(draft_md, home):
    polish.apply(draft_md.relative_to(home).as_posix(), choice(hook_script="말: 가\n| 표처럼 보이는 줄"), sha(draft_md))
    assert "\n\\| 표처럼 보이는 줄\n" in body_of(draft_md)
    d = conti.parse_draft(draft_md)
    assert d["sections"][0]["script"] == "말: 가\n| 표처럼 보이는 줄" and len(d["sections"][0]["shots"]) == 1


def test_apply_warns_about_text_after_hook_table(draft_md, home):
    old = "| 1 | 탑뷰 | 탑뷰 | 2초 | 컵라면 4개 나란히 |\n"
    text = draft_md.read_bytes().decode("utf-8")
    assert old in text
    draft_md.write_bytes(text.replace(old, old + "표 아래 글\n").encode("utf-8"))
    result = polish.apply(draft_md.relative_to(home).as_posix(), choice(), sha(draft_md))
    assert any("표 아래" in w for w in result["warnings"])
    assert "표 아래 글" in body_of(draft_md)


def test_apply_inserts_packaging_when_memo_missing(draft_md, home):
    text = draft_md.read_bytes().decode("utf-8")
    text = text[:text.index("## 내 메모")].rstrip("\n") + "\n"
    draft_md.write_bytes(text.encode("utf-8"))
    polish.apply(draft_md.relative_to(home).as_posix(), choice(), sha(draft_md))
    out = draft_md.read_bytes().decode("utf-8")
    assert out.endswith("\n") and not out.endswith("\n\n") and out.count("## 패키징") == 1
    assert "\n\n## 패키징\n" in out
    again = polish.apply(draft_md.relative_to(home).as_posix(), choice(title="바뀐 제목 3"), sha(draft_md))
    out = draft_md.read_bytes().decode("utf-8")
    assert out.endswith("\n") and not out.endswith("\n\n") and out.count("## 패키징") == 1 and again["warnings"] == []


def test_hooks_json_wording():
    data = json.loads((ROOT / "skills/yt-polish/hooks.json").read_text("utf-8"))
    assert "0:08에 시청자가 떠난다" in data["rules"][0]
    example = next(h for h in data["hooks"] if h["id"] == "someone-elses-result")["example"]
    assert "가상의 예시 채널" in example


# SKILL.md의 고정 예시는 evaluate가 실제로 내는 값과 같아야 한다
EXAMPLE_REFS = {"ref": 10, "hook_type": {"result_first": 6, "question": 3, "situation": 1},
                "title_pattern": {"number+exclaim": 5, "number": 3, "question": 2}, "examples": []}


def skill_example() -> tuple[dict, str]:
    text = (ROOT / "skills/yt-polish/SKILL.md").read_text("utf-8")
    example = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    shown = text.split("## 6.", 1)[1].split("```\n", 1)[1].split("\n```", 1)[0]
    return example, shown


def test_skill_example_matches_evaluate():
    import re
    example, shown = skill_example()
    ctx = {"hook": {"start": 0.0, "end": 2.0}, "cps": 4.5, "refs": EXAMPLE_REFS}
    result = polish.evaluate(ctx, example)
    assert len(example["hooks"]) == 5 and len(example["titles"]) == 10
    assert result["thumbnail"]["lint"]["overlap"] == [] and result["thumbnail"]["lint"]["over_limit"] is False
    assert result["hooks"][0]["lint"]["fits"] is True and result["hooks"][0]["lint"]["score"] == 100
    assert "0:00–0:02" in shown and "초당 4.5자" in shown
    hook_part, rest = shown.split("🏷", 1)
    title_part, thumb_part = rest.split("🖼", 1)
    for part, key in ((hook_part, "hooks"), (title_part, "titles")):
        heads = re.findall(r"^\s*(\d+)\) \[(\d+)점\] (.+)$", part, flags=re.M)
        assert len(heads) >= 2
        for (n, score, label), item in zip(heads, result[key]):
            assert int(score) == item["lint"]["score"], (key, n)
            assert (item["formula"] if key == "hooks" else item["title"]) in label
            if key == "titles":
                assert f"(폭 {item['lint']['width']})" in label
        evidence = re.findall(r"근거: ([^·\n]+?)(?: · 지적: (.+))?$", part, flags=re.M)
        assert len(evidence) == len(heads)
        for (ev, issues), item in zip(evidence, result[key]):
            assert ev.strip() == item["evidence"] and (issues or "") == "; ".join(item["lint"]["issues"])
    thumb = result["thumbnail"]
    assert f"[{thumb['lint']['score']}점]" in thumb_part and " / ".join(thumb["words"]) in thumb_part
    shown_hooks = len(re.findall(r"^\s*\d+\) \[", hook_part, flags=re.M))
    for item in result["hooks"][:shown_hooks]:
        assert all(line in hook_part for line in item["script"].splitlines())


def test_score_hook_caption_only_judged_by_reading_time():
    long = polish.score_hook("자막: " + "가" * 40, 2.0, 7.0)          # 40자 / 12cps = 3.33초
    assert long["chars"] == 0 and long["caption_chars"] == 40
    assert long["read_sec"] == pytest.approx(3.33, abs=0.01)
    assert long["need_sec"] == pytest.approx(3.33, abs=0.01) and long["fits"] is False
    assert any("자막을 읽는 데 약 3.3초" in i and "훅 구간 2초" in i for i in long["issues"])
    short = polish.score_hook("자막: 라면 5개 " + "가" * 10, 2.0, 7.0)  # 공백 제외 14자
    assert short["caption_chars"] == 14 and short["fits"] is True
    assert not any("자막을 읽는" in i for i in short["issues"])
    assert long["score"] < short["score"]


def test_score_hook_mixed_uses_larger_of_speak_and_read():
    spoken_heavy = polish.score_hook("말: " + "가" * 14 + "\n자막: 라면", 2.0, 7.0)
    assert spoken_heavy["need_sec"] == spoken_heavy["speak_sec"] == pytest.approx(2.0)
    caption_heavy = polish.score_hook("말: 가나\n자막: " + "가" * 36, 2.0, 7.0)
    assert caption_heavy["need_sec"] == caption_heavy["read_sec"] == pytest.approx(3.0)
    assert caption_heavy["fits"] is False
    assert polish.score_hook("말: 가나\n자막: " + "가" * 36, None, 7.0)["fits"] is True


# --- final fix pass ----------------------------------------------------------------

def test_relint_scores_edits_without_claude(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    out = polish.relint(rel, hook_script="말: " + "가" * 40, title="라면 도전", thumb_words=["라면", "끝판왕"])
    assert set(out) == {"hook", "title", "thumbnail"}
    assert out["hook"]["fits"] is False and out["hook"]["speak_sec"] > 2     # 훅 구간 2초
    assert out["title"]["overlap"] if "overlap" in out["title"] else "라면" in " ".join(out["title"]["issues"])
    assert out["thumbnail"]["overlap"] == ["라면"]
    assert set(polish.relint(rel, title="제목 3개")) == {"title"}
    # 고친 제목의 레퍼런스 근거도 다시 계산한다(패턴이 바뀌면 근거 문장도 바뀐다)
    stats = polish.ref_stats(conti.refstore.read(doc_key(draft_md))[0].get("category"))
    for t in ("라면 5봉지 먹방!", "라면 먹방"):
        assert polish.relint(rel, title=t)["title"]["evidence"] == polish.evidence_title(t, stats)
    assert set(polish.relint(rel, thumb_words=["가"])) == {"thumbnail"}
    assert set(polish.relint(rel, hook_script="")) == {"hook"}


@pytest.mark.parametrize("kw", [{"hook_script": 3}, {"title": ["x"]}, {"thumb_words": "x"}, {"thumb_words": [1]}])
def test_relint_rejects_bad_types(draft_md, home, kw):
    with pytest.raises(ScriptError) as e:
        polish.relint(draft_md.relative_to(home).as_posix(), **kw)
    assert e.value.code == "bad_request"


def test_apply_same_title_keeps_hand_corrected_title_pattern(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    first = polish.apply(rel, choice(), sha(draft_md))
    text = draft_md.read_bytes().decode("utf-8")
    edited = text.replace("title_pattern: [number]", "title_pattern: [curiosity]", 1)
    assert edited != text
    draft_md.write_bytes(edited.encode("utf-8"))
    polish.apply(rel, choice(), sha(draft_md))
    fm, _ = conti.refstore.split_frontmatter(draft_md.read_bytes().decode("utf-8"))
    assert fm["title_pattern"] == ["curiosity"] and first["hash"] != sha(draft_md)


def test_apply_preserves_user_packaging_text_and_unknown_blocks(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    first = polish.apply(rel, choice(), sha(draft_md))
    text = draft_md.read_bytes().decode("utf-8")
    text = text.replace("## 패키징\n", "## 패키징\n\n내가 쓴 서문 메모\n", 1)
    text = text.replace("### 이력", "### 내 아이디어\n- 노란 자막 시도\n\n### 이력", 1) if "### 이력" in text \
        else text.replace("\n## 내 메모", "\n### 내 아이디어\n- 노란 자막 시도\n\n## 내 메모", 1)
    draft_md.write_bytes(text.encode("utf-8"))
    polish.apply(rel, choice(title="두 번째 제목 7개"), sha(draft_md))
    polish.apply(rel, choice(title="세 번째 제목 8개"), sha(draft_md))
    body = body_of(draft_md)
    pack = body[body.index("## 패키징"):body.index("## 내 메모")]
    assert pack.count("내가 쓴 서문 메모") == 1 and pack.count("### 내 아이디어") == 1 and "- 노란 자막 시도" in pack
    assert pack.index("서문") < pack.index("### 썸네일 지시서") < pack.index("### 내 아이디어") or \
        pack.index("서문") < pack.index("### 내 아이디어")
    assert pack.count("### 썸네일 지시서") == 1 and pack.count("### 이력") == 1 and first["hash"]


def test_apply_ignores_trailing_spaces_in_old_hook(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    text = draft_md.read_bytes().decode("utf-8")
    assert "자막: 4단계 다 먹을 수 있을까?\n" in text
    draft_md.write_bytes(text.replace("자막: 4단계 다 먹을 수 있을까?\n", "자막: 4단계 다 먹을 수 있을까?   \n", 1).encode("utf-8"))
    result = polish.apply(rel, choice(title="컵라면 4단계 | 도전", hook_script="자막: 4단계 다 먹을 수 있을까?"), sha(draft_md))
    assert "hook" not in result["changed"] and "title" not in result["changed"]
    assert "이전 훅 대본" not in body_of(draft_md)


def test_apply_bom_round_trip(draft_md, home):
    rel = draft_md.relative_to(home).as_posix()
    draft_md.write_bytes(b"\xef\xbb\xbf" + draft_md.read_bytes())
    polish.apply(rel, choice(), sha(draft_md))
    raw = draft_md.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and not raw.startswith(b"\xef\xbb\xbf\xef\xbb\xbf")
    assert conti.parse_draft(draft_md)["title"] == "컵라면 4단계 전부 먹어 봤다"
