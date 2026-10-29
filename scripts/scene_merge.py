"""반복 컷을 장면 하나로 묶는 순수 함수 (파일 IO·락 없음)."""
from __future__ import annotations

import copy
from typing import Any

from scripts.conti import _norm


def groups(cuts: list[dict[str, Any]]) -> list[list[int]]:
    """섹션별로 정규화한 action이 같은 컷 번호(n) 묶음. 첫 등장 순서, 단독 컷도 한 묶음."""
    order: list[tuple[Any, str]] = []
    found: dict[tuple[Any, str], list[int]] = {}
    for cut in cuts:
        key = (cut["section"], _norm(cut.get("action", "")))
        if key not in found:
            found[key] = []
            order.append(key)
        found[key].append(cut["n"])
    return [found[key] for key in order]


def has_repeats(cuts: list[dict[str, Any]]) -> bool:
    return any(len(g) > 1 for g in groups(cuts))


def _join_unique(members: list[dict[str, Any]], key: str) -> str:
    seen: list[str] = []
    for member in members:
        value = member.get(key) or ""
        if value and value not in seen:
            seen.append(value)
    return " / ".join(seen)


def merge_fields(members: list[dict[str, Any]]) -> dict[str, Any]:
    """묶음의 대표 컷. line은 비워 두고(호출자가 채움) merged_ids에 모든 id를 담는다."""
    first = members[0]
    merged: dict[str, Any] = {
        "id": first["id"], "section": first["section"], "size": first.get("size"),
        "angle": first.get("angle"), "action": first.get("action"),
        "sec": sum(float(m.get("sec") or 0) for m in members),
        "guide": _join_unique(members, "guide"), "caption": _join_unique(members, "caption"),
        "sfx": _join_unique(members, "sfx"), "line": "",
        "merged_ids": [m["id"] for m in members],
    }
    for member in members:
        if member.get("ref_frame"):
            merged["ref_frame"] = member["ref_frame"]
            break
    return merged


def joined_lines(cuts: list[dict[str, Any]], section: int) -> str:
    return " ".join(c["line"] for c in cuts if c["section"] == section and c.get("line"))


def transfer_footage(state: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    """사라질 id의 사용자 판정만 남는 id로 옮기고 AI 판정은 양쪽 모두 지운다. 새 dict를 돌려준다."""
    out = copy.deepcopy(state)
    cuts: dict[str, Any] = out["cuts"]
    moved: dict[str, Any] = {}
    for gone, keep in id_map.items():
        entry = cuts.pop(gone, None)
        if entry and entry.get("user") and keep not in moved:
            moved[keep] = entry
    for keep in set(id_map.values()):
        if not (cuts.get(keep) or {}).get("user"):
            cuts.pop(keep, None)
            if keep in moved:
                cuts[keep] = moved[keep]
    return out
