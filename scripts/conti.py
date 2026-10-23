"""콘티: draft를 컷 단위 콘티(전체 대사 + 컷별 대사·자막·효과음·가이드)로 저장하고, HTML 스토리보드를 그린다.

사용법 (작업 폴더에서):
  python -m scripts.conti context <draft.md>                          draft 파싱 + 말 속도 + 포맷
  python -m scripts.conti save <draft.md> --conti <json> [--force]    → drafts/{draft}-conti.md + .html
  python -m scripts.conti render <conti.md>                           고친 프런트매터로 HTML만 다시 그린다
  python -m scripts.conti update <conti.md> --cut N [--line S] [--guide S] [--caption S] [--sfx S]
                          [--ref-frame S] [--expect-hash H]           컷 하나 고치기 (--ref-frame ""은 고정 해제)
  python -m scripts.conti scenes <conti.md> --cut N [--limit 6]       그 컷에 쓸 참고 장면 후보
  python -m scripts.conti cut-context <conti.md> --cut N              컷 하나의 앞뒤 대사·섹션·글자 수 한도

conti JSON (yt-conti 규칙)
  {"script": [{"text"}],                                      draft 섹션마다 하나, 같은 순서 (전체 대사)
   "cuts": [{"section": <섹션 번호 0~>, "size", "angle", "sec", "action", "line", "caption", "sfx", "guide"}]}
- 검증(실패): 섹션 수와 script 수, 섹션별 컷 길이 합 = 섹션 길이(±0.3초), 샷 크기 어휘, action·guide 필수
- 경고(저장은 함): 대사가 컷 길이에 비해 김(말 속도 기준), 컷 대사를 이으면 그 섹션 전체 대사와 다름
- 말 속도 = 카테고리 ref 영상 자막의 초당 글자 수 중앙값(sparse 제외). 없으면 4.5
- 시각화: 컷마다 샷 크기 도식(SVG) + 레퍼런스 대표 프레임(참고용). 이미지는 base64로 넣어 한 파일로 만든다
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import statistics
from datetime import date
from pathlib import Path
from typing import Any

from scripts import features, fetch, motif, refstore
from scripts.common import ArgParser, ScriptError, run, work_root, workspace_dir
from scripts.features import SHOT_SIZES
from scripts.report import SLOTS, _vars
from scripts.videomd import LABELS, _existing_memo, fmt_time, load_cuts

DEFAULT_CPS = 4.5
SEC_TOLERANCE = 0.3
OVERFLOW_SLACK = 1.15          # 말 속도 × 컷 길이의 115%까지는 허용
SCHEMA_VERSION = 2
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
            blocks[-1]["script"].append(line.strip())
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
    return draft.with_name(f"{draft.stem}-conti.md")


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
    out_cuts, prev_section = [], 0
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
        out_cuts.append({"n": i, "section": sec_idx, "size": c["size"], "angle": c.get("angle") or None, "sec": sec,
                         **{f: str(c.get(f) or "").strip() for f in ("action", "line", "caption", "sfx", "guide")}})
        pinned = c.get("ref_frame")
        if isinstance(pinned, str) and pinned.strip():
            out_cuts[-1]["ref_frame"] = pinned.strip()
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
    """ref 대표 프레임 후보. 프레임별 라벨(workspace/shots)이 있으면 exact, 없으면 섹션별 대표 샷 크기로 approx."""
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
            for lab in json.loads(labels_path.read_text("utf-8")).get("labels") or []:
                pool.append({"frame": lab["frame"], "size": lab["size"], "section": lab.get("section"),
                             "video": vid, "title": fm.get("title"), "match": "exact"})
            continue
        by_section = (fm.get("shots") or {}).get("by_section") or {}
        for scene in (load_cuts(vid) or {}).get("scenes") or []:
            label = _section_at(fm.get("sections") or [], (scene["start"] + scene["end"]) / 2)
            if scene.get("frame") and by_section.get(label):
                pool.append({"frame": scene["frame"], "size": by_section[label], "section": label,
                             "video": vid, "title": fm.get("title"), "match": "approx"})
    return [p for p in pool if (work_root() / p["frame"]).exists()]


def match_frames(cuts: list[dict], sections: list[dict], pool: list[dict]) -> dict[int, dict]:
    """컷마다 참고 프레임 하나. 같은 크기+섹션 → 같은 크기 순. 각 단계에서 exact가 approx보다 먼저, 그다음 덜 쓴 프레임."""
    used: dict[str, int] = {}
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


def render_html(fm: dict) -> str:
    script, cuts = fm["script"], fm["cuts"]
    vertical = fm["format"] == "shorts"
    sections = [{"label": s["label"]} for s in script]
    refs = match_frames(cuts, sections, frame_pool(fm.get("category")))
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
        ref = refs.get(c["n"])
        pinned = c.get("ref_frame")
        if pinned and (work_root() / pinned).exists():            # 사용자가 고정한 프레임이 자동 매칭보다 먼저
            ref = {"frame": pinned, "title": Path(pinned).name, "match": "pinned"}
        uri = _data_uri(ref["frame"]) if ref else None
        tag = {"exact": "참고", "pinned": "참고(고정)"}.get(ref["match"] if ref else "", "참고(근사)")
        ref_html = (f'<figure><img class="ref" src="{uri}" alt="참고 프레임: {_esc(ref["title"])}">'
                    f'<figcaption title="{_esc(ref["title"])}">{tag} · '
                    f'{_esc(ref["title"])}</figcaption></figure>'
                    if uri else '<figure><div class="noref">같은 샷의 레퍼런스 프레임 없음</div>'
                                '<figcaption>&nbsp;</figcaption></figure>')
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
    approx = sum(1 for c in cuts if c["n"] in refs and refs[c["n"]]["match"] != "exact"
                 and not (c.get("ref_frame") and (work_root() / c["ref_frame"]).exists()))
    note = ('<p class="sub" style="margin-top:24px;font-size:12px">참고 프레임은 같은 샷 크기의 레퍼런스 장면으로, '
            "구도만 참고하세요. 도식은 샷 크기를 단순화한 그림입니다."
            + (f" '참고(근사)' {approx}장은 프레임별 샷 분류가 없어 섹션 대표 샷 크기로 고른 것이라 틀릴 수 있습니다. "
               "/yt-coach:yt-add &lt;카테고리&gt; --shots-only로 보강하면 정확해집니다." if approx else "") + "</p>")
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_esc(fm['title'])} 콘티</title><style>{CSS % (_vars(0), _vars(1), _vars(1))}"
            f"main{{--ar:{'9/16' if vertical else '16/9'}}}</style></head>"
            f"<body><main>{head}{full}{board}{table}{note}</main></body></html>")


def _write_html(md_path: Path, fm: dict) -> Path:
    out = md_path.with_suffix(".html")
    out.write_text(render_html(fm), encoding="utf-8")
    return out


def save(draft: str | Path, conti_json: Path, force: bool = False) -> dict:
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
    rate = speech_rate(d["fm"].get("category"))
    total = d["sections"][-1]["end"]
    fm = {"date": date.today().isoformat(), "draft": _rel(draft_path), "html": _rel(target.with_suffix(".html")),
          "category": d["fm"].get("category"), "title": d["title"], "format": video_format(total),
          "target_sec": total, "speech_cps": rate["cps"], "script": script, "cuts": cuts,
          "schema_version": SCHEMA_VERSION}
    memo = _existing_memo(target)
    target.write_bytes(refstore.join_frontmatter(fm, render_md(fm, memo)).encode("utf-8"))
    html_path = _write_html(target, fm)
    Path(conti_json).unlink(missing_ok=True)
    return {"status": "saved", "path": _rel(target), "html": _rel(html_path), "abs_html": str(html_path),
            "cuts": len(cuts), "speech_cps": rate["cps"], "warnings": warnings(script, cuts, rate["cps"])}


def render(conti: str | Path) -> dict:
    path = _resolve(conti)
    if not path.exists():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {_rel(path)}")
    fm, _ = refstore.read_md(path)
    if not fm.get("cuts") or not fm.get("script"):
        raise ScriptError("invalid_conti", "콘티 프런트매터에 script·cuts가 없습니다")
    sections = [{"idx": i, "label": s["label"], "start": float(s["start"]), "end": float(s["end"])}
                for i, s in enumerate(fm["script"])]
    script, cuts = validate({"script": fm["script"], "cuts": fm["cuts"]}, sections)  # 고친 시간·순서를 다시 계산
    fm = {**fm, "script": script, "cuts": cuts}
    html_path = _write_html(path, fm)
    return {"status": "rendered", "html": _rel(html_path), "abs_html": str(html_path), "cuts": len(cuts),
            "warnings": warnings(script, cuts, fm["speech_cps"])}


# --- 컷 하나 편집 · 장면 후보 ----------------------------------------------------------

def content_hash(path: str | Path) -> str:
    """파일 바이트 sha256. 화면이 불러온 뒤 파일이 밖에서 바뀌었는지 확인하는 데 쓴다."""
    return hashlib.sha256(_resolve(path).read_bytes()).hexdigest()


def _load_conti(path: Path) -> dict:
    if not path.exists():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {_rel(path)}")
    fm, _ = refstore.read_md(path)
    if not fm.get("cuts") or not fm.get("script"):
        raise ScriptError("invalid_conti", "콘티 프런트매터에 script·cuts가 없습니다")
    sections = [{"idx": i, "label": s["label"], "start": float(s["start"]), "end": float(s["end"])}
                for i, s in enumerate(fm["script"])]
    script, cuts = validate({"script": fm["script"], "cuts": fm["cuts"]}, sections)
    return {**fm, "script": script, "cuts": cuts}


def _find_cut(cuts: list[dict], n: int) -> dict:
    cut = next((c for c in cuts if c["n"] == n), None)
    if cut is None:
        raise ScriptError("cut_not_found", f"컷 {n}번이 없습니다 (1~{len(cuts)})")
    return cut


def _check_edit(fields: dict) -> dict:
    unknown = sorted(set(fields) - set(EDITABLE_FIELDS))
    if unknown:
        raise ScriptError("invalid_edit", f"고칠 수 없는 필드입니다: {', '.join(unknown)} (허용: {', '.join(EDITABLE_FIELDS)})")
    edit = {k: ("" if v is None else str(v)).strip() for k, v in fields.items()}
    if "guide" in edit and not edit["guide"]:
        raise ScriptError("invalid_edit", "가이드는 비울 수 없습니다")
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


def update_cut(conti: str | Path, n: int, fields: dict, expected_hash: str | None = None) -> dict:
    """컷 하나의 line·guide·caption·sfx·ref_frame을 고친다. 본문은 다시 그리되 '## 내 메모'와 줄바꿈 방식은 보존."""
    path = _resolve(conti)
    if not path.exists():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {_rel(path)}")
    raw = path.read_bytes()
    if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ScriptError("conti_changed", "파일이 밖에서 바뀌었습니다. 다시 불러오세요")
    edit = _check_edit(fields)
    fm = _load_conti(path)
    cut = _find_cut(fm["cuts"], n)
    for key, value in edit.items():
        if key == "ref_frame" and not value:
            cut.pop("ref_frame", None)
        else:
            cut[key] = value
    if "line" in edit:                                   # 컷 대사를 이은 것이 섹션 전체 대사
        lines = [c["line"] for c in fm["cuts"] if c["section"] == cut["section"] and c["line"]]
        fm["script"][cut["section"]]["text"] = " ".join(lines)
    fm["schema_version"] = SCHEMA_VERSION
    nl = "\r\n" if raw.lstrip(b"\xef\xbb\xbf").startswith(b"---\r\n") else "\n"
    memo = _existing_memo(path).replace("\r\n", "\n")
    body = render_md(fm, memo).replace("\n", nl)
    path.write_bytes(refstore.join_frontmatter(fm, body, nl).encode("utf-8"))
    html_path = _write_html(path, fm)
    return {"status": "updated", "path": _rel(path), "html": _rel(html_path), "abs_html": str(html_path),
            "hash": content_hash(path), "cut": _find_cut(fm["cuts"], n),
            "warnings": warnings(fm["script"], fm["cuts"], fm["speech_cps"])}


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
            out.append({k: p.get(k) for k in ("frame", "size", "section", "video", "title", "match")})
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
    args = parser.parse_args(argv)
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
