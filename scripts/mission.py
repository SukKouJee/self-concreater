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

from scripts import ctx, keys, motif, refstore
from scripts.common import ArgParser, ScriptError, run

KINDS = ["관찰", "분석", "기획", "촬영", "편집", "업로드"]
STAGES = ["관찰", "모방", "제작", "변형"]
DIFFICULTIES = ["easy", "right", "hard"]
PROFILE_SECTIONS = ["관심 주제", "포맷", "롤모델", "하루 시간", "피하고 싶은 스타일", "강점", "약점", "성장 단계"]
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES, STEP = 15, 30, 20, 5
HISTORY_DAYS = 7


def _today(value: str | None) -> str:
    return value or Date.today().isoformat()


def _mission_key(day: str) -> str:
    return keys.mission_md(day)


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

def _mission_record(key: str) -> dict:
    """미션 md(문서 키) 한 건의 요약."""
    fm, body = refstore.read(key)
    title = re.search(r"^### (.+?) \(\d+분", body, re.M)
    return {"date": str(fm.get("date") or keys.stem(key)), "title": title.group(1) if title else None,
            "kind": fm.get("kind"), "status": fm.get("status"), "difficulty": fm.get("difficulty"),
            "minutes": fm.get("minutes"), "skip_reason": fm.get("skip_reason"), "source": fm.get("source")}


def _missions_between(start: str, end: str) -> list[dict]:
    records = []
    for meta in ctx.here().store.list(keys.MISSIONS, "*.md"):
        if not start <= keys.stem(meta.key) <= end:
            continue
        try:
            records.append(_mission_record(meta.key))
        except ScriptError as e:
            if e.code != "bad_frontmatter":
                raise
            # 사용자가 고치는 중인 기록 하나가 전체 조회를 막지 않도록 건너뛴다
    return records


def _starred() -> list[dict]:
    found = []
    for folder in ctx.here().store.folders(keys.REFS, "ref_*"):
        _, body = refstore.read(folder + "index.md")
        for card in motif.parse_cards(body):
            if card["starred"]:
                found.append({"category": keys.category_name(folder), "id": card["id"],
                              "type": card["type"], "title": card["title"]})
    return found


def _drafts(limit: int = 3) -> list[dict]:
    metas = list(reversed(ctx.here().store.list(keys.DRAFTS, "*.md")))[:limit]       # 이름 역순(Windows는 대소문자 무시)
    out = []
    for meta in metas:
        fm, _ = refstore.read(meta.key)
        out.append({"path": meta.key, "title": fm.get("title"),
                    "status": fm.get("status"), "category": fm.get("category")})
    return out


def _diagnosis_gaps() -> list[dict]:
    out = []
    for meta in sorted(ctx.here().store.list(keys.MINE, "*.md"), key=lambda m: m.mtime, reverse=True):
        fm, _ = refstore.read(meta.key)
        intentional = set(fm.get("intentional") or [])
        gaps = [g for g in fm.get("gaps") or [] if g not in intentional]
        if gaps:
            out.append({"name": keys.stem(meta.key), "category": fm.get("category"), "date": str(fm.get("date")),
                        "nearest": fm.get("nearest"), "gaps": gaps})
    return out


def context(date: str | None = None) -> dict:
    day = _today(date)
    profile = motif._profile()
    since = (Date.fromisoformat(day) - timedelta(days=HISTORY_DAYS)).isoformat()
    yesterday = (Date.fromisoformat(day) - timedelta(days=1)).isoformat()
    history = _missions_between(since, yesterday)
    base = base_minutes(profile)
    today = _mission_key(day)
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
        "today": _mission_record(today) if ctx.here().store.exists(today) else None,
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

    store = ctx.here().store
    key = _mission_key(day)
    swapped_from, memo = None, None
    if store.exists(key):
        if not replace:
            raise ScriptError("mission_exists", f"{day} 미션이 이미 있습니다. 바꾸려면 swap(--replace)")
        swapped_from = _mission_record(key)["title"]
        memo = _memo_tail(refstore.read(key)[1])        # 사용자의 메모(와 그 뒤 내용)는 새 미션으로 옮긴다

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
    if memo is not None:
        body += "\n" + memo
    store.write(key, refstore.join_frontmatter(fm, body).encode("utf-8"))
    Path(mission_path).unlink(missing_ok=True)
    return {"status": "saved", "path": key, "swapped_from": swapped_from}


