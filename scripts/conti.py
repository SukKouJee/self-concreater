"""콘티: draft를 컷 단위 콘티(전체 대사 + 컷별 대사·자막·효과음·가이드)로 저장하고, HTML 스토리보드를 그린다.

사용법 (작업 폴더에서):
  python -m scripts.conti context <draft.md>                          draft 파싱 + 말 속도 + 포맷
  python -m scripts.conti save <draft.md> --conti <json> [--force]    → drafts/{draft}-conti.md + .html
  python -m scripts.conti render <conti.md>                           고친 프런트매터로 HTML만 다시 그린다
  python -m scripts.conti update <conti.md> --cut N [--line S] [--guide S] [--caption S] [--sfx S]
                          [--ref-frame S] [--expect-hash H]           컷 하나 고치기 (--ref-frame ""은 고정 해제)
  python -m scripts.conti scenes <conti.md> --cut N [--limit 6]       그 컷에 쓸 참고 장면 후보
  python -m scripts.conti cut-context <conti.md> --cut N              컷 하나의 앞뒤 대사·섹션·글자 수 한도
  python -m scripts.conti trash <conti.md>                             콘티와 그 산출물을 workspace/trash/로 옮긴다
  python -m scripts.conti history <conti.md>                           이력 목록(최신순)
  python -m scripts.conti history-name <conti.md> --label S [--expect-hash H]   지금 상태에 이름 붙이기
  python -m scripts.conti history-restore <conti.md> --n N [--expect-hash H]    그 버전으로 되돌리기
  python -m scripts.conti history-diff <conti.md> --a N [--b M]        버전 비교(--b 생략 = 지금)
  python -m scripts.conti merge <conti.md>                             반복 컷을 장면으로 합칠 계획(미리보기 JSON)
  python -m scripts.conti merge <conti.md> --cuts <json> --hash H      합친 컷(또는 Claude 나누기 출력)을 저장

conti JSON (yt-conti 규칙)
  {"script": [{"text"}],                                      draft 섹션마다 하나, 같은 순서 (전체 대사)
   "cuts": [{"section": <섹션 번호 0~>, "size", "angle", "sec", "action", "line", "caption", "sfx", "guide"}]}
- 검증(실패): 섹션 수와 script 수, 섹션별 컷 길이 합 = 섹션 길이(±0.3초), 샷 크기 어휘, action·guide 필수
- 경고(저장은 함): 대사가 컷 길이에 비해 김(말 속도 기준), 컷 대사를 이으면 그 섹션 전체 대사와 다름
- 말 속도 = 카테고리 ref 영상 자막의 초당 글자 수 중앙값(sparse 제외). 없으면 thresholds.json의 speech_default_cps(4.5)
- 시각화: 컷마다 샷 크기 도식(SVG) + 레퍼런스 대표 프레임(참고용). 이미지는 base64로 넣어 한 파일로 만든다
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import statistics
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from scripts import features, fetch, motif, refstore
from scripts.common import TEMPLATES_DIR, ArgParser, ScriptError, move_to_trash, run, work_root, workspace_dir
from scripts.features import SHOT_SIZES
from scripts.report import SLOTS, _vars
from scripts.videomd import LABELS, _existing_memo, fmt_time, load_cuts, memo_from_body

DEFAULT_CPS = float(json.loads((TEMPLATES_DIR / "thresholds.json").read_text("utf-8"))["speech_default_cps"])
CONTI_SUFFIX = "-conti"
SEC_TOLERANCE = 0.3
OVERFLOW_SLACK = 1.15          # 말 속도 × 컷 길이의 115%까지는 허용
SCHEMA_VERSION = 3
HISTORY_LOCK = threading.RLock()   # 해시 확인 → md 쓰기 → 이력 기록을 한 덩어리로(같은 프로세스의 동시 요청)
EDITABLE_FIELDS = ("line", "guide", "caption", "sfx", "ref_frame")

_HEAD_RE = re.compile(r"^### (\d+:\d{2})–(\d+:\d{2}) · (\w+)(?:/(\w+))? \(")
_ROW_RE = re.compile(r"^\| (\d+) \| (.+?) \| (.*?) \| ([\d.]+)초 \| (.*) \|\s*$")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(work_root().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else work_root() / p


# --- draft 읽기 ---------------------------------------------------------------------

def parse_draft(path: Path) -> dict:
    """draft md → 프런트매터 섹션(시간 기준) + 본문의 섹션별 대본·샷 표(사용자가 고친 내용 그대로)."""
    if not path.exists():
        raise ScriptError("draft_not_found", f"기획안이 없습니다: {_rel(path)} (먼저 /yt-draft)")
    fm, body = refstore.read_md(path)
    if not fm.get("sections"):
        raise ScriptError("invalid_draft", f"기획안 프런트매터에 sections가 없습니다: {_rel(path)}")
    blocks: list[dict] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            if blocks:
                blocks[-1]["closed"] = True
            continue
        m = _HEAD_RE.match(line)
        if m:
            blocks.append({"label": m.group(3), "script": [], "shots": []})
            continue
        if not blocks or blocks[-1].get("closed"):
            continue
        row = _ROW_RE.match(line)
        if row:
            blocks[-1]["shots"].append({"size": row.group(2).strip(), "angle": row.group(3).strip().strip("-") or None,
                                        "sec": float(row.group(4)), "action": row.group(5).replace("\\|", "|").strip()})
        elif line.strip() and not line.startswith("|"):
            blocks[-1]["script"].append(re.sub(r"^\\(#{1,6}(?=\s|$)|\|)", r"\1", line.strip()))   # 제목(#)·표(|) 이스케이프 제거
    sections = []
    for i, s in enumerate(fm["sections"]):
        block = blocks[i] if i < len(blocks) and blocks[i]["label"] == s["label"] else {"script": [], "shots": []}
        sections.append({"idx": i, "label": s["label"], **({"sub": s["sub"]} if s.get("sub") else {}),
                         "start": float(s["start"]), "end": float(s["end"]),
                         "script": "\n".join(block["script"]), "shots": block["shots"]})
    return {"fm": fm, "title": fm.get("title") or path.stem, "sections": sections}


def speech_rate(category: str | None) -> dict:
    """ref 영상 자막의 초당 글자 수 중앙값. 말이 거의 없는(sparse) 영상은 뺀다."""
    rates = []
    if category:
        try:
            refs = features.load_videos(category)
        except ScriptError:
            refs = []
        for r in refs:
            try:
                raw = fetch.load_raw(r["id"])
            except ScriptError:
                continue
            cps, sparse = fetch.speech_density((raw.get("transcript") or {}).get("segments") or [],
                                               (raw.get("meta") or {}).get("duration"))
            if cps and not sparse:
                rates.append(cps)
    if not rates:
        return {"cps": DEFAULT_CPS, "basis": 0}
    return {"cps": round(statistics.median(rates), 1), "basis": len(rates)}


def video_format(target_sec: float) -> str:
    return "shorts" if target_sec <= fetch.SHORTS_MAX_SEC else "long"


def conti_path(draft: Path) -> Path:
    return draft.with_name(f"{draft.stem}{CONTI_SUFFIX}.md")


def safe_draft_path(value: Any, *, conti_file: bool | None = None) -> Path:
    """작업 폴더의 drafts/ 안 .md 파일만 허용한다(상대 경로 → 절대 경로).
    conti_file: True면 콘티 파일만, False면 기획안만, None이면 둘 다."""
    if not isinstance(value, str) or not value:
        raise ScriptError("bad_request", "path가 필요합니다")
    drafts = (work_root() / "drafts").resolve()
    try:
        target = (work_root() / value).resolve()
    except (OSError, ValueError) as e:
        raise ScriptError("bad_path", f"잘못된 경로입니다: {value}") from e
    if not target.is_relative_to(drafts) or target.suffix.lower() != ".md" or "\x00" in value:
        raise ScriptError("bad_path", f"drafts 폴더 안의 .md 파일만 쓸 수 있습니다: {value}")
    if conti_file is not None and target.stem.endswith(CONTI_SUFFIX) != conti_file:
        raise ScriptError("bad_path", f"{'콘티' if conti_file else '기획안'} 파일이 아닙니다: {value}")
    return target


def context(draft: str | Path) -> dict:
    path = _resolve(draft)
    d = parse_draft(path)
    target = d["sections"][-1]["end"]
    rate = speech_rate(d["fm"].get("category"))
    return {
        "draft": _rel(path),
        "conti": _rel(conti_path(path)),
        "conti_exists": conti_path(path).exists(),
        "title": d["title"],
        "category": d["fm"].get("category"),
        "format": video_format(target),
        "target_sec": target,
        "hook_type": d["fm"].get("hook_type"),
        "narrative_type": d["fm"].get("narrative_type"),
        "script_only": d["fm"].get("status") == "script",
        "speech_cps": rate["cps"],
        "speech_basis": rate["basis"],
        "sections": [{**s, "max_chars": int(rate["cps"] * (s["end"] - s["start"]))} for s in d["sections"]],
        "profile": motif._profile(),
        "shot_sizes": SHOT_SIZES,
    }


# --- 검증 · 경고 ---------------------------------------------------------------------

def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_conti", f"콘티가 형식에 맞지 않습니다: {message}")


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE)


_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_ID_RE = re.compile(r"[a-z0-9]{8}")


def new_cut_id() -> str:
    """컷을 번호와 무관하게 가리키는 8자 id (소문자+숫자)."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(8))


