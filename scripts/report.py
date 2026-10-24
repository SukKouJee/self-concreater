"""카테고리 리포트: 축별 판정, 아키타입 비교, 컷 길이 분포, 내 콘텐츠 근접도 추이를 HTML 한 장으로.

사용법 (작업 폴더에서):
  python -m scripts.report <category> [--date YYYY-MM-DD]      → reports/<category>-<date>.html

- 외부 자원 없이 한 파일(인라인 CSS·SVG). 라이트/다크 모두 지원
- 색: dataviz 기준 팔레트(검증된 categorical 슬롯). 섹션 라벨은 슬롯 1~7 고정, 아키타입은 A1부터 슬롯 순서 고정
- 모든 차트에 범례와 표가 있고, 마크에 hover 툴팁(<title>)이 있다. 값 글자는 시리즈 색이 아니라 본문 색
"""
from __future__ import annotations

import html
import json
import math
import re
from datetime import date as Date
from pathlib import Path
from typing import Any

from scripts import features, refstore
from scripts.common import ArgParser, run, work_root, workspace_dir
from scripts.videomd import LABELS

# dataviz 기준 팔레트 categorical 슬롯 (light, dark)
SLOTS = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"), ("#eda100", "#c98500"),
         ("#e87ba4", "#d55181"), ("#008300", "#008300"), ("#4a3aa7", "#9085e9"), ("#e34948", "#e66767")]
VERDICT_KO = {"typable": "유형 있음", "weak": "약한 경향", "untypable": "유형 없음", "insufficient": "데이터 부족"}
VERDICT_ICON = {"typable": "●", "weak": "◐", "untypable": "○", "insufficient": "–"}

CSS = """
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
%s}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;
--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;%s}}
:root[data-theme="dark"]{--page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;
--axis:#383835;%s}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.6 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:36px 0 12px}.sub{color:var(--ink2);margin:0}
.card{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:16px;margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%%;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);
vertical-align:top}th{color:var(--ink2);font-weight:600;white-space:nowrap}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0;font-size:13px;color:var(--ink2)}
.legend span{display:inline-flex;align-items:center;gap:6px}.sw{width:12px;height:12px;border-radius:3px;display:inline-block}
.badge{white-space:nowrap}svg text{fill:var(--muted);font-size:11px}svg .lbl{fill:var(--ink2);font-size:12px}
.dot{stroke:var(--surface);stroke-width:2}.empty{color:var(--ink2)}
details{margin-top:8px}summary{cursor:pointer;color:var(--ink2);font-size:13px}
"""


def _esc(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{round(x * 100)}%"


def _vars(mode: int) -> str:
    return "".join(f"--s{i + 1}:{pair[mode]};" for i, pair in enumerate(SLOTS))


def _legend(items: list[tuple[str, str]]) -> str:
    return '<div class="legend">' + "".join(
        f'<span><i class="sw" style="background:var({var})"></i>{_esc(name)}</span>' for name, var in items) + "</div>"


# --- 데이터 ------------------------------------------------------------------------

def _axis_notes(index_body: str) -> dict:
    notes = {}
    for m in re.finditer(r"^\| (\w+) \| \w+ \([^)]*\) \| (.*) \|$", index_body, re.M):
        notes[m.group(1)] = m.group(2).replace("\\|", "|")
    return notes


def _mine_histories(category: str) -> list[dict]:
    folder = work_root() / "mine"
    out = []
    for p in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        fm, _ = refstore.read_md(p)
        if fm.get("category") == category and fm.get("history"):
            out.append({"name": p.stem, "nearest": fm.get("nearest") or {}, "history": fm["history"]})
    return out


# --- 차트 ---------------------------------------------------------------------------

def skeleton_chart(archetypes: list[dict], names: dict) -> str:
    """아키타입별 섹션 비율 가로 누적 막대. 세그먼트 사이 2px 간격."""
    width, bar_h, gap, left = 640, 22, 18, 150
    height = len(archetypes) * (bar_h + gap) + 8
    rows = []
    for i, a in enumerate(archetypes):
        y = 4 + i * (bar_h + gap)
        rows.append(f'<text class="lbl" x="0" y="{y + 15}">{_esc(a["id"])} {_esc(names.get(a["id"]) or "")[:12]}</text>')
        x, plot = left, width - left
        parts = [(l, a["mean_ratio"].get(l, 0)) for l in LABELS if a["mean_ratio"].get(l, 0) > 0]
        total = sum(v for _, v in parts) or 1
        for label, v in parts:
            w = max(plot * v / total - 2, 1)
            slot = LABELS.index(label) + 1
            rows.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" fill="var(--s{slot})">'
                        f'<title>{_esc(a["id"])} · {label} {_pct(v)}</title></rect>')
            x += w + 2
    used = sorted({l for a in archetypes for l in a["mean_ratio"] if a["mean_ratio"][l] > 0}, key=LABELS.index)
    return (_legend([(l, f"--s{LABELS.index(l) + 1}") for l in used])
            + f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="아키타입별 섹션 비율">'
            + "".join(rows) + "</svg>")


