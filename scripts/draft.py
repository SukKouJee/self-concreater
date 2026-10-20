"""기획(draft): 모티브 카드·아키타입을 입력으로 모으고, Claude가 쓴 대본+샷 리스트를 검증·계산해 저장한다.

사용법 (작업 폴더에서):
  python -m scripts.draft context <category> <motif-id | archetype-id> [--topic <주제>]
  python -m scripts.draft save <category> --draft <json>          → drafts/{date}-{slug}.md

draft JSON (yt-draft 규칙)
  {"title", "topic", "source", "archetype"?, "hook_type", "narrative_type",
   "sections": [{"label", "sub"?, "start", "end", "script", "shots": [{"size", "angle"?, "sec", "action"}]}],
   "prep"?: [..]}
- 프런트매터는 영상 md와 같은 축(sections, hook_type, narrative_type, pacing)을 쓴다 → /yt-diagnose가 그대로 비교
- 계획 컷 길이 = 섹션 길이 / 샷 수. 아키타입과 섹션 비율 차이를 표로 붙인다
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from scripts import motif, refstore
from scripts.features import SHOT_SIZES
from scripts.common import ArgParser, ScriptError, run, work_root
from scripts.videomd import BODY_SUBS, HOOK_TYPES, LABELS, NARRATIVE_TYPES, fmt_time, title_patterns

_ARCHETYPE_RE = re.compile(r"^A\d+$")


# --- context ----------------------------------------------------------------------

def _block(body: str, heading_re: str) -> str | None:
    lines = body.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if re.match(heading_re, line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    return "\n".join(lines[start:end]).strip()


def context(category: str, source: str, topic: str | None = None) -> dict:
    cat = refstore.existing_category_dir(category)
    index_fm, index_body = refstore.read_md(cat / "index.md")
    typify = motif._typify(category, index_fm)
    archetypes = (typify or {}).get("archetypes") or []

    if _ARCHETYPE_RE.match(source):
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


def validate(data: dict) -> dict:
    if not str(data.get("title") or "").strip():
        raise _invalid("title이 비어 있습니다")
    if data.get("hook_type") not in HOOK_TYPES:
        raise _invalid(f"hook_type={data.get('hook_type')!r} (허용: {', '.join(HOOK_TYPES)})")
    if data.get("narrative_type") not in NARRATIVE_TYPES:
        raise _invalid(f"narrative_type={data.get('narrative_type')!r} (허용: {', '.join(NARRATIVE_TYPES)})")
    sections = sorted(data.get("sections") or [], key=lambda s: float(s["start"]))
    if not sections:
        raise _invalid("sections가 비어 있습니다")
    prev = 0.0
    for s in sections:
        if s.get("label") not in LABELS:
            raise _invalid(f"label={s.get('label')!r} (허용: {', '.join(LABELS)})")
        if s.get("sub") is not None and (s["label"] != "body" or s["sub"] not in BODY_SUBS):
            raise _invalid(f"sub={s.get('sub')!r}는 body에만, {', '.join(BODY_SUBS)} 중 하나")
        s["start"], s["end"] = prev, float(s["end"])
        if s["end"] <= s["start"]:
            raise _invalid(f"섹션 {s['label']}의 구간이 비었습니다")
        if not s.get("shots") and not data.get("script_only"):
            raise _invalid(f"섹션 {s['label']}에 샷이 없습니다")
        for shot in s.get("shots") or []:
            if shot.get("size") not in SHOT_SIZES:
                raise _invalid(f"샷 크기 {shot.get('size')!r} (허용: {', '.join(SHOT_SIZES)})")
        prev = s["end"]
    return {**data, "sections": sections}


def planned_pacing(sections: list[dict]) -> dict:
    if any(not s.get("shots") for s in sections):
        return {"avg_cut_sec": None, "by_section": {}}   # 대본만 있는 기획: 컷 정보 없음
    by: dict[str, list[float]] = {}
    for s in sections:
        dur, n = by.get(s["label"], [0.0, 0])
        by[s["label"]] = [dur + s["end"] - s["start"], n + len(s["shots"])]
    total = sum(s["end"] - s["start"] for s in sections)
    shots = sum(len(s["shots"]) for s in sections)
    return {"avg_cut_sec": round(total / shots, 2),
            "by_section": {k: round(d / n, 2) for k, (d, n) in by.items()}}


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


def render(data: dict, pacing: dict, comp: dict | None) -> str:
    out = [f"# {data['title']}", "", "## 개요",
           f"- 주제: {data.get('topic') or '-'}",
           f"- 출처: {data.get('source') or '-'}" + (f" · 아키타입 {comp['archetype']} {comp['name']}" if comp else ""),
           f"- 길이: {fmt_time(data['sections'][-1]['end'])} · " + (
               f"계획 컷 {sum(len(s['shots']) for s in data['sections'])}개 (평균 {pacing['avg_cut_sec']}초)"
               if pacing["avg_cut_sec"] else "대본만 (샷 리스트 없음)"),
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
                f"(아키타입 평균 컷 {comp['archetype_avg_cut']}초 · 이 기획 "
                f"{str(pacing['avg_cut_sec']) + '초' if pacing['avg_cut_sec'] else '컷 정보 없음'})", "",
                "| 섹션 | 기획 | 아키타입 | 차이 |", "|---|---|---|---|"]
        out += [f"| {r['label']} | {_pct(r['draft'])} | {_pct(r['archetype'])} | {r['gap']:+.0%} |" for r in comp["ratio"]]
    if data.get("prep"):
        out += ["", "## 준비물", *[f"- {p}" for p in data["prep"]]]
    out += ["", "## 내 메모", ""]
    return "\n".join(out)


def save(category: str, draft_path: Path) -> dict:
    refstore.existing_category_dir(category)
    try:
        data = json.loads(Path(draft_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("invalid_draft", f"기획 JSON을 읽을 수 없습니다: {e}") from e
    data = validate(data)
    index_fm, _ = refstore.read_md(refstore.existing_category_dir(category) / "index.md")
    archetypes = (motif._typify(category, index_fm) or {}).get("archetypes") or []
    archetype = next((a for a in archetypes if a["id"] == data.get("archetype")), None)
    pacing = planned_pacing(data["sections"])
    comp = compare(data["sections"], archetype)

    today = date.today().isoformat()
    drafts = work_root() / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    base = f"{today}-{slugify(data['title'])}"
    path, i = drafts / f"{base}.md", 2
    while path.exists():
        path, i = drafts / f"{base}-{i}.md", i + 1

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
    path.write_bytes(refstore.join_frontmatter(fm, render(data, pacing, comp)).encode("utf-8"))
    Path(draft_path).unlink(missing_ok=True)
    return {"status": "saved", "path": path.relative_to(work_root()).as_posix(), "pacing": pacing, "compare": comp}


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
    args = parser.parse_args(argv)
    if args.cmd == "context":
        return context(args.category, args.source, args.topic)
    path = Path(args.draft)
    return save(args.category, path if path.is_absolute() else work_root() / path)


if __name__ == "__main__":
    run(main)