FRAMES_PREFIX = "workspace/frames/"


def ref_auto_of(value: Any) -> dict | None:
    """컷의 ref_auto(Claude가 내용으로 고른 사진) 정리. 형식이 맞으면 {"frame", "match": "content"}, 아니면 None.
    frame은 작업 폴더 기준 workspace/frames/ 아래 상대 경로(슬래시, `..`·드라이브 없음)만 받는다."""
    if not isinstance(value, dict) or value.get("match") != "content":
        return None
    frame = value.get("frame")
    if not isinstance(frame, str) or not frame.startswith(FRAMES_PREFIX) or "\\" in frame or ":" in frame:
        return None
    if any(part in ("", ".", "..") for part in frame.split("/")):
        return None
    return {"frame": frame, "match": "content"}


def validate(data: dict, sections: list[dict]) -> tuple[list[dict], list[dict]]:
    """(script, cuts) — script에 섹션 시간을 붙이고, cuts에 번호·시작·끝을 붙인다."""
    script = data.get("script") or []
    if len(script) != len(sections):
        raise _invalid(f"script는 섹션마다 하나씩 {len(sections)}개여야 합니다 (지금 {len(script)}개)")
    cuts = data.get("cuts") or []
    if not cuts:
        raise _invalid("cuts가 비어 있습니다")
    out_script = [{"label": s["label"], "start": s["start"], "end": s["end"], "text": str(t.get("text") or "").strip()}
                  for s, t in zip(sections, script)]
    out_cuts, prev_section, seen_ids = [], 0, set()
    for i, c in enumerate(cuts, 1):
        sec_idx = c.get("section")
        if not isinstance(sec_idx, int) or not 0 <= sec_idx < len(sections):
            raise _invalid(f"컷 {i}의 section={sec_idx!r} (0~{len(sections) - 1})")
        if sec_idx < prev_section:
            raise _invalid(f"컷 {i}: 섹션 순서가 거꾸로입니다")
        prev_section = sec_idx
        if c.get("size") not in SHOT_SIZES:
            raise _invalid(f"컷 {i}의 샷 크기 {c.get('size')!r} (허용: {', '.join(SHOT_SIZES)})")
        try:
            sec = float(c.get("sec"))
        except (TypeError, ValueError):
            sec = 0.0
        if sec <= 0:
            raise _invalid(f"컷 {i}의 sec가 0보다 커야 합니다")
        for field in ("action", "guide"):
            if not str(c.get(field) or "").strip():
                raise _invalid(f"컷 {i}의 {field}가 비어 있습니다")
        cut_id = c.get("id")
        if not isinstance(cut_id, str) or not _ID_RE.fullmatch(cut_id) or cut_id in seen_ids:
            cut_id = new_cut_id()
            while cut_id in seen_ids:
                cut_id = new_cut_id()
        seen_ids.add(cut_id)
        out_cuts.append({"n": i, "id": cut_id, "section": sec_idx, "size": c["size"], "angle": c.get("angle") or None, "sec": sec,
                         **{f: str(c.get(f) or "").strip() for f in ("action", "line", "caption", "sfx", "guide")}})
        pinned = c.get("ref_frame")
        if isinstance(pinned, str) and pinned.strip():
            out_cuts[-1]["ref_frame"] = pinned.strip()
        auto = ref_auto_of(c.get("ref_auto"))
        if auto:
            out_cuts[-1]["ref_auto"] = auto
    for s in sections:
        mine = [c for c in out_cuts if c["section"] == s["idx"]]
        total, length = sum(c["sec"] for c in mine), s["end"] - s["start"]
        if not mine:
            raise _invalid(f"섹션 {s['idx']}({s['label']})에 컷이 없습니다")
        if abs(total - length) > SEC_TOLERANCE:
            raise _invalid(f"섹션 {s['idx']}({s['label']}) 컷 길이 합 {total:g}초 ≠ 섹션 길이 {length:g}초")
        t = s["start"]
        for c in mine:                                   # 반올림 오차는 섹션 끝에 맞춰 흡수
            c["start"], t = round(t, 2), t + c["sec"]
            c["end"] = round(t, 2)
        mine[-1]["end"] = s["end"]
    return out_script, out_cuts


def warnings(script: list[dict], cuts: list[dict], cps: float) -> list[dict]:
    out = []
    for c in cuts:
        chars, fits = len(_norm(c["line"])), int(cps * c["sec"])
        if chars > cps * c["sec"] * OVERFLOW_SLACK + 1:
            out.append({"kind": "line_too_long", "cut": c["n"], "chars": chars, "fits": fits,
                        "message": f"컷 {c['n']} 대사 {chars}자 — {c['sec']:g}초에는 약 {fits}자"})
    for i, s in enumerate(script):
        joined = "".join(_norm(c["line"]) for c in cuts if c["section"] == i)
        if joined != _norm(s["text"]):
            out.append({"kind": "script_mismatch", "section": i,
                        "message": f"섹션 {i}({s['label']}) 컷 대사를 이으면 전체 대사와 다릅니다"})
    return out


# --- 참고 프레임 ---------------------------------------------------------------------

def _section_at(sections: list[dict], t: float) -> str | None:
    for s in sections:
        if s["start"] <= t < s["end"]:
            return s["label"]
    return None


def frame_pool(category: str | None) -> list[dict]:
    """ref 대표 프레임 후보. 프레임별 라벨(workspace/shots)이 있으면 exact, 없으면 섹션별 대표 샷 크기로 approx.
    라벨의 한 줄 설명(desc)·앵글도 싣는다(없으면 None)."""
    if not category:
        return []
    try:
        refs = features.load_videos(category)
    except ScriptError:
        return []
    pool = []
    for r in refs:
        fm, vid = r["fm"], r["id"]
        labels_path = workspace_dir("shots", f"{vid}.json")
        if labels_path.exists():
            # 저장된 section은 분류 시점 값이라 라벨 교정 뒤에는 낡는다 → 현재 sections로 장면 중간점에서 다시 구한다
            # 컷 json이 깨졌거나 시간이 숫자가 아니면 저장된 section을 그대로 쓴다
            try:
                mid = {sc["frame"]: (float(sc["start"]) + float(sc["end"])) / 2
                       for sc in ((load_cuts(vid) or {}).get("scenes") or [])
                       if sc.get("frame") and "start" in sc and "end" in sc}
            except (ValueError, TypeError, AttributeError, OSError):
                mid = {}
            for lab in json.loads(labels_path.read_text("utf-8")).get("labels") or []:
                t = mid.get(lab["frame"])
                current = _section_at(fm.get("sections") or [], t) if t is not None else None
                desc = lab.get("desc")
                pool.append({"frame": lab["frame"], "size": lab["size"], "section": current or lab.get("section"),
                             "video": vid, "title": fm.get("title"), "match": "exact", "angle": lab.get("angle"),
                             "desc": desc if isinstance(desc, str) and desc.strip() else None})
            continue
        by_section = (fm.get("shots") or {}).get("by_section") or {}
        for scene in (load_cuts(vid) or {}).get("scenes") or []:
            label = _section_at(fm.get("sections") or [], (scene["start"] + scene["end"]) / 2)
            if scene.get("frame") and by_section.get(label):
                pool.append({"frame": scene["frame"], "size": by_section[label], "section": label,
                             "video": vid, "title": fm.get("title"), "match": "approx", "angle": None, "desc": None})
    return [p for p in pool if (work_root() / p["frame"]).exists()]


def match_frames(cuts: list[dict], sections: list[dict], pool: list[dict],
                 used: dict[str, int] | None = None) -> dict[int, dict]:
    """컷마다 참고 프레임 하나. 같은 크기+섹션 → 같은 크기 순. 각 단계에서 exact가 approx보다 먼저, 그다음 덜 쓴 프레임.
    used: 이미 다른 컷이 쓴 프레임 → 횟수(고정·내용 맞춤 사진). 주면 그 수부터 센다(넘긴 dict는 바꾸지 않는다)."""
    used = dict(used or {})
    out = {}
    for c in cuts:
        label = sections[c["section"]]["label"]
        tiers = [lambda p: p["size"] == c["size"] and p["section"] == label, lambda p: p["size"] == c["size"]]
        for tier in tiers:
            cands = [p for p in pool if tier(p)]
            if cands:
                best = min(cands, key=lambda p: (p["match"] != "exact", used.get(p["frame"], 0), p["frame"]))
                used[best["frame"]] = used.get(best["frame"], 0) + 1
                out[c["n"]] = best
                break
    return out


# --- 도식 스케치 ----------------------------------------------------------------------

