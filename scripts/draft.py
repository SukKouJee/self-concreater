"""기획(draft): 모티브 카드·아키타입을 입력으로 모으고, Claude가 쓴 대본+샷 리스트를 검증·계산해 저장한다.

사용법 (작업 폴더에서):
  python -m scripts.draft context <category> <motif-id | archetype-id> [--topic <주제>]
  python -m scripts.draft save <category> --draft <json>          → drafts/{date}-{slug}.md
  python -m scripts.draft trash <draft.md>                         기획안과 그 콘티·산출물을 workspace/trash/로 옮긴다

draft JSON (yt-draft 규칙)
  {"title", "topic", "source", "archetype"?, "hook_type", "narrative_type",
   "sections": [{"label", "sub"?, "start", "end", "script", "shots": [{"size", "angle"?, "sec", "action"}]}],
   "prep"?: [..]}
- 프런트매터는 영상 md와 같은 축(sections, hook_type, narrative_type, pacing)을 쓴다 → /yt-diagnose가 그대로 비교
- 샷 리스트는 장면 단위라 pacing.avg_cut_sec는 두지 않는다(basis: scenes). 아키타입과 섹션 비율 차이를 표로 붙인다
"""
from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

from scripts import conti, ctx, keys, motif, refstore
from scripts.features import SHOT_SIZES
from scripts.common import DATA_NOTICE, ArgParser, ScriptError, run
from scripts.videomd import BODY_SUBS, HOOK_TYPES, LABELS, NARRATIVE_TYPES, fmt_time, title_patterns

_ARCHETYPE_RE = re.compile(r"^A\d+$")
LIBRARY_PREFIX = "library:"
LIBRARY_BODY_LIMIT = 3000


# --- context ----------------------------------------------------------------------