def pacing_chart(points: list[dict], archetypes: list[dict]) -> str:
    """ref 영상별 평균 컷 길이 점 그림(로그 축). 색 = 소속 아키타입, 없으면 회색."""
    if not points:
        return '<p class="empty">컷 통계가 있는 영상이 없습니다.</p>'
    width, height, left, right, mid = 640, 120, 16, 16, 56
    lo, hi = math.log(0.5), math.log(max(30.0, max(p["avg"] for p in points) * 1.2))
    sx = lambda v: left + (math.log(v) - lo) / (hi - lo) * (width - left - right)
    member = {m: i for i, a in enumerate(archetypes) for m in a["members"]}
    ticks = [t for t in (0.5, 1, 2, 5, 10, 20, 30, 60) if lo <= math.log(t) <= hi]
    parts = [f'<line x1="{left}" x2="{width - right}" y1="{mid + 22}" y2="{mid + 22}" stroke="var(--axis)"/>']
    for t in ticks:
        parts.append(f'<line x1="{sx(t):.1f}" x2="{sx(t):.1f}" y1="12" y2="{mid + 22}" stroke="var(--grid)"/>'
                     f'<text x="{sx(t):.1f}" y="{mid + 38}" text-anchor="middle">{t:g}초</text>')
    for j, p in enumerate(sorted(points, key=lambda p: p["avg"])):
        idx = member.get(p["id"])
        fill = f"var(--s{idx + 1})" if idx is not None and idx < len(SLOTS) else "var(--muted)"
        y = mid - 14 + (j % 3) * 14                     # 겹침을 줄이려 3줄로 엇갈림
        parts.append(f'<circle class="dot" cx="{sx(p["avg"]):.1f}" cy="{y}" r="6" fill="{fill}">'
                     f'<title>{_esc(p["title"])} · 평균 컷 {p["avg"]}초</title></circle>')
    legend = [(f'{a["id"]}', f"--s{i + 1}") for i, a in enumerate(archetypes[:len(SLOTS)])] + [("아키타입 밖", "--muted")]
    return (_legend(legend) + f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'aria-label="영상별 평균 컷 길이">' + "".join(parts) + "</svg>")


def sparkline(values: list[float]) -> str:
    w, h = 120, 28
    if len(values) < 2:
        return ""
    pts = " ".join(f"{4 + i * (w - 8) / (len(values) - 1):.1f},{h - 4 - v * (h - 8):.1f}" for i, v in enumerate(values))
    last = values[-1]
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="구조 근접도 추이">'
            f'<polyline points="{pts}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>'
            f'<circle cx="{w - 4}" cy="{h - 4 - last * (h - 8):.1f}" r="4" fill="var(--s1)">'
            f'<title>최근 {_pct(last)}</title></circle></svg>')


# --- 조립 -------------------------------------------------------------------------

