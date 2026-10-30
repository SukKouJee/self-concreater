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

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import ctx, keys, refstore
from scripts.stores import VersionConflict
from scripts.common import ArgParser, ScriptError, run
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
# 화면·리포트 표시 이름(scripts/ui/static/labels.js SECTION_KO, BODY_SUB_KO와 같게). 파일은 코드 그대로 둔다
SECTION_KO = {"hook": "훅", "intro": "인트로", "body": "본문", "demo": "시연", "transition": "전환",
              "cta": "행동 유도(CTA)", "outro": "아웃트로"}
BODY_SUB_KO = {"list_item": "목록 항목", "story": "이야기", "explain": "설명", "compare": "비교"}
_SECTION_CODE_RE = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(SECTION_KO) + r")(?:/("
                              + "|".join(BODY_SUB_KO) + r"))?(?![A-Za-z0-9_])")
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
    try:
        raw = ctx.here().shared.get(keys.cuts(video_id))
    except FileNotFoundError:
        return None
    return json.loads(raw.decode("utf-8"))


def video_md_key(video_id: str, category: str) -> str:
    """영상 md 키 `refs/ref_<cat>/videos/<id>.md`. 카테고리가 없으면 category_not_found."""
    refstore.require_category(category)
    return keys.video_md(category, video_id)


# --- context ----------------------------------------------------------------------

def examples(category: str) -> list[str]:
    """사용자가 고친(reviewed: true) ref 영상 md, 최근 수정순 최대 3개."""
    metas = ctx.here().store.list(keys.videos_dir_at(refstore.require_category(category)), "*.md")
    picked = []
    for meta in sorted(metas, key=lambda m: m.mtime, reverse=True):
        fm = refstore.read(meta.key)[0]
        if fm.get("reviewed") is True and fm.get("role") == "ref":
            picked.append(meta.key)
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
        "md_exists": ctx.here().store.exists(video_md_key(video_id, category)),
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


def _existing_memo(key: str) -> str:
    """기존 문서(키)의 '## 내 메모' 아래 내용. 문서가 없으면 ""."""
    store = ctx.here().store
    if not store.exists(key):
        return ""
    _, body = store.read(key).parsed
    return memo_from_body(body)


def memo_from_body(body: str) -> str:
    """본문의 '## 내 메모' 아래 내용(없으면 ""). 제목은 메모 가드와 같은 판정(정확히 그 제목인 줄, `## 내 메모장`은 아님)."""
    tail = refstore.memo_after(body)
    return "" if tail is None else tail.lstrip("\r\n")


# --- 기법 카탈로그 -------------------------------------------------------------------

def drop_video_techniques(category: str, video_id: str) -> None:
    """다시 분석할 때 그 영상의 예전 기법 출처를 뺀다. 출처가 하나도 안 남은 항목은 지운다."""
    refstore.require_category(category)
    key = keys.techniques_md(category)
    fm, body = refstore.read(key)
    link = f"[{video_id}](videos/{video_id}.md)"
    lines = body.split("\n")
    memo = refstore.memo_span(lines) or (0, 0)        # 내 메모 절은 카탈로그가 아니다: 기법 형식 줄이 있어도 그대로 둔다
    kept = []
    for i, line in enumerate(lines):
        if not memo[0] <= i < memo[1] and line.startswith("- **") and link in line:
            head, _, sources = line.partition(" — ")
            remaining = [s for s in sources.split(", ") if s and s != link]
            if not remaining:
                continue
            line = f"{head} — {', '.join(remaining)}"
        kept.append(line)
    ctx.here().store.write(key, refstore.join_frontmatter(fm, "\n".join(kept)).encode("utf-8"))


def append_techniques(category: str, video_id: str, techniques: list[dict]) -> tuple[int, int]:
    """techniques.md의 종류별 섹션에 추가. 같은 이름이 있으면 출처 링크만 붙인다."""
    refstore.require_category(category)
    key = keys.techniques_md(category)
    fm, body = refstore.read(key)
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

    ctx.here().store.write(key, refstore.join_frontmatter(fm, "\n".join(lines)).encode("utf-8"))
    return added, merged


# --- write ------------------------------------------------------------------------