def _block(body: str, heading_re: str) -> str | None:
    lines = body.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if re.match(heading_re, line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    return "\n".join(lines[start:end]).strip()


def _library_template(source: str) -> dict:
    """`library:<id>` 출처의 지인 틀을 읽는다. 없거나 틀이 아니면 library_item_missing."""
    from scripts import library                      # library가 motif·conti를 import하므로 지연 import

    item_id = source[len(LIBRARY_PREFIX):]
    try:
        item = library.get(item_id)
    except ScriptError as e:
        raise ScriptError("library_item_missing", f"지인 라이브러리에서 틀을 찾을 수 없습니다: {item_id} ({e.message})") from e
    if item["kind"] != "template":
        raise ScriptError("library_item_missing", f"기획안 틀이 아닌 항목입니다: {item_id}")
    return item


def sources(category: str) -> dict[str, Any]:
    """기획안 출처 목록(★ 카드, 나머지 카드, 아키타입). 지인 틀은 라이브러리 쪽이라 여기 넣지 않는다.

    기획안 화면(api_draft)과 도움말 챗봇 상황(api_help)이 같은 목록을 쓴다.
    """
    details = motif.card_details(category)

    def brief(c: dict) -> dict:
        return {k: c[k] for k in ("id", "title", "type", "type_ko", "idea")}

    index_fm, _ = refstore.read(refstore.require_category(category) + "index.md")
    typify = motif._typify(category, index_fm)
    archetypes = [{"id": a["id"], "name": a.get("name"), "size": a.get("size"), "skeleton": a.get("skeleton"),
                   "mean_duration": a.get("mean_duration")} for a in (typify or {}).get("archetypes") or []]
    return {
        "stars": [brief(c) for c in details if c["starred"]],
        "cards": [brief(c) for c in details if not c["starred"]],
        "archetypes": archetypes,
        "has_typify": motif.has_typify(category),
    }


def context(category: str, source: str, topic: str | None = None) -> dict:
    index_fm, index_body = refstore.read(refstore.require_category(category) + "index.md")
    typify = motif._typify(category, index_fm)
    archetypes = (typify or {}).get("archetypes") or []

    if source.startswith(LIBRARY_PREFIX):
        # 틀은 어느 카테고리 것이든 뼈대로만 쓴다. 기획안은 항상 내 카테고리(category)에 만든다.
        item = _library_template(source)
        fm = item["frontmatter"]
        src = {
            "kind": "library_template", "id": source, "title": item["title"], "data_notice": DATA_NOTICE,
            "author": item["author"], "template_category": item["category"], "note": item.get("note"),
            "worked": item["worked"], "target_sec": item["target_sec"], "hook_type": item["hook_type"],
            "narrative_type": item["narrative_type"], "sections": fm.get("sections"), "pacing": fm.get("pacing"),
            "shot_mix": fm.get("shot_mix"), "text": item["body"][:LIBRARY_BODY_LIMIT],
            "template": {"author": item["author"], "title": item["title"]},   # 서버 소유: UI는 작업에 보관해 저장 때 다시 넣는다. Claude 출력의 값은 쓰지 않는다
        }
    elif _ARCHETYPE_RE.match(source):
        arc = next((a for a in archetypes if a["id"] == source), None)
        text = _block(index_body, rf"^### {source}\. ")
        if not arc or not text:
            raise ScriptError("source_not_found", f"아키타입이 없습니다: {source} (먼저 /yt-typify)")
        src = {"kind": "archetype", "id": source, "title": arc.get("name"), "text": text}
    else:
        card = next((c for c in motif.parse_cards(index_body) if c["id"] == source), None)
        if not card:
            raise ScriptError("source_not_found", f"모티브 카드가 없습니다: {source}")
        text = _block(index_body, rf"^### (★ )?{re.escape(source)} ")
        src = {"kind": "motif", "id": source, "title": card["title"], "type": card["type"], "text": text}

    return {
        "category": category,
        "topic": topic,
        "source": src,
        "archetypes": archetypes,
        "axes": (typify or {}).get("axes"),
        "profile": motif._profile(),
        "shot_sizes": SHOT_SIZES,
        "vocab": {"label": LABELS, "body_sub": BODY_SUBS, "hook_type": HOOK_TYPES,
                  "narrative_type": NARRATIVE_TYPES},
    }


# --- 검증 · 계산 --------------------------------------------------------------------

def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_draft", f"기획안이 형식에 맞지 않습니다: {message}")


def _one_line(value: Any) -> str:
    """줄바꿈·연속 공백을 접어 한 줄로 만든다(md 표·제목·목록이 깨지지 않게)."""
    return " ".join(str(value).split())


def _escape_headings(text: str) -> str:
    """줄바꿈을 LF로 맞추고, 진짜 md 제목(`#`~`######` + 공백/줄끝)이나 표처럼 보이는 줄(`|`) 앞에 백슬래시를 붙인다.
    `#해시태그`는 그대로. 읽을 때(conti.parse_draft)는 이 백슬래시를 되돌린다."""
    return refstore.escape_headings(text)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise _invalid(f"{name}은(는) 숫자여야 합니다")
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise _invalid(f"{name}은(는) 숫자여야 합니다: {value!r}") from e
    if not math.isfinite(number):
        raise _invalid(f"{name}은(는) 유한한 숫자여야 합니다")
    return number


TEMPLATE_LIMITS = {"author": 40, "title": 100}


def _clean_template(value: Any) -> dict:
    """지인 틀 표시용 {author, title}: 한 줄로 접고 길이를 자른다. 모양이 틀리면 invalid_draft."""
    if not isinstance(value, dict):
        raise _invalid("template은 {author, title} 객체여야 합니다")
    out = {k: _one_line(value.get(k) or "")[:n] for k, n in TEMPLATE_LIMITS.items()}
    if not all(out.values()):
        raise _invalid("template의 author와 title이 필요합니다")
    return out


def validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise _invalid("기획안은 객체여야 합니다")
    title = _one_line(data.get("title") or "")
    if not title:
        raise _invalid("title이 비어 있습니다")
    if data.get("hook_type") not in HOOK_TYPES:
        raise _invalid(f"hook_type={data.get('hook_type')!r} (허용: {', '.join(HOOK_TYPES)})")
    if data.get("narrative_type") not in NARRATIVE_TYPES:
        raise _invalid(f"narrative_type={data.get('narrative_type')!r} (허용: {', '.join(NARRATIVE_TYPES)})")
    raw_sections = data.get("sections") or []
    if not isinstance(raw_sections, list):
        raise _invalid("sections는 목록이어야 합니다")
    if not raw_sections:
        raise _invalid("sections가 비어 있습니다")
    if any(not isinstance(s, dict) for s in raw_sections):
        raise _invalid("섹션은 객체여야 합니다")
    prep = data.get("prep")
    if prep is not None and not isinstance(prep, list):
        raise _invalid("prep은 목록이어야 합니다")
    starts = [_finite(s.get("start"), "섹션 start") for s in raw_sections]
    ordered = [s for _, s in sorted(zip(starts, raw_sections), key=lambda pair: pair[0])]
    sections: list[dict] = []
    prev = 0.0
    for raw in ordered:
        s = dict(raw)
        if s.get("label") not in LABELS:
            raise _invalid(f"label={s.get('label')!r} (허용: {', '.join(LABELS)})")
        if s.get("sub") is not None and (s["label"] != "body" or s["sub"] not in BODY_SUBS):
            raise _invalid(f"sub={s.get('sub')!r}는 body에만, {', '.join(BODY_SUBS)} 중 하나")
        s["start"], s["end"] = prev, _finite(s.get("end"), "섹션 end")
        if s["end"] <= s["start"]:
            raise _invalid(f"섹션 {s['label']}의 구간이 비었습니다")
        shots = s.get("shots") or []
        if not isinstance(shots, list):
            raise _invalid(f"섹션 {s['label']}의 shots는 목록이어야 합니다")
        if not shots and not data.get("script_only"):
            raise _invalid(f"섹션 {s['label']}에 샷이 없습니다")
        clean_shots: list[dict] = []
        for raw_shot in shots:
            if not isinstance(raw_shot, dict):
                raise _invalid(f"섹션 {s['label']}의 샷은 객체여야 합니다")
            shot = dict(raw_shot)
            if shot.get("size") not in SHOT_SIZES:
                raise _invalid(f"샷 크기 {shot.get('size')!r} (허용: {', '.join(SHOT_SIZES)})")
            if shot.get("sec") is None:
                raise _invalid(f"섹션 {s['label']}의 샷 sec가 필요합니다")
            if _finite(shot["sec"], "샷 sec") <= 0:
                raise _invalid(f"샷 sec는 0보다 커야 합니다: {shot['sec']!r}")
            for key in ("angle", "action"):
                if shot.get(key) is not None:
                    shot[key] = _one_line(shot[key])
            clean_shots.append(shot)
        if "shots" in s:
            s["shots"] = clean_shots
        if s.get("script") is not None:
            s["script"] = _escape_headings(str(s["script"]))
        sections.append(s)
        prev = s["end"]
    out = {**data, "title": title, "sections": sections}
    for key in ("topic", "source"):
        if data.get(key) is not None:
            out[key] = _one_line(data[key])
    if data.get("template") is not None:
        out["template"] = _clean_template(data["template"])
    if prep is not None:
        out["prep"] = [line for line in (_one_line(p) for p in prep) if line]
    return out


def planned_pacing(sections: list[dict]) -> dict:
    """기획의 pacing 프런트매터. 샷 리스트는 장면 단위(한 장면 10~60초)라 레퍼런스 평균 컷(약 3초)과 견줄 수 없어
    평균 컷을 두지 않는다(`basis: scenes`, 진단 pacing 축은 '컷 정보 없음'). 예전에 저장된 기획의 값은 그대로 읽는다."""
    if any(not s.get("shots") for s in sections):
        return {"avg_cut_sec": None, "by_section": {}}   # 대본만 있는 기획: 컷 정보 없음
    return {"avg_cut_sec": None, "by_section": {}, "basis": "scenes"}


def _scene_summary(sections: list[dict]) -> str:
    """개요 길이 줄의 장면 요약: "장면 N개 · 평균 X초"."""
    count = sum(len(s["shots"]) for s in sections)
    total = sum(s["end"] - s["start"] for s in sections)
    return f"장면 {count}개 · 평균 {round(total / count, 2)}초"


def compare(sections: list[dict], archetype: dict | None) -> dict | None:
    if not archetype:
        return None
    total = sections[-1]["end"]
    ratio: dict[str, float] = {}
    for s in sections:
        ratio[s["label"]] = ratio.get(s["label"], 0) + (s["end"] - s["start"]) / total
    labels = list(dict.fromkeys(list(ratio) + list(archetype["mean_ratio"])))
    rows = [{"label": l, "draft": round(ratio.get(l, 0), 2), "archetype": archetype["mean_ratio"].get(l, 0.0)}
            for l in labels]
    for r in rows:
        r["gap"] = round(r["draft"] - r["archetype"], 2)
    return {"archetype": archetype["id"], "name": archetype.get("name"), "ratio": rows,
            "max_gap": max(abs(r["gap"]) for r in rows),
            "archetype_avg_cut": (archetype.get("pacing") or {}).get("avg_cut_sec")}


# --- 저장 -------------------------------------------------------------------------

def slugify(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip()
    return re.sub(r"[\s_]+", "-", slug)[:40].strip("-") or "draft"


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def render(data: dict, pacing: dict, comp: dict | None, template_line: str | None = None) -> str:
    origin = [f"- 틀: {template_line}"] if template_line else []
    out = [f"# {data['title']}", "", "## 개요",
           f"- 주제: {data.get('topic') or '-'}",
           f"- 출처: {data.get('source') or '-'}" + (f" · 아키타입 {comp['archetype']} {comp['name']}" if comp else ""),
           *origin,
           f"- 길이: {fmt_time(data['sections'][-1]['end'])} · " + (
               _scene_summary(data["sections"]) if pacing.get("basis") == "scenes" else "대본만 (샷 리스트 없음)"),
           f"- 훅: {data['hook_type']} · 서사: {data['narrative_type']}", "", "## 대본"]
    n = 0
    for s in data["sections"]:
        label = f"{s['label']}/{s['sub']}" if s.get("sub") else s["label"]
        out += ["", f"### {fmt_time(s['start'])}–{fmt_time(s['end'])} · {label} ({round(s['end'] - s['start'])}초)",
                f"{str(s.get('script') or '').strip()}"]
        if s.get("shots"):
            out += ["", "| # | 샷 | 앵글 | 길이 | 내용 |", "|---|---|---|---|---|"]
        for shot in s.get("shots") or []:
            n += 1
            action = str(shot.get("action") or "").replace("|", "\\|")
            out.append(f"| {n} | {shot['size']} | {shot.get('angle') or '-'} | {shot.get('sec', '-')}초 | {action} |")
    if comp:
        out += ["", "## 아키타입 비교", f"{comp['archetype']} {comp['name']} 평균과 섹션 비율 비교 "
                f"(아키타입 평균 컷 {comp['archetype_avg_cut']}초 · "
                f"{'이 기획은 장면 단위라 컷 길이 비교 없음' if pacing.get('basis') == 'scenes' else '이 기획 컷 정보 없음'})", "",
                "| 섹션 | 기획 | 아키타입 | 차이 |", "|---|---|---|---|"]
        out += [f"| {r['label']} | {_pct(r['draft'])} | {_pct(r['archetype'])} | {r['gap']:+.0%} |" for r in comp["ratio"]]
    if data.get("prep"):
        out += ["", "## 준비물", *[f"- {p}" for p in data["prep"]]]
    out += ["", "## 내 메모", ""]
    return "\n".join(out)


def find_archetype(category: str, archetype_id: Any) -> dict | None:
    """유형화 결과에서 아키타입을 찾는다. 없거나 유형화 전이면 None."""
    index_fm, _ = refstore.read(refstore.require_category(category) + "index.md")
    archetypes = (motif._typify(category, index_fm) or {}).get("archetypes") or []
    return next((a for a in archetypes if a["id"] == archetype_id), None)


def _template_line(data: dict) -> str:
    """개요의 "틀:" 줄. 저장은 라이브러리에 의존하지 않는다: draft JSON의 template -> 조회 -> 대체 문구 순."""
    tpl = data.get("template")
    if not tpl:
        try:
            item = _library_template(data["source"])
            tpl = {"author": _one_line(item["author"]), "title": _one_line(item["title"])}
        except (ScriptError, OSError):
            return f"지인 틀({data['source'][len(LIBRARY_PREFIX):]})"       # 연결이 끊겼거나 삭제됨: 저장은 막지 않는다
    return f"{tpl['author']}의 {tpl['title']}"


def save(category: str, draft_path: Path) -> dict:
    refstore.require_category(category)
    try:
        data = json.loads(Path(draft_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("invalid_draft", f"기획 JSON을 읽을 수 없습니다: {e}") from e
    data = validate(data)
    archetype = find_archetype(category, data.get("archetype"))
    pacing = planned_pacing(data["sections"])
    comp = compare(data["sections"], archetype)
    source = data.get("source")
    template_line = _template_line(data) if isinstance(source, str) and source.startswith(LIBRARY_PREFIX) else None

    today = date.today().isoformat()
    store = ctx.here().store
    base = f"{today}-{slugify(data['title'])}"
    fm = {
        "date": today, "category": category, "title": data["title"], "topic": data.get("topic"),
        "source": data.get("source"), "archetype": data.get("archetype"),
        "target_sec": float(data["sections"][-1]["end"]),
        "sections": [{"label": s["label"], **({"sub": s["sub"]} if s.get("sub") else {}),
                      "start": round(s["start"], 2), "end": round(s["end"], 2)} for s in data["sections"]],
        "hook_type": data["hook_type"], "narrative_type": data["narrative_type"],
        "title_pattern": title_patterns(data["title"]), "pacing": pacing,
        "status": "script" if data.get("script_only") else "draft", "schema_version": 1,
    }
    content = refstore.join_frontmatter(fm, render(data, pacing, comp, template_line)).encode("utf-8")
    key, i = keys.draft_md(base), 2
    while True:
        try:
            store.create(key, content)           # exclusive create: never overwrite a file that appeared meanwhile
            break
        except FileExistsError:
            key, i = keys.draft_md(f"{base}-{i}"), i + 1
    Path(draft_path).unlink(missing_ok=True)
    return {"status": "saved", "path": key, "pacing": pacing, "compare": comp}


def trash(draft_md: str | Path) -> dict:
    """기획안을 지운다: 기획안 md와 그 콘티(md·HTML·SRT·자막 영상)를 `workspace/trash/<시각>/`으로 옮긴다."""
    path = conti.safe_draft_path(str(draft_md), conti_file=False)     # drafts/ 안의 기획안만
    c = ctx.here()
    key = c.scratch.key_of(path)
    if not c.store.exists(key):
        raise ScriptError("draft_not_found", f"기획안을 찾을 수 없습니다: {draft_md}")
    # conti.trash_targets는 Path를 낸다(Task 8에서 키로 바뀜): 키로 바꿔 휴지통 저장소에 맡긴다
    targets = [key, *(c.scratch.key_of(p) for p in conti.trash_targets(conti.conti_path(path)))]
    return c.trash.move(targets, last=key)


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.draft")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("context")
    c.add_argument("category")
    c.add_argument("source")
    c.add_argument("--topic")
    s = sub.add_parser("save")
    s.add_argument("category")
    s.add_argument("--draft", required=True)
    sub.add_parser("trash").add_argument("draft")
    args = parser.parse_args(argv)
    if args.cmd == "trash":
        return trash(args.draft)
    if args.cmd == "context":
        return context(args.category, args.source, args.topic)
    return save(args.category, ctx.here().scratch.arg_path(args.draft))


if __name__ == "__main__":
    run(main)
