"""홈 화면용 파이프라인 현황: 카테고리별 진행 단계, 오늘의 미션, 최근 산출물.

사용법 (작업 폴더에서):
  python -m scripts.status              모든 카테고리
  python -m scripts.status <category>   해당 카테고리만

출력: {"initialized", "work_root", "categories": [...], "mission": {...}, "recent": [...]}
단계 판정은 md 프런트매터와 파일 존재 여부만 사용한다(읽기 전용).
"""
from __future__ import annotations

import datetime
import math
import posixpath
import re
from typing import Any

from scripts import ctx, keys, mission, refstore
from scripts.common import ArgParser, ScriptError, run

RECENT_LIMIT = 5
# 메모장이 CP949로 저장한 md, 목록을 만든 뒤 지워진 md: 건너뛰고 나머지를 보여 준다
_UNREADABLE = (ScriptError, OSError, UnicodeDecodeError)

_STEPS = [
    ("collect", "레퍼런스 수집", "add"),
    ("typify", "유형화", "typify"),
    ("draft", "기획", "draft"),
    ("conti", "콘티", "conti"),
    ("diagnose", "진단", "diagnose"),
]

# (키, 이름, 선택 여부): 영상(기획안) 하나의 단계. 순서가 화면 순서다.
VIDEO_STEPS: list[tuple[str, str, bool]] = [
    ("draft", "기획안", False),
    ("polish", "다듬기", True),
    ("conti", "콘티", False),
    ("filmed", "촬영함", False),
    ("captions", "자막 영상", True),
    ("diagnose", "진단", False),
]
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PACKAGING_RE = re.compile(r"^## 패키징\s*$", re.MULTILINE)


def _iso_date(text: str) -> str | None:
    """`YYYY-MM-DD` 모양이면서 실제 달력에 있는 날짜만 통과시킨다."""
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def filmed_date(value: Any) -> str | None:
    """`datetime.date`나 `YYYY-MM-DD` 문자열(실제 달력 날짜)만 "YYYY-MM-DD"로, 그 밖은 None."""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        return _iso_date(value)
    return None


def _norm_target(value: Any) -> str | None:
    """mine target을 작업 폴더 기준 경로 모양으로 맞춘다: 역슬래시는 슬래시로, 앞의 ./ 는 지운다."""
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _mine_records(category: str) -> tuple[int, set[str]]:
    """카테고리의 mine md를 한 번만 읽어 (진단 기록 수, 정규화한 target 집합)을 낸다. 깨진 md는 건너뛴다."""
    count, targets = 0, set()
    for meta in ctx.here().store.list(keys.MINE, "*.md"):
        try:
            fm = refstore.read(meta.key)[0]
        except _UNREADABLE:
            continue
        if fm.get("category") != category:
            continue
        count += 1
        target = _norm_target(fm.get("target"))
        if target:
            targets.add(target)
    return count, targets


def _video_title(fm: dict, stem: str) -> str:
    title = fm.get("title")
    if isinstance(title, (str, int, float)) and not isinstance(title, bool) and str(title).strip():
        return str(title).strip()
    return stem


def _video_date(fm: dict, name: str) -> str | None:
    value = fm.get("date")
    found = filmed_date(value) if not isinstance(value, str) else _iso_date(value[:10])
    return found or _iso_date(name[:10])


