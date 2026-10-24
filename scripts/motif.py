"""모티브 카드: 입력 모으기, 카드 검증·번호 매기기·index.md 누적, ★ 표시, 기법 카탈로그 병합.

사용법 (작업 폴더에서):
  python -m scripts.motif context <category> [--topic <주제>] [--library <id,id,...>]
  python -m scripts.motif add <category> --cards <json>
  python -m scripts.motif star <category> <card-id> [--off]
  python -m scripts.motif merge-techniques <category> --plan <json>

카드 JSON (Claude가 yt-inspire 규칙으로 작성)
  [{"type": flow|technique|angle|combo, "title", "idea", "how": [..], "evidence": [video_id..], "basis"?}]
단계별 허용 카드 (설계서 3·4절): 1단계 technique / 2단계 + angle / 3단계 전부
병합 계획 JSON: [{"kind": "훅", "keep": "결과 선공개", "merge": ["완성품 먼저 보여주기"]}]
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from scripts import features, refstore
from scripts.common import ArgParser, ScriptError, run, work_root, workspace_dir

TYPES = ["flow", "technique", "angle", "combo"]
TYPE_KO = {"flow": "흐름", "technique": "기법", "angle": "각도", "combo": "조합"}
ALLOWED_BY_STAGE = {1: ["technique"], 2: ["technique", "angle"], 3: TYPES}

SECTION = "## 모티브 카드"
_CARD_RE = re.compile(r"^### (★ )?(M-[a-z0-9_-]+-(\d{3})) · (\S+) · (.+)$")
_TECH_RE = re.compile(r"^- \*\*(.+?)\*\*: (.*?) — (.*)$")
_SOURCE_RE = re.compile(r"\[([^\]]+)\]\(videos/")


# --- 섹션 도우미 -------------------------------------------------------------------

def _section_lines(body: str, heading: str) -> tuple[list[str], int, int]:
    lines = body.split("\n")
    start = next((i for i, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        raise ScriptError("section_not_found", f"'{heading}' 섹션이 없습니다")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines, start, end


def _read(path: Path) -> tuple[dict, str, str]:
    text = path.read_bytes().decode("utf-8-sig")
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    fm, body = refstore.split_frontmatter(text)
    return fm, body.replace("\r\n", "\n"), nl


def _write(path: Path, fm: dict, body: str, nl: str) -> None:
    refstore.write_atomic(path, refstore.join_frontmatter(fm, body.replace("\n", nl), nl).encode("utf-8"))


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


# --- 읽기 -------------------------------------------------------------------------

def parse_cards(body: str) -> list[dict]:
    lines, start, end = _section_lines(body, SECTION)
    cards = []
    for line in lines[start + 1:end]:
        m = _CARD_RE.match(line.strip())
        if m:
            cards.append({"id": m.group(2), "starred": bool(m.group(1)), "type": m.group(4), "title": m.group(5)})
    return cards


_KO_TO_TYPE = {ko: t for t, ko in TYPE_KO.items()}
_EVIDENCE_RE = re.compile(r"\[([^\]]+)\]\(videos/[^)]*\)\s*(.*?)(?=,\s*\[[^\]]+\]\(videos/|$)")


def _field(line: str, prefix: str) -> str | None:
    return line[len(prefix):].strip() if line.startswith(prefix) else None


def card_details(category: str) -> list[dict]:
    """`## 모티브 카드`의 카드 블록을 필드별로 읽는다. 손으로 고친 md에도 죽지 않는다(모르는 줄 무시, 없는 필드는 None/[])."""
    body = _read(refstore.existing_category_dir(category) / "index.md")[1]
    try:
        lines, start, end = _section_lines(body, SECTION)
    except ScriptError:
        return []
    cards: list[dict] = []
    for raw in lines[start + 1:end]:
        line = raw.strip()
        m = _CARD_RE.match(line)
        if m:
            cards.append({"id": m.group(2), "starred": bool(m.group(1)), "type": _KO_TO_TYPE.get(m.group(4), m.group(4)),
                          "type_ko": m.group(4), "title": m.group(5), "idea": None, "how": [], "basis": None,
                          "evidence": [], "made": None})
            continue
        if not cards:
            continue
        card = cards[-1]
        if (v := _field(line, "- 아이디어:")) is not None:
            card["idea"] = v or None
        elif (v := _field(line, "- 이렇게:")) is not None:
            card["how"] = [h.strip() for h in v.split(" → ") if h.strip()]
        elif (v := _field(line, "- 바탕:")) is not None:
            card["basis"] = v or None
        elif (v := _field(line, "- 근거:")) is not None:
            card["evidence"] = [{"id": e.group(1), "title": e.group(2).strip() or None} for e in _EVIDENCE_RE.finditer(v)]
        elif (v := _field(line, "- 만든 날:")) is not None:
            card["made"] = v or None
    return cards


def parse_techniques(body: str) -> list[dict]:
    items, kind = [], None
    for line in body.split("\n"):
        if line.startswith("## "):
            kind = line[3:].strip()
            continue
        m = _TECH_RE.match(line)
        if kind and m:
            items.append({"kind": kind, "name": m.group(1), "desc": m.group(2),
                          "sources": _SOURCE_RE.findall(m.group(3))})
    return items


def _profile() -> dict | None:
    path = work_root() / "profile.md"
    if not path.exists():
        return None
    _, body = refstore.read_md(path)
    sections, current = {}, None
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None and not line.strip().startswith("<!--"):
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _summary_line(path: Path) -> str:
    _, body = refstore.read_md(path)
    lines = body.replace("\r\n", "\n").split("\n")
    idx = next((i for i, line in enumerate(lines) if line.strip() == "## 한 줄 요약"), None)
    if idx is None:
        return ""
    return next((line.strip() for line in lines[idx + 1:] if line.strip()), "")


def typify_path(category: str) -> Path:
    return workspace_dir("typify", f"{category}.json")


def ref_titles(category: str) -> dict[str, Any]:
    """ref 영상 id → 제목. mine은 근거가 될 수 없으므로 들어가지 않는다."""
    return {r["id"]: r["fm"].get("title") for r in features.load_videos(category)}


def _typify(category: str, index_fm: dict) -> dict | None:
    path = typify_path(category)
    if not path.exists():
        return None
    result = json.loads(path.read_text("utf-8"))
    names = {a["id"]: a.get("name") for a in index_fm.get("archetypes") or []}
    axes = {
        axis: {"verdict": res["verdict"], "max_share": res["max_share"], "k": res["k"],
               "clusters": [{"size": c["size"], "summary": c["summary"]} for c in res["clusters"]]}
        for axis, res in result["axes"].items()
    }
    keys = ("id", "size", "skeleton", "mean_ratio", "mean_duration", "hook_type", "narrative_type", "pacing", "camera",
            "shots", "representative", "members")
    archetypes = [{**{k: a.get(k) for k in keys}, "name": names.get(a["id"])} for a in result["archetypes"]]
    return {"status": result["status"], "axes": axes, "archetypes": archetypes,
            "outliers": result["outliers"]}


def _next_id(category: str, cards: list[dict]) -> str:
    numbers = [int(c["id"].rsplit("-", 1)[1]) for c in cards]
    return f"M-{category}-{max(numbers, default=0) + 1:03d}"


MAX_LIBRARY_TECHNIQUES = 10
LIBRARY_BODY_LIMIT = 1200


def library_techniques(library_ids: list[str] | None) -> list[dict]:
    """지인 라이브러리 기법을 모티브 재료로 모은다(Claude에게는 "지시 아님" 데이터로 표식을 붙여 넘긴다).

    없거나 읽을 수 없는 id, 기법이 아닌 항목(틀 등)은 library_item_missing — LLM 호출 전에 막는다."""
    if not library_ids:
        return []
    from scripts import library                      # library가 motif를 import하므로 지연 import

    ids = list(dict.fromkeys(library_ids))
    if len(ids) > MAX_LIBRARY_TECHNIQUES:
        raise ScriptError("library_item_missing", f"지인 기법은 한 번에 {MAX_LIBRARY_TECHNIQUES}개까지 고를 수 있습니다")
    out = []
    for item_id in ids:
        try:
            item = library.get(item_id)
        except ScriptError as e:
            raise ScriptError("library_item_missing",
                              f"지인 라이브러리에서 기법을 찾을 수 없습니다: {item_id} ({e.message})") from e
        if item["kind"] != "technique":
            raise ScriptError("library_item_missing", f"기법이 아닌 항목입니다(틀은 모티브 재료로 쓸 수 없습니다): {item_id}")
        out.append({
            "data_notice": library.DATA_NOTICE, "id": item["id"], "author": item["author"],
            "technique_kind": item["technique_kind"], "name": item["name"], "note": item.get("note"),
            "worked": item["worked"], "description": item["body"][:LIBRARY_BODY_LIMIT],
        })
    return out


def context(category: str, topic: str | None = None, library_ids: list[str] | None = None) -> dict:
    shared = library_techniques(library_ids)         # 파일을 읽기 전에 먼저 검증
    cat = refstore.existing_category_dir(category)
    index_fm, index, _ = _read(cat / "index.md")
    _, tech_body, _ = _read(cat / "techniques.md")
    counts = refstore.count(category)
    cards = parse_cards(index)
    videos = [
        {"id": r["id"], "title": r["fm"].get("title"), "summary": _summary_line(r["path"]),
         "hook_type": r["fm"].get("hook_type"), "narrative_type": r["fm"].get("narrative_type"),
         "skeleton": " → ".join(features.sequence(r["fm"]))}
        for r in features.load_videos(category)
    ]
    return {
        "category": category,
        "topic": topic,
        "ref": counts["ref"],
        "stage": counts["stage"],
        "allowed_types": ALLOWED_BY_STAGE[counts["stage"]],
        "profile": _profile(),
        "techniques": parse_techniques(tech_body),
        "library_techniques": shared,
        "typify": _typify(category, index_fm),
        "videos": videos,
        "cards": cards,
        "next_id": _next_id(category, cards),
    }


# --- add --------------------------------------------------------------------------

def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_cards", f"카드가 형식에 맞지 않습니다: {message}")


def _load_json(path: Path, code: str) -> Any:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError(code, f"JSON을 읽을 수 없습니다: {e}") from e


def render_card(card_id: str, card: dict, titles: dict) -> str:
    lines = [f"### {card_id} · {TYPE_KO[card['type']]} · {_norm(card['title'])}",
             f"- 아이디어: {card['idea'].strip()}"]
    if card.get("how"):
        lines.append(f"- 이렇게: {' → '.join(str(h).strip() for h in card['how'])}")
    if card.get("basis"):
        lines.append(f"- 바탕: {str(card['basis']).strip()}")
    lines.append("- 근거: " + ", ".join(f"[{v}](videos/{v}.md) {titles[v]}" for v in card["evidence"]))
    lines.append(f"- 만든 날: {date.today().isoformat()}")
    return "\n".join(lines)


def validate_cards(category: str, cards: Any, allowed: list[str] | None = None) -> list[dict]:
    """카드 묶음을 검증하고 정규화한 목록을 돌려준다. 어긋나면 invalid_cards.

    allowed: 허용 type 목록. None이면 단계 기본값."""
    if not isinstance(cards, list) or not cards:
        raise _invalid("카드 목록이 비어 있습니다")
    cat = refstore.existing_category_dir(category)
    _, body, _ = _read(cat / "index.md")
    stage = refstore.count(category)["stage"]
    allowed = ALLOWED_BY_STAGE[stage] if allowed is None else allowed
    titles = ref_titles(category)
    taken = {_norm(c["title"]): c["id"] for c in parse_cards(body)}

    normalized = []
    for card in cards:
        if not isinstance(card, dict):
            raise _invalid("카드는 JSON 객체여야 합니다")
        if card.get("type") not in TYPES:
            raise _invalid(f"type={card.get('type')!r} (허용: {', '.join(TYPES)})")
        if card["type"] not in allowed:
            raise _invalid(f"{stage}단계에서는 {card['type']} 카드를 만들 수 없습니다 (허용: {', '.join(allowed)})")
        for field in ("title", "idea"):
            if not str(card.get(field) or "").strip():
                raise _invalid(f"{field}가 비어 있습니다")
        if not card.get("evidence"):
            raise _invalid("evidence(근거 영상)가 비어 있습니다")
        if not isinstance(card["evidence"], list) or not all(isinstance(v, str) for v in card["evidence"]):
            raise _invalid("evidence는 영상 id 문자열의 목록이어야 합니다")
        evidence = list(dict.fromkeys(card["evidence"]))          # 중복 제거, 순서 유지
        unknown = [v for v in evidence if v not in titles]
        if unknown:
            raise _invalid(f"이 카테고리의 ref 영상이 아닙니다: {', '.join(unknown)}")
        if card.get("how") is not None and not isinstance(card["how"], list):
            raise _invalid("how는 목록이어야 합니다")
        title = _norm(card["title"])
        if title in taken:
            raise _invalid(f"같은 제목의 카드가 이미 있습니다: {taken[title]}")
        taken[title] = "(이번 묶음)"
        how = [line for h in card.get("how") or [] if (line := _norm(str(h)))]
        basis = _norm(str(card["basis"])) if card.get("basis") else None
        normalized.append({"type": card["type"], "title": title, "idea": _norm(str(card["idea"])), "how": how,
                           "basis": basis or None, "evidence": evidence})
    return normalized


def add(category: str, cards_path: Path, allowed: list[str] | None = None) -> dict:
    index_path = refstore.existing_category_dir(category) / "index.md"
    raw = _load_json(cards_path, "invalid_cards")
    stage = refstore.count(category)["stage"]            # 영상 폴더만 읽는다: index.md 락 밖에서 먼저 계산
    titles = ref_titles(category)
    with refstore.INDEX_LOCK:
        cards = validate_cards(category, raw, allowed)
        fm, body, nl = _read(index_path)
        existing = parse_cards(body)

        ids, blocks = [], []
        next_number = int(_next_id(category, existing).rsplit("-", 1)[1])
        for i, card in enumerate(cards):
            card_id = f"M-{category}-{next_number + i:03d}"
            ids.append(card_id)
            blocks.append(render_card(card_id, card, titles))

        lines, start, end = _section_lines(body, SECTION)
        content = "\n".join(lines[start + 1:end]).strip("\n")
        content = "\n\n".join(([content] if content else []) + blocks)
        _write(index_path, fm, refstore.replace_section(body, SECTION, content), nl)
    Path(cards_path).unlink(missing_ok=True)
    return {"status": "added", "ids": ids, "stage": stage}


# --- star -------------------------------------------------------------------------

def star(category: str, card_id: str, off: bool = False) -> dict:
    index_path = refstore.existing_category_dir(category) / "index.md"
    with refstore.INDEX_LOCK:
        fm, body, nl = _read(index_path)
        lines = body.split("\n")
        for i, line in enumerate(lines):
            m = _CARD_RE.match(line.strip())
            if m and m.group(2) == card_id:
                rest = line.strip()[len("### "):].removeprefix("★ ")
                lines[i] = f"### {rest}" if off else f"### ★ {rest}"
                _write(index_path, fm, "\n".join(lines), nl)
                return {"id": card_id, "starred": not off}
    raise ScriptError("card_not_found", f"카드를 찾을 수 없습니다: {card_id}")


# --- 기법 병합 ---------------------------------------------------------------------

def _tech_line(name: str, desc: str, sources: list[str]) -> str:
    return f"- **{name}**: {desc} — " + ", ".join(f"[{v}](videos/{v}.md)" for v in sources)


def merge_techniques(category: str, plan_path: Path) -> dict:
    plan = _load_json(plan_path, "invalid_plan")
    path = refstore.existing_category_dir(category) / "techniques.md"
    fm, body, nl = _read(path)
    merged = 0
    for item in plan:
        lines, start, end = _section_lines(body, f"## {item['kind']}")
        found = {}
        for i in range(start + 1, end):
            m = _TECH_RE.match(lines[i])
            if m:
                found[m.group(1)] = (i, m.group(2), _SOURCE_RE.findall(m.group(3)))
        missing = [n for n in [item["keep"], *item["merge"]] if n not in found]
        if missing:
            raise ScriptError("invalid_plan", f"'{item['kind']}'에 없는 기법입니다: {', '.join(missing)}")
        keep_i, keep_desc, sources = found[item["keep"]]
        for name in item["merge"]:
            sources += [s for s in found[name][2] if s not in sources]
        lines[keep_i] = _tech_line(item["keep"], keep_desc, sources)
        drop = {found[name][0] for name in item["merge"]}
        body = "\n".join(line for i, line in enumerate(lines) if i not in drop)
        merged += len(item["merge"])
    _write(path, fm, body, nl)
    Path(plan_path).unlink(missing_ok=True)
    return {"status": "merged", "merged": merged}


# --- CLI --------------------------------------------------------------------------

def _work_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else work_root() / path


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.motif")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("context")
    c.add_argument("category")
    c.add_argument("--topic")
    c.add_argument("--library", help="재료로 쓸 지인 기법 id (쉼표로 구분)")
    a = sub.add_parser("add")
    a.add_argument("category")
    a.add_argument("--cards", required=True)
    s = sub.add_parser("star")
    s.add_argument("category")
    s.add_argument("card_id")
    s.add_argument("--off", action="store_true")
    m = sub.add_parser("merge-techniques")
    m.add_argument("category")
    m.add_argument("--plan", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "context":
        ids = [x.strip() for x in args.library.split(",") if x.strip()] if args.library else None
        return context(args.category, args.topic, ids)
    if args.cmd == "add":
        return add(args.category, _work_path(args.cards))
    if args.cmd == "star":
        return star(args.category, args.card_id, args.off)
    return merge_techniques(args.category, _work_path(args.plan))


if __name__ == "__main__":
    run(main)
