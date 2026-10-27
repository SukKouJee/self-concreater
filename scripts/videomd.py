"""영상별 md: 분석 입력을 모으고(context), Claude의 분석 결과를 검증·계산해 저장한다(write).

사용법 (작업 폴더에서):
  python -m scripts.videomd context <video_id> --category <cat>
  python -m scripts.videomd write <video_id> --category <cat> [--role ref|mine] --analysis <json> [--force]
  python -m scripts.videomd relabel <video_id> --category <cat> --changes <json> --hash <sha256>
  python -m scripts.videomd trash <video_id> --category <cat>     영상 md와 분석 데이터를 workspace/trash/로 옮긴다

역할 분담
- Claude(yt-analyze 스킬): 섹션 나누기, 훅·서사 유형, 요약·시놉시스, 기법 → 분석 JSON
- 이 스크립트: 고정 라벨 검증, 섹션 경계를 컷에 맞춤, 섹션별 컷 통계, 제목 패턴, md·기법 카탈로그 저장

분석 JSON
  {"summary", "synopsis", "hook_type", "narrative_type",
   "sections": [{"label", "sub"?, "start", "end", "content", "shot"?}],
   "techniques": [{"kind": 훅|전환|엔딩|촬영, "name", "desc", "at"?}]}

relabel: 사용자가 고친 섹션 라벨·유형만 프런트매터에 저장한다(본문·`## 내 메모`는 건드리지 않는다).
  섹션별 샷 요약(`fm.shots`)은 다시 계산하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import refstore
from scripts.common import ArgParser, ScriptError, move_to_trash, run, work_root, workspace_dir
from scripts.fetch import check_duplicates, load_raw, speech_density

# 설계서 5절 고정 라벨. 바꾸면 schema_version을 올리고 마이그레이션을 쓴다
LABELS = ["hook", "intro", "body", "demo", "transition", "cta", "outro"]
BODY_SUBS = ["list_item", "story", "explain", "compare"]
HOOK_TYPES = ["question", "result_first", "shock_line", "situation"]
NARRATIVE_TYPES = ["problem_solution", "list", "journey", "compare", "reaction"]
TECHNIQUE_KINDS = ["훅", "전환", "엔딩", "촬영"]
SCHEMA_VERSION = 1

SNAP_SEC = 0.7          # 섹션 경계에서 이 거리 안에 컷이 있으면 컷에 맞춘다
MAX_EXAMPLES = 3
CAMERA_KO = {"static": "고정", "pan": "패닝", "zoom": "줌"}
CAMERA_SHARE = 0.25     # 섹션 안에서 이 비중 이상인 카메라 판정만 표에 적는다


# --- 계산 도우미 ------------------------------------------------------------------

def title_patterns(title: str) -> list[str]:
    checks = [
        ("number", r"\d"),
        ("question", r"[?？]"),
        ("bracket", r"[\[\]()（）【】「」<>]"),
        ("exclaim", r"[!！]"),
    ]
    return [name for name, pattern in checks if re.search(pattern, title or "")]


def fmt_time(sec: float) -> str:
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def load_cuts(video_id: str) -> dict | None:
    path = workspace_dir("cuts", f"{video_id}.json")
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def video_md_path(video_id: str, category: str) -> Path:
    return refstore.existing_category_dir(category) / "videos" / f"{video_id}.md"


def _rel(path: Path) -> str:
    return path.relative_to(work_root()).as_posix()


# --- context ----------------------------------------------------------------------

def examples(category: str) -> list[str]:
    """사용자가 고친(reviewed: true) ref 영상 md, 최근 수정순 최대 3개."""
    videos = refstore.existing_category_dir(category) / "videos"
    picked = []
    for path in sorted(videos.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        fm = refstore.read_md(path)[0]
        if fm.get("reviewed") is True and fm.get("role") == "ref":
            picked.append(_rel(path))
        if len(picked) == MAX_EXAMPLES:
            break
    return picked


def context(video_id: str, category: str) -> dict:
    raw = load_raw(video_id)
    cuts = load_cuts(video_id)
    meta, transcript = raw["meta"], raw.get("transcript")
    segments = (transcript or {}).get("segments", [])
    cps, sparse = speech_density(segments, meta.get("duration"))

    scenes = None
    if cuts:
        scenes = "\n".join(
            f"[{s['start']:.1f}–{s['end']:.1f}] {s['camera']}" + (f" · frame {s['frame']}" if s["frame"] else "")
            for s in cuts["scenes"]
        )
    return {
        "id": video_id,
        "title": meta.get("title"),
        "channel": meta.get("channel"),
        "duration_sec": meta.get("duration"),
        "format": raw["format"],
        "transcript_source": (transcript or {}).get("source"),
        "language": (transcript or {}).get("language"),
        "chars_per_sec": cps,
        "sparse": sparse if transcript else True,
        "transcript": "\n".join(f"[{s['start']:.1f}] {s['text']}" for s in segments) if transcript else None,
        "avg_cut_sec": cuts["avg_cut_sec"] if cuts else None,
        "scenes": scenes,
        "examples": examples(category),
        "md_exists": video_md_path(video_id, category).exists(),
        "vocab": {
            "label": LABELS, "body_sub": BODY_SUBS, "hook_type": HOOK_TYPES,
            "narrative_type": NARRATIVE_TYPES, "technique_kind": TECHNIQUE_KINDS,
        },
    }


# --- 검증 -------------------------------------------------------------------------

def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_analysis", f"분석 결과가 형식에 맞지 않습니다: {message}")


def _require_choice(value: Any, choices: list[str], field: str) -> None:
    if value not in choices:
        raise _invalid(f"{field}={value!r} (허용: {', '.join(choices)})")


def validate(analysis: dict, duration: float, cut_times: list[float]) -> dict:
    for field in ("summary", "synopsis"):
        if not str(analysis.get(field) or "").strip():
            raise _invalid(f"{field}가 비어 있습니다")
    _require_choice(analysis.get("hook_type"), HOOK_TYPES, "hook_type")
    _require_choice(analysis.get("narrative_type"), NARRATIVE_TYPES, "narrative_type")

    raw_sections = analysis.get("sections") or []
    if not raw_sections:
        raise _invalid("sections가 비어 있습니다")
    sections = []
    for s in sorted(raw_sections, key=lambda s: float(s.get("start", 0))):
        _require_choice(s.get("label"), LABELS, "label")
        sub = s.get("sub")
        if sub is not None:
            if s["label"] != "body":
                raise _invalid(f"sub는 body에만 씁니다 (label={s['label']}, sub={sub})")
            _require_choice(sub, BODY_SUBS, "sub")
        sections.append({
            "label": s["label"], "sub": sub,
            "start": float(s["start"]), "end": float(s["end"]),
            "content": str(s.get("content") or "").strip(),
            "shot": str(s.get("shot") or "").strip(),
        })

    # 경계: 처음은 0, 끝은 영상 길이, 사이는 이어 붙이고 가까운 컷에 맞춘다
    prev = 0.0
    for i, s in enumerate(sections):
        s["start"] = prev
        if i == len(sections) - 1:
            s["end"] = float(duration)
        else:
            end = min(s["end"], float(duration))
            near = [c for c in cut_times if abs(c - end) <= SNAP_SEC]
            if near:
                end = min(near, key=lambda c: abs(c - end))
            s["end"] = end
        if s["end"] <= s["start"]:
            raise _invalid(f"섹션 {s['label']}의 구간이 비었습니다 ({s['start']}–{s['end']})")
        s["start"], s["end"] = round(s["start"], 2), round(s["end"], 2)
        prev = s["end"]

    techniques = []
    for t in analysis.get("techniques") or []:
        _require_choice(t.get("kind"), TECHNIQUE_KINDS, "kind")
        if not str(t.get("name") or "").strip():
            raise _invalid("기법 name이 비어 있습니다")
        techniques.append({"kind": t["kind"], "name": t["name"].strip(),
                           "desc": str(t.get("desc") or "").strip(), "at": t.get("at")})

    return {**analysis, "sections": sections, "techniques": techniques}


# --- 섹션별 컷 통계 · 촬영 요약 -------------------------------------------------------

def _segments_in(section: dict, cut_times: list[float]) -> int:
    return 1 + sum(1 for c in cut_times if section["start"] < c < section["end"])


def pacing(sections: list[dict], cuts: dict | None) -> dict:
    if not cuts:
        return {"avg_cut_sec": None, "by_section": {}}
    totals: dict[str, list[float]] = {}
    for s in sections:
        dur, segs = totals.setdefault(s["label"], [0.0, 0])
        totals[s["label"]] = [dur + s["end"] - s["start"], segs + _segments_in(s, cuts["cuts"])]
    return {
        "avg_cut_sec": cuts["avg_cut_sec"],
        "by_section": {label: round(dur / segs, 2) for label, (dur, segs) in totals.items()},
    }


def shot_summary(section: dict, cuts: dict | None) -> str:
    if not cuts:
        return "-"
    share: dict[str, float] = {}
    for sc in cuts["scenes"]:
        overlap = min(sc["end"], section["end"]) - max(sc["start"], section["start"])
        if overlap > 0:
            share[sc["camera"]] = share.get(sc["camera"], 0) + overlap
    length = section["end"] - section["start"]
    labels = [c for c, d in sorted(share.items(), key=lambda kv: -kv[1]) if d / length >= CAMERA_SHARE]
    avg = length / _segments_in(section, cuts["cuts"])
    return f"{'·'.join(CAMERA_KO[c] for c in labels) or '고정'} · 컷 {avg:.1f}초"


# --- md 렌더링 ----------------------------------------------------------------------

def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ") or "-"


def render_body(analysis: dict, cuts: dict | None, memo: str) -> str:
    rows = []
    for s in analysis["sections"]:
        label = f"{s['label']}/{s['sub']}" if s["sub"] else s["label"]
        shot = s["shot"] or shot_summary(s, cuts)
        rows.append(f"| {fmt_time(s['start'])}–{fmt_time(s['end'])} | {label} | {_cell(s['content'])} | {_cell(shot)} |")

    techniques = []
    for t in analysis["techniques"]:
        at = f" ({fmt_time(t['at'])})" if t.get("at") is not None else ""
        techniques.append(f"- **{t['kind']} · {t['name']}**{at}: {t['desc']}")

    parts = [
        "## 한 줄 요약", analysis["summary"].strip(), "",
        "## 시놉시스", analysis["synopsis"].strip(), "",
        "## 섹션", "| 구간 | 라벨 | 내용 | 촬영 |", "|---|---|---|---|", *rows, "",
        "## 눈에 띄는 기법", *(techniques or ["- (없음)"]), "",
        "## 내 메모",
    ]
    return "\n".join(parts) + "\n" + memo


def _existing_memo(path: Path) -> str:
    if not path.exists():
        return ""
    _, body = refstore.read_md(path)
    return memo_from_body(body)


def memo_from_body(body: str) -> str:
    """본문의 '## 내 메모' 아래 내용(없으면 "")."""
    marker = "## 내 메모"
    idx = body.find(marker)
    if idx < 0:
        return ""
    return body[idx + len(marker):].lstrip("\r\n")


# --- 기법 카탈로그 -------------------------------------------------------------------

def drop_video_techniques(category: str, video_id: str) -> None:
    """다시 분석할 때 그 영상의 예전 기법 출처를 뺀다. 출처가 하나도 안 남은 항목은 지운다."""
    path = refstore.existing_category_dir(category) / "techniques.md"
    fm, body = refstore.read_md(path)
    link = f"[{video_id}](videos/{video_id}.md)"
    kept = []
    for line in body.split("\n"):
        if line.startswith("- **") and link in line:
            head, _, sources = line.partition(" — ")
            remaining = [s for s in sources.split(", ") if s and s != link]
            if not remaining:
                continue
            line = f"{head} — {', '.join(remaining)}"
        kept.append(line)
    path.write_bytes(refstore.join_frontmatter(fm, "\n".join(kept)).encode("utf-8"))


def append_techniques(category: str, video_id: str, techniques: list[dict]) -> tuple[int, int]:
    """techniques.md의 종류별 섹션에 추가. 같은 이름이 있으면 출처 링크만 붙인다."""
    path = refstore.existing_category_dir(category) / "techniques.md"
    fm, body = refstore.read_md(path)
    lines = body.split("\n")
    link = f"[{video_id}](videos/{video_id}.md)"
    added = merged = 0

    for t in techniques:
        heading = f"## {t['kind']}"
        start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
        if start is None:
            lines += ["", heading]
            start = len(lines) - 1
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        prefix = f"- **{t['name']}**"
        existing = next((i for i in range(start + 1, end) if lines[i].startswith(prefix)), None)
        if existing is not None:
            if link not in lines[existing]:
                lines[existing] += f", {link}"
                merged += 1
            continue
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"{prefix}: {t['desc']} — {link}")
        added += 1

    path.write_bytes(refstore.join_frontmatter(fm, "\n".join(lines)).encode("utf-8"))
    return added, merged


# --- write ------------------------------------------------------------------------

def write(video_id: str, category: str, role: str, analysis_path: Path, force: bool = False) -> dict:
    path = video_md_path(video_id, category)
    if path.exists() and not force:
        return {"id": video_id, "status": "exists", "md": _rel(path)}
    check_duplicates(video_id, category, role)
    rewriting = path.exists()

    raw = load_raw(video_id)
    cuts = load_cuts(video_id)
    meta = raw["meta"]
    duration = float(meta.get("duration") or (cuts or {}).get("duration_sec") or 0)
    try:
        analysis = json.loads(Path(analysis_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("invalid_analysis", f"분석 JSON을 읽을 수 없습니다: {e}") from e
    analysis = validate(analysis, duration, cuts["cuts"] if cuts else [])
    sections = analysis["sections"]

    fm = {
        "id": video_id,
        "url": raw["url"],
        "role": role,
        "channel": meta.get("channel"),
        "title": meta.get("title"),
        "duration_sec": meta.get("duration"),
        "format": raw["format"],
        "transcript_source": (raw.get("transcript") or {}).get("source"),
        "sections": [
            {"label": s["label"], **({"sub": s["sub"]} if s["sub"] else {}), "start": s["start"], "end": s["end"]}
            for s in sections
        ],
        "hook_type": analysis["hook_type"],
        "narrative_type": analysis["narrative_type"],
        "title_pattern": title_patterns(meta.get("title") or ""),
        "pacing": pacing(sections, cuts),
        "shots": None,
        "camera": cuts["camera"] if cuts else None,
        "reviewed": False,
        "schema_version": SCHEMA_VERSION,
    }
    # 다시 쓸 때는 기존 파일의 줄바꿈 방식(CRLF/LF)을 따르고 '## 내 메모'는 그대로 옮긴다
    nl = "\r\n" if path.exists() and path.read_bytes().startswith(b"---\r\n") else "\n"
    memo = _existing_memo(path).replace("\r\n", "\n")
    body = render_body(analysis, cuts, memo).replace("\n", nl)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(refstore.join_frontmatter(fm, body, nl).encode("utf-8"))

    if rewriting:
        drop_video_techniques(category, video_id)
    added, merged = append_techniques(category, video_id, analysis["techniques"])
    stage = refstore.update_stage(category)
    Path(analysis_path).unlink(missing_ok=True)
    return {
        "id": video_id,
        "status": "written",
        "md": _rel(path),
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sections": len(sections),
        "techniques_added": added,
        "techniques_merged": merged,
        "stage": {k: stage[k] for k in ("ref", "mine", "stage")},
        "stage_text": stage["text"],
    }


# --- relabel ----------------------------------------------------------------------

VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RELABEL_LOCK = threading.Lock()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stale() -> ScriptError:
    return ScriptError("ref_changed", "화면을 연 뒤 md가 바뀌었습니다. 다시 불러온 뒤 고쳐 주세요")


def _bad_labels(message: str) -> ScriptError:
    return ScriptError("invalid_labels", message)


def _where(i: int, section: dict) -> str:
    """오류 메시지용 위치 표기: 'i번째 섹션(m:ss–m:ss)'. 시간이 숫자가 아니면 번호만 쓴다."""
    try:
        return f"{i}번째 섹션({fmt_time(section['start'])}–{fmt_time(section['end'])})"
    except (KeyError, TypeError, ValueError):
        return f"{i}번째 섹션"


def _relabeled_sections(old: Any, edits: Any) -> list[dict]:
    """기존 섹션 dict를 복사해 label/sub만 교체한다(시간 등 다른 키 보존, body가 아니면 sub 삭제).

    요청이 전체 상태다: body 항목에서 sub를 생략하면 기존 sub가 지워진다.
    """
    if not isinstance(old, list) or not all(isinstance(s, dict) and "label" in s for s in old):
        raise ScriptError("bad_md", "영상 md의 섹션(sections)이 올바르지 않아 라벨을 고칠 수 없습니다")
    if not isinstance(edits, list) or len(edits) != len(old):
        raise _bad_labels(f"sections는 기존과 같은 길이({len(old)})의 목록이어야 합니다")
    out = []
    for i, (section, edit) in enumerate(zip(old, edits), start=1):
        where = _where(i, section)
        if not isinstance(edit, dict):
            raise _bad_labels(f"{where}은 객체여야 합니다")
        label, sub = edit.get("label"), edit.get("sub")
        if label not in LABELS:
            raise _bad_labels(f"{where}의 라벨이 올바르지 않습니다: {label!r}")
        if sub is not None and label != "body":
            raise _bad_labels(f"{where}: sub는 body에만 쓸 수 있습니다")
        if sub is not None and sub not in BODY_SUBS:
            raise _bad_labels(f"{where}의 sub가 올바르지 않습니다: {sub!r}")
        item = {k: v for k, v in section.items() if k not in ("label", "sub")}
        item = {"label": label, **item}
        if sub:
            item["sub"] = sub
        out.append(item)
    return out


def _valid_cuts(cuts: Any) -> bool:
    return isinstance(cuts, dict) and isinstance(cuts.get("cuts"), list) and "avg_cut_sec" in cuts


def relabel(video_id: str, category: str, changes: dict, expected_hash: str) -> dict:
    """섹션 라벨/sub, hook_type, narrative_type 교정을 프런트매터에 저장한다. 들어온 키만 바꾼다."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ScriptError("bad_request", f"잘못된 영상 id입니다: {video_id!r}")
    path = video_md_path(video_id, category)
    if not path.is_file():
        raise ScriptError("video_not_found", f"영상 md가 없습니다: {video_id}")
    with _RELABEL_LOCK:
        if _file_hash(path) != expected_hash:
            raise _stale()
        fm, _ = refstore.read_md(path)

        updates: dict[str, Any] = {}
        if "sections" in changes:
            new_sections = _relabeled_sections(fm.get("sections"), changes["sections"])
            if new_sections != fm.get("sections"):
                updates["sections"] = new_sections
        for field, vocab in (("hook_type", HOOK_TYPES), ("narrative_type", NARRATIVE_TYPES)):
            if field in changes:
                value = changes[field]
                if value not in vocab:
                    raise _bad_labels(f"{field} 값이 올바르지 않습니다: {value!r}")
                if value != fm.get(field):
                    updates[field] = value

        rel = _rel(path)
        if not updates:
            return {"id": video_id, "md": rel, "hash": expected_hash, "changed": [],
                    "reviewed": bool(fm.get("reviewed")), "pacing_stale": False}
        pacing_stale = False
        changed = list(updates)
        if "sections" in updates:
            try:
                cuts = load_cuts(video_id)
            except (ValueError, OSError):
                cuts = None                     # 깨진 컷 json이면 pacing은 그대로 둔다
            pacing_stale = not _valid_cuts(cuts)
            if not pacing_stale:
                try:
                    updates["pacing"] = pacing(updates["sections"], cuts)
                except (KeyError, TypeError, ValueError) as e:
                    raise ScriptError("bad_md", "섹션 시작/끝 또는 컷 값이 숫자가 아니어서 pacing을 다시 계산할 수 없습니다") from e
        updates["reviewed"] = True
        if _file_hash(path) != expected_hash:   # 쓰기 직전 재확인(경쟁 구간 축소)
            raise _stale()
        refstore.update_frontmatter(path, updates)
        return {"id": video_id, "md": rel, "hash": _file_hash(path), "changed": changed, "reviewed": True,
                "pacing_stale": pacing_stale}