def _require_mission(day: str) -> str:
    key = _mission_key(day)
    if not ctx.here().store.exists(key):
        raise ScriptError("no_mission", f"{day} 미션이 없습니다. 먼저 /yt-mission today")
    return key


def submit(file: str, difficulty: str, feedback: str, date: str | None = None, note: str | None = None) -> dict:
    day = _today(date)
    if difficulty not in DIFFICULTIES:
        raise ScriptError("usage", f"difficulty는 {', '.join(DIFFICULTIES)} 중 하나입니다")
    key = _require_mission(day)
    fm, body = refstore.read(key)
    name = _line((Path(file.replace(chr(92), "/")).name or file) if file else "")
    lines = [*([f"- 한 일: {_line(note)}"] if _line(note) else []),
             *([f"- 파일: {name}"] if name else []), f"- 난이도: {difficulty}"]
    body = refstore.replace_section(body, "## 제출", "\n".join(lines))
    body = refstore.replace_section(body, "## 피드백", refstore.escape_headings(feedback.strip()))
    fm.update({"status": "submitted", "difficulty": difficulty, "skip_reason": None})
    ctx.here().store.write(key, refstore.join_frontmatter(fm, body).encode("utf-8"))
    return {"status": "submitted", "date": day, "difficulty": difficulty}


def skip(reason: str, date: str | None = None) -> dict:
    day = _today(date)
    key = _require_mission(day)
    refstore.update_frontmatter(key, {"status": "skipped", "skip_reason": _line(reason) or None})
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
    for folder in ctx.here().store.folders(keys.REFS, "ref_*"):
        _, body = refstore.read(folder + "index.md")
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


MEMO_HEADING = "## 내 메모"


def _bad_review(message: str) -> ScriptError:
    return ScriptError("invalid_review", message)


def _line_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _bad_review(f"{name}가 목록이 아닙니다")
    items = []
    for item in value:
        if isinstance(item, (list, dict)):
            raise _bad_review(f"{name}의 항목은 문자열이어야 합니다")
        if _line(item):
            items.append(_line(item))
    return items


def validate_review(data: Any) -> dict:
    """회고 JSON을 검증하고 정리한 사본을 돌려준다. 문제가 있으면 invalid_review."""
    if not isinstance(data, dict):
        raise _bad_review("JSON 객체가 아닙니다")
    summary = data.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise _bad_review("summary가 문자열이 아닙니다")
    if not (summary or "").strip():
        raise _bad_review("summary가 비어 있습니다")
    proposal = data.get("profile_proposal")
    if proposal is None:
        proposal = {}
    if not isinstance(proposal, dict):
        raise _bad_review("profile_proposal이 객체가 아닙니다")
    cleaned: dict[str, str] = {}
    for key, value in proposal.items():
        if key not in PROFILE_SECTIONS:
            raise _bad_review(f"프로필에 없는 섹션입니다: {key}")
        if not isinstance(value, str):
            raise _bad_review(f"profile_proposal의 {key} 값이 문자열이 아닙니다")
        if not value.strip():
            continue                                   # 빈 제안은 없는 것으로 본다(성장 단계 포함)
        if key == "성장 단계" and value.strip() not in STAGES:
            raise _bad_review(f"성장 단계는 {', '.join(STAGES)} 중 하나입니다")
        cleaned[key] = value.strip()
    return {"summary": (summary or "").strip(), "repeated_weakness": _line_list(data.get("repeated_weakness"), "repeated_weakness"),
            "next_week": _line_list(data.get("next_week"), "next_week"), "profile_proposal": cleaned}


def _memo_tail(body: str) -> str | None:
    """기존 회고 본문에서 `## 내 메모` 줄부터 끝까지(LF 기준). 없으면 None."""
    lines = body.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.rstrip() == MEMO_HEADING), None)
    return None if start is None else "\n".join(lines[start:])