def _target_sec(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _video(rel: str, fm: dict, body: str, version: str, diagnosed_targets: set[str]) -> dict:
    c = ctx.here()
    stem = keys.stem(rel)
    conti_key = keys.conti_md(rel)
    filmed_at = filmed_date(fm.get("filmed_at"))
    done = {
        "draft": True,
        "polish": _PACKAGING_RE.search(body) is not None,
        "conti": c.store.exists(conti_key),
        "filmed": filmed_at is not None,
        "captions": c.objects.exists(keys.captions_mp4(conti_key)),
        "diagnose": rel in diagnosed_targets,
    }
    steps = [{"key": k, "done": done[k], "optional": optional} for k, _, optional in VIDEO_STEPS]
    return {
        "path": rel, "title": _video_title(fm, stem), "date": _video_date(fm, posixpath.basename(rel)),
        "target_sec": _target_sec(fm.get("target_sec")), "steps": steps,
        "next": next((s["key"] for s in steps if not s["done"] and not s["optional"]), None),
        "progress": sum(1 for s in steps if s["done"] and not s["optional"]),
        "filmed_at": filmed_at, "hash": version,
    }


def _videos(category: str, _targets: set[str] | None = None) -> list[dict]:
    """카테고리의 기획안 하나하나를 영상으로 본다. 미완 영상(진행 큰 순 → 날짜 최근 → 파일 이름 역순) 뒤에 완료 영상.

    파일 수정 시각은 쓰지 않는다: 촬영 체크·다듬기 저장이 수정 시각을 바꿔 순서가 튄다.
    """
    targets = _mine_records(category)[1] if _targets is None else _targets
    videos: list[dict] = []
    store = ctx.here().store
    for meta in store.list(keys.DRAFTS, "*.md"):
        if keys.stem(meta.key).endswith(keys.CONTI_SUFFIX):
            continue
        try:
            doc = store.read(meta.key)               # the file may be gone or replaced since the listing
            fm, body = doc.parsed
            if fm.get("category") != category:
                continue
            video = _video(meta.key, fm, body, doc.version, targets)
        except _UNREADABLE:
            continue
        videos.append(video)
    videos.sort(key=lambda v: v["path"], reverse=True)
    videos.sort(key=lambda v: v["date"] or "", reverse=True)
    videos.sort(key=lambda v: v["progress"], reverse=True)
    videos.sort(key=lambda v: v["next"] is None)
    return videos


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


def _typified_at(category: str) -> Any:
    """저장(apply)된 유형화만 완료로 본다: index.md 프런트매터의 typified_at."""
    try:
        return refstore.read(refstore.require_category(category) + "index.md")[0].get("typified_at")
    except ScriptError:
        return None


def _next_action(videos: list[dict], current: str | None, next_step: dict | None) -> dict:
    """맨 위 카드용: 미완 영상의 다음 단계 > 카테고리 단계 > 새 기획안."""
    pending = next((v for v in videos if v["next"] is not None), None)
    if pending is not None:
        return {"kind": "video", "path": pending["path"], "step": pending["next"], "title": pending["title"]}
    if current is not None and next_step is not None:
        return {"kind": "category", **next_step}
    return {"kind": "new_draft"}


def _category_status(entry: dict) -> dict:
    category, ref, mine = entry["category"], entry["ref"], entry["mine"]
    preliminary, _ = refstore._stage_limits()
    diagnosed, targets = _mine_records(category)
    videos = _videos(category, targets)
    conti_count = sum(1 for v in videos if next(s["done"] for s in v["steps"] if s["key"] == "conti"))
    typified = _typified_at(category) is not None

    done_detail = {
        "collect": (ref >= preliminary, f"ref {ref}개 / mine {mine}개", ref),
        "typify": (typified, "유형화 완료" if typified else "아직 없음", None),
        "draft": (len(videos) >= 1, f"기획안 {len(videos)}개", len(videos)),
        "conti": (conti_count >= 1, f"콘티 {conti_count}개", conti_count),
        "diagnose": (diagnosed >= 1, f"내 영상 {diagnosed}개", diagnosed),
    }
    steps = [{"key": k, "label": label, "done": done_detail[k][0], "detail": done_detail[k][1],
              "count": done_detail[k][2]} for k, label, _ in _STEPS]
    current = next((s["key"] for s in steps if not s["done"]), None)
    next_step = _next(current, ref, preliminary) if current else None
    return {
        "category": category, "ref": ref, "mine": mine, "stage": entry["stage"],
        "stage_text": refstore.stage_text(ref, mine),
        "steps": steps, "current": current, "next": next_step,
        "videos": videos, "next_action": _next_action(videos, current, next_step),
    }


def _mission_card() -> dict:
    day = mission._today(None)
    key = keys.mission_md(day)
    if not ctx.here().store.exists(key):
        return {"date": day, "exists": False, "status": None, "kind": None, "path": None}
    try:
        fm, _ = refstore.read(key)
    except ScriptError:
        fm = {}                           # 사용자가 고치는 중인 미션 md가 홈 화면을 막지 않게 한다
    return {"date": day, "exists": True, "status": fm.get("status"), "kind": fm.get("kind"), "path": key}


def _recent() -> list[dict]:
    c = ctx.here()
    found = [(m, "draft") for m in c.store.list(keys.DRAFTS, "*.md") if not keys.stem(m.key).endswith(keys.CONTI_SUFFIX)]
    found += [(m, "conti") for m in c.objects.list(keys.DRAFTS, keys.CONTI_HTML_GLOB)]
    found += [(m, "report") for m in c.objects.list(keys.REPORTS, "*.html")]
    items = [{"path": m.key, "kind": kind, "mtime": m.mtime} for m, kind in found]
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items[:RECENT_LIMIT]


def overview(category: str | None = None) -> dict:
    categories = [
        _category_status(c) for c in refstore.list_categories()
        if category is None or c["category"] == category
    ]
    store = ctx.here().store
    if category is not None and not categories and store.folder_exists(keys.REFS):
        refstore.require_category(category)  # 없는 카테고리는 category_not_found
    return {
        "initialized": store.folder_exists(keys.REFS),
        "work_root": store.label(),
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
