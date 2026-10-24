"""다듬기: 저장된 기획안의 훅·제목·썸네일을 한국어 기준으로 점수 매기고, 고른 것을 기획안에 반영한다.

사용법 (작업 폴더에서):
  python -m scripts.polish context <draft.md>                              기획안 + 공식 21가지 + 레퍼런스 통계
  python -m scripts.polish evaluate <draft.md> --data <json>               Claude 출력 검증 + 점수·지적·근거
  python -m scripts.polish apply <draft.md> --choice <json> --hash <sha>   고른 것을 기획안에 반영

polish JSON (yt-polish 규칙)
  {"hooks": [{"script", "formula", "hook_type", "why"}],
   "titles": [{"title", "why"}],
   "thumbnail": {"words": [..], "expression", "framing", "background"}}
choice JSON
  {"title", "hook_script"?, "hook_formula"?, "thumbnail": {"words", "expression", "framing", "background"}}
- 점수는 제안일 뿐 저장을 막지 않는다. 저장은 사용자가 고른 뒤 apply에서만 한다
- apply는 제목(프런트매터·`# ` 줄), 훅 섹션의 대본 문단을 바꾸고 `## 패키징`(썸네일 지시서·훅 공식·이력)을 쓴다.
  샷 표와 `## 내 메모`는 건드리지 않는다. 줄바꿈(CRLF/LF)은 파일 그대로 유지한다
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from scripts import conti, features, motif, refstore
from scripts.common import PLUGIN_ROOT, TEMPLATES_DIR, ArgParser, ScriptError, run, work_root
from scripts.draft import _escape_headings, _one_line
from scripts.features import TITLE_PATTERNS
from scripts.videomd import HOOK_TYPES, title_patterns

HOOKS_PATH = PLUGIN_ROOT / "skills" / "yt-polish" / "hooks.json"
HOOK_COUNT = (3, 7)             # 허용 범위 (규칙은 5개)
TITLE_COUNT = (5, 12)           # 허용 범위 (규칙은 10개)
HOOK_SEC_SLACK = 0.5            # 말하는 시간이 훅 길이를 이 정도까지 넘는 것은 허용
MAX_EXAMPLES = 8
PACKAGING = "## 패키징"
MEMO = "## 내 메모"
BOM = b"\xef\xbb\xbf"

_GREETING_RE = re.compile(r"^(안녕|오늘은|여러분 안녕|구독)")
_SPOKEN_RE = re.compile(r"^\s*말\s*[:：]\s*")
_LABELED_RE = re.compile(r"^\s*(말|자막)\s*[:：]")
_PUNCT_RE = re.compile(r"[!?！？]")


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return json.loads((TEMPLATES_DIR / "thresholds.json").read_text("utf-8"))["polish"]


@lru_cache(maxsize=1)
def _hooks_file() -> dict:
    return json.loads(HOOKS_PATH.read_text("utf-8"))


def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_polish", f"다듬기 결과가 형식에 맞지 않습니다: {message}")


# --- 점수 · 지적 -----------------------------------------------------------------------

def display_width(text: str) -> int:
    """화면 폭 근사치. 한글·한자·전각·이모지 2, 결합 문자·제로폭 0, 그 밖 1."""
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        wide = unicodedata.east_asian_width(ch) in ("W", "F") or ord(ch) >= 0x1F000
        width += 2 if wide else 1
    return width


def _tokens(text: str) -> list[str]:
    """공백 기준 낱말에서 기호를 떼고 2글자 이상만 남긴다."""
    return [t for t in (re.sub(r"[^\w]", "", raw) for raw in text.split()) if len(t) >= 2]


def _overlap(words: list[str], title: str) -> list[str]:
    """썸네일 낱말 중 제목 낱말과 겹치는 것(한쪽이 다른 쪽에 들어 있으면 겹침: '라면' ~ '라면을')."""
    title_tokens = _tokens(title)
    found: list[str] = []
    for word in (t for w in words for t in _tokens(w)):
        if word not in found and any(word in tt or tt in word for tt in title_tokens):
            found.append(word)
    return found


def lint_title(title: str, thumb_words: list[str] | None = None) -> dict:
    """제목 점검. 점수 = 100에서 감점: 모바일 잘림 -15, 데스크톱 잘림 -25(추가), 숫자 없음 -10,
    느낌표·물음표 3개 이상 -15, 썸네일 문구와 겹침 -20. 0 미만은 0."""
    cfg = _cfg()
    width = display_width(title)
    mobile_cut, desktop_cut = width > cfg["title_width_mobile"], width > cfg["title_width_desktop"]
    has_number = bool(re.search(r"\d", title))
    marks = len(_PUNCT_RE.findall(title))
    overlap = _overlap(thumb_words or [], title)
    issues: list[str] = []
    score = 100
    if mobile_cut:
        issues.append(f"모바일 피드에서 잘림 (폭 {width} > {cfg['title_width_mobile']})")
        score -= 15
    if desktop_cut:
        issues.append(f"데스크톱 검색에서도 잘림 (폭 {width} > {cfg['title_width_desktop']})")
        score -= 25
    if not has_number:
        issues.append("숫자가 없음")
        score -= 10
    if marks >= 3:
        issues.append(f"느낌표·물음표가 {marks}개 — 많으면 스팸처럼 보임")
        score -= 15
    if overlap:
        issues.append(f"썸네일 문구와 겹치는 낱말: {', '.join(overlap)} — 제목과 썸네일은 다른 말로")
        score -= 20
    return {"title": title, "width": width, "mobile_cut": mobile_cut, "desktop_cut": desktop_cut,
            "patterns": title_patterns(title), "has_number": has_number, "issues": issues, "score": max(score, 0)}


def lint_thumbnail(words: list[str], title: str) -> dict:
    """썸네일 문구 점검. 점수 = 100에서 감점: 문구 없음 -40, 단어 수 초과 -30, 제목과 겹침 -30. 0 미만은 0."""
    limit = _cfg()["thumb_max_words"]
    count = sum(len(w.split()) for w in words)
    over_limit = count > limit
    overlap = _overlap(words, title)
    issues: list[str] = []
    score = 100
    if count == 0:
        issues.append("문구가 없음")
        score -= 40
    if over_limit:
        issues.append(f"문구가 {count}단어 — {limit}단어 이하로 (피드 크기에서는 네 번째 단어부터 뭉개짐)")
        score -= 30
    if overlap:
        issues.append(f"제목과 겹치는 낱말: {', '.join(overlap)} — 같은 약속을 다른 말로")
        score -= 30
    return {"words": words, "count": count, "over_limit": over_limit, "overlap": overlap,
            "issues": issues, "score": max(score, 0)}


def _spoken(script: str) -> list[str]:
    return [_SPOKEN_RE.sub("", line).strip() for line in script.splitlines() if _SPOKEN_RE.match(line)]


def score_hook(script: str, hook_sec: float | None, cps: float) -> dict:
    """훅 대본 점검. 글자 수는 `말:` 줄만(공백 제외). 점수 = 100에서 감점: 훅 길이 초과 -30,
    인사·구독으로 시작 -30, 숫자 없음 -10, `말:`/`자막:` 줄 아님 -10. 0 미만은 0."""
    spoken = _spoken(script)
    chars = sum(len("".join(line.split())) for line in spoken)
    speak_sec = chars / cps
    fits = hook_sec is None or speak_sec <= hook_sec + HOOK_SEC_SLACK
    has_number = bool(re.search(r"\d", script))
    first = spoken[0] if spoken else _LABELED_RE.sub("", next(iter(script.strip().splitlines()), "")).strip()
    greeting = bool(_GREETING_RE.match(first))
    labeled = any(_LABELED_RE.match(line) for line in script.splitlines())
    issues: list[str] = []
    score = 100
    if not fits and hook_sec is not None:
        issues.append(f"말하는 데 약 {speak_sec:.1f}초 — 훅은 {hook_sec:g}초")
        score -= 30
    if greeting:
        issues.append("인사·구독 부탁으로 시작함 — 첫 문장에서 제목의 약속부터 확인")
        score -= 30
    if not has_number:
        issues.append("숫자가 없음")
        score -= 10
    if not labeled:
        issues.append("`말:`/`자막:` 줄로 쓰지 않음")
        score -= 10
    return {"chars": chars, "speak_sec": round(speak_sec, 2), "fits": fits, "has_number": has_number,
            "opens_with_greeting": greeting, "issues": issues, "score": max(score, 0)}


# --- 레퍼런스 근거 -----------------------------------------------------------------------

def _pattern_key(patterns: list[str]) -> str:
    ordered = [p for p in TITLE_PATTERNS if p in patterns]
    return "+".join(ordered) or "none"


def ref_stats(category: str | None) -> dict:
    """ref 영상의 훅 유형·제목 패턴 분포. mine은 기준에 섞지 않는다."""
    empty: dict = {"ref": 0, "hook_type": {}, "title_pattern": {}, "examples": []}
    if not category:
        return empty
    try:
        refs = features.load_videos(category)
    except ScriptError as e:
        if e.code == "category_not_found":
            return empty
        raise
    hooks: dict[str, int] = {}
    patterns: dict[str, int] = {}
    for r in refs:
        fm = r["fm"]
        if fm.get("hook_type"):
            hooks[fm["hook_type"]] = hooks.get(fm["hook_type"], 0) + 1
        key = _pattern_key(fm.get("title_pattern") or title_patterns(str(fm.get("title") or "")))
        patterns[key] = patterns.get(key, 0) + 1
    examples = [str(r["fm"]["title"]) for r in refs if r["fm"].get("title")][:MAX_EXAMPLES]
    return {"ref": len(refs), "hook_type": hooks, "title_pattern": patterns, "examples": examples}


def evidence_hook(hook_type: str, stats: dict) -> str:
    if not stats["ref"]:
        return "레퍼런스가 없어 근거를 낼 수 없음"
    k = stats["hook_type"].get(hook_type, 0)
    if not k:
        return "내 레퍼런스에는 없는 훅 유형"
    return f"이 훅 유형은 내 레퍼런스 {stats['ref']}개 중 {k}개"


def evidence_title(title: str, stats: dict) -> str:
    if not stats["ref"]:
        return "레퍼런스가 없어 근거를 낼 수 없음"
    key = _pattern_key(title_patterns(title))
    label = "패턴 없음" if key == "none" else key
    return f"같은 제목 패턴({label})은 내 레퍼런스 {stats['ref']}개 중 {stats['title_pattern'].get(key, 0)}개"


# --- context ---------------------------------------------------------------------------

def _draft_path(value: Any) -> Path:
    """기존 콘티 화면의 경로 규칙을 그대로 쓴다: drafts/ 안의 .md, 콘티 파일 아님."""
    from scripts.ui.api_conti import _safe_path        # 늦게 불러온다: 화면 모듈은 서버 등록을 함께 한다
    return _safe_path(value, conti_file=False)


def context(draft: str | Path) -> dict:
    path = _draft_path(str(draft))
    d = conti.parse_draft(path)
    category = d["fm"].get("category")
    hook = next((s for s in d["sections"] if s["label"] == "hook"), None)
    rate = conti.speech_rate(category)
    return {
        "draft": conti._rel(path),
        "category": category,
        "title": d["title"],
        "hook": ({"label": "hook", "start": hook["start"], "end": hook["end"], "script": hook["script"]}
                 if hook else None),
        "sections": [s["label"] for s in d["sections"]],
        "profile": motif._profile(),
        "refs": ref_stats(category),
        "cps": rate["cps"] if rate["basis"] else _cfg()["hook_default_cps"],
        "formulas": [{k: h[k] for k in ("id", "name", "shape", "example", "fails_when", "hook_type")}
                     for h in _hooks_file()["hooks"]],
        "hash": conti.content_hash(path),
        "conti_exists": conti.conti_path(path).exists(),
    }


# --- evaluate --------------------------------------------------------------------------

def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{name}이(가) 비어 있습니다")
    return _one_line(value)


def _script(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{name}이(가) 비어 있습니다")
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line.strip())


def _list(value: Any, name: str, bounds: tuple[int, int] | None = None) -> list:
    if not isinstance(value, list):
        raise _invalid(f"{name}은(는) 목록이어야 합니다")
    if bounds and not bounds[0] <= len(value) <= bounds[1]:
        raise _invalid(f"{name}은(는) {bounds[0]}~{bounds[1]}개여야 합니다 (지금 {len(value)}개)")
    return value


def _thumbnail(value: Any) -> dict:
    if not isinstance(value, dict):
        raise _invalid("thumbnail은 객체여야 합니다")
    words = [_text(w, "썸네일 문구") for w in _list(value.get("words"), "썸네일 문구(words)")]
    if not words:
        raise _invalid("썸네일 문구(words)가 비어 있습니다")
    return {"words": words, **{k: _text(value.get(k), f"썸네일 {k}") for k in ("expression", "framing", "background")}}


def _formula_ids() -> dict[str, dict]:
    return {h["id"]: h for h in _hooks_file()["hooks"]}


def _validate_output(data: Any) -> dict:
    if not isinstance(data, dict):
        raise _invalid("결과는 객체여야 합니다")
    formulas = _formula_ids()
    hooks = []
    for i, raw in enumerate(_list(data.get("hooks"), "hooks", HOOK_COUNT), 1):
        if not isinstance(raw, dict):
            raise _invalid(f"훅 {i}은(는) 객체여야 합니다")
        if raw.get("formula") not in formulas:
            raise _invalid(f"훅 {i}의 formula={raw.get('formula')!r}는 hooks.json에 없는 공식입니다")
        if raw.get("hook_type") not in HOOK_TYPES:
            raise _invalid(f"훅 {i}의 hook_type={raw.get('hook_type')!r} (허용: {', '.join(HOOK_TYPES)})")
        hooks.append({"script": _script(raw.get("script"), f"훅 {i}의 script"), "formula": raw["formula"],
                      "hook_type": raw["hook_type"], "why": _text(raw.get("why"), f"훅 {i}의 why")})
    titles = []
    for i, raw in enumerate(_list(data.get("titles"), "titles", TITLE_COUNT), 1):
        if not isinstance(raw, dict):
            raise _invalid(f"제목 {i}은(는) 객체여야 합니다")
        titles.append({"title": _text(raw.get("title"), f"제목 {i}의 title"), "why": _text(raw.get("why"), f"제목 {i}의 why")})
    return {"hooks": hooks, "titles": titles, "thumbnail": _thumbnail(data.get("thumbnail"))}


def evaluate(ctx: dict, data: dict) -> dict:
    """Claude 출력을 검증하고 후보마다 점수·지적·근거를 붙여 점수 내림차순(같으면 원래 순서)으로 돌려준다.
    썸네일은 점수가 가장 높은 제목과 겹침을 본다(제목별 겹침은 각 제목의 lint에 들어 있다)."""
    clean = _validate_output(data)
    names = {h["id"]: h["name"] for h in _hooks_file()["hooks"]}
    hook_sec = (ctx["hook"]["end"] - ctx["hook"]["start"]) if ctx.get("hook") else None
    words = clean["thumbnail"]["words"]
    hooks = [{**h, "formula_name": names[h["formula"]], "lint": score_hook(h["script"], hook_sec, ctx["cps"]),
              "evidence": evidence_hook(h["hook_type"], ctx["refs"])} for h in clean["hooks"]]
    titles = [{**t, "lint": lint_title(t["title"], words), "evidence": evidence_title(t["title"], ctx["refs"])}
              for t in clean["titles"]]
    hooks.sort(key=lambda h: -h["lint"]["score"])
    titles.sort(key=lambda t: -t["lint"]["score"])
    return {"hooks": hooks, "titles": titles,
            "thumbnail": {**clean["thumbnail"], "lint": lint_thumbnail(words, titles[0]["title"])}}


# --- apply -----------------------------------------------------------------------------

def _validate_choice(choice: Any) -> dict:
    if not isinstance(choice, dict):
        raise _invalid("선택은 객체여야 합니다")
    formula = choice.get("hook_formula")
    if formula is not None and formula not in _formula_ids():
        raise _invalid(f"hook_formula={formula!r}는 hooks.json에 없는 공식입니다")
    script = choice.get("hook_script")
    return {"title": _text(choice.get("title"), "title"),
            "hook_script": None if script is None else _script(script, "hook_script"),
            "hook_formula": formula, "thumbnail": _thumbnail(choice.get("thumbnail"))}


def _hook_heading(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        m = conti._HEAD_RE.match(line)
        if m and m.group(3) == "hook":
            return i
    return None


def _swap_hook(lines: list[str], heading: int, script: str) -> tuple[list[str], str]:
    """훅 제목 다음의 대본 문단(샷 표 `|` 줄 또는 다음 `###`/`##` 전까지)만 바꾼다. (새 줄들, 이전 대본)."""
    end = next((i for i in range(heading + 1, len(lines)) if lines[i].startswith(("|", "### ", "## "))), len(lines))
    region = lines[heading + 1:end]
    lead = next((i for i, line in enumerate(region) if line.strip()), len(region))
    trail = 0 if lead == len(region) else next(i for i, line in enumerate(reversed(region)) if line.strip())
    old = "\n".join(region[lead:len(region) - trail])
    new = [line.rstrip() for line in _escape_headings(script).strip("\n").split("\n")]
    keep_tail = region[len(region) - trail:] if trail else []
    if not region:
        keep_tail = [""]            # 훅 제목 바로 뒤가 표였다면 읽기 좋게 한 줄 띄운다
    return lines[:heading + 1] + region[:lead] + new + keep_tail + lines[end:], old


def _find_section(lines: list[str], heading: str) -> tuple[int, int] | None:
    start = next((i for i, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start, end


def _sub_blocks(section: list[str]) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current = None
    for line in section:
        if line.startswith("### "):
            current = line[4:].strip()
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {k: _trim_blank(v) for k, v in blocks.items()}


def _trim_blank(lines: list[str]) -> list[str]:
    start = next((i for i, line in enumerate(lines) if line.strip()), len(lines))
    end = len(lines) - next((i for i, line in enumerate(reversed(lines)) if line.strip()), len(lines))
    return lines[start:end]


def _packaging(existing: list[str], pick: dict, history: list[str]) -> list[str]:
    old = _sub_blocks(existing)
    thumb = pick["thumbnail"]
    out = [PACKAGING, "", "### 썸네일 지시서", f"- 문구: {' / '.join(thumb['words'])}", f"- 표정: {thumb['expression']}",
           f"- 구도: {thumb['framing']}", f"- 배경·대비: {thumb['background']}"]
    formula = _formula_ids().get(pick["hook_formula"] or "")
    if formula:
        out += ["", "### 훅 공식", f"- {formula['id']} {formula['name']} ({formula['hook_type']}) — {formula['shape']}"]
    elif old.get("훅 공식"):
        out += ["", "### 훅 공식", *old["훅 공식"]]
    log = [*old.get("이력", []), *history]
    if log:
        out += ["", "### 이력", *log]
    return out + [""]


def apply(draft: str | Path, choice: dict, expected_hash: str) -> dict:
    path = _draft_path(str(draft))
    if not path.exists():
        raise ScriptError("draft_not_found", f"기획안이 없습니다: {conti._rel(path)}")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ScriptError("draft_changed", "화면을 연 뒤 기획안이 바뀌었습니다. 다시 불러와 주세요")
    pick = _validate_choice(choice)
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    fm, body = refstore.split_frontmatter(text)
    lines = body.replace("\r\n", "\n").split("\n")
    changed: list[str] = []
    warnings: list[str] = []
    entry: list[str] = []

    old_hook = ""
    if pick["hook_script"] is not None:
        heading = _hook_heading(lines)
        if heading is None:
            warnings.append("기획안에 훅 섹션이 없어 훅 대본은 바꾸지 않았습니다")
        else:
            lines, old_hook = _swap_hook(lines, heading, pick["hook_script"])
            if old_hook != _escape_headings(pick["hook_script"]).strip("\n"):
                changed.append("hook")
                if old_hook:
                    entry += ["  - 이전 훅 대본:", *(f"    > {line}" for line in old_hook.split("\n"))]

    old_title = str(fm.get("title") or "")
    if pick["title"] != old_title:
        changed.insert(0, "title")
        entry.insert(0, f"  - 이전 제목: {old_title}")
    fm["title"], fm["title_pattern"] = pick["title"], title_patterns(pick["title"])
    h1 = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if h1 is None:
        lines[:0] = [f"# {pick['title']}", ""]
    else:
        lines[h1] = f"# {pick['title']}"
    changed.append("thumbnail")

    found = _find_section(lines, PACKAGING)
    block = _packaging(lines[found[0] + 1:found[1]] if found else [], pick,
                       [f"- {date.today().isoformat()}", *entry] if entry else [])
    if found:
        lines[found[0]:found[1]] = block
    else:
        memo = _find_section(lines, MEMO)
        at = memo[0] if memo else (len(lines) - 1 if lines and lines[-1] == "" else len(lines))
        if at > 0 and lines[at - 1].strip():
            block = ["", *block]
        lines[at:at] = block

    if conti.conti_path(path).exists():
        warnings.append("이 기획안의 콘티가 이미 있습니다. 제목·훅 대본이 바뀌었으니 /yt-coach:yt-conti 로 콘티를 다시 만들어 주세요 "
                        "(콘티의 대사는 자동으로 바뀌지 않습니다)")
    out = refstore.join_frontmatter(fm, "\n".join(lines).replace("\n", nl), nl).encode("utf-8")
    refstore.write_atomic(path, BOM + out if raw.startswith(BOM) else out)
    return {"path": conti._rel(path), "hash": conti.content_hash(path), "changed": changed, "warnings": warnings}


# --- CLI -------------------------------------------------------------------------------

def _read_json(value: str, what: str) -> Any:
    try:
        return json.loads(conti._resolve(value).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise _invalid(f"{what} JSON을 읽을 수 없습니다: {e}") from e


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.polish")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("context").add_argument("draft")
    e = sub.add_parser("evaluate")
    e.add_argument("draft")
    e.add_argument("--data", required=True)
    a = sub.add_parser("apply")
    a.add_argument("draft")
    a.add_argument("--choice", required=True)
    a.add_argument("--hash", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "context":
        return context(args.draft)
    if args.cmd == "evaluate":
        return evaluate(context(args.draft), _read_json(args.data, "다듬기"))
    return apply(args.draft, _read_json(args.choice, "선택"), args.hash)


if __name__ == "__main__":
    run(main)
