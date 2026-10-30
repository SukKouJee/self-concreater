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

import json
import re
import unicodedata
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from scripts import conti, ctx, features, keys, motif, refstore
from scripts.common import PLUGIN_ROOT, TEMPLATES_DIR, ArgParser, ScriptError, run
from scripts.stores import VersionConflict
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
_CAPTION_RE = re.compile(r"^\s*자막\s*[:：]\s*")
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
    width = prev = 0              # prev: 방금 센 글자의 폭 (VS16이 1 → 2로 올린다)
    joined = flag_open = False    # joined: ZWJ 바로 뒤 글자는 앞 글자에 붙는다 / flag_open: 국기 앞 글자를 센 직후
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        if joined:
            joined = False
        elif ch == "‍":
            joined = True
        elif cp == 0xFE0F:
            if prev == 1:
                width, prev = width + 1, 2
        elif 0x1F3FB <= cp <= 0x1F3FF or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue                                          # 피부색 수정자·결합 문자·제로폭
        elif 0x1F1E6 <= cp <= 0x1F1FF:                        # 지역 표시 문자: 한 쌍이 국기 하나
            flag_open = not flag_open
            if flag_open:
                width, prev = width + 2, 2
        else:
            flag_open = False
            prev = 2 if unicodedata.east_asian_width(ch) in ("W", "F") or cp >= 0x1F000 else 1
            width += prev
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


def _captions(script: str) -> list[str]:
    return [_CAPTION_RE.sub("", line).strip() for line in script.splitlines() if _CAPTION_RE.match(line)]