def build(category: str, date: str | None = None) -> dict:
    day = date or Date.today().isoformat()
    cat = refstore.existing_category_dir(category)
    index_fm, index_body = refstore.read_md(cat / "index.md")
    counts = refstore.count(category)
    names = {a["id"]: a.get("name") for a in index_fm.get("archetypes") or []}
    typify_path = workspace_dir("typify", f"{category}.json")
    typify = json.loads(typify_path.read_text("utf-8")) if typify_path.exists() else None
    refs = features.load_videos(category)

    out = [f"<h1>{_esc(category)} 레퍼런스 리포트</h1>",
           f'<p class="sub">{day} · ref {counts["ref"]}개 · mine {counts["mine"]}개 · {counts["stage"]}단계'
           + (f' · 유형화 {"정식" if typify["status"] == "formal" else "예비"} ({_esc(typify["run_at"][:10])})' if typify else "")
           + "</p>"]

    out.append("<h2>축별 판정</h2>")
    if not typify:
        out.append('<div class="card"><p class="empty">아직 유형화 결과가 없습니다. ref를 5개 이상 모아 '
                   "/yt-coach:yt-typify를 실행하세요.</p></div>")
        archetypes = []
    else:
        notes = _axis_notes(index_body)
        rows = "".join(
            f'<tr><td>{axis}</td><td class="badge">{VERDICT_ICON[r["verdict"]]} {VERDICT_KO[r["verdict"]]}</td>'
            f'<td class="num">{r["silhouette"] if r["silhouette"] is not None else "-"}</td>'
            f'<td class="num">{_pct(r["max_share"])}</td><td>{_esc(notes.get(axis, ""))}</td></tr>'
            for axis, r in typify["axes"].items())
        out.append('<div class="card"><table><thead><tr><th>축</th><th>판정</th><th>실루엣</th><th>최대 군집</th>'
                   f"<th>근거</th></tr></thead><tbody>{rows}</tbody></table></div>")
        archetypes = typify["archetypes"]

    out.append("<h2>아키타입 비교</h2>")
    if archetypes:
        table = "".join(
            f'<tr><td>{_esc(a["id"])} {_esc(names.get(a["id"]))}</td>'
            f'<td class="num">{a["size"]}개</td><td>{_esc(a["skeleton"])}</td>'
            f'<td class="num">{a["pacing"]["avg_cut_sec"] or "-"}초</td>'
            f'<td>{_esc(" · ".join(f"{k} {_pct(v)}" for k, v in (a.get("camera") or {}).items()))}</td>'
            f'<td>{_esc(" · ".join(f"{s} {_pct(v)}" for s, v in ((a.get("shots") or {}).get("top_sizes") or [])))}</td></tr>'
            for i, a in enumerate(archetypes))
        out.append('<div class="card">' + skeleton_chart(archetypes, names)
                   + "<details open><summary>표로 보기</summary><table><thead><tr><th>아키타입</th><th>영상</th>"
                   "<th>골격</th><th>평균 컷</th><th>카메라</th><th>샷 경향</th></tr></thead>"
                   f"<tbody>{table}</tbody></table></details></div>")
    else:
        out.append('<div class="card"><p class="empty">아키타입이 아직 없습니다.</p></div>')

    out.append("<h2>컷 길이 분포</h2>")
    points = [{"id": r["id"], "title": r["fm"].get("title"), "avg": (r["fm"].get("pacing") or {}).get("avg_cut_sec")}
              for r in refs]
    points = [p for p in points if p["avg"]]
    table = "".join(f'<tr><td>{_esc(p["title"])}</td><td class="num">{p["avg"]}초</td></tr>'
                    for p in sorted(points, key=lambda p: p["avg"]))
    out.append('<div class="card">' + pacing_chart(points, archetypes)
               + "<details><summary>표로 보기</summary><table><thead><tr><th>영상</th><th>평균 컷</th></tr></thead>"
               f"<tbody>{table}</tbody></table></details></div>")

    out.append("<h2>내 콘텐츠 근접도</h2>")
    histories = _mine_histories(category)
    if histories:
        rows = []
        for h in histories:
            values = [e.get("structure") for e in h["history"] if e.get("structure") is not None]
            trend = " → ".join(_pct(v) for v in values) or "-"
            rows.append(f'<tr><td>{_esc(h["name"])}</td><td>{_esc(h["nearest"].get("id"))} {_esc(h["nearest"].get("name"))}</td>'
                        f'<td class="num">{len(h["history"])}회</td><td class="num">{trend}</td><td>{sparkline(values)}</td></tr>')
        out.append('<div class="card"><table><thead><tr><th>대상</th><th>가까운 아키타입</th><th>진단</th>'
                   f"<th>구조 근접도 추이</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>")
    else:
        out.append('<div class="card"><p class="empty">아직 진단 기록이 없습니다. /yt-coach:yt-diagnose로 내 영상이나 기획안을 비교해 보세요.</p></div>')

    page = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_esc(category)} 리포트</title><style>{CSS % (_vars(0), _vars(1), _vars(1))}</style></head>"
            f"<body><main>{''.join(out)}</main></body></html>")
    path = work_root() / "reports" / f"{category}-{day}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    refstore.write_atomic(path, page.encode("utf-8"))
    return {"status": "built", "path": path.relative_to(work_root()).as_posix(), "abs_path": str(path)}


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.report")
    parser.add_argument("category")
    parser.add_argument("--date")
    args = parser.parse_args(argv)
    return build(args.category, args.date)


if __name__ == "__main__":
    run(main)