# --- 삭제(휴지통) -------------------------------------------------------------------

def video_data(video_id: str) -> list[Path]:
    """영상 하나의 분석 데이터(workspace/) 중 있는 것: 자막·메타(raw), 컷, 장면 프레임 폴더, 샷 분류."""
    paths = [workspace_dir("raw", f"{video_id}.json"), workspace_dir("cuts", f"{video_id}.json"),
             workspace_dir("frames", video_id), workspace_dir("shots", f"{video_id}.json")]
    return [p for p in paths if p.exists()]


def _typified_ids(category: str) -> set[str]:
    """저장된 유형화 결과에 들어간 영상 id(대표·이상치·축별 묶음 포함). 결과가 없거나 깨졌으면 빈 집합."""
    try:
        result = json.loads(workspace_dir("typify", f"{category}.json").read_text("utf-8"))
        ids = {v for a in result.get("archetypes") or [] for v in a.get("members") or []}
        ids |= {o.get("id") for o in result.get("outliers") or []}
        ids |= {v for res in (result.get("axes") or {}).values() for c in res.get("clusters") or []
                for v in c.get("members") or []}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()
    return ids


def trash_video(video_id: str, category: str) -> dict:
    """레퍼런스(또는 내 영상) 하나를 지운다: 영상 md를 `workspace/trash/<시각>/`으로 옮기고, 다른 카테고리에
    같은 영상이 없으면 분석 데이터(workspace/raw·cuts·frames·shots)도 함께 옮긴다. 기법 카탈로그에서는
    그 영상의 출처를 뺀다(다시 분석할 때와 같다). 유형화 결과는 건드리지 않고 typified로 알린다."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id):
        raise ScriptError("bad_request", f"잘못된 영상 id입니다: {video_id!r}")
    path = video_md_path(video_id, category)
    if not path.is_file():
        raise ScriptError("video_not_found", f"영상 md가 없습니다: {video_id}")
    elsewhere = [p for p in (work_root() / "refs").glob(f"ref_*/videos/{video_id}.md") if p.resolve() != path.resolve()]
    data = [] if elsewhere else video_data(video_id)
    result = move_to_trash([path, *data], last=path)
    # 여기부터는 옮긴 뒤의 정리다. 실패해도 삭제는 끝났으니 오류 대신 표시만 남긴다.
    techniques_stale = False
    if (path.parent.parent / "techniques.md").is_file():
        try:
            drop_video_techniques(category, video_id)
        except (ScriptError, OSError):
            techniques_stale = True       # 카탈로그가 깨졌거나 다른 프로그램이 열어 두었다
    try:
        refstore.update_stage(category)   # index.md의 단계(영상 수 기준)를 줄어든 수에 맞춘다
    except (ScriptError, OSError):
        pass
    return {**result, "kept_data": bool(elsewhere), "typified": video_id in _typified_ids(category),
            "techniques_stale": techniques_stale}


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.videomd")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ctx = sub.add_parser("context")
    ctx.add_argument("video_id")
    ctx.add_argument("--category", required=True)
    wr = sub.add_parser("write")
    wr.add_argument("video_id")
    wr.add_argument("--category", required=True)
    wr.add_argument("--role", choices=["ref", "mine"], default="ref")
    wr.add_argument("--analysis", required=True)
    wr.add_argument("--force", action="store_true")
    rl = sub.add_parser("relabel")
    rl.add_argument("video_id")
    rl.add_argument("--category", required=True)
    rl.add_argument("--changes", required=True)
    rl.add_argument("--hash", required=True)
    tr = sub.add_parser("trash")
    tr.add_argument("video_id")
    tr.add_argument("--category", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "trash":
        return trash_video(args.video_id, args.category)

    if args.cmd == "context":
        return context(args.video_id, args.category)
    if args.cmd == "relabel":
        changes_path = Path(args.changes)
        if not changes_path.is_absolute():
            changes_path = work_root() / changes_path
        try:
            changes = json.loads(changes_path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            raise ScriptError("invalid_labels", f"changes json을 읽지 못했습니다: {e}") from e
        if not isinstance(changes, dict):
            raise ScriptError("invalid_labels", "changes json은 객체여야 합니다")
        return relabel(args.video_id, args.category, changes, args.hash)
    # 실행기(ytc)는 플러그인 폴더에서 돌므로 상대 경로는 작업 폴더 기준으로 푼다
    analysis = Path(args.analysis)
    if not analysis.is_absolute():
        analysis = work_root() / analysis
    return write(args.video_id, args.category, args.role, analysis, args.force)


if __name__ == "__main__":
    run(main)