def sketch(size: str, vertical: bool) -> str:
    """샷 크기 도식. 선은 본문 보조색, 면은 격자색. 9:16(쇼츠) 또는 16:9."""
    w, h = (90, 160) if vertical else (160, 90)
    cx, cy, m = w / 2, h / 2, min(w, h)
    line = 'fill="none" stroke="var(--ink2)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"'
    fill = 'fill="var(--grid)" stroke="var(--ink2)" stroke-width="1.6"'
    parts: list[str] = []
    if size == "익스트림 클로즈업":
        for k in range(5):                                      # 화면을 채운 면발
            x = w * (0.18 + k * 0.16)
            parts.append(f'<path d="M{x:.1f} 0 C{x - m * .12:.1f} {h * .35:.1f} {x + m * .12:.1f} {h * .65:.1f} '
                         f'{x:.1f} {h}" {line}/>')
    elif size == "클로즈업":
        r = m * 0.42
        parts += [f'<ellipse cx="{cx}" cy="{cy + r * .25:.1f}" rx="{r:.1f}" ry="{r * .62:.1f}" {fill}/>',
                  f'<ellipse cx="{cx}" cy="{cy + r * .05:.1f}" rx="{r * .8:.1f}" ry="{r * .4:.1f}" {line}/>',
                  f'<path d="M{cx + r * .2:.1f} {cy - r * 1.3:.1f} L{cx + r * .05:.1f} {cy + r * .1:.1f} '
                  f'M{cx + r * .45:.1f} {cy - r * 1.25:.1f} L{cx + r * .2:.1f} {cy + r * .1:.1f}" {line}/>']
    elif size in ("바스트", "미디엄"):
        head = m * (0.17 if size == "바스트" else 0.11)
        hy = h * (0.42 if size == "바스트" else 0.36)
        parts += [f'<circle cx="{cx}" cy="{hy:.1f}" r="{head:.1f}" {fill}/>',
                  f'<path d="M{cx - head * 2.6:.1f} {h} C{cx - head * 2.6:.1f} {hy + head * 1.6:.1f} '
                  f'{cx + head * 2.6:.1f} {hy + head * 1.6:.1f} {cx + head * 2.6:.1f} {h}" {fill}/>']
        if size == "미디엄":
            ty = h * 0.78
            parts.append(f'<path d="M0 {ty:.1f} H{w}" {line}/>'
                         f'<ellipse cx="{cx - m * .18:.1f}" cy="{ty - 3:.1f}" rx="{m * .1:.1f}" ry="{m * .04:.1f}" {line}/>'
                         f'<ellipse cx="{cx + m * .18:.1f}" cy="{ty - 3:.1f}" rx="{m * .1:.1f}" ry="{m * .04:.1f}" {line}/>')
    elif size == "와이드":
        fy = h * 0.68
        parts += [f'<path d="M0 {fy:.1f} H{w} M{w * .15:.1f} {h * .2:.1f} V{fy:.1f} M{w * .85:.1f} {h * .2:.1f} V{fy:.1f}" {line}/>',
                  f'<circle cx="{cx}" cy="{fy - m * .2:.1f}" r="{m * .045:.1f}" {fill}/>',
                  f'<path d="M{cx - m * .07:.1f} {fy:.1f} V{fy - m * .1:.1f} H{cx + m * .07:.1f} V{fy:.1f}" {fill}/>',
                  f'<rect x="{cx + m * .1:.1f}" y="{fy - m * .07:.1f}" width="{m * .22:.1f}" height="{m * .07:.1f}" {line}/>']
    elif size == "탑뷰":
        r = m * 0.17
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x, y = cx + dx * r * 1.25, cy + dy * r * (1.25 if not vertical else 1.6)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" {fill}/><circle cx="{x:.1f}" cy="{y:.1f}" '
                         f'r="{r * .55:.1f}" {line}/>')
    elif size == "POV":
        ty = h * 0.45
        parts += [f'<path d="M0 {ty:.1f} H{w}" {line}/>',
                  f'<ellipse cx="{cx}" cy="{ty + m * .12:.1f}" rx="{m * .22:.1f}" ry="{m * .08:.1f}" {line}/>',
                  f'<path d="M{w * .05:.1f} {h} C{w * .12:.1f} {h * .8:.1f} {w * .3:.1f} {h * .72:.1f} {w * .36:.1f} {h * .78:.1f} '
                  f'L{w * .3:.1f} {h}Z" {fill}/>',
                  f'<path d="M{w * .95:.1f} {h} C{w * .88:.1f} {h * .8:.1f} {w * .7:.1f} {h * .72:.1f} {w * .64:.1f} {h * .78:.1f} '
                  f'L{w * .7:.1f} {h}Z" {fill}/>']
    else:  # 인서트
        bw, bh = m * 0.62, m * 0.42
        x, y = cx - bw / 2, cy - bh / 2
        parts += [f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="3" {fill}/>']
        parts += [f'<path d="M{x + bw * .15:.1f} {y + bh * k:.1f} H{x + bw * (.85 if k < .6 else .55):.1f}" {line}/>'
                  for k in (.35, .55, .75)]
    return (f'<svg class="sk" viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(size)} 도식">'
            f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="4" fill="var(--surface)" stroke="var(--axis)"/>'
            + "".join(parts) + "</svg>")


# --- 저장 · 렌더 ----------------------------------------------------------------------

def _cell(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ") or "-"


def render_md(fm: dict, memo: str) -> str:
    cuts, script = fm["cuts"], fm["script"]
    avg = round(fm["target_sec"] / len(cuts), 2)
    out = [f"# {fm['title']} — 콘티", "", "## 개요",
           f"- 기획안: [{Path(fm['draft']).name}]({Path(fm['draft']).name})",
           f"- 길이: {fmt_time(fm['target_sec'])} · 컷 {len(cuts)}개 (평균 {avg}초) · {fm['format']}",
           f"- 말 속도 기준: 초당 {fm['speech_cps']}자",
           f"- 스토리보드: [{Path(fm['html']).name}]({Path(fm['html']).name})",
           "- 고치려면 위 프런트매터의 `script`·`cuts`를 수정한 뒤 `/yt-coach:yt-conti <이 파일>`로 HTML을 다시 그린다",
           "", "## 전체 대사"]
    for i, s in enumerate(script):
        out += ["", f"### {fmt_time(s['start'])}–{fmt_time(s['end'])} · {s['label']}", s["text"] or "(대사 없음)"]
    out += ["", "## 콘티", "", "| # | 시간 | 섹션 | 샷 | 대사 | 자막 | 효과음 | 가이드 |", "|---|---|---|---|---|---|---|---|"]
    for c in cuts:
        shot = f"{c['size']}" + (f" · {c['angle']}" if c.get("angle") else "")
        out.append(f"| {c['n']} | {fmt_time(c['start'])} ({c['sec']:g}초) | {script[c['section']]['label']} | "
                   f"{_cell(shot)}<br>{_cell(c['action'])} | {_cell(c['line'])} | {_cell(c['caption'])} | "
                   f"{_cell(c['sfx'])} | {_cell(c['guide'])} |")
    out += ["", "## 내 메모", memo]
    return "\n".join(out).rstrip() + "\n"


CSS = """
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--warn:#a35a00;%s}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;
--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--warn:#f0b35a;%s}}
:root[data-theme="dark"]{--page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;
--axis:#383835;--warn:#f0b35a;%s}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.6 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:32px 0 12px}.sub{color:var(--ink2);margin:0}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0;font-size:13px;color:var(--ink2)}
.legend span{display:inline-flex;align-items:center;gap:6px}.sw{width:12px;height:12px;border-radius:3px;display:inline-block}
.script{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:8px 16px}
.script div{display:grid;grid-template-columns:110px 1fr;gap:12px;padding:8px 0;border-bottom:1px solid var(--grid)}
.script div:last-child{border-bottom:0}.script b{font-weight:600;color:var(--ink2);font-size:13px;white-space:nowrap}
.script p{margin:0;white-space:pre-wrap}
.board{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.cut{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:10px;break-inside:avoid;
border-top:4px solid var(--sc)}
.cut header{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:13px;color:var(--ink2)}
.cut header b{color:var(--ink);font-size:15px}.pics{display:flex;gap:6px;margin:8px 0}
.pics figure{margin:0;flex:1;min-width:0}.pics figcaption{font-size:11px;color:var(--muted);line-height:1.3;margin-top:2px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sk,.ref{width:100%%;display:block;border-radius:4px}.ref{aspect-ratio:var(--ar);object-fit:cover;border:1px solid var(--axis)}
.noref{aspect-ratio:var(--ar);border:1px dashed var(--axis);border-radius:4px;display:flex;align-items:center;
justify-content:center;font-size:11px;color:var(--muted);text-align:center;padding:4px}
dl{margin:0;display:grid;grid-template-columns:44px 1fr;gap:2px 8px;font-size:13px}dt{color:var(--muted)}dd{margin:0}
dd.line{font-size:15px;font-weight:600}.warn{color:var(--warn);font-size:12px;margin-top:6px}
table{border-collapse:collapse;width:100%%;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);
vertical-align:top}th{color:var(--ink2);white-space:nowrap}details{margin-top:24px}summary{cursor:pointer;color:var(--ink2)}
.tbl{overflow-x:auto;background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:8px;margin-top:8px}
@media print{body{background:#fff}main{max-width:none;padding:0}.board{grid-template-columns:repeat(3,1fr)}
details{display:none}.cut{border-color:#ccc}}
"""


def _esc(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _data_uri(rel: str) -> str | None:
    path = work_root() / rel
    if not path.exists():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


REF_TAGS = {"pinned": "참고(고정)", "content": "참고", "composition": "구도만 참고"}


def cut_refs(fm: dict) -> dict[int, dict]:
    """컷 번호 → 참고 프레임 {frame, title, match, desc}. 순서: 사용자가 고정한 프레임(ref_frame, `pinned`) →
    Claude가 내용으로 고른 프레임(ref_auto, `content`) → 샷 크기(+섹션) 맞춤(`composition`). 파일이 없으면 다음으로.
    desc는 그 프레임의 한 줄 설명(레퍼런스 라벨에 있을 때, 없으면 None). Claude는 부르지 않는다."""
    pool = frame_pool(fm.get("category"))
    by_frame = {p["frame"]: p for p in pool}
    root = work_root()

    def ref(frame: str, match: str) -> dict:
        known = by_frame.get(frame) or {}
        return {"frame": frame, "title": known.get("title") or Path(frame).name, "match": match, "desc": known.get("desc")}

    refs: dict[int, dict] = {}
    rest = []
    for c in fm["cuts"]:
        pinned = c.get("ref_frame")
        auto = (ref_auto_of(c.get("ref_auto")) or {}).get("frame")
        if pinned and (root / pinned).exists():
            refs[c["n"]] = {**ref(pinned, "pinned"), "title": Path(pinned).name}
        elif auto and (root / auto).exists():
            refs[c["n"]] = ref(auto, "content")
        else:
            rest.append(c)
    sections = [{"label": s["label"]} for s in fm["script"]]
    taken: dict[str, int] = {}
    for r in refs.values():                              # 고정·내용 맞춤이 쓴 사진은 구도 맞춤에서 뒤로
        taken[r["frame"]] = taken.get(r["frame"], 0) + 1
    for n, m in match_frames(rest, sections, pool, taken).items():
        refs[n] = {"frame": m["frame"], "title": m.get("title"), "match": "composition", "desc": m.get("desc")}
    return refs


def ref_tag(match: str) -> str:
    """참고 사진 표시 이름: 고정 '참고(고정)', 내용 맞춤 '참고', 구도 맞춤 '구도만 참고'."""
    return REF_TAGS.get(match, REF_TAGS["composition"])


def ref_figure(ref: dict | None) -> str:
    """참고 프레임 한 칸(이미지는 base64로 넣는다). 없으면 빈 칸 안내. 아래 줄은 '참고 · <설명>'(설명이 없으면 제목)."""
    uri = _data_uri(ref["frame"]) if ref else None
    if not uri or not ref:
        return ('<figure><div class="noref">같은 샷의 레퍼런스 프레임 없음</div>'
                '<figcaption>&nbsp;</figcaption></figure>')
    title = _esc(ref["title"])
    return (f'<figure><img class="ref" src="{uri}" alt="참고 프레임: {title}">'
            f'<figcaption title="{title}">{ref_tag(ref["match"])} · {_esc(ref.get("desc") or ref["title"])}</figcaption></figure>')


def render_html(fm: dict) -> str:
    script, cuts = fm["script"], fm["cuts"]
    vertical = fm["format"] == "shorts"
    refs = cut_refs(fm)
    warn = {w["cut"]: w for w in warnings(script, cuts, fm["speech_cps"]) if w["kind"] == "line_too_long"}
    color = lambda label: f"var(--s{LABELS.index(label) + 1})" if label in LABELS else "var(--muted)"
    used = list(dict.fromkeys(s["label"] for s in script))

    head = (f"<h1>{_esc(fm['title'])}</h1><p class=\"sub\">콘티 · {fmt_time(fm['target_sec'])} · 컷 {len(cuts)}개 · "
            f"평균 {round(fm['target_sec'] / len(cuts), 2)}초 · 말 속도 기준 초당 {fm['speech_cps']}자"
            + (f" · 대사 길이 경고 {len(warn)}개" if warn else "") + "</p>"
            + '<div class="legend">' + "".join(f'<span><i class="sw" style="background:{color(l)}"></i>{_esc(l)}</span>'
                                               for l in used) + "</div>")
    full = '<h2>전체 대사</h2><div class="script">' + "".join(
        f'<div><b>{fmt_time(s["start"])}–{fmt_time(s["end"])} {_esc(s["label"])}</b><p>{_esc(s["text"]) or "<i>(대사 없음)</i>"}</p></div>'
        for s in script) + "</div>"

    cards = []
    for c in cuts:
        label = script[c["section"]]["label"]
        ref_html = ref_figure(refs.get(c["n"]))
        rows = [("화면", _esc(c["action"]), ""), ("대사", _esc(c["line"]) or "—", "line"), ("자막", _esc(c["caption"]) or "—", ""),
                ("효과음", _esc(c["sfx"]) or "—", ""), ("가이드", _esc(c["guide"]), "")]
        w = warn.get(c["n"])
        cards.append(
            f'<article class="cut" style="--sc:{color(label)}"><header><b>#{c["n"]}</b>'
            f'<span>{fmt_time(c["start"])} · {c["sec"]:g}초 · {_esc(label)}</span></header>'
            f'<div class="pics"><figure>{sketch(c["size"], vertical)}<figcaption>{_esc(c["size"])}'
            f'{" · " + _esc(c["angle"]) if c.get("angle") else ""}</figcaption></figure>{ref_html}</div>'
            + "<dl>" + "".join(f'<dt>{k}</dt><dd class="{cls}">{v}</dd>' for k, v, cls in rows) + "</dl>"
            + (f'<p class="warn">⚠ 대사 {w["chars"]}자 — {c["sec"]:g}초에는 약 {w["fits"]}자</p>' if w else "")
            + "</article>")
    board = '<h2>스토리보드</h2><div class="board">' + "".join(cards) + "</div>"
    table = ('<details><summary>표로 보기</summary><div class="tbl"><table><thead><tr><th>#</th><th>시간</th><th>섹션</th>'
             '<th>샷</th><th>화면</th><th>대사</th><th>자막</th><th>효과음</th><th>가이드</th></tr></thead><tbody>'
             + "".join(f'<tr><td>{c["n"]}</td><td>{fmt_time(c["start"])} ({c["sec"]:g}초)</td>'
                       f'<td>{_esc(script[c["section"]]["label"])}</td><td>{_esc(c["size"])}</td><td>{_esc(c["action"])}</td>'
                       f'<td>{_esc(c["line"])}</td><td>{_esc(c["caption"])}</td><td>{_esc(c["sfx"])}</td>'
                       f'<td>{_esc(c["guide"])}</td></tr>' for c in cuts)
             + "</tbody></table></div></details>")
    loose = sum(1 for r in refs.values() if r["match"] == "composition")
    if not loose:
        why = ""
    elif any(p.get("desc") for p in frame_pool(fm.get("category"))):
        why = f" '구도만 참고' {loose}장은 장면 내용에 맞는 사진이 없어 샷 크기만 맞췄습니다."
    else:
        why = (f" '구도만 참고' {loose}장은 샷 크기만 맞춘 사진이라 장면 내용과 다를 수 있습니다. "
               "레퍼런스 사진 설명을 만들고 사진을 다시 고르면 장면에 맞는 사진이 붙습니다.")
    note = ('<p class="sub" style="margin-top:24px;font-size:12px">'
            "'참고'는 장면 내용에 맞춰 고른 레퍼런스 사진입니다. 도식은 샷 크기를 단순화한 그림입니다." + why + "</p>")
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_esc(fm['title'])} 콘티</title><style>{CSS % (_vars(0), _vars(1), _vars(1))}"
            f"main{{--ar:{'9/16' if vertical else '16/9'}}}</style></head>"
            f"<body><main>{head}{full}{board}{table}{note}</main></body></html>")


def _write_html(md_path: Path, fm: dict) -> Path:
    out = md_path.with_suffix(".html")
    out.write_text(render_html(fm), encoding="utf-8")
    return out


def save(draft: str | Path, conti_json: Path, force: bool = False, now: float | None = None) -> dict:
    with HISTORY_LOCK:
        return _save_locked(draft, conti_json, force, now)


def _trusted_ref_auto(cuts: list[dict], category: str | None) -> list[dict]:
    """저장할 콘티 JSON의 ref_auto 중 같은 카테고리의 설명 있는 후보 프레임만 남긴다(CLI에서 Claude가 적어 낸 값은
    검증 없이 믿지 않는다. 화면의 만들기 작업이 고른 값은 후보에서 나왔으므로 그대로 남는다)."""
    from scripts import conti_refs          # conti_refs가 이 모듈을 쓰므로 함수 안에서 불러온다

    if not any(c.get("ref_auto") for c in cuts):
        return cuts
    known = {c["frame"] for c in conti_refs.candidates(category)}
    return [{k: v for k, v in c.items() if k != "ref_auto"} if c.get("ref_auto") and c["ref_auto"]["frame"] not in known
            else c for c in cuts]


def _save_locked(draft: str | Path, conti_json: Path, force: bool, now: float | None) -> dict:
    from scripts import conti_history       # conti_history가 이 모듈을 쓰므로 함수 안에서 불러온다

    draft_path = _resolve(draft)
    d = parse_draft(draft_path)
    target = conti_path(draft_path)
    if target.exists() and not force:
        raise ScriptError("conti_exists", f"이미 콘티가 있습니다: {_rel(target)} (다시 만들려면 --force, 내 메모는 보존)")
    try:
        data = json.loads(Path(conti_json).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("invalid_conti", f"콘티 JSON을 읽을 수 없습니다: {e}") from e
    script, cuts = validate(data, d["sections"])
    cuts = _trusted_ref_auto(cuts, d["fm"].get("category"))
    rate = speech_rate(d["fm"].get("category"))
    total = d["sections"][-1]["end"]
    fm = {"date": date.today().isoformat(), "draft": _rel(draft_path), "html": _rel(target.with_suffix(".html")),
          "category": d["fm"].get("category"), "title": d["title"], "format": video_format(total),
          "target_sec": total, "speech_cps": rate["cps"], "script": script, "cuts": cuts,
          "schema_version": SCHEMA_VERSION}
    skipped = None
    if target.exists():                                  # 다시 만들기: 덮어쓰기 전 상태를 이력에 남긴다
        try:
            conti_history.record(target, _load_conti(target), "before_recreate", now=now)
        except ScriptError as e:
            if e.code != "invalid_conti":
                raise
            skipped = e.message                          # 깨진 파일은 상태를 읽을 수 없어 이력만 건너뛴다(다시 만들기는 계속)
    memo = _existing_memo(target)
    target.write_bytes(refstore.join_frontmatter(fm, render_md(fm, memo)).encode("utf-8"))
    html_path = _write_html(target, fm)
    Path(conti_json).unlink(missing_ok=True)
    conti_history.record(target, fm, "created", now=now)
    return {"status": "saved", "path": _rel(target), "html": _rel(html_path), "abs_html": str(html_path),
            "cuts": len(cuts), "speech_cps": rate["cps"], "warnings": warnings(script, cuts, rate["cps"]),
            **({"history_skipped": skipped} if skipped else {})}


def render(conti: str | Path) -> dict:
    path = _resolve(conti)
    if not path.exists():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {_rel(path)}")
    _, fm, _ = ensure_ids_file(read_conti(path))         # id가 없던 옛 파일은 먼저 저장해 HTML과 파일의 id를 맞춘다
    html_path = _write_html(path, fm)
    return {"status": "rendered", "html": _rel(html_path), "abs_html": str(html_path), "cuts": len(fm["cuts"]),
            "warnings": warnings(fm["script"], fm["cuts"], fm["speech_cps"])}


# --- 컷 하나 편집 · 장면 후보 ----------------------------------------------------------

def content_hash(path: str | Path) -> str:
    """파일 바이트 sha256. 화면이 불러온 뒤 파일이 밖에서 바뀌었는지 확인하는 데 쓴다."""
    return bytes_hash(_resolve(path).read_bytes())


def bytes_hash(raw: bytes) -> str:
    """이미 읽은(또는 막 쓴) 파일 바이트의 content_hash."""
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ContiFile:
    """콘티 md를 한 번 읽은 결과. 한 요청 안에서 같은 파일을 다시 읽고 파싱하지 않도록 넘겨 쓴다.
    raw = 디스크 바이트, stored = 프런트매터 그대로(검증 전), body = 본문('## 내 메모' 포함)."""
    path: Path
    raw: bytes
    stored: dict
    body: str


def read_conti(path: Path) -> ContiFile:
    if not path.exists():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {_rel(path)}")
    return parse_conti(path, path.read_bytes())


def parse_conti(path: Path, raw: bytes) -> ContiFile:
    stored, body = refstore.parse_md(raw, path)
    return ContiFile(path, raw, stored, body)


def validated(stored: dict) -> dict:
    """프런트매터 → 검증하고 컷 번호·시간을 붙인 fm."""
    if not stored.get("cuts") or not stored.get("script"):
        raise ScriptError("invalid_conti", "콘티 프런트매터에 script·cuts가 없습니다")
    sections = [{"idx": i, "label": s["label"], "start": float(s["start"]), "end": float(s["end"])}
                for i, s in enumerate(stored["script"])]
    script, cuts = validate({"script": stored["script"], "cuts": stored["cuts"]}, sections)
    return {**stored, "script": script, "cuts": cuts}


def _load_conti(path: Path) -> dict:
    return validated(read_conti(path).stored)


def _find_cut(cuts: list[dict], n: int) -> dict:
    cut = next((c for c in cuts if c["n"] == n), None)
    if cut is None:
        raise ScriptError("cut_not_found", f"컷 {n}번이 없습니다 (1~{len(cuts)})")
    return cut


MIN_CUT_SEC, MAX_CUT_SEC, SEC_STEP = 0.5, 600.0, 0.5   # 합친 장면은 몇 분이 될 수 있다
CUT_DEFAULTS = {"size": "미디엄", "sec": 4, "action": "새 컷", "guide": "촬영 지시를 적어 주세요"}
UPDATABLE_FIELDS = (*EDITABLE_FIELDS, "sec", "size", "action")
PICK_FIELDS = ("action", "guide", "size")                # 내용 맞춤 사진(ref_auto)을 고를 때 본 필드
LAST_CUT_MESSAGE = "섹션에 컷이 하나는 있어야 해요"


def _check_sec(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ScriptError("invalid_edit", "컷 길이는 숫자여야 합니다")
    try:
        sec = float(value)
    except ValueError as e:
        raise ScriptError("invalid_edit", "컷 길이는 숫자여야 합니다") from e
    if not MIN_CUT_SEC <= sec <= MAX_CUT_SEC or (sec / SEC_STEP) != round(sec / SEC_STEP):
        raise ScriptError("invalid_edit", f"컷 길이는 {MIN_CUT_SEC:g}~{MAX_CUT_SEC:g}초, {SEC_STEP:g}초 단위여야 합니다")
    return sec


def _check_edit(fields: dict) -> dict:
    unknown = sorted(set(fields) - set(UPDATABLE_FIELDS))
    if unknown:
        raise ScriptError("invalid_edit", f"고칠 수 없는 필드입니다: {', '.join(unknown)} (허용: {', '.join(UPDATABLE_FIELDS)})")
    edit: dict[str, Any] = {k: ("" if v is None else str(v)).strip() for k, v in fields.items() if k != "sec"}
    if "sec" in fields:
        edit["sec"] = _check_sec(fields["sec"])
    if "guide" in edit and not edit["guide"]:
        raise ScriptError("invalid_edit", "가이드는 비울 수 없습니다")
    if "action" in edit and not edit["action"]:
        raise ScriptError("invalid_edit", "동작은 비울 수 없습니다")
    if "size" in edit and edit["size"] not in SHOT_SIZES:
        raise ScriptError("invalid_edit", f"샷 크기 {edit['size']!r}은 쓸 수 없습니다 (허용: {', '.join(SHOT_SIZES)})")
    ref = edit.get("ref_frame")
    if ref:
        target = _resolve(ref)
        try:
            rel = target.resolve().relative_to(work_root().resolve()).as_posix()
        except ValueError as e:
            raise ScriptError("invalid_edit", f"작업 폴더 밖의 파일은 고정할 수 없습니다: {ref}") from e
        if not target.is_file():
            raise ScriptError("invalid_edit", f"참고 프레임 파일이 없습니다: {ref}")
        edit["ref_frame"] = rel
    return edit


def _write_conti(path: Path, raw: bytes, fm: dict) -> Path:
    """fm으로 md·html을 다시 쓴다(메모는 지금 파일에서 읽는다). 새 HTML 경로를 돌려준다."""
    return _write_conti_file(ContiFile(path, raw, {}, "## 내 메모\n" + _existing_memo(path)), fm)[1]


def _write_conti_file(cf: ContiFile, fm: dict) -> tuple[ContiFile, Path]:
    """fm으로 md·html을 다시 쓴다. '## 내 메모'와 줄바꿈 방식(CRLF/LF)은 이미 읽은 cf에서 가져와 보존한다.
    (방금 쓴 내용(stored = 쓴 fm), 새 HTML 경로)를 돌려준다."""
    fm["schema_version"] = SCHEMA_VERSION
    nl = "\r\n" if cf.raw.lstrip(b"\xef\xbb\xbf").startswith(b"---\r\n") else "\n"
    memo = memo_from_body(cf.body).replace("\r\n", "\n")
    body = render_md(fm, memo).replace("\n", nl)
    data = refstore.join_frontmatter(fm, body, nl).encode("utf-8")
    refstore.write_atomic(cf.path, data)
    return ContiFile(cf.path, data, fm, body), _write_html(cf.path, fm)


def _section_lines(fm: dict) -> list[list[str]]:
    return [[c.get("line") or "" for c in fm["cuts"] if c["section"] == i] for i in range(len(fm["script"]))]


def retime(fm: dict, rebuild: set[int] | None = None) -> dict:
    """컷 순서·길이(sec)대로 섹션 시간과 컷 n/start/end를 다시 계산한 새 fm. 섹션은 0에서 이어 붙는다.
    섹션 대사(text)는 rebuild에 든 섹션만 컷 line을 이어 다시 만든다(None이면 전부). 나머지는 손으로 고친 값을 둔다."""
    script, t = [], 0.0
    for i, s in enumerate(fm["script"]):
        mine = [c for c in fm["cuts"] if c["section"] == i]
        length = sum(float(c["sec"]) for c in mine)
        text = " ".join(c["line"] for c in mine if c.get("line")) if rebuild is None or i in rebuild else s.get("text") or ""
        script.append({"label": s["label"], "start": round(t, 2), "end": round(t + length, 2), "text": text})
        t += length
    sections = [{"idx": i, "label": s["label"], "start": s["start"], "end": s["end"]} for i, s in enumerate(script)]
    new_script, new_cuts = validate({"script": script, "cuts": fm["cuts"]}, sections)
    return {**fm, "script": new_script, "cuts": new_cuts}


def ensure_ids(path: str | Path) -> bool:
    """파일 컷에 id가 없으면 붙여 다시 쓴다(schema 3). 이미 모두 있으면 아무것도 안 하고 False."""
    return ensure_ids_file(read_conti(_resolve(path)))[2]


def ensure_ids_file(cf: ContiFile) -> tuple[ContiFile, dict, bool]:
    """이미 읽은 콘티로 ensure_ids. (지금 파일 내용, 검증한 fm, 다시 썼는지)."""
    loaded = validated(cf.stored)
    on_disk = [c.get("id") for c in cf.stored.get("cuts") or []]
    if cf.stored.get("schema_version", 0) >= SCHEMA_VERSION and on_disk == [c["id"] for c in loaded["cuts"]]:
        return cf, loaded, False
    written, _ = _write_conti_file(cf, loaded)
    return written, loaded, True


def update_cut(conti: str | Path, n: int, fields: dict, expected_hash: str | None = None,
               now: float | None = None) -> dict:
    """컷 하나의 필드를 고친다(`edit_cuts`의 update). 반환: edit_cuts 결과 + 고친 컷(`cut`)."""
    with HISTORY_LOCK:
        result, fm = _edit_locked(conti, {"op": "update", "n": n, "fields": fields}, expected_hash, now)
    return {**result, "cut": _find_cut(fm["cuts"], n)}


def edit_cuts_fm(conti: str | Path, op: dict, expected_hash: str | None, now: float | None = None
                 ) -> tuple[dict, dict]:
    """edit_cuts와 같고, 쓴 fm(검증·시간 계산 끝)도 함께 돌려준다. 화면 응답을 파일을 다시 읽지 않고 만들 때 쓴다."""
    with HISTORY_LOCK:
        return _edit_locked(conti, op, expected_hash, now)


def edit_cuts(conti: str | Path, op: dict, expected_hash: str | None, now: float | None = None) -> dict:
    """컷 구조 편집: insert / delete / move / duplicate / update. 섹션 시간은 컷 길이 합으로 다시 계산한다.
    move의 `to`는 컷을 뺀 뒤 결과 목록에서의 0부터 시작하는 최종 위치다. 예: 컷 [A,B,C,D]에서 A를
    `{"op": "move", "id": A, "to": 2}`로 옮기면 [B,C,A,D]. `section`을 생략하면 컷의 섹션을 유지하되 순서가
    거꾸로 되면 그 자리 앞 컷의 섹션(맨 앞이면 다음 컷의 섹션)을 따른다.
    insert의 `cut`은 INSERT_FIELDS만 받는다. 지운 컷을 되돌릴 때 `id`(형식이 맞고 파일에서 안 쓰면 그대로)와
    `angle`이 살아난다.
    반환: {"status", "path", "html", "abs_html", "hash", "cut_id"(새로 생기거나 고친 컷, 없으면 None), "warnings"}."""
    with HISTORY_LOCK:
        return _edit_locked(conti, op, expected_hash, now)[0]


def _cut_ref(cuts: list[dict], op: dict) -> int:
    """op의 id(우선) 또는 n이 가리키는 컷의 0부터 시작하는 위치."""
    if op.get("id") is not None:
        found = next((i for i, c in enumerate(cuts) if c["id"] == op["id"]), None)
        if found is None:
            raise ScriptError("cut_not_found", f"컷 {op['id']}이(가) 없습니다")
        return found
    n = op.get("n")
    if isinstance(n, bool) or not isinstance(n, int):
        raise ScriptError("invalid_edit", "id 또는 n으로 컷을 가리켜야 합니다")
    return cuts.index(_find_cut(cuts, n))


def _int_arg(op: dict, key: str, low: int, high: int) -> int:
    value = op.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ScriptError("invalid_edit", f"{key}는 {low}~{high} 사이 정수여야 합니다")
    return value


def _section_arg(op: dict, sections: int) -> int | None:
    if op.get("section") is None:
        return None
    return _int_arg(op, "section", 0, sections - 1)


def _fit_section(cuts: list[dict], pos: int, section: int | None, current: int | None) -> int:
    """cuts[pos]에 놓을 섹션. 지정(section)이 앞뒤 컷과 순서가 맞아야 하고, 지정이 없으면 current를 쓰되
    순서가 거꾸로면 그 자리의 섹션(앞 컷, 맨 앞이면 다음 컷)으로 맞춘다."""
    prev = cuts[pos - 1]["section"] if pos > 0 else None
    nxt = cuts[pos + 1]["section"] if pos + 1 < len(cuts) else None

    def fits(s: int) -> bool:
        return (prev is None or prev <= s) and (nxt is None or s <= nxt)

    if section is not None:
        if not fits(section):
            raise ScriptError("invalid_edit", "그 자리에는 그 섹션의 컷을 둘 수 없습니다 (섹션 순서가 거꾸로 됩니다)")
        return section
    if current is not None and fits(current):
        return current
    if prev is not None:
        return prev
    return nxt if nxt is not None else 0


INSERT_FIELDS = ("id", "section", "angle", *UPDATABLE_FIELDS)


def _check_insert(extra: dict, sections: int) -> dict:
    """insert의 cut. 지운 컷을 되살릴 때 id·angle·section까지 받는다.
    id는 형식이 맞을 때만 후보(파일에서 쓰는 중이면 새로 발급), section은 그 자리에 맞으면 따르는 힌트."""
    unknown = sorted(set(extra) - set(INSERT_FIELDS))
    if unknown:
        raise ScriptError("invalid_edit", f"넣을 수 없는 컷 항목입니다: {', '.join(unknown)} (허용: {', '.join(INSERT_FIELDS)})")
    angle = extra.get("angle")
    if angle is not None and not isinstance(angle, str):
        raise ScriptError("invalid_edit", "angle은 문자열이나 null이어야 합니다")
    section = None if extra.get("section") is None else _int_arg(extra, "section", 0, sections - 1)
    cut_id = extra.get("id")
    return {"id": cut_id if isinstance(cut_id, str) and _ID_RE.fullmatch(cut_id) else None,
            "angle": (angle or "").strip() or None, "section": section,
            "fields": _check_edit({k: v for k, v in extra.items() if k in UPDATABLE_FIELDS})}


def _unused_id(cuts: list[dict]) -> str:
    taken = {c["id"] for c in cuts}
    cut_id = new_cut_id()
    while cut_id in taken:
        cut_id = new_cut_id()
    return cut_id


def picked_changed(cut: dict, fields: dict) -> bool:
    """내용 맞춤 사진(ref_auto)을 고른 근거(PICK_FIELDS) 중 하나라도 값이 달라지는지."""
    return any(key in fields and fields[key] != cut.get(key) for key in PICK_FIELDS)


def _apply_op(fm: dict, op: dict) -> tuple[dict, str | None]:
    """op를 적용한 새 fm(시간은 아직 안 맞춤)과 새로 생기거나 고친 컷 id."""
    kind = op.get("op")
    cuts = [dict(c) for c in fm["cuts"]]
    sections = len(fm["script"])
    cut_id: str | None = None
    if kind == "insert":
        at = _int_arg(op, "at", 0, len(cuts))
        extra = op.get("cut") or {}
        if not isinstance(extra, dict):
            raise ScriptError("invalid_edit", "cut은 JSON 객체여야 합니다")
        given = _check_insert(extra, sections)
        taken = {c["id"] for c in cuts}
        cut_id = given["id"] if given["id"] is not None and given["id"] not in taken else _unused_id(cuts)
        new = {"id": cut_id, "section": 0, "angle": given["angle"], "line": "", "caption": "", "sfx": "",
               **CUT_DEFAULTS, **given["fields"]}
        if not new.get("ref_frame"):
            new.pop("ref_frame", None)
        cuts.insert(at, new)
        hint = given["section"] if given["section"] is not None else cuts[at - 1]["section"] if at > 0 else 0
        new["section"] = _fit_section(cuts, at, _section_arg(op, sections), hint)
    elif kind == "delete":
        cuts.pop(_cut_ref(cuts, op))
    elif kind == "move":
        pos = _cut_ref(cuts, op)
        to = _int_arg(op, "to", 0, len(cuts) - 1)
        cut = cuts.pop(pos)
        cuts.insert(to, cut)
        cut["section"] = _fit_section(cuts, to, _section_arg(op, sections), cut["section"])
        cut_id = cut["id"]
    elif kind == "duplicate":
        pos = _cut_ref(cuts, op)
        copy = {**cuts[pos], "id": _unused_id(cuts)}
        cuts.insert(pos + 1, copy)
        cut_id = copy["id"]
    elif kind == "update":
        pos = _cut_ref(cuts, op)
        fields = op.get("fields")
        if not isinstance(fields, dict):
            raise ScriptError("invalid_edit", "fields는 JSON 객체여야 합니다")
        edit = _check_edit(fields)
        if picked_changed(cuts[pos], edit):              # 사진을 고른 근거가 바뀌면 내용 맞춤 사진은 버린다(구도 맞춤으로)
            cuts[pos].pop("ref_auto", None)
        for key, value in edit.items():
            if key == "ref_frame" and not value:
                cuts[pos].pop("ref_frame", None)
            else:
                cuts[pos][key] = value
        cut_id = cuts[pos]["id"]
    else:
        raise ScriptError("invalid_edit", f"알 수 없는 편집입니다: {kind!r} (insert, delete, move, duplicate, update)")
    if {c["section"] for c in cuts} != set(range(sections)):
        raise ScriptError("last_cut", LAST_CUT_MESSAGE)
    return {**fm, "cuts": cuts}, cut_id


def _edit_locked(conti: str | Path, op: dict, expected_hash: str | None, now: float | None) -> tuple[dict, dict]:
    from scripts import conti_history

    if not isinstance(op, dict):
        raise ScriptError("invalid_edit", "편집 내용은 JSON 객체여야 합니다")
    path = _resolve(conti)
    if not path.exists():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {_rel(path)}")
    raw = path.read_bytes()                              # 이 편집에서 콘티 md를 읽는 것은 이 한 번뿐
    if expected_hash is not None and bytes_hash(raw) != expected_hash:
        raise ScriptError("conti_changed", "파일이 밖에서 바뀌었습니다. 다시 불러오세요")
    # 편집 전 상태를 이력에 남기려면 id가 파일에 있어야 한다
    current, before, _ = ensure_ids_file(parse_conti(path, raw))
    edited, cut_id = _apply_op(before, op)               # 잘못된 편집은 이력·파일을 건드리기 전에 실패한다
    old_lines, new_lines = _section_lines(before), _section_lines(edited)
    fm = retime(edited, {i for i, lines in enumerate(new_lines) if lines != old_lines[i]})   # 컷 대사가 달라진 섹션만 대사를 다시 이음
    conti_history.check_external(path, before, now)
    written, html_path = _write_conti_file(current, fm)
    conti_history.record(path, fm, "auto", now=now)
    result = {"status": "updated", "path": _rel(path), "html": _rel(html_path), "abs_html": str(html_path),
              "hash": bytes_hash(written.raw), "cut_id": cut_id,
              "warnings": warnings(fm["script"], fm["cuts"], fm["speech_cps"])}
    return result, fm


def scenes(conti: str | Path, n: int, limit: int = 6) -> list[dict]:
    """컷에 쓸 참고 장면 후보. 같은 크기+섹션 → 같은 크기 순, 각 단계 안에서 exact 먼저, 프레임 경로 순."""
    fm = _load_conti(_resolve(conti))
    cut = _find_cut(fm["cuts"], n)
    label = fm["script"][cut["section"]]["label"]
    pool = frame_pool(fm.get("category"))
    tiers = [[p for p in pool if p["size"] == cut["size"] and p["section"] == label],
             [p for p in pool if p["size"] == cut["size"]]]
    out, seen = [], set()
    for tier in tiers:
        for p in sorted(tier, key=lambda p: (p["match"] != "exact", p["frame"])):
            if p["frame"] in seen:
                continue
            seen.add(p["frame"])
            out.append({k: p.get(k) for k in ("frame", "size", "section", "video", "title", "match", "desc")})
    return out[:limit]


def cut_context(conti: str | Path, n: int) -> dict:
    """컷 하나를 다듬을 때 필요한 맥락: 앞뒤 컷 대사, 섹션 전체 대사, 글자 수 한도, draft의 훅·서사 유형."""
    fm = _load_conti(_resolve(conti))
    cut = _find_cut(fm["cuts"], n)
    section = fm["script"][cut["section"]]
    draft_fm: dict = {}
    draft_path = _resolve(fm["draft"]) if fm.get("draft") else None
    if draft_path and draft_path.exists():
        draft_fm, _ = refstore.read_md(draft_path)
    prev = next((c for c in fm["cuts"] if c["n"] == n - 1), None)
    nxt = next((c for c in fm["cuts"] if c["n"] == n + 1), None)
    return {"cut": cut, "prev_line": prev["line"] if prev else None, "next_line": nxt["line"] if nxt else None,
            "section_label": section["label"], "section_text": section["text"], "speech_cps": fm["speech_cps"],
            "max_chars": int(fm["speech_cps"] * cut["sec"]), "hook_type": draft_fm.get("hook_type"),
            "narrative_type": draft_fm.get("narrative_type"), "title": fm.get("title"), "profile": motif._profile()}


SRT_TRACKS = {"caption": ("caption", "captions"), "line": ("line", "lines")}


def _srt_time(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    sec, milli = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{milli:03d}"


def _srt_text(text: Any) -> str:
    """앞뒤 공백·빈 줄을 정리하고 최대 2줄로 맞춘다(셋째 줄부터는 둘째 줄 끝에 이어 붙인다)."""
    lines = [ln.strip() for ln in str(text or "").replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) > 2:
        lines = [lines[0], " ".join(lines[1:])]
    return "\n".join(lines).replace("-->", "→")


_THRESHOLDS = json.loads((TEMPLATES_DIR / "thresholds.json").read_text("utf-8"))
SRT_FIT = _THRESHOLDS["srt"]
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"})
MIN_GAP_SEC = 0.01            # 맞출 컷이 없을 때 경계 사이 최소 간격


def retime_boundaries(planned: list[float], actual_duration: float, cuts: list[float],
                      snap_min: float, snap_ratio: float) -> dict:
    """계획 경계(0 ~ 계획 총길이)를 실제 영상 길이로 늘이고, 가까운 실제 컷이 있으면 그 시각에 붙인다."""
    if len(planned) < 2 or planned[-1] <= 0 or actual_duration <= 0:
        raise ScriptError("bad_request", "계획 경계와 영상 길이가 필요합니다")
    scale = actual_duration / planned[-1]
    limit = actual_duration - MIN_GAP_SEC        # 끝 경계와 0.01초 이상 떨어진 컷만 맞춘다
    boundaries = [0.0]
    used: set[int] = set()
    snapped = 0
    for i in range(1, len(planned) - 1):
        prev_mapped = boundaries[-1]
        t = planned[i] * scale
        tol = max(snap_min, snap_ratio * min(planned[i] - planned[i - 1], planned[i + 1] - planned[i]) * scale)
        near = [(abs(c - t), c, k) for k, c in enumerate(cuts) if k not in used and prev_mapped < c <= limit and abs(c - t) <= tol]
        if near:
            _, chosen, k = min(near)            # 거리 같으면 시각이 앞선 컷
            used.add(k)
            snapped += 1
            boundaries.append(chosen)
        else:
            boundaries.append(max(min(t, limit), prev_mapped + MIN_GAP_SEC))
    boundaries.append(actual_duration)
    return {"boundaries": boundaries, "scale": scale, "snapped": snapped,
            "inner": len(planned) - 2, "detected": len(cuts)}


def _clean_video_path(value: Any) -> Path:
    """붙여넣기로 들어온 영상 경로(앞뒤 공백·따옴표)를 정리하고 파일인지 확인한다. 영상은 읽기만 한다."""
    if not isinstance(value, (str, Path)):
        raise ScriptError("bad_video", "영상 파일 경로가 필요합니다")
    text = str(value).strip().strip("\"'").strip()
    if not text or "\x00" in text:
        raise ScriptError("bad_video", "영상 파일 경로가 필요합니다")
    try:
        path = _resolve(text)
        is_file = path.is_file()
        size = path.stat().st_size if is_file else 0
    except (OSError, ValueError) as e:
        raise ScriptError("bad_video", f"잘못된 영상 경로입니다: {text}") from e
    if not is_file:
        raise ScriptError("bad_video", f"영상 파일을 찾을 수 없습니다: {text}")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ScriptError("bad_video", f"영상 파일이 아닙니다(mp4, mov, m4v, mkv, webm, avi): {path.name}")
    if size <= 0:
        raise ScriptError("bad_video", f"비어 있는 파일입니다: {path.name}")
    return path


def _srt_blocks(cuts: list[dict], field: str, spans_ms: list[tuple[int, int]]) -> list[list[Any]]:
    """컷별 [시작ms, 끝ms]에서 자막 블록을 만든다: 빈 컷은 건너뛰고, 시간이 이어지는 같은 자막은 합친다."""
    blocks: list[list[Any]] = []          # [start_ms, end_ms, text]
    for cut, (start, end) in zip(cuts, spans_ms):
        if end <= start:
            continue                      # 길이가 없는 구간은 자막으로 내지 않는다
        text = _srt_text(cut.get(field))
        if not text:
            continue
        if blocks and blocks[-1][2] == text and blocks[-1][1] == start:
            blocks[-1][1] = end
        else:
            blocks.append([start, end, text])
    return blocks


def _planned_spans_ms(cuts: list[dict]) -> list[tuple[int, int]]:
    spans, clock = [], 0
    for cut in cuts:
        end = clock + round(float(cut["sec"]) * 1000)
        spans.append((clock, end))
        clock = end
    return spans


def _retime_spans_ms(cuts: list[dict], video: str | Path | None, progress: Callable[[str], None] | None
                     ) -> tuple[list[tuple[int, int]], dict]:
    from scripts import cuts as cuts_mod        # cv2·scenedetect는 영상을 맞출 때만 불러온다

    path = _clean_video_path(video)
    if progress:
        progress("detect")
    found = cuts_mod.detect_cuts(path)
    planned = [0.0]
    for cut in cuts:
        planned.append(planned[-1] + float(cut["sec"]))
    fit = retime_boundaries(planned, found["duration_sec"], found["cuts"], SRT_FIT["snap_min_sec"], SRT_FIT["snap_ratio"])
    ms = [round(b * 1000) for b in fit["boundaries"]]
    warnings = []
    if not found["cuts"]:
        warnings.append("실제 컷을 찾지 못해 길이 비율로만 맞췄습니다")
    scale_low, scale_high = SRT_FIT["scale_warn"]
    if not scale_low <= fit["scale"] <= scale_high:
        warnings.append(f"콘티 길이와 많이 다릅니다(실제 {found['duration_sec']:.1f}초 / 계획 {planned[-1]:.1f}초)"
                        " — 다른 영상이 아닌지 확인하세요")
    retime = {"video": path.name, "detected": fit["detected"], "snapped": fit["snapped"],
              "inner": fit["inner"], "scale": round(fit["scale"], 4), "warnings": warnings}
    return list(zip(ms, ms[1:])), retime


def caption_blocks(conti: str | Path, track: str = "caption", video: str | Path | None = None,
                   progress: Callable[[str], None] | None = None) -> dict:
    """콘티의 자막(또는 대사) 블록과 시간을 계산한다. export_srt와 자막 영상 렌더가 같은 계산을 쓴다.

    video가 없으면 컷 sec의 ms 정수 누적, 있으면 그 영상의 실제 컷에 맞춘 시간. 영상 파일은 읽기만 한다.
    돌려주는 값: path(콘티 Path), fm(콘티 프런트매터), blocks([[시작ms, 끝ms, 텍스트]]), duration_ms,
    retime(맞춤 요약 또는 None), video(정리된 영상 Path 또는 None).
    """
    if track not in SRT_TRACKS:
        raise ScriptError("bad_request", f"track은 caption 또는 line이어야 합니다: {track}")
    path = safe_draft_path(_rel(_resolve(conti)), conti_file=True)   # drafts/ 안의 *-conti.md만(CLI도 API와 같은 규칙)
    field, _ = SRT_TRACKS[track]
    fm = _load_conti(path)
    retime: dict | None = None
    video_path: Path | None = None
    if video is None:
        spans = _planned_spans_ms(fm["cuts"])
    else:
        spans, retime = _retime_spans_ms(fm["cuts"], video, progress)
        video_path = _clean_video_path(video)
    blocks = _srt_blocks(fm["cuts"], field, spans)
    return {"path": path, "fm": fm, "blocks": blocks, "duration_ms": spans[-1][1] if spans else 0,
            "retime": retime, "video": video_path}


def export_srt(conti: str | Path, track: str = "caption", video: str | Path | None = None,
               progress: Callable[[str], None] | None = None) -> dict:
    """컷별 caption(또는 line)을 CapCut용 .srt로 내보낸다.

    video가 없으면 컷 sec의 ms 정수 누적(오차가 쌓이지 않음). video(자막 없는 편집본)가 있으면 그 영상의
    실제 컷 시각에 맞춘다. 영상 파일은 읽기만 한다. progress는 "detect" → "write" 단계 알림용.
    """
    calc = caption_blocks(conti, track, video, progress)
    path, blocks, retime = calc["path"], calc["blocks"], calc["retime"]
    suffix = SRT_TRACKS[track][1]
    result: dict[str, Any] = {"path": None, "track": track, "count": len(blocks),
                              "duration_sec": calc["duration_ms"] / 1000}
    if retime is not None:
        result["retime"] = retime
    if not blocks:
        return result
    if progress:
        progress("write")
    body = "".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(blocks, 1))
    out = path.with_name(f"{path.stem[: -len(CONTI_SUFFIX)]}-{suffix}.srt")
    with out.open("w", encoding="utf-8", newline="") as f:
        f.write(body.replace("\n", "\r\n"))
    result["path"] = _rel(out)
    return result


def conti_outputs(path: Path) -> list[Path]:
    """콘티 md와 그 콘티로 만든 파일(스토리보드 HTML, SRT 두 종류, 자막 영상, 촬영 영상의 남은 컷 HTML·위치 CSV)
    중 있는 것. 기획안은 넣지 않는다."""
    base = path.stem[: -len(CONTI_SUFFIX)]
    names = [path.name, path.with_suffix(".html").name,
             *(f"{base}-{suffix}.srt" for _, suffix in SRT_TRACKS.values()), f"{base}-captions.mp4",
             f"{base}-remaining.html", f"{base}-footage.csv"]
    return [path.with_name(n) for n in names if path.with_name(n).is_file()]


def trash_targets(path: Path) -> list[Path]:
    """콘티를 지울 때 함께 옮길 것: 산출물 + 이력 폴더 + 컷 파일 폴더 + 촬영 영상 판정 파일(있으면)."""
    from scripts import conti_history, footage_match

    folders = [conti_history.history_dir(path), path.with_name(f"{path.stem[: -len(CONTI_SUFFIX)]}-cuts")]
    state = footage_match.state_path(path)
    return [*conti_outputs(path), *(f for f in folders if f.is_dir()), *([state] if state.is_file() else [])]


def trash(conti: str | Path) -> dict:
    """콘티를 지운다: 콘티 md와 그 산출물을 `workspace/trash/<시각>/`으로 옮긴다(되돌릴 수 있게). 기획안은 그대로."""
    path = safe_draft_path(str(conti), conti_file=True)       # drafts/ 안의 콘티 파일만
    if not path.is_file():
        raise ScriptError("not_found", f"콘티 파일이 없습니다: {_rel(path)}")
    # 이름만 -conti로 끝나는 기획안을 옮기지 않도록, 콘티 프런트매터(script·cuts)가 있는지 본다.
    fm, _ = refstore.read_md(path)
    if "cuts" not in fm or "script" not in fm:
        raise ScriptError("not_conti", f"콘티 파일이 아닙니다(프런트매터에 script·cuts가 없음): {_rel(path)}")
    return move_to_trash(trash_targets(path), last=path)


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.conti")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("context").add_argument("draft")
    s = sub.add_parser("save")
    s.add_argument("draft")
    s.add_argument("--conti", required=True)
    s.add_argument("--force", action="store_true")
    sub.add_parser("render").add_argument("conti")
    u = sub.add_parser("update")
    u.add_argument("conti")
    u.add_argument("--cut", type=int, required=True)
    for field in EDITABLE_FIELDS:
        u.add_argument(f"--{field.replace('_', '-')}", dest=field, default=None)
    u.add_argument("--expect-hash", dest="expect_hash", default=None)
    sc = sub.add_parser("scenes")
    sc.add_argument("conti")
    sc.add_argument("--cut", type=int, required=True)
    sc.add_argument("--limit", type=int, default=6)
    cc = sub.add_parser("cut-context")
    cc.add_argument("conti")
    cc.add_argument("--cut", type=int, required=True)
    sr = sub.add_parser("srt")
    sr.add_argument("conti")
    sr.add_argument("--track", choices=sorted(SRT_TRACKS), default="caption")
    sr.add_argument("--video", default=None)
    sub.add_parser("trash").add_argument("conti")
    sub.add_parser("history").add_argument("conti")
    hn = sub.add_parser("history-name")
    hn.add_argument("conti")
    hn.add_argument("--label", required=True)
    hn.add_argument("--expect-hash", dest="expect_hash", default=None)
    hr = sub.add_parser("history-restore")
    hr.add_argument("conti")
    hr.add_argument("--n", type=int, required=True)
    hr.add_argument("--expect-hash", dest="expect_hash", default=None)
    mg = sub.add_parser("merge")
    mg.add_argument("conti")
    mg.add_argument("--cuts", default=None)
    mg.add_argument("--hash", default=None)
    hd = sub.add_parser("history-diff")
    hd.add_argument("conti")
    hd.add_argument("--a", type=int, required=True)
    hd.add_argument("--b", type=int, default=None)
    args = parser.parse_args(argv)
    if args.cmd == "trash":
        return trash(args.conti)
    if args.cmd == "merge":
        from scripts import scene_merge     # scene_merge가 이 모듈을 쓰므로 함수 안에서 불러온다

        return scene_merge.run_cli(args.conti, args.cuts, args.hash)
    if args.cmd.startswith("history"):
        from scripts import conti_history

        if args.cmd == "history":
            conti_history.baseline(args.conti)
            return conti_history.list_versions(args.conti)
        if args.cmd == "history-name":
            return conti_history.name(args.conti, args.label, args.expect_hash)
        if args.cmd == "history-restore":
            return conti_history.restore(args.conti, args.n, args.expect_hash)
        return conti_history.diff_versions(args.conti, args.a, args.b)
    if args.cmd == "srt":
        return export_srt(args.conti, args.track, args.video)
    if args.cmd == "context":
        return context(args.draft)
    if args.cmd == "render":
        return render(args.conti)
    if args.cmd == "update":
        fields = {f: getattr(args, f) for f in EDITABLE_FIELDS if getattr(args, f) is not None}
        return update_cut(args.conti, args.cut, fields, args.expect_hash)
    if args.cmd == "scenes":
        return scenes(args.conti, args.cut, args.limit)
    if args.cmd == "cut-context":
        return cut_context(args.conti, args.cut)
    return save(args.draft, _resolve(args.conti), args.force)


if __name__ == "__main__":
    run(main)
