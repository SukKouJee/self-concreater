"""일일 미션과 주간 회고.

사용법 (작업 폴더에서):
  python -m scripts.mission context [--date YYYY-MM-DD]
  python -m scripts.mission save --mission <json> [--date] [--replace]        → missions/<date>.md
  python -m scripts.mission submit <file> --difficulty easy|right|hard --feedback <text> [--note <text>] [--date]
  python -m scripts.mission skip --reason <text> [--date]
  python -m scripts.mission week [YYYY-Www]
  python -m scripts.mission review-save <YYYY-Www> --review <json>              → reviews/<week>.md
  python -m scripts.mission profile-update --updates <json> [--date]

- 재료: ★모티브(모든 카테고리) → 최근 draft → 진단 갭(intentional 제외) → 미션 이력
- 크기: 프로필 '하루 시간'(15~30분)에서 시작, 최근 3개 중 hard·skip 2개 이상이면 −5분, easy 2개 이상이면 +5분
mission JSON: {"title", "kind", "minutes", "source", "why", "steps": [..], "deliverable"}
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts import motif, refstore
from scripts.common import ArgParser, ScriptError, run, work_root

KINDS = ["관찰", "분석", "기획", "촬영", "편집", "업로드"]
STAGES = ["관찰", "모방", "제작", "변형"]
DIFFICULTIES = ["easy", "right", "hard"]
PROFILE_SECTIONS = ["관심 주제", "포맷", "롤모델", "하루 시간", "피하고 싶은 스타일", "강점", "약점", "성장 단계"]
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES, STEP = 15, 30, 20, 5
HISTORY_DAYS = 7


def _today(value: str | None) -> str:
    return value or Date.today().isoformat()


def _missions_dir() -> Path:
    return work_root() / "missions"


def _mission_path(day: str) -> Path:
    return _missions_dir() / f"{day}.md"


def _load_json(path: Path, code: str) -> Any:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError(code, f"JSON을 읽을 수 없습니다: {e}") from e


# --- 난이도 ------------------------------------------------------------------------

def base_minutes(profile: dict | None) -> int:
    m = re.search(r"\d+", (profile or {}).get("하루 시간") or "")
    return min(MAX_MINUTES, max(MIN_MINUTES, int(m.group()))) if m else DEFAULT_MINUTES


def recommend(recent: list[dict], base: int) -> dict:
    """recent: 끝난 미션(오래된 순). 최근 3개를 본다."""
    last = [m for m in recent if m["status"] in ("submitted", "skipped")][-3:]
    current = last[-1]["minutes"] if last and last[-1].get("minutes") else base
    tough = sum(1 for m in last if m["status"] == "skipped" or m.get("difficulty") == "hard")
    easy = sum(1 for m in last if m.get("difficulty") == "easy")
    if tough >= 2:
        return {"minutes": max(MIN_MINUTES, current - STEP), "adjust": "easier"}
    if easy >= 2:
        return {"minutes": min(MAX_MINUTES, current + STEP), "adjust": "harder"}
    return {"minutes": current, "adjust": "same"}


# --- context ----------------------------------------------------------------------

def _mission_record(path: Path) -> dict:
    fm, body = refstore.read_md(path)
    title = re.search(r"^### (.+?) \(\d+분", body, re.M)
    return {"date": str(fm.get("date") or path.stem), "title": title.group(1) if title else None,
            "kind": fm.get("kind"), "status": fm.get("status"), "difficulty": fm.get("difficulty"),
            "minutes": fm.get("minutes"), "skip_reason": fm.get("skip_reason"), "source": fm.get("source")}


def _missions_between(start: str, end: str) -> list[dict]:
    folder = _missions_dir()
    if not folder.is_dir():
        return []
    records = []
    for p in sorted(folder.glob("*.md")):
        if not start <= p.stem <= end:
            continue
        try:
            records.append(_mission_record(p))
        except ScriptError as e:
            if e.code != "bad_frontmatter":
                raise
            # 사용자가 고치는 중인 기록 하나가 전체 조회를 막지 않도록 건너뛴다
    return records


def _starred() -> list[dict]:
    found = []
    for cat_dir in sorted((work_root() / "refs").glob("ref_*")):
        _, body = refstore.read_md(cat_dir / "index.md")
        for card in motif.parse_cards(body):
            if card["starred"]:
                found.append({"category": cat_dir.name.removeprefix("ref_"), "id": card["id"],
                              "type": card["type"], "title": card["title"]})
    return found


def _drafts(limit: int = 3) -> list[dict]:
    folder = work_root() / "drafts"
    paths = sorted(folder.glob("*.md"), reverse=True)[:limit] if folder.is_dir() else []
    out = []
    for p in paths:
        fm, _ = refstore.read_md(p)
        out.append({"path": p.relative_to(work_root()).as_posix(), "title": fm.get("title"),
                    "status": fm.get("status"), "category": fm.get("category")})
    return out


def _diagnosis_gaps() -> list[dict]:
    folder = work_root() / "mine"
    out = []
    for p in sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []:
        fm, _ = refstore.read_md(p)
        intentional = set(fm.get("intentional") or [])
        gaps = [g for g in fm.get("gaps") or [] if g not in intentional]
        if gaps:
            out.append({"name": p.stem, "category": fm.get("category"), "date": str(fm.get("date")),
                        "nearest": fm.get("nearest"), "gaps": gaps})
    return out


def context(date: str | None = None) -> dict:
    day = _today(date)
    profile = motif._profile()
    since = (Date.fromisoformat(day) - timedelta(days=HISTORY_DAYS)).isoformat()
    yesterday = (Date.fromisoformat(day) - timedelta(days=1)).isoformat()
    history = _missions_between(since, yesterday)
    base = base_minutes(profile)
    today = _mission_path(day)
    return {
        "date": day,
        "profile": profile,
        "stage": (profile or {}).get("성장 단계") or "관찰",
        "base_minutes": base,
        "recommended": recommend(history, base),
        "starred": _starred(),
        "drafts": _drafts(),
        "gaps": _diagnosis_gaps(),
        "history": history,
        "today": _mission_record(today) if today.exists() else None,
        "kinds": KINDS,
    }


# --- save / submit / skip ----------------------------------------------------------

def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_mission", f"미션이 형식에 맞지 않습니다: {message}")


def _line(value: Any) -> str:
    """한 줄 필드: 앞뒤 공백을 지우고 안의 줄바꿈·연속 공백을 한 칸으로 줄인다."""
    return " ".join(str(value or "").split())


def validate_mission(data: Any) -> dict:
    """미션 JSON을 검증하고 정리한 사본을 돌려준다. 문제가 있으면 invalid_mission."""
    if not isinstance(data, dict):
        raise _invalid("JSON 객체가 아닙니다")
    title = _line(data.get("title"))
    if not title:
        raise _invalid("title이 비어 있습니다")
    if data.get("kind") not in KINDS:
        raise _invalid(f"kind={data.get('kind')!r} (허용: {', '.join(KINDS)})")
    minutes = data.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 10 <= minutes <= MAX_MINUTES:
        raise _invalid(f"minutes={minutes!r} (10~{MAX_MINUTES}분)")
    raw_steps = data.get("steps")
    if raw_steps is None:
        raw_steps = []
    if not isinstance(raw_steps, list):
        raise _invalid("steps가 목록이 아닙니다")
    steps = [line for line in (_line(s) for s in raw_steps) if line]
    if not steps:
        raise _invalid("steps가 비어 있습니다")
    return {"title": title, "kind": data["kind"], "minutes": int(minutes), "source": _line(data.get("source")) or None,
            "why": _line(data.get("why")) or None, "steps": steps,
            "deliverable": _line(data.get("deliverable")) or None}


def save(mission_path: Path, date: str | None = None, replace: bool = False) -> dict:
    day = _today(date)
    data = validate_mission(_load_json(mission_path, "invalid_mission"))
    minutes = data["minutes"]

    path = _mission_path(day)
    swapped_from = None
    if path.exists():
        if not replace:
            raise ScriptError("mission_exists", f"{day} 미션이 이미 있습니다. 바꾸려면 swap(--replace)")
        swapped_from = _mission_record(path)["title"]

    profile = motif._profile()
    fm = {"date": day, "stage": (profile or {}).get("성장 단계") or "관찰", "kind": data["kind"],
          "status": "open", "source": data["source"], "minutes": minutes,
          "difficulty": None, "skip_reason": None}
    if swapped_from:
        fm["swapped_from"] = swapped_from
    fm["schema_version"] = 1
    body = "\n".join([
        f"# {day} 미션", "", "## 미션", f"### {data['title']} ({minutes}분 · {data['kind']})",
        *([f"- 왜: {data['why']}"] if data["why"] else []),
        *([f"- 재료: {data['source']}"] if data["source"] else []), "",
        *[f"{i}. {s}" for i, s in enumerate(data["steps"], 1)], "",
        *([f"- 결과물: {data['deliverable']}"] if data["deliverable"] else []), "",
        "## 제출", "", "## 피드백", "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    refstore.write_atomic(path, refstore.join_frontmatter(fm, body).encode("utf-8"))
    Path(mission_path).unlink(missing_ok=True)
    return {"status": "saved", "path": path.relative_to(work_root()).as_posix(), "swapped_from": swapped_from}


def _require_mission(day: str) -> Path:
    path = _mission_path(day)
    if not path.exists():
        raise ScriptError("no_mission", f"{day} 미션이 없습니다. 먼저 /yt-mission today")
    return path


def submit(file: str, difficulty: str, feedback: str, date: str | None = None, note: str | None = None) -> dict:
    day = _today(date)
    if difficulty not in DIFFICULTIES:
        raise ScriptError("usage", f"difficulty는 {', '.join(DIFFICULTIES)} 중 하나입니다")
    path = _require_mission(day)
    fm, body = refstore.read_md(path)
    name = (Path(file.replace(chr(92), "/")).name or file) if file else ""
    lines = [*([f"- 한 일: {_line(note)}"] if _line(note) else []),
             *([f"- 파일: {name}"] if name else []), f"- 난이도: {difficulty}"]
    body = refstore.replace_section(body, "## 제출", "\n".join(lines))
    body = refstore.replace_section(body, "## 피드백", feedback.strip())
    fm.update({"status": "submitted", "difficulty": difficulty})
    refstore.write_atomic(path, refstore.join_frontmatter(fm, body).encode("utf-8"))
    return {"status": "submitted", "date": day, "difficulty": difficulty}


def skip(reason: str, date: str | None = None) -> dict:
    day = _today(date)
    path = _require_mission(day)
    refstore.update_frontmatter(path, {"status": "skipped", "skip_reason": reason.strip() or None})
    return {"status": "skipped", "date": day}


# --- 주간 회고 ---------------------------------------------------------------------

def _week_range(week: str | None) -> tuple[str, Date, Date]:
    if week is None:
        y, w, _ = Date.today().isocalendar()
        week = f"{y}-W{w:02d}"
    m = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if not m:
        raise ScriptError("usage", f"주는 YYYY-Www 형식입니다: {week!r}")
    monday = Date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    return week, monday, monday + timedelta(days=6)


def week(week_id: str | None = None) -> dict:
    week_id, start, end = _week_range(week_id)
    s, e = start.isoformat(), end.isoformat()
    missions = _missions_between(s, e)
    statuses = Counter(m["status"] for m in missions)
    cards = 0
    for cat_dir in (work_root() / "refs").glob("ref_*"):
        _, body = refstore.read_md(cat_dir / "index.md")
        cards += sum(1 for d in re.findall(r"^- 만든 날: (\S+)", body, re.M) if s <= d <= e)
    diagnoses = [d for d in _diagnosis_gaps() if s <= d["date"] <= e]
    return {
        "week": week_id, "range": [s, e],
        "missions": {"total": len(missions), "submitted": statuses["submitted"], "skipped": statuses["skipped"],
                     "open": statuses["open"],
                     "completion": round(statuses["submitted"] / len(missions), 2) if missions else 0.0},
        "difficulty": dict(Counter(m["difficulty"] for m in missions if m["difficulty"])),
        "skip_reasons": [m["skip_reason"] for m in missions if m["skip_reason"]],
        "kinds": dict(Counter(m["kind"] for m in missions)),
        "items": missions,
        "cards_created": cards,
        "diagnosis_gaps": diagnoses,
        "refs": refstore.list_categories(),
        "profile": motif._profile(),
    }


def review_save(week_id: str, review_path: Path) -> dict:
    data = _load_json(review_path, "invalid_review")
    if not str(data.get("summary") or "").strip():
        raise ScriptError("invalid_review", "summary가 비어 있습니다")
    stats = week(week_id)
    path = work_root() / "reviews" / f"{stats['week']}.md"
    fm = {"week": stats["week"], "range": stats["range"], "missions": stats["missions"],
          "difficulty": stats["difficulty"], "proposal": data.get("profile_proposal") or {},
          "approved": False, "schema_version": 1}
    proposal = data.get("profile_proposal") or {}
    weakness = [f"- {w}" for w in data.get("repeated_weakness") or []] or ["- 없음"]
    proposal_lines = [f"- {k}: {v}" for k, v in proposal.items()] or ["- 없음"]
    lines = [f"# {stats['week']} 주간 회고 ({stats['range'][0]} ~ {stats['range'][1]})", "",
             "## 요약", data["summary"].strip(), "",
             "## 숫자", f"- 미션 {stats['missions']['total']}개 중 제출 {stats['missions']['submitted']} · "
             f"건너뜀 {stats['missions']['skipped']} (완료율 {round(stats['missions']['completion'] * 100)}%)",
             f"- 새 모티브 카드 {stats['cards_created']}장", "",
             "## 반복된 약점", *weakness, "",
             "## 다음 주 방향", *[f"- {n}" for n in data.get("next_week") or []], "",
             "## 프로필 변경 제안", *proposal_lines, "",
             "## 내 메모", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(refstore.join_frontmatter(fm, "\n".join(lines)).encode("utf-8"))
    Path(review_path).unlink(missing_ok=True)
    return {"status": "saved", "path": path.relative_to(work_root()).as_posix()}


def profile_update(updates_path: Path, date: str | None = None) -> dict:
    updates = _load_json(updates_path, "invalid_profile_update")
    unknown = [k for k in updates if k not in PROFILE_SECTIONS]
    if unknown:
        raise ScriptError("invalid_profile_update", f"프로필에 없는 섹션입니다: {', '.join(unknown)}")
    if "성장 단계" in updates and str(updates["성장 단계"]).strip() not in STAGES:
        raise ScriptError("invalid_profile_update", f"성장 단계는 {', '.join(STAGES)} 중 하나입니다")
    path = work_root() / "profile.md"
    if not path.exists():
        raise ScriptError("no_profile", "profile.md가 없습니다. 먼저 /yt-init")
    fm, body = refstore.read_md(path)
    body = body.replace("\r\n", "\n")
    for section, text in updates.items():
        body = refstore.replace_section(body, f"## {section}", str(text).strip())
    fm["updated"] = _today(date)
    path.write_bytes(refstore.join_frontmatter(fm, body).encode("utf-8"))
    Path(updates_path).unlink(missing_ok=True)
    review_dir = work_root() / "reviews"
    return {"status": "updated", "sections": list(updates), "updated": fm["updated"],
            "reviews": sorted(p.stem for p in review_dir.glob("*.md")) if review_dir.is_dir() else []}


# --- CLI --------------------------------------------------------------------------

def _work_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else work_root() / p


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.mission")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("context")
    c.add_argument("--date")
    s = sub.add_parser("save")
    s.add_argument("--mission", required=True)
    s.add_argument("--date")
    s.add_argument("--replace", action="store_true")
    sb = sub.add_parser("submit")
    sb.add_argument("file")
    sb.add_argument("--difficulty", required=True, choices=DIFFICULTIES)
    sb.add_argument("--feedback", required=True)
    sb.add_argument("--note")
    sb.add_argument("--date")
    sk = sub.add_parser("skip")
    sk.add_argument("--reason", default="")
    sk.add_argument("--date")
    w = sub.add_parser("week")
    w.add_argument("week", nargs="?")
    r = sub.add_parser("review-save")
    r.add_argument("week")
    r.add_argument("--review", required=True)
    pu = sub.add_parser("profile-update")
    pu.add_argument("--updates", required=True)
    pu.add_argument("--date")
    args = parser.parse_args(argv)

    if args.cmd == "context":
        return context(args.date)
    if args.cmd == "save":
        return save(_work_path(args.mission), args.date, args.replace)
    if args.cmd == "submit":
        return submit(args.file, args.difficulty, args.feedback, args.date, args.note)
    if args.cmd == "skip":
        return skip(args.reason, args.date)
    if args.cmd == "week":
        return week(args.week)
    if args.cmd == "review-save":
        return review_save(args.week, _work_path(args.review))
    return profile_update(_work_path(args.updates), args.date)


if __name__ == "__main__":
    run(main)