def score_hook(script: str, hook_sec: float | None, cps: float) -> dict:
    """훅 대본 점검. `chars`는 `말:` 줄 글자 수, `caption_chars`는 `자막:` 줄 글자 수(둘 다 공백 제외).
    말하는 시간 speak_sec = chars/cps, 자막 읽는 시간 read_sec = caption_chars/caption_read_cps,
    필요 시간 need_sec = max(speak_sec, read_sec). 접두어 없는 줄은 세지 않는다.
    점수 = 100에서 감점: need_sec이 훅 길이 초과 -30,
    인사·구독으로 시작 -30, 숫자 없음 -10, `말:`/`자막:` 줄 아님 -10. 0 미만은 0."""
    spoken = _spoken(script)
    chars = sum(len("".join(line.split())) for line in spoken)
    speak_sec = chars / cps
    caption_chars = sum(len("".join(line.split())) for line in _captions(script))
    read_sec = caption_chars / _cfg()["caption_read_cps"]
    need_sec = max(speak_sec, read_sec)
    fits = hook_sec is None or need_sec <= hook_sec + HOOK_SEC_SLACK
    has_number = bool(re.search(r"\d", script))
    first = spoken[0] if spoken else _LABELED_RE.sub("", next(iter(script.strip().splitlines()), "")).strip()
    greeting = bool(_GREETING_RE.match(first))
    labeled = any(_LABELED_RE.match(line) for line in script.splitlines())
    issues: list[str] = []
    score = 100
    if not fits and hook_sec is not None:
        if speak_sec >= read_sec:
            issues.append(f"말하는 데 약 {speak_sec:.1f}초 — 훅은 {hook_sec:g}초")
        else:
            issues.append(f"자막을 읽는 데 약 {read_sec:.1f}초 — 훅 구간 {hook_sec:g}초보다 김")
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
    return {"chars": chars, "speak_sec": round(speak_sec, 2), "caption_chars": caption_chars,
            "read_sec": round(read_sec, 2), "need_sec": round(need_sec, 2), "fits": fits, "has_number": has_number,
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

def _draft_key(value: Any) -> str:
    """콘티 화면의 경로 규칙을 그대로 쓴다: drafts/ 안의 .md, 콘티 파일 아님(resolve 기준 탈출 검사) → 기획안 키."""
    return conti.safe_draft_key(value, conti_file=False)


def context(draft: str | Path) -> dict:
    key = _draft_key(str(draft))
    d = conti.parse_draft(key)
    category = d["fm"].get("category")
    hook = next((s for s in d["sections"] if s["label"] == "hook"), None)
    rate = conti.speech_rate(category)
    return {
        "draft": key,
        "category": category,
        "title": d["title"],
        "hook": ({"label": "hook", "start": hook["start"], "end": hook["end"], "script": hook["script"]}
                 if hook else None),
        "sections": [s["label"] for s in d["sections"]],
        "profile": motif._profile(),
        "refs": ref_stats(category),
        "cps": rate["cps"],
        "formulas": [{k: h[k] for k in ("id", "name", "shape", "example", "fails_when", "hook_type")}
                     for h in _hooks_file()["hooks"]],
        "hash": ctx.here().store.read(key).version,
        "conti_exists": ctx.here().store.exists(keys.conti_md(key)),
    }


def relint(draft: str | Path, hook_script: Any = None, title: Any = None, thumb_words: Any = None) -> dict:
    """고친 값만 다시 점검한다(Claude 호출 없음). 훅 구간 길이와 말속도는 기획안에서 구한다. 준 값에 대한 결과만 돌려준다."""
    for name, value, kind in (("hook_script", hook_script, str), ("title", title, str)):
        if value is not None and not isinstance(value, kind):
            raise ScriptError("bad_request", f"{name}은(는) 문자열이어야 합니다")
    if thumb_words is not None and (not isinstance(thumb_words, list) or not all(isinstance(w, str) for w in thumb_words)):
        raise ScriptError("bad_request", "thumb_words는 문자열 목록이어야 합니다")
    d = conti.parse_draft(_draft_key(str(draft)))
    out: dict = {}
    clean_title = None if title is None else _one_line(title)
    words = None if thumb_words is None else [w for w in (_one_line(x) for x in thumb_words) if w]
    if hook_script is not None:
        hook = next((s for s in d["sections"] if s["label"] == "hook"), None)
        hook_sec = (hook["end"] - hook["start"]) if hook else None
        lines = [line.rstrip() for line in hook_script.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        cps = conti.speech_rate(d["fm"].get("category"))["cps"]
        out["hook"] = score_hook("\n".join(line for line in lines if line.strip()), hook_sec, cps)
    if clean_title is not None:
        out["title"] = {**lint_title(clean_title, words),
                        "evidence": evidence_title(clean_title, ref_stats(d["fm"].get("category")))}
    if words is not None:
        out["thumbnail"] = lint_thumbnail(words, clean_title or "")
    return out


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
    hook_type = choice.get("hook_type")
    if hook_type is None and formula is not None:
        hook_type = _formula_ids()[formula]["hook_type"]          # 후보의 hook_type이 없으면 공식의 것
    if hook_type is not None and hook_type not in HOOK_TYPES:
        raise _invalid(f"hook_type={hook_type!r} (허용: {', '.join(HOOK_TYPES)})")
    return {"title": _text(choice.get("title"), "title"),
            "hook_script": None if script is None else _script(script, "hook_script"),
            "hook_formula": formula, "hook_type": hook_type, "thumbnail": _thumbnail(choice.get("thumbnail"))}


def _hook_heading(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        m = conti._HEAD_RE.match(line)
        if m and m.group(3) == "hook":
            return i
    return None


def _hook_end(lines: list[str], heading: int) -> int:
    return next((i for i in range(heading + 1, len(lines)) if lines[i].startswith(("|", "### ", "## "))), len(lines))


def _text_after_table(lines: list[str], heading: int) -> bool:
    """훅 샷 표 뒤에 표가 아닌 글이 다음 제목 전까지 있는가(대본 교체로는 바뀌지 않는 글)."""
    i = _hook_end(lines, heading)
    while i < len(lines) and lines[i].startswith("|"):
        i += 1
    while i < len(lines) and not lines[i].startswith(("### ", "## ")):
        if lines[i].strip():
            return True
        i += 1
    return False


def _rstrip_lines(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _swap_hook(lines: list[str], heading: int, script: str) -> tuple[list[str], str]:
    """훅 제목 다음의 대본 문단(샷 표 `|` 줄 또는 다음 `###`/`##` 전까지)만 바꾼다. (새 줄들, 이전 대본)."""
    end = _hook_end(lines, heading)
    region = lines[heading + 1:end]
    new = [line.rstrip() for line in _escape_headings(script).strip("\n").split("\n")]
    lead = next((i for i, line in enumerate(region) if line.strip()), len(region))
    if lead == len(region):         # 비어 있던 문단: 제목 바로 뒤에 넣고 한 줄 띄운다
        return lines[:heading + 1] + new + [""] + lines[end:], ""
    trail = next(i for i, line in enumerate(reversed(region)) if line.strip())
    old = "\n".join(region[lead:len(region) - trail])
    return lines[:heading + 1] + region[:lead] + new + region[len(region) - trail:] + lines[end:], old


def _set_overview_hook(lines: list[str], hook_type: str) -> list[str]:
    """`## 개요` 구간 안의 첫 `- 훅: X` 줄의 X만 바꾼다(` · 서사: …`는 그대로). 구간 밖(내 메모 등)은 건드리지 않는다."""
    span = _find_section(lines, "## 개요")
    if span is None:
        return lines
    out = list(lines)
    for i in range(span[0] + 1, span[1]):
        if out[i].startswith("- 훅: "):
            out[i] = re.sub(r"^(- 훅: )\S+", lambda m: m.group(1) + hook_type, out[i])
            break
    return out


def _find_section(lines: list[str], heading: str) -> tuple[int, int] | None:
    start = next((i for i, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start, end


def _split_packaging(section: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """`## 패키징` 본문을 첫 `###` 앞 글(서문)과 `###` 블록들(순서 유지)로 나눈다. 앞뒤 빈 줄은 뗀다."""
    preamble: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    for line in section:
        if line.startswith("### "):
            blocks.append((line[4:].strip(), []))
        elif blocks:
            blocks[-1][1].append(line)
        else:
            preamble.append(line)
    return _trim_blank(preamble), [(name, _trim_blank(body)) for name, body in blocks]


def _trim_blank(lines: list[str]) -> list[str]:
    start = next((i for i, line in enumerate(lines) if line.strip()), len(lines))
    end = len(lines) - next((i for i, line in enumerate(reversed(lines)) if line.strip()), len(lines))
    return lines[start:end]


def _packaging(existing: list[str], pick: dict, history: list[str]) -> list[str]:
    """썸네일 지시서·훅 공식만 새로 쓰고 이력에 덧붙인다. 서문과 모르는 `###` 블록은 사용자 것이라 그대로 둔다."""
    preamble, old = _split_packaging(existing)
    thumb = pick["thumbnail"]
    thumb_block = [f"- 문구: {' / '.join(thumb['words'])}", f"- 표정: {thumb['expression']}",
                   f"- 구도: {thumb['framing']}", f"- 배경·대비: {thumb['background']}"]
    formula = _formula_ids().get(pick["hook_formula"] or "")
    formula_block = ([f"- {formula['id']} {formula['name']} ({formula['hook_type']}) — {formula['shape']}"]
                     if formula else None)
    blocks: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for name, body in old:
        if name not in seen and name == "썸네일 지시서":
            body = thumb_block
        elif name not in seen and name == "훅 공식" and formula_block is not None:
            body = formula_block
        elif name not in seen and name == "이력":
            body = [*body, *history]
        seen.add(name)
        blocks.append((name, body))
    if "썸네일 지시서" not in seen:
        blocks.insert(0, ("썸네일 지시서", thumb_block))
    if "훅 공식" not in seen and formula_block is not None:
        blocks.insert(1 + next((i for i, (n, _) in enumerate(blocks) if n == "썸네일 지시서"), -1), ("훅 공식", formula_block))
    if "이력" not in seen and history:
        blocks.append(("이력", history))
    out = [PACKAGING, ""]
    if preamble:
        out += [*preamble, ""]
    for name, body in blocks:
        out += [f"### {name}", *body, ""]
    return out


def apply(draft: str | Path, choice: dict, expected_hash: str) -> dict:
    key = _draft_key(str(draft))
    store = ctx.here().store
    if not store.exists(key):
        raise ScriptError("draft_not_found", f"기획안이 없습니다: {key}")
    doc = store.read(key)
    raw = doc.raw
    if doc.version != expected_hash:
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
            if _text_after_table(lines, heading):
                warnings.append("훅 샷 표 아래에 대본이 아닌 글이 있습니다. 대본 교체로는 바뀌지 않으니 직접 확인해 주세요")
            lines, old_hook = _swap_hook(lines, heading, pick["hook_script"])
            if _rstrip_lines(old_hook) != _rstrip_lines(_escape_headings(pick["hook_script"]).strip("\n")):
                changed.append("hook")
                old_type = fm.get("hook_type")
                if pick["hook_type"] and pick["hook_type"] != old_type:
                    fm["hook_type"] = pick["hook_type"]
                    lines = _set_overview_hook(lines, pick["hook_type"])
                    entry.append(f"  - 이전 훅 유형: {old_type or '없음'}")
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
        if memo:
            at = memo[0]
        else:                       # 내 메모가 없으면 맨 끝에 넣고, 파일 끝 줄바꿈은 하나만 남긴다
            if lines[-1] != "":
                lines.append("")
            at, block = len(lines) - 1, block[:-1]
        if at > 0 and lines[at - 1].strip():
            block = ["", *block]
        lines[at:at] = block

    if store.exists(keys.conti_md(key)):
        warnings.append("이 기획안의 콘티가 이미 있습니다. 제목·훅 대본이 바뀌었으니 /yt-coach:yt-conti 로 콘티를 다시 만들어 주세요 "
                        "(콘티의 대사는 자동으로 바뀌지 않습니다)")
    out = refstore.join_frontmatter(fm, "\n".join(lines).replace("\n", nl), nl).encode("utf-8")
    try:
        version = store.write(key, BOM + out if raw.startswith(BOM) else out, expect_version=doc.version)
    except VersionConflict as e:                 # 읽은 뒤 다른 곳에서 바꿈
        raise ScriptError("draft_changed", "화면을 연 뒤 기획안이 바뀌었습니다. 다시 불러와 주세요") from e
    return {"path": key, "hash": version, "changed": changed, "warnings": warnings}


# --- CLI -------------------------------------------------------------------------------

def _read_json(value: str, what: str) -> Any:
    try:
        return json.loads(ctx.here().scratch.arg_path(value).read_text("utf-8"))
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