def review_save(week_id: str, review_path: Path) -> dict:
    data = validate_review(_load_json(review_path, "invalid_review"))
    stats = week(week_id)
    store = ctx.here().store
    key = keys.review_md(stats["week"])
    old_fm: dict = {}
    nl, memo = "\n", f"{MEMO_HEADING}\n"
    if store.exists(key):
        text = store.read(key).raw.decode("utf-8-sig")
        try:
            old_fm, old_body = refstore.split_frontmatter(text)
        except ScriptError as e:
            raise ScriptError(e.code, f"기존 회고 파일을 읽을 수 없습니다(reviews/{stats['week']}.md): {e.message}. "
                                      "파일을 고치거나 삭제한 뒤 다시 저장해 주세요") from e
        nl = "\r\n" if "\r\n" in text else "\n"
        memo = _memo_tail(old_body) or memo                  # 사용자의 메모(와 그 뒤 내용)는 그대로 옮긴다
    proposal = data["profile_proposal"]
    fm = {**old_fm, "week": stats["week"], "range": stats["range"], "missions": stats["missions"],
          "difficulty": stats["difficulty"], "proposal": proposal, "approved": False, "schema_version": 1}
    weakness = [f"- {w}" for w in data["repeated_weakness"]] or ["- 없음"]
    proposal_lines = [f"- {k}: {refstore.escape_headings(v)}" for k, v in proposal.items()] or ["- 없음"]
    lines = [f"# {stats['week']} 주간 회고 ({stats['range'][0]} ~ {stats['range'][1]})", "",
             "## 요약", refstore.escape_headings(data["summary"]), "",
             "## 숫자", f"- 미션 {stats['missions']['total']}개 중 제출 {stats['missions']['submitted']} · "
             f"건너뜀 {stats['missions']['skipped']} (완료율 {round(stats['missions']['completion'] * 100)}%)",
             f"- 새 모티브 카드 {stats['cards_created']}장", "",
             "## 반복된 약점", *weakness, "",
             "## 다음 주 방향", *[f"- {n}" for n in data["next_week"]], "",
             "## 프로필 변경 제안", *proposal_lines, ""]
    body = ("\n".join(lines) + "\n" + memo).replace("\n", nl)
    store.write(key, refstore.join_frontmatter(fm, body, nl).encode("utf-8"))
    Path(review_path).unlink(missing_ok=True)
    return {"status": "saved", "path": key}


def profile_update(updates_path: Path, date: str | None = None) -> dict:
    updates = _load_json(updates_path, "invalid_profile_update")
    unknown = [k for k in updates if k not in PROFILE_SECTIONS]
    if unknown:
        raise ScriptError("invalid_profile_update", f"프로필에 없는 섹션입니다: {', '.join(unknown)}")
    if "성장 단계" in updates and str(updates["성장 단계"]).strip() not in STAGES:
        raise ScriptError("invalid_profile_update", f"성장 단계는 {', '.join(STAGES)} 중 하나입니다")
    store = ctx.here().store
    if not store.exists(keys.PROFILE):
        raise ScriptError("no_profile", "profile.md가 없습니다. 먼저 /yt-init")
    text = store.read(keys.PROFILE).raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"
    fm, body = refstore.split_frontmatter(text)
    body = body.replace("\r\n", "\n")
    for section, value in updates.items():
        # 값 안의 '## ' 줄이 섹션 구조를 깨지 않도록 막는다
        body = refstore.replace_section(body, f"## {section}", refstore.escape_headings(str(value).strip()))
    fm["updated"] = _today(date)
    store.write(keys.PROFILE, refstore.join_frontmatter(fm, body.replace("\n", nl), nl).encode("utf-8"))
    Path(updates_path).unlink(missing_ok=True)
    return {"status": "updated", "sections": list(updates), "updated": fm["updated"],
            "reviews": sorted(keys.stem(m.key) for m in store.list(keys.REVIEWS, "*.md"))}


# --- CLI --------------------------------------------------------------------------

def _work_path(value: str) -> Path:
    return ctx.here().scratch.arg_path(value)


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
