"""영상 md 프런트매터 → 축별 수치 피처. 계산 입력은 프런트매터뿐이다(사용자 수정이 곧 교정).

축 (설계서 5.2)
- structure: 라벨별 시간 비중, body sub 비중, 섹션 수, 길이
- hook:      훅 유형 one-hot + 훅 길이
- narrative: 서사 유형 one-hot
- title:     제목 패턴(제목에서 다시 계산) + 길이
- pacing:    평균 컷 길이, 훅·본론 컷 길이 (로그)
- shots:     Phase 10(Vision)에서 추가
"""
from __future__ import annotations

import math
from pathlib import Path

from scripts import refstore
from scripts.videomd import BODY_SUBS, HOOK_TYPES, LABELS, NARRATIVE_TYPES, title_patterns

AXES = ["structure", "hook", "narrative", "title", "pacing"]
TITLE_PATTERNS = ["number", "question", "bracket", "exclaim"]
STRUCTURE_FIELDS = [f"share:{l}" for l in LABELS] + [f"sub:{s}" for s in BODY_SUBS] + ["sections", "duration"]


def record(path: Path) -> dict:
    fm, _ = refstore.read_md(path)
    return {"id": str(fm.get("id") or path.stem), "fm": fm, "path": path}


def load_videos(category: str, role: str = "ref") -> list[dict]:
    """카테고리의 영상 중 role이 일치하는 것만 (유형화는 ref만. mine은 섞지 않는다)."""
    videos = refstore.existing_category_dir(category) / "videos"
    records = [record(p) for p in sorted(videos.glob("*.md"))]
    return sorted((r for r in records if r["fm"].get("role") == role), key=lambda r: r["id"])


def duration(fm: dict) -> float:
    sections = fm.get("sections") or []
    return float(fm.get("duration_sec") or (sections[-1]["end"] if sections else 0) or 1)


def sequence(fm: dict) -> list[str]:
    """섹션 라벨 순서. 연속 중복은 합친다. body는 sub를 붙인다."""
    seq: list[str] = []
    for s in fm.get("sections") or []:
        name = f"{s['label']}/{s['sub']}" if s.get("sub") else s["label"]
        if not seq or seq[-1] != name:
            seq.append(name)
    return seq


def structure_vec(fm: dict) -> list[float]:
    dur = duration(fm)
    sections = fm.get("sections") or []
    length = lambda pred: sum(s["end"] - s["start"] for s in sections if pred(s)) / dur
    return (
        [length(lambda s, l=l: s["label"] == l) for l in LABELS]
        + [length(lambda s, x=x: s["label"] == "body" and s.get("sub") == x) for x in BODY_SUBS]
        + [min(len(sections), 10) / 10, math.log1p(dur) / math.log1p(3600)]
    )


def hook_seconds(fm: dict) -> float:
    hook = next((s for s in fm.get("sections") or [] if s["label"] == "hook"), None)
    return float(hook["end"] - hook["start"]) if hook else 0.0


def hook_vec(fm: dict) -> list[float]:
    return [1.0 if fm.get("hook_type") == h else 0.0 for h in HOOK_TYPES] + [min(hook_seconds(fm) / 10, 1.0)]


def narrative_vec(fm: dict) -> list[float]:
    return [1.0 if fm.get("narrative_type") == n else 0.0 for n in NARRATIVE_TYPES]


def title_vec(fm: dict) -> list[float]:
    title = str(fm.get("title") or "")
    found = title_patterns(title)
    return [1.0 if p in found else 0.0 for p in TITLE_PATTERNS] + [min(len(title) / 60, 1.0)]


def pacing_vec(fm: dict) -> list[float] | None:
    pacing = fm.get("pacing") or {}
    avg = pacing.get("avg_cut_sec")
    if not avg:
        return None
    by = pacing.get("by_section") or {}
    return [math.log(avg), math.log(by.get("hook") or avg), math.log(by.get("body") or avg)]


VECTORS = {
    "structure": structure_vec, "hook": hook_vec, "narrative": narrative_vec,
    "title": title_vec, "pacing": pacing_vec,
}


def axis_matrix(records: list[dict], axis: str) -> tuple[list[str], list[list[float]]]:
    """값이 없는 영상(예: 컷 통계 없음)은 그 축에서만 빠진다."""
    ids, rows = [], []
    for r in records:
        vec = VECTORS[axis](r["fm"])
        if vec is not None:
            ids.append(r["id"])
            rows.append(vec)
    return ids, rows
