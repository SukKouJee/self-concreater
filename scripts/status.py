"""홈 화면용 파이프라인 현황: 카테고리별 진행 단계, 오늘의 미션, 최근 산출물.

사용법 (작업 폴더에서):
  python -m scripts.status              모든 카테고리
  python -m scripts.status <category>   해당 카테고리만

출력: {"initialized", "work_root", "categories": [...], "mission": {...}, "recent": [...]}
단계 판정은 md 프런트매터와 파일 존재 여부만 사용한다(읽기 전용).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import mission, refstore
from scripts.common import ArgParser, ScriptError, run, work_root, workspace_dir

RECENT_LIMIT = 5

_STEPS = [
    ("collect", "레퍼런스 수집", "add"),
    ("typify", "유형화", "typify"),
    ("draft", "기획", "draft"),
    ("conti", "콘티", "conti"),
    ("diagnose", "진단", "diagnose"),
]


def _rel(path: Path) -> str:
    return path.relative_to(work_root()).as_posix()


def _category_of(path: Path) -> Any:
    """프런트매터가 없거나 깨진 md(메모 등)는 어느 카테고리에도 세지 않는다(/api/drafts와 같은 처리)."""
    try:
        return refstore.read_md(path)[0].get("category")
    except ScriptError:
        return None


def _drafts(category: str) -> list[Path]:
    drafts = work_root() / "drafts"
    return [
        p for p in sorted(drafts.glob("*.md"))
        if not p.stem.endswith("-conti") and _category_of(p) == category
    ]


def _next(key: str, ref: int, preliminary: int) -> dict:
    texts = {
        "collect": f"레퍼런스 {preliminary - ref}개 더 모으면 유형화를 할 수 있어요",
        "typify": "레퍼런스가 충분해요. 유형화를 해 보세요",
        "draft": "유형화 결과로 기획안을 만들어 보세요",
        "conti": "기획안을 콘티로 바꿔 보세요",
        "diagnose": "내 영상이나 기획안을 진단해 보세요",
    }
    menu = next(m for k, _, m in _STEPS if k == key)
    return {"text": texts[key], "menu": menu}


def _category_status(entry: dict) -> dict:
    category, ref, mine = entry["category"], entry["ref"], entry["mine"]
    preliminary, _ = refstore._stage_limits()
    drafts = _drafts(category)
    conti_count = sum(1 for p in drafts if p.with_name(f"{p.stem}-conti.md").exists())
    mine_dir = work_root() / "mine"
    diagnosed = sum(1 for p in sorted(mine_dir.glob("*.md")) if _category_of(p) == category)
    typified = workspace_dir("typify", f"{category}.json").exists()

    done_detail = {
        "collect": (ref >= preliminary, f"ref {ref}개 / mine {mine}개"),
        "typify": (typified, "유형화 완료" if typified else "아직 없음"),
        "draft": (len(drafts) >= 1, f"기획안 {len(drafts)}개"),
        "conti": (conti_count >= 1, f"콘티 {conti_count}개"),
        "diagnose": (diagnosed >= 1, f"내 영상 {diagnosed}개"),
    }
    steps = [{"key": k, "label": label, "done": done_detail[k][0], "detail": done_detail[k][1]}
             for k, label, _ in _STEPS]
    current = next((s["key"] for s in steps if not s["done"]), None)
    return {
        "category": category, "ref": ref, "mine": mine, "stage": entry["stage"],
        "stage_text": refstore.stage_text(ref, mine),
        "steps": steps, "current": current,
        "next": _next(current, ref, preliminary) if current else None,
    }


def _mission_card() -> dict:
    day = mission._today(None)
    path = mission._mission_path(day)
    if not path.exists():
        return {"date": day, "exists": False, "status": None, "kind": None, "path": None}
    try:
        fm, _ = refstore.read_md(path)
    except ScriptError:
        fm = {}                           # 사용자가 고치는 중인 미션 md가 홈 화면을 막지 않게 한다
    return {"date": day, "exists": True, "status": fm.get("status"), "kind": fm.get("kind"), "path": _rel(path)}


def _recent() -> list[dict]:
    root = work_root()
    found: list[tuple[Path, str]] = []
    found += [(p, "draft") for p in (root / "drafts").glob("*.md") if not p.stem.endswith("-conti")]
    found += [(p, "conti") for p in (root / "drafts").glob("*-conti.html")]
    found += [(p, "report") for p in (root / "reports").glob("*.html")]
    items = [{"path": _rel(p), "kind": kind, "mtime": p.stat().st_mtime} for p, kind in found]
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items[:RECENT_LIMIT]


def overview(category: str | None = None) -> dict:
    categories = [
        _category_status(c) for c in refstore.list_categories()
        if category is None or c["category"] == category
    ]
    if category is not None and not categories and (work_root() / "refs").is_dir():
        refstore.existing_category_dir(category)  # 없는 카테고리는 category_not_found
    return {
        "initialized": (work_root() / "refs").is_dir(),
        "work_root": str(work_root()),
        "categories": categories,
        "mission": _mission_card(),
        "recent": _recent(),
    }


def main(argv: list[str]) -> dict:
    parser = ArgParser(prog="python -m scripts.status")
    parser.add_argument("category", nargs="?")
    args = parser.parse_args(argv)
    return overview(args.category)


if __name__ == "__main__":
    run(main)