def write(video_id: str, category: str, role: str, analysis_path: Path, force: bool = False) -> dict:
    store = ctx.here().store
    key = video_md_key(video_id, category)
    if store.exists(key) and not force:
        return {"id": video_id, "status": "exists", "md": key}
    check_duplicates(video_id, category, role)
    rewriting = store.exists(key)

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
    # Doc.nl이 아니라 디스크 바이트 그대로 본다: BOM이 붙은 CRLF 파일은 지금도 LF로 다시 쓴다(바이트 보존)
    nl = "\r\n" if store.exists(key) and store.read(key).raw.startswith(b"---\r\n") else "\n"
    memo = _existing_memo(key).replace("\r\n", "\n")
    body = render_body(analysis, cuts, memo).replace("\n", nl)
    store.write(key, refstore.join_frontmatter(fm, body, nl).encode("utf-8"))

    if rewriting:
        drop_video_techniques(category, video_id)
    added, merged = append_techniques(category, video_id, analysis["techniques"])
    stage = refstore.update_stage(category)
    Path(analysis_path).unlink(missing_ok=True)
    return {
        "id": video_id,
        "status": "written",
        "md": key,
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
    store = ctx.here().store
    key = video_md_key(video_id, category)
    if not store.exists(key):
        raise ScriptError("video_not_found", f"영상 md가 없습니다: {video_id}")
    with _RELABEL_LOCK:
        doc = store.read(key)
        if doc.version != expected_hash:
            raise _stale()
        fm, _ = doc.parsed

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

        if not updates:
            return {"id": video_id, "md": key, "hash": expected_hash, "changed": [],
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
        try:                                    # 쓰는 순간 바이트가 expected_hash와 같을 때만 쓴다
            refstore.update_frontmatter(key, updates, expect_version=expected_hash)
        except VersionConflict as e:
            raise _stale() from e
        return {"id": video_id, "md": key, "hash": store.read(key).version, "changed": changed, "reviewed": True,
                "pacing_stale": pacing_stale}


# --- 삭제(휴지통) -------------------------------------------------------------------

def video_data(video_id: str) -> list[str]:
    """영상 하나의 분석 데이터(workspace/) 중 있는 것의 키: 자막·메타(raw), 컷, 장면 프레임 폴더(끝 `/` 없이), 샷 분류."""
    shared = ctx.here().shared
    frames = keys.frames_dir(video_id)
    found = [k for k in (keys.raw(video_id), keys.cuts(video_id)) if shared.exists(k)]
    if shared.folder_exists(frames):
        found.append(frames.removesuffix("/"))
    return found + [k for k in (keys.shots(video_id),) if shared.exists(k)]


def _typified_ids(category: str) -> set[str]:
    """저장된 유형화 결과에 들어간 영상 id(대표·이상치·축별 묶음 포함). 결과가 없거나 깨졌으면 빈 집합."""
    try:
        result = json.loads(ctx.here().objects.get(keys.typify(category)).decode("utf-8"))
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
    store = ctx.here().store
    key = video_md_key(video_id, category)
    if not store.exists(key):
        raise ScriptError("video_not_found", f"영상 md가 없습니다: {video_id}")
    elsewhere = [k for k in (keys.video_md_at(prefix, video_id) for prefix in store.folders(keys.REFS, keys.CATEGORY_GLOB))
                 if k != key and store.exists(k)]
    data = [] if elsewhere else video_data(video_id)
    result = ctx.here().trash.move([key, *data], last=key)
    # 여기부터는 옮긴 뒤의 정리다. 실패해도 삭제는 끝났으니 오류 대신 표시만 남긴다.
    techniques_stale = False
    if store.exists(keys.techniques_md(category)):
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
    cx = sub.add_parser("context")
    cx.add_argument("video_id")
    cx.add_argument("--category", required=True)
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
        changes_path = ctx.here().scratch.arg_path(args.changes)
        try:
            changes = json.loads(changes_path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            raise ScriptError("invalid_labels", f"changes json을 읽지 못했습니다: {e}") from e
        if not isinstance(changes, dict):
            raise ScriptError("invalid_labels", "changes json은 객체여야 합니다")
        return relabel(args.video_id, args.category, changes, args.hash)
    # 실행기(ytc)는 플러그인 폴더에서 돌므로 상대 경로는 작업 폴더 기준으로 푼다
    analysis = ctx.here().scratch.arg_path(args.analysis)
    return write(args.video_id, args.category, args.role, analysis, args.force)


if __name__ == "__main__":
    run(main)


def section_name(label: str, sub: str | None = None) -> str:
    """"body", "story" → "본문(이야기)". 모르는 코드는 그대로."""
    name = SECTION_KO.get(label, label)
    return f"{name}({BODY_SUB_KO.get(sub, sub)})" if sub else name


def humanize_sections(text: str | None) -> str:
    """표시용: 글 안의 섹션 코드("hook → body/story")를 이름으로. 낱말 단위만 바꾼다."""
    if not isinstance(text, str):
        return ""
    return _SECTION_CODE_RE.sub(lambda m: section_name(m.group(1), m.group(2)), text)
