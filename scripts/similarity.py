"""진단: 내 콘텐츠(mine 영상 md · draft · 대본)를 아키타입과 축별로 비교하고 결과를 mine/에 쓴다.

사용법 (작업 폴더에서):
  python -m scripts.similarity compare <category> <target.md>
  python -m scripts.similarity save <category> <target.md> --notes <json>     → mine/<name>.md
  python -m scripts.similarity intentional <category> <name> <gap-key> [--off]

- 비교 대상: refs/ref_<cat>/videos/<id>.md(role: mine) 또는 drafts/*.md. ref 영상은 대상이 아니다
- 근접도(0~1, 단일 종합 점수 없음)
  structure·pacing: 1 − (아키타입 중심까지 거리 ÷ 다른 ref 영상들이 그 중심에서 떨어진 거리의 중앙값), 0~1로 자름
                    1 = 중심과 같음, 0 = 다른 유형 영상만큼 다름 (백분위는 작은·촘촘한 아키타입에서 0%로 무너져 쓰지 않는다)
- 가장 가까운 아키타입: structure 실제 거리가 가장 짧은 것
  hook·narrative: 구성 영상 중 같은 값의 비율 / title: 패턴 자카드 평균
- 갭 키: structure:<label>(비중 차 ≥ 0.1), hook, narrative(아키타입의 50% 이상 우세 값과 다름), pacing:avg_cut(평균 컷 ±30%)
- intentional로 표시한 갭은 이후 진단·미션에서 뺀다. 진단은 아키타입을 바꾸지 않는다
notes JSON: {"summary", "gaps": {<gap-key>: 설명}, "actions": [1~3개]}
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from scripts import ctx, features, keys, refstore
from scripts.common import ArgParser, ScriptError, run

STRUCTURE_GAP = 0.1
PACING_GAP = 0.3
DOMINANT_SHARE = 0.5      # hook·narrative는 아키타입 안에서 이 비율 이상인 값이 있을 때만 '기준'으로 본다
FALLBACK_SCALE = {"structure": 0.5, "pacing": 0.7, "shots": 0.7}   # 다른 ref가 없을 때(아키타입이 하나뿐) 쓰는 거리 척도


# --- 대상 · 아키타입 ---------------------------------------------------------------

def _load_typify(category: str) -> dict:
    objects = ctx.here().objects
    key = keys.typify(category)
    if not objects.exists(key):
        raise ScriptError("no_typify_result", f"유형화 결과가 없습니다. 먼저 /yt-typify {category}")
    try:
        result = json.loads(objects.get(key).decode("utf-8"))
    except (OSError, ValueError) as e:                # 깨진 JSON은 유형화 결과가 없는 것으로 본다
        raise ScriptError("no_typify_result", f"유형화 결과를 읽을 수 없습니다. 다시 /yt-typify {category}") from e
    if not isinstance(result, dict) or not result.get("archetypes"):
        raise ScriptError("no_archetype", "비교할 아키타입이 없습니다(구조가 비슷한 ref 묶음이 아직 없음).")
    return result


def typify_gate(category: str) -> dict[str, bool | None]:
    """진단 가능 여부: 비교할 아키타입이 있는 유형화 결과가 있으면 available.

    preliminary는 그 결과의 status가 "formal"이 아닌지(마지막 유형화 실행 때 ref 수로 정해진다)다.
    진단 화면(api_diagnose)과 도움말 챗봇 상황(api_help)이 같은 판단을 쓴다.
    """
    try:
        result = _load_typify(category)
    except ScriptError:
        return {"available": False, "preliminary": None}
    return {"available": True, "preliminary": result.get("status") != "formal"}


def _names(category: str) -> dict:
    refstore.require_category(category)
    fm, _ = refstore.read(keys.index_md(category))
    return {a["id"]: a.get("name") for a in fm.get("archetypes") or []}


def _diag_key(name: str) -> str:
    return keys.mine_md(name)


def _target(value: str | Path) -> dict:
    """진단 대상(작업 폴더 기준 키·상대 경로 또는 절대 경로) → {name, fm, rel}. `rel`은 문서 키다."""
    scratch = ctx.here().scratch
    path = scratch.arg_path(value)
    not_found = ScriptError("target_not_found", f"진단할 파일이 없습니다: {path}")
    try:
        key = scratch.key_of(path)
    except ScriptError as e:                           # 작업 폴더 밖
        raise not_found from e
    if not keys.is_doc(key) or not ctx.here().store.exists(key):
        raise not_found
    fm, _ = refstore.read(key)
    if fm.get("role") == "ref":
        raise ScriptError("not_mine", "ref 영상은 진단 대상이 아닙니다. 내 영상은 /yt-add <cat> mine <링크>로 넣으세요.")
    if not fm.get("sections"):
        raise ScriptError("no_sections", "섹션 정보가 없어 비교할 수 없습니다.")
    stem = keys.stem(key)
    name = str(fm.get("id") or stem) if fm.get("role") == "mine" else stem
    return {"name": name, "fm": fm, "rel": key}


# --- 근접도 -----------------------------------------------------------------------

def _vectors(axis: str, fms: list[dict]) -> np.ndarray:
    return np.array([v for v in (features.VECTORS[axis](fm) for fm in fms) if v is not None])


def _vector_proximity(axis: str, target_fm: dict, member_fms: list[dict],
                      other_fms: list[dict]) -> tuple[float | None, float | None]:
    """(근접도, 중심까지 거리). other_fms = 이 아키타입에 속하지 않은 ref."""
    vec = features.VECTORS[axis](target_fm)
    members, others = _vectors(axis, member_fms), _vectors(axis, other_fms)
    if vec is None or not len(members):
        return None, None
    center = members.mean(axis=0)
    dist = float(np.linalg.norm(np.array(vec) - center))
    scale = float(np.median([np.linalg.norm(x - center) for x in others])) if len(others) else FALLBACK_SCALE[axis]
    return round(min(1.0, max(0.0, 1 - dist / max(scale, 1e-6))), 2), dist


def _share(value: Any, values: list[Any]) -> float:
    return round(sum(1 for v in values if v == value) / len(values), 2)


def _title_proximity(target_fm: dict, member_fms: list[dict]) -> float:
    t = set(features.title_patterns(str(target_fm.get("title") or "")))
    scores = []
    for fm in member_fms:
        m = set(features.title_patterns(str(fm.get("title") or "")))
        scores.append(1.0 if not (t | m) else len(t & m) / len(t | m))
    return round(float(np.mean(scores)), 2)


def proximities(target_fm: dict, member_fms: list[dict], other_fms: list[dict]) -> dict:
    structure, distance = _vector_proximity("structure", target_fm, member_fms, other_fms)
    pacing, _ = _vector_proximity("pacing", target_fm, member_fms, other_fms)
    shots, _ = _vector_proximity("shots", target_fm, member_fms, other_fms)
    return {
        "structure": {"proximity": structure, "distance": None if distance is None else round(distance, 3)},
        "hook": {"proximity": _share(target_fm.get("hook_type"), [fm.get("hook_type") for fm in member_fms])},
        "narrative": {"proximity": _share(target_fm.get("narrative_type"),
                                          [fm.get("narrative_type") for fm in member_fms])},
        "title": {"proximity": _title_proximity(target_fm, member_fms)},
        "pacing": {"proximity": pacing, **({"note": "컷 정보 없음"} if pacing is None else {})},
        "shots": {"proximity": shots, **({"note": "샷 분류 없음"} if shots is None else {})},
    }


# --- 갭 ---------------------------------------------------------------------------

def _ratios(fm: dict) -> dict:
    vec = features.structure_vec(fm)
    return {l: vec[features.STRUCTURE_FIELDS.index(f"share:{l}")] for l in features.LABELS}


def gaps(target_fm: dict, archetype: dict, member_fms: list[dict]) -> list[dict]:
    found = []
    ratios = _ratios(target_fm)
    for label in features.LABELS:
        t, a = round(ratios[label], 2), archetype["mean_ratio"].get(label, 0.0)
        if abs(t - a) >= STRUCTURE_GAP:
            found.append({"key": f"structure:{label}", "target": t, "archetype": a})
    for axis, field in (("hook", "hook_type"), ("narrative", "narrative_type")):
        dominant, count = Counter(fm.get(field) for fm in member_fms).most_common(1)[0]
        if count / len(member_fms) >= DOMINANT_SHARE and target_fm.get(field) != dominant:
            found.append({"key": axis, "target": target_fm.get(field), "archetype": dominant})
    t_cut = (target_fm.get("pacing") or {}).get("avg_cut_sec")
    a_cut = (archetype.get("pacing") or {}).get("avg_cut_sec")
    if t_cut and a_cut and abs(math.log(t_cut / a_cut)) >= math.log(1 + PACING_GAP):
        found.append({"key": "pacing:avg_cut", "target": t_cut, "archetype": a_cut})
    return found


# --- compare ----------------------------------------------------------------------

def _intentional(name: str) -> list[str]:
    key = _diag_key(name)
    return list(refstore.read(key)[0].get("intentional") or []) if ctx.here().store.exists(key) else []


def compare(category: str, target_path: str | Path) -> dict:
    typify = _load_typify(category)
    names = _names(category)
    target = _target(target_path)
    by_id = {r["id"]: r["fm"] for r in features.load_videos(category)}

    scored = []
    for arc in typify["archetypes"]:
        member_fms = [by_id[m] for m in arc["members"] if m in by_id]
        if member_fms:
            others = [fm for vid, fm in by_id.items() if vid not in arc["members"]]
            scored.append((arc, member_fms, proximities(target["fm"], member_fms, others)))
    if not scored:
        raise ScriptError("no_archetype", "아키타입 구성 영상을 찾을 수 없습니다. /yt-typify를 다시 실행하세요.")
    arc, member_fms, axes = min(scored, key=lambda s: s[2]["structure"]["distance"])

    intentional = _intentional(target["name"])
    all_gaps = gaps(target["fm"], arc, member_fms)
    return {
        "category": category,
        "target": {"name": target["name"], "path": target["rel"], "title": target["fm"].get("title"),
                   "kind": "video" if target["fm"].get("role") == "mine" else target["fm"].get("status", "draft")},
        "preliminary": typify["status"] != "formal",
        "nearest": {"id": arc["id"], "name": names.get(arc["id"]), "skeleton": arc["skeleton"],
                    "mean_ratio": arc["mean_ratio"], "pacing": arc["pacing"]},
        "axes": axes,
        "others": [{"id": a["id"], "name": names.get(a["id"]), "structure": p["structure"]["proximity"]}
                   for a, _, p in scored if a["id"] != arc["id"]],
        "target_sequence": " → ".join(features.sequence(target["fm"])),
        "gaps": [g for g in all_gaps if g["key"] not in intentional],
        "intentional": intentional,
    }


# --- save · intentional ------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "-" if x is None else f"{round(x * 100)}%"


def _memo(key: str) -> str:
    if not ctx.here().store.exists(key):
        return ""
    body = refstore.read(key)[1].replace("\r\n", "\n")
    idx = body.find("## 내 메모")
    return body[idx + len("## 내 메모"):].lstrip("\n") if idx >= 0 else ""


def validate_notes(result: dict, notes: Any) -> dict:
    """notes를 검증하고 정리해 돌려준다(CLI save·UI 공통). 요약은 한 줄로, 행동은 공백을 뺀다.

    result["gaps"]의 모든 key에 설명이 있어야 하며, 사라진 갭의 설명은 버린다."""
    bad_shape = ScriptError("invalid_notes", "notes 형식이 맞지 않습니다: summary는 문자열, gaps는 객체, actions는 문자열 목록이어야 합니다")
    if not isinstance(notes, dict):
        raise bad_shape
    summary, gap_notes, raw_actions = notes.get("summary"), notes.get("gaps"), notes.get("actions")
    gap_notes = {} if gap_notes is None else gap_notes
    raw_actions = [] if raw_actions is None else raw_actions
    summary_ok = summary is None or isinstance(summary, str)
    gaps_ok = isinstance(gap_notes, dict) and all(isinstance(v, str) for v in gap_notes.values())
    actions_ok = isinstance(raw_actions, list) and all(isinstance(a, str) for a in raw_actions)
    if not (summary_ok and gaps_ok and actions_ok):
        raise bad_shape
    clean_summary = " ".join((summary or "").split())
    missing = [g["key"] for g in result["gaps"] if not gap_notes.get(g["key"], "").strip()]
    actions = [a.strip() for a in raw_actions if a.strip()]
    if missing or not clean_summary or not 1 <= len(actions) <= 3:
        raise ScriptError("invalid_notes", "요약, 모든 갭의 설명, 다음 행동 1~3개가 필요합니다"
                          + (f" (빠진 갭: {', '.join(missing)})" if missing else ""))
    return {"summary": clean_summary, "gaps": {g["key"]: gap_notes[g["key"]].strip() for g in result["gaps"]},
            "actions": actions}


def save(category: str, target_path: str | Path, notes_path: Path) -> dict:
    result = compare(category, target_path)
    try:
        notes = json.loads(Path(notes_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("invalid_notes", f"notes JSON을 읽을 수 없습니다: {e}") from e
    notes = validate_notes(result, notes)
    gap_notes, actions = notes["gaps"], notes["actions"]

    store = ctx.here().store
    key = _diag_key(result["target"]["name"])
    old = refstore.read(key)[0] if store.exists(key) else {}
    prox = {axis: v["proximity"] for axis, v in result["axes"].items()}
    history = list(old.get("history") or []) + [{"date": date.today().isoformat(),
                                                 "nearest": result["nearest"]["id"], **prox}]
    fm = {
        "target": result["target"]["path"], "category": category, "date": date.today().isoformat(),
        "nearest": {"id": result["nearest"]["id"], "name": result["nearest"]["name"]},
        "proximity": prox, "gaps": [g["key"] for g in result["gaps"]],
        "intentional": result["intentional"], "preliminary": result["preliminary"],
        "history": history, "schema_version": 1,
    }
    near = result["nearest"]
    lines = [f"# 진단: {result['target']['title'] or result['target']['name']}", "",
             f"_{date.today().isoformat()} · 가장 가까운 아키타입 {near['id']} {near['name']}"
             + (" · 예비 진단(ref 10개 미만)" if result["preliminary"] else "") + "_", "",
             "## 요약", notes["summary"], "",
             "## 축별 근접도", f"내 골격: {result['target_sequence']} / 아키타입 골격: {near['skeleton']}", "",
             "| 축 | 근접도 |", "|---|---|",
             *[f"| {axis} | {_pct(v['proximity'])}{' (' + v['note'] + ')' if v.get('note') else ''} |"
               for axis, v in result["axes"].items()], "",
             "## 갭"]
    lines += [f"- **{g['key']}** (나 {g['target']} / 아키타입 {g['archetype']}): {gap_notes[g['key']]}"
              for g in result["gaps"]] or ["- 없음"]
    lines += ["", "## 다음 행동", *[f"{i}. {a}" for i, a in enumerate(actions, 1)], ""]
    if result["intentional"]:
        lines += ["## 의도한 차이", *[f"- {k}" for k in result["intentional"]], ""]
    lines += ["## 내 메모", ""]
    store.write(key, refstore.join_frontmatter(fm, "\n".join(lines) + _memo(key)).encode("utf-8"))
    Path(notes_path).unlink(missing_ok=True)
    return {"status": "saved", "path": key,
            "nearest": fm["nearest"], "proximity": prox, "gaps": fm["gaps"]}


def intentional(category: str, name: str, key: str, off: bool = False) -> dict:
    refstore.require_category(category)
    doc_key = _diag_key(name)
    if not ctx.here().store.exists(doc_key):
        raise ScriptError("diagnosis_not_found", f"진단 기록이 없습니다: mine/{name}.md (먼저 진단)")
    marked = list(refstore.read(doc_key)[0].get("intentional") or [])
    if off:
        marked = [k for k in marked if k != key]
    elif key not in marked:
        marked.append(key)
    refstore.update_frontmatter(doc_key, {"intentional": marked})
    return {"name": name, "intentional": marked}


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.similarity")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("category")
    c.add_argument("target")
    s = sub.add_parser("save")
    s.add_argument("category")
    s.add_argument("target")
    s.add_argument("--notes", required=True)
    i = sub.add_parser("intentional")
    i.add_argument("category")
    i.add_argument("name")
    i.add_argument("key")
    i.add_argument("--off", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "compare":
        return compare(args.category, Path(args.target))
    if args.cmd == "save":
        return save(args.category, Path(args.target), ctx.here().scratch.arg_path(args.notes))
    return intentional(args.category, args.name, args.key, args.off)


if __name__ == "__main__":
    run(main)
