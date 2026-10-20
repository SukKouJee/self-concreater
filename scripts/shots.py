"""Vision 샷 분류: 분류할 대표 프레임을 고르고(context), Claude의 프레임별 분류를 장면 길이로 가중해 md에 쓴다(apply).

사용법 (작업 폴더에서):
  python -m scripts.shots pending <category>                          shots가 비어 있는 영상 목록
  python -m scripts.shots context <video_id> --category <cat>          분류할 프레임(최대 frames.shots_sample장)
  python -m scripts.shots apply <video_id> --category <cat> --labels <json>

- 프레임은 cuts.py가 저장한 장면 대표 프레임에서 고르게 뽑는다. 각 프레임은 가까운 장면들을 대표하고,
  그 장면 길이 합이 가중치가 된다(분포는 '영상 시간 중 몇 %'의 뜻)
- 결과는 영상 md 프런트매터 shots에만 쓴다. 본문은 건드리지 않는다
labels JSON: [{"frame", "size", "angle", "face", "hands_only", "broll", "text_overlay"}]
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import refstore
from scripts.common import TEMPLATES_DIR, ArgParser, ScriptError, run, work_root
from scripts.cuts import pick_frame_scenes
from scripts.features import SHOT_ANGLES, SHOT_FLAGS, SHOT_SIZES
from scripts.videomd import load_cuts, video_md_path

SAMPLE = json.loads((TEMPLATES_DIR / "thresholds.json").read_text("utf-8"))["frames"]["shots_sample"]


def _section_at(fm: dict, t: float) -> str | None:
    for s in fm.get("sections") or []:
        if s["start"] <= t < s["end"]:
            return s["label"]
    sections = fm.get("sections") or []
    return sections[-1]["label"] if sections else None


def context(video_id: str, category: str) -> dict:
    md = video_md_path(video_id, category)
    if not md.exists():
        raise ScriptError("video_not_found", f"영상 md가 없습니다: {video_id} (먼저 /yt-add)")
    fm, _ = refstore.read_md(md)
    cuts = load_cuts(video_id)
    scenes = [s for s in (cuts or {}).get("scenes", []) if s.get("frame")]
    if not scenes:
        raise ScriptError("no_frames", f"대표 프레임이 없습니다: {video_id} (cuts를 먼저 실행)")

    picked = pick_frame_scenes(len(scenes), SAMPLE)
    weights = [0.0] * len(picked)
    for j, scene in enumerate((cuts or {}).get("scenes", [])):
        nearest = min(range(len(picked)), key=lambda i: abs(scenes[picked[i]]["start"] - scene["start"]))
        weights[nearest] += scene["end"] - scene["start"]
    frames = []
    for i, idx in zip(range(len(picked)), picked):
        s = scenes[idx]
        frames.append({"frame": s["frame"], "start": s["start"], "end": s["end"], "camera": s.get("camera"),
                       "weight": round(weights[i], 2), "section": _section_at(fm, (s["start"] + s["end"]) / 2)})
    return {"id": video_id, "title": fm.get("title"), "has_shots": bool(fm.get("shots")), "frames": frames,
            "vocab": {"size": SHOT_SIZES, "angle": SHOT_ANGLES, "flags": SHOT_FLAGS}}


def pending(category: str) -> dict:
    videos = refstore.existing_category_dir(category) / "videos"
    todo = []
    for p in sorted(videos.glob("*.md")):
        if not refstore.read_md(p)[0].get("shots"):
            todo.append(p.stem)
    return {"category": category, "pending": todo}


def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_shots", f"샷 분류가 형식에 맞지 않습니다: {message}")


def apply(video_id: str, category: str, labels_path: Path) -> dict:
    ctx = context(video_id, category)
    by_frame = {f["frame"]: f for f in ctx["frames"]}
    try:
        labels = json.loads(Path(labels_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("invalid_shots", f"분류 JSON을 읽을 수 없습니다: {e}") from e
    if not labels:
        raise _invalid("분류가 비어 있습니다")
    for lab in labels:
        if lab.get("frame") not in by_frame:
            raise _invalid(f"이번 표본에 없는 프레임입니다: {lab.get('frame')}")
        if lab.get("size") not in SHOT_SIZES:
            raise _invalid(f"size={lab.get('size')!r} (허용: {', '.join(SHOT_SIZES)})")
        if lab.get("angle") not in SHOT_ANGLES:
            raise _invalid(f"angle={lab.get('angle')!r} (허용: {', '.join(SHOT_ANGLES)})")
        for flag in SHOT_FLAGS:
            if not isinstance(lab.get(flag), bool):
                raise _invalid(f"{flag}는 true/false여야 합니다 ({lab.get('frame')})")

    total = sum(by_frame[l["frame"]]["weight"] for l in labels) or 1.0
    share = lambda pred: round(sum(by_frame[l["frame"]]["weight"] for l in labels if pred(l)) / total, 2)
    by_section: dict[str, dict[str, float]] = {}
    for lab in labels:
        sec = by_frame[lab["frame"]]["section"]
        if sec:
            sizes = by_section.setdefault(sec, {})
            sizes[lab["size"]] = sizes.get(lab["size"], 0) + by_frame[lab["frame"]]["weight"]
    result = {
        "size": {s: v for s in SHOT_SIZES if (v := share(lambda l, s=s: l["size"] == s)) > 0},
        "angle": {a: v for a in SHOT_ANGLES if (v := share(lambda l, a=a: l["angle"] == a)) > 0},
        **{flag: share(lambda l, f=flag: l[f]) for flag in SHOT_FLAGS},
        "by_section": {sec: max(sizes, key=sizes.get) for sec, sizes in by_section.items()},
        "frames": len(labels),
    }
    refstore.update_frontmatter(video_md_path(video_id, category), {"shots": result})
    Path(labels_path).unlink(missing_ok=True)
    return {"status": "applied", "id": video_id, "frames": len(labels), "shots": result}


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.shots")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending").add_argument("category")
    c = sub.add_parser("context")
    c.add_argument("video_id")
    c.add_argument("--category", required=True)
    a = sub.add_parser("apply")
    a.add_argument("video_id")
    a.add_argument("--category", required=True)
    a.add_argument("--labels", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "pending":
        return pending(args.category)
    if args.cmd == "context":
        return context(args.video_id, args.category)
    path = Path(args.labels)
    return apply(args.video_id, args.category, path if path.is_absolute() else work_root() / path)


if __name__ == "__main__":
    run(main)
