"""유형화: ref 영상만으로 축별 군집·판정·아키타입·이상치를 만들고, 확정된 내용을 index.md에 쓴다.

사용법 (작업 폴더에서):
  python -m scripts.cluster run <category>                      → workspace/typify/<cat>.json
  python -m scripts.cluster apply <category> --notes <json>     → index.md 판정·아키타입·이상치

- mine은 스크립트에서 항상 뺀다(끌 수 없음)
- 단계: ref 5개 미만 too_few / 5~9 preliminary / 10 이상 formal
- 수치 축(structure, pacing): 계층 군집(평균 연결), k는 실루엣 최대. 너무 촘촘히 모여 있으면 한 유형
- 범주 축(hook, narrative, title): 값 그대로 묶는다. 실루엣은 그 묶음의 one-hot 기준
- 판정: thresholds.json의 실루엣·최대 군집 점유율 → typable | weak | untypable
- 아키타입: structure 군집 중 2개 이상인 것. 이전 아키타입과 구성이 절반 이상 겹치면 이름(prev_name)을 이어받도록 제안

notes JSON (apply 입력, Claude가 작성·사용자가 이름 확정)
  {"axes": {"structure": "근거 문장", ...}, "archetypes": [{"id": "A1", "name": "이름"}]}
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

from scripts import features, refstore
from scripts.common import TEMPLATES_DIR, ArgParser, ScriptError, run as run_main, work_root, workspace_dir

NUMERIC_AXES = ["structure", "pacing", "shots"]
CATEGORICAL_AXES = ["hook", "narrative", "title"]
MAX_K = 5
PREV_NAME_OVERLAP = 0.5
CAMERA_KO = {"static": "고정", "pan": "패닝", "zoom": "줌"}
VERDICT_KO = {"typable": "유형 있음", "weak": "약한 경향", "untypable": "유형 없음", "insufficient": "데이터 부족"}


def _thresholds() -> dict:
    return json.loads((TEMPLATES_DIR / "thresholds.json").read_text("utf-8"))


def verdict(silhouette: float, share: float, t: dict) -> str:
    if silhouette >= t["typable"]["silhouette"] and share >= t["typable"]["max_cluster_share"]:
        return "typable"
    if silhouette >= t["weak"]["silhouette"] and share >= t["weak"]["max_cluster_share"]:
        return "weak"
    return "untypable"


# --- 군집 -------------------------------------------------------------------------

def _mean_pairwise(X: np.ndarray) -> float:
    pairs = list(combinations(range(len(X)), 2))
    return float(np.mean([np.linalg.norm(X[i] - X[j]) for i, j in pairs])) if pairs else 0.0


def numeric_labels(X: np.ndarray, single_spread: float) -> tuple[np.ndarray, float]:
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import silhouette_score

    n = len(X)
    if n < 3 or _mean_pairwise(X) < single_spread:
        return np.zeros(n, dtype=int), 1.0
    best: tuple[float, np.ndarray] | None = None
    for k in range(2, min(MAX_K, n - 1) + 1):
        labels = AgglomerativeClustering(n_clusters=k, linkage="average").fit_predict(X)
        score = float(silhouette_score(X, labels))
        if best is None or score > best[0] + 1e-9:   # 동점이면 작은 k
            best = (score, labels)
    return best[1], best[0]


def categorical_labels(keys: list[str]) -> tuple[np.ndarray, float]:
    from sklearn.metrics import silhouette_score

    order = sorted(set(keys))
    labels = np.array([order.index(k) for k in keys])
    k, n = len(order), len(keys)
    if k == 1:
        return labels, 1.0
    if k == n:
        return labels, 0.0
    onehot = np.eye(k)[labels]
    return labels, float(silhouette_score(onehot, labels))


def _category_key(axis: str, fm: dict) -> str:
    if axis == "hook":
        return str(fm.get("hook_type"))
    if axis == "narrative":
        return str(fm.get("narrative_type"))
    found = features.title_patterns(str(fm.get("title") or ""))
    return "+".join(found) or "(패턴 없음)"


# --- 요약 -------------------------------------------------------------------------

def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return round(float(np.mean(values)), 2) if values else None


def _skeleton(fms: list[dict]) -> str:
    seqs = Counter(" → ".join(features.sequence(fm)) for fm in fms)
    return seqs.most_common(1)[0][0]


def _mean_ratio(fms: list[dict]) -> dict:
    ratios: dict[str, float] = {}
    for label in features.LABELS:
        idx = features.STRUCTURE_FIELDS.index(f"share:{label}")
        mean = float(np.mean([features.structure_vec(fm)[idx] for fm in fms]))
        if mean > 0:
            ratios[label] = round(mean, 2)
    return ratios


def _pacing_summary(fms: list[dict]) -> dict:
    pacings = [fm.get("pacing") or {} for fm in fms]
    return {
        "avg_cut_sec": _mean([p.get("avg_cut_sec") for p in pacings]),
        "by_section": {
            label: _mean([(p.get("by_section") or {}).get(label) for p in pacings])
            for label in ("hook", "body")
        },
    }


def shots_summary(fms: list[dict]) -> dict | None:
    shots = [fm["shots"] for fm in fms if fm.get("shots")]
    if not shots:
        return None
    sizes = {s: float(np.mean([(x.get("size") or {}).get(s, 0) for x in shots])) for s in features.SHOT_SIZES}
    top = sorted(((s, round(v, 2)) for s, v in sizes.items() if v > 0), key=lambda kv: -kv[1])[:3]
    return {"top_sizes": [list(kv) for kv in top],
            **{f: _mean([x.get(f) for x in shots]) for f in features.SHOT_FLAGS}}


def summarize(axis: str, fms: list[dict]) -> dict:
    if axis == "structure":
        return {"skeleton": _skeleton(fms), "mean_ratio": _mean_ratio(fms),
                "mean_duration": _mean([features.duration(fm) for fm in fms])}
    if axis == "pacing":
        return _pacing_summary(fms)
    if axis == "shots":
        return shots_summary(fms)
    if axis == "hook":
        return {"hook_type": dict(Counter(fm.get("hook_type") for fm in fms)),
                "hook_sec": _mean([features.hook_seconds(fm) for fm in fms])}
    if axis == "narrative":
        return {"narrative_type": dict(Counter(fm.get("narrative_type") for fm in fms))}
    return {"pattern": _category_key("title", fms[0]),
            "examples": [str(fm.get("title")) for fm in fms[:2]]}


def analyze_axis(axis: str, records: list[dict], t: dict) -> dict:
    if axis in NUMERIC_AXES:
        ids, rows = features.axis_matrix(records, axis)
    else:
        ids = [r["id"] for r in records]
    n = len(ids)
    if n < t["stages"]["preliminary"]:
        return {"verdict": "insufficient", "n": n, "k": 0, "silhouette": None,
                "max_share": None, "clusters": [], "singletons": []}

    by_id = {r["id"]: r["fm"] for r in records}
    if axis in NUMERIC_AXES:
        labels, sil = numeric_labels(np.array(rows), t["single_type_spread"][axis])
    else:
        labels, sil = categorical_labels([_category_key(axis, by_id[i]) for i in ids])

    groups: dict[int, list[str]] = {}
    for vid, label in zip(ids, labels):
        groups.setdefault(int(label), []).append(vid)
    ordered = sorted(groups.values(), key=lambda m: (-len(m), m[0]))
    clusters = [
        {"id": f"{axis}-{i + 1}", "size": len(m), "members": m, "summary": summarize(axis, [by_id[v] for v in m])}
        for i, m in enumerate(ordered)
    ]
    share = round(len(ordered[0]) / n, 2)
    return {
        "verdict": verdict(sil, share, t),
        "n": n,
        "k": len(ordered),
        "silhouette": round(sil, 2),
        "max_share": share,
        "clusters": clusters,
        "singletons": [m[0] for m in ordered if len(m) == 1] if len(ordered) > 1 else [],
    }


# --- 아키타입 ---------------------------------------------------------------------

def _previous_archetypes(category: str) -> list[dict]:
    fm, _ = refstore.read_md(refstore.existing_category_dir(category) / "index.md")
    return fm.get("archetypes") or []


def _prev_name(members: list[str], previous: list[dict]) -> str | None:
    best, best_score = None, 0.0
    for prev in previous:
        a, b = set(members), set(prev.get("members") or [])
        score = len(a & b) / len(a | b) if a | b else 0
        if score > best_score:
            best, best_score = prev.get("name"), score
    return best if best_score >= PREV_NAME_OVERLAP else None


def archetypes(structure: dict, records: list[dict], previous: list[dict]) -> list[dict]:
    by_id = {r["id"]: r["fm"] for r in records}
    result = []
    for cluster in structure["clusters"]:
        if cluster["size"] < 2:
            continue
        members = cluster["members"]
        fms = [by_id[v] for v in members]
        vecs = np.array([features.structure_vec(fm) for fm in fms])
        rep = members[int(np.argmin(np.linalg.norm(vecs - vecs.mean(axis=0), axis=1)))]
        cameras = [fm["camera"] for fm in fms if fm.get("camera")]
        result.append({
            "id": f"A{len(result) + 1}",
            "size": len(members),
            "members": members,
            "skeleton": cluster["summary"]["skeleton"],
            "mean_ratio": cluster["summary"]["mean_ratio"],
            "mean_duration": cluster["summary"]["mean_duration"],
            "representative": {"id": rep, "title": by_id[rep].get("title")},
            "hook_type": dict(Counter(fm.get("hook_type") for fm in fms)),
            "narrative_type": dict(Counter(fm.get("narrative_type") for fm in fms)),
            "pacing": _pacing_summary(fms),
            "camera": {k: _mean([c.get(k) for c in cameras]) for k in ("static", "pan", "zoom")} if cameras else None,
            "shots": shots_summary(fms),
            "prev_name": _prev_name(members, previous),
        })
    return result


# --- run --------------------------------------------------------------------------

def result_path(category: str) -> Path:
    return workspace_dir("typify", f"{category}.json")


def run(category: str) -> dict:
    t = _thresholds()
    records = features.load_videos(category)       # ref만
    n = len(records)
    if n < t["stages"]["preliminary"]:
        return {"category": category, "status": "too_few", "ref": n, "need": t["stages"]["preliminary"] - n}

    axes = {axis: analyze_axis(axis, records, t) for axis in features.AXES}
    titles = {r["id"]: r["fm"].get("title") for r in records}
    outliers: dict[str, list[str]] = {}
    for axis, res in axes.items():
        # 범주 축에서 혼자인 값은 그 축에 뚜렷한 유형이 있을 때만 '벗어남'이다
        if axis in CATEGORICAL_AXES and res["verdict"] != "typable":
            continue
        for vid in res["singletons"]:
            outliers.setdefault(vid, []).append(axis)

    result = {
        "category": category,
        "status": "formal" if n >= t["stages"]["formal"] else "preliminary",
        "ref": n,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "axes": axes,
        "archetypes": archetypes(axes["structure"], records, _previous_archetypes(category)),
        "outliers": [{"id": v, "title": titles[v], "axes": a} for v, a in sorted(outliers.items())],
    }
    path = result_path(category)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# --- apply ------------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "-" if x is None else f"{round(x * 100)}%"


def _counts(d: dict) -> str:
    return " · ".join(f"{k} {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))


def _link(vid: str) -> str:
    return f"[{vid}](videos/{vid}.md)"


def render_judgement(result: dict, axis_notes: dict) -> str:
    stage = "정식 판정" if result["status"] == "formal" else "예비 관찰 (10개부터 정식 판정)"
    lines = [f"_{date.today().isoformat()} · ref {result['ref']}개 · {stage}_", "",
             "| 축 | 판정 | 근거 |", "|---|---|---|"]
    for axis, res in result["axes"].items():
        if res["verdict"] == "insufficient":
            stat = f"데이터 부족 ({res['n']}개)"
        else:
            stat = f"{VERDICT_KO[res['verdict']]} · 실루엣 {res['silhouette']} · 최대 군집 {_pct(res['max_share'])} · {res['k']}갈래"
        note = str(axis_notes.get(axis, "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {axis} | {res['verdict']} ({stat}) | {note} |")
    return "\n".join(lines)


def render_archetypes(result: dict, names: dict) -> str:
    if not result["archetypes"]:
        return "_구조가 비슷한 영상 묶음이 아직 없습니다._"
    blocks = []
    for a in result["archetypes"]:
        ratio = " · ".join(f"{k} {_pct(v)}" for k, v in a["mean_ratio"].items())
        pacing = a["pacing"]
        by = pacing["by_section"]
        pace = f"평균 컷 {pacing['avg_cut_sec']}초" if pacing["avg_cut_sec"] else "컷 통계 없음"
        parts = [f"{k} {v}" for k, v in by.items() if v is not None]
        if pacing["avg_cut_sec"] and parts:
            pace += f" ({' · '.join(parts)})"
        camera = " · ".join(f"{CAMERA_KO[k]} {_pct(v)}" for k, v in a["camera"].items()) if a["camera"] else "-"
        rep = a["representative"]
        blocks.append("\n".join([
            f"### {a['id']}. {names[a['id']]} ({a['size']}개)",
            f"- 골격: {a['skeleton']}",
            f"- 평균 비율: {ratio} · 평균 길이 {a['mean_duration']}초",
            f"- 훅: {_counts(a['hook_type'])} / 서사: {_counts(a['narrative_type'])}",
            f"- 페이싱: {pace}",
            f"- 촬영 경향: {camera}",
            *([f"- 샷 경향: {_shot_line(a['shots'])}"] if a.get("shots") else []),
            f"- 대표 영상: [{rep['title']}](videos/{rep['id']}.md)",
            f"- 구성: {', '.join(_link(v) for v in a['members'])}",
        ]))
    return "\n\n".join(blocks)


def _shot_line(shots: dict) -> str:
    sizes = " · ".join(f"{s} {_pct(v)}" for s, v in shots["top_sizes"])
    return f"{sizes} / 얼굴 {_pct(shots['face'])} · 손만 {_pct(shots['hands_only'])} · 자막 {_pct(shots['text_overlay'])}"


def render_outliers(result: dict) -> str:
    if not result["outliers"]:
        return "_없음_"
    return "\n".join(
        f"- {_link(o['id'])} {o['title']}: {', '.join(o['axes'])}에서 혼자 동떨어짐"
        for o in result["outliers"]
    )


def apply(category: str, notes_path: Path) -> dict:
    path = result_path(category)
    if not path.exists():
        raise ScriptError("no_typify_result", f"유형화 결과가 없습니다. 먼저 run을 실행하세요: {category}")
    result = json.loads(path.read_text("utf-8"))
    try:
        notes = json.loads(Path(notes_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("invalid_notes", f"notes JSON을 읽을 수 없습니다: {e}") from e

    axis_notes = notes.get("axes") or {}
    names = {a["id"]: str(a.get("name") or "").strip() for a in notes.get("archetypes") or []}
    missing = [a for a, res in result["axes"].items()
               if res["verdict"] != "insufficient" and not str(axis_notes.get(a) or "").strip()]
    missing += [a["id"] for a in result["archetypes"] if not names.get(a["id"])]
    if missing:
        raise ScriptError("invalid_notes", f"근거 문장 또는 아키타입 이름이 빠졌습니다: {', '.join(missing)}")

    index = refstore.existing_category_dir(category) / "index.md"
    text = index.read_bytes().decode("utf-8-sig")
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    fm, body = refstore.split_frontmatter(text)
    body = refstore.replace_section(body, "## 판정", render_judgement(result, axis_notes))
    body = refstore.replace_section(body, "## 아키타입", render_archetypes(result, names))
    body = refstore.replace_section(body, "## 이상치", render_outliers(result))
    fm["typified_at"] = date.today().isoformat()
    fm["typify_status"] = result["status"]
    fm["archetypes"] = [{"id": a["id"], "name": names[a["id"]], "members": a["members"]}
                        for a in result["archetypes"]]
    index.write_bytes(refstore.join_frontmatter(fm, body.replace("\r\n", "\n").replace("\n", nl), nl).encode("utf-8"))
    Path(notes_path).unlink(missing_ok=True)
    return {"status": "applied", "index": index.relative_to(work_root()).as_posix(),
            "archetypes": [{"id": a["id"], "name": names[a["id"]]} for a in result["archetypes"]]}


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.cluster")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("category")
    ap = sub.add_parser("apply")
    ap.add_argument("category")
    ap.add_argument("--notes", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        return run(args.category)
    notes_path = Path(args.notes)
    if not notes_path.is_absolute():
        notes_path = work_root() / notes_path
    return apply(args.category, notes_path)


if __name__ == "__main__":
    run_main(main)
