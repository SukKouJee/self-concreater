"""Vision 샷 분류: 분류할 대표 프레임을 고르고(context), Claude의 프레임별 분류를 장면 길이로 가중해 md에 쓴다(apply).

사용법 (작업 폴더에서):
  python -m scripts.shots pending <category>                          shots가 비었거나 프레임별 라벨이 없는 영상 목록
  python -m scripts.shots context <video_id> --category <cat>          분류할 프레임(최대 frames.shots_sample장)
  python -m scripts.shots apply <video_id> --category <cat> --labels <json>
  python -m scripts.shots describe-context <video_id> --category <cat>   설명(desc)이 없는 저장 프레임 최대 8장
  python -m scripts.shots apply-desc <video_id> --category <cat> --labels <json>
  python -m scripts.shots desc-status <category>                       전체 프레임 수·설명 있는 수·남은 영상

- 프레임은 cuts.py가 저장한 장면 대표 프레임에서 고르게 뽑는다. 각 프레임은 가까운 장면들을 대표하고,
  그 장면 길이 합이 가중치가 된다(분포는 '영상 시간 중 몇 %'의 뜻)
- 결과는 영상 md 프런트매터 shots에만 쓴다. 본문은 건드리지 않는다
- 프레임별 라벨(+섹션)은 workspace/shots/{id}.json에 남긴다 (콘티 참고 프레임 매칭용, 기계용 원본층)
labels JSON: [{"frame", "size", "angle", "face", "hands_only", "broll", "text_overlay", "desc"?}]
- desc: 프레임 한 줄 설명(1~60자). 콘티 장면에 맞는 레퍼런스 사진을 글로 고르는 데 쓴다. 영상 md에는 쓰지 않는다
- apply-desc는 영상의 저장된 대표 프레임 전부(분류 표본 밖 포함)가 대상이다. 이미 라벨이 있는 프레임은 desc만 갱신하고,
  없는 프레임은 size·angle·표시까지 받아 새 라벨로 더한다. md의 shots 비율은 바꾸지 않는다(분류 표본으로만 계산).
  파일이 지워진 프레임은 건너뛰고 세지 않는다
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import refstore
from scripts.common import TEMPLATES_DIR, ArgParser, ScriptError, run, work_root, workspace_dir
from scripts.cuts import pick_frame_scenes
from scripts.features import SHOT_ANGLES, SHOT_FLAGS, SHOT_SIZES
from scripts.videomd import load_cuts, video_md_path

DESC_MAX = 60
DESC_BATCH = 8
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
    todo, relabel = [], []
    for p in sorted(videos.glob("*.md")):
        if not refstore.read_md(p)[0].get("shots"):
            todo.append(p.stem)
        elif not workspace_dir("shots", f"{p.stem}.json").exists():
            relabel.append(p.stem)          # 비율만 있고 프레임별 라벨이 없음(Phase 12 이전 분류) → 콘티 참고 프레임용
    return {"category": category, "pending": todo + relabel, "relabel": relabel}


def _invalid(message: str) -> ScriptError:
    return ScriptError("invalid_shots", f"샷 분류가 형식에 맞지 않습니다: {message}")


def _check_shot(lab: dict) -> None:
    if lab.get("size") not in SHOT_SIZES:
        raise _invalid(f"size={lab.get('size')!r} (허용: {', '.join(SHOT_SIZES)})")
    if lab.get("angle") not in SHOT_ANGLES:
        raise _invalid(f"angle={lab.get('angle')!r} (허용: {', '.join(SHOT_ANGLES)})")
    for flag in SHOT_FLAGS:
        if not isinstance(lab.get(flag), bool):
            raise _invalid(f"{flag}는 true/false여야 합니다 ({lab.get('frame')})")


def _check_desc(lab: dict) -> str:
    desc = lab.get("desc")
    text = desc.strip() if isinstance(desc, str) else ""
    if not 1 <= len(text) <= DESC_MAX:
        raise _invalid(f"desc는 1~{DESC_MAX}자 문장이어야 합니다 ({lab.get('frame')})")
    return text


def _labels_file(video_id: str) -> Path:
    return workspace_dir("shots", f"{video_id}.json")


def _read_labels(video_id: str) -> list[dict]:
    try:
        data = json.loads(_labels_file(video_id).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    labels = data.get("labels") if isinstance(data, dict) else None
    return [l for l in labels if isinstance(l, dict) and l.get("frame")] if isinstance(labels, list) else []


def _write_labels(video_id: str, labels: list[dict]) -> None:
    out = _labels_file(video_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"id": video_id, "labels": labels}, ensure_ascii=False, indent=1), encoding="utf-8")


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
        _check_shot(lab)
        if "desc" in lab:
            lab["desc"] = _check_desc(lab)

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
    old = {l["frame"]: l for l in _read_labels(video_id)}
    fresh = {}
    for lab in labels:
        entry = {k: lab[k] for k in ("frame", "size", "angle", *SHOT_FLAGS)}
        entry["section"] = by_frame[lab["frame"]]["section"]
        desc = lab.get("desc") or old.get(lab["frame"], {}).get("desc")
        if desc:
            entry["desc"] = desc
        fresh[lab["frame"]] = entry
    # 표본 밖 프레임의 라벨(apply-desc가 더한 것)은 그대로 둔다
    _write_labels(video_id, [*fresh.values(), *(l for f, l in old.items() if f not in fresh)])
    Path(labels_path).unlink(missing_ok=True)
    return {"status": "applied", "id": video_id, "frames": len(labels), "shots": result}


# ---- 사진 설명(desc) -------------------------------------------------------------------

def _stored_frames(video_id: str) -> list[dict]:
    """cuts가 저장한 장면 대표 프레임 전부(장면 순서). 파일이 지워진 것은 뺀다."""
    root = work_root()
    scenes = (load_cuts(video_id) or {}).get("scenes", [])
    return [s for s in scenes if s.get("frame") and (root / s["frame"]).is_file()]


def _undescribed(video_id: str) -> list[dict]:
    described = {l["frame"] for l in _read_labels(video_id) if l.get("desc")}
    return [s for s in _stored_frames(video_id) if s["frame"] not in described]


def _video_fm(video_id: str, category: str) -> dict:
    md = video_md_path(video_id, category)
    if not md.exists():
        raise ScriptError("video_not_found", f"영상 md가 없습니다: {video_id} (먼저 /yt-add)")
    return refstore.read_md(md)[0]


def describe_context(video_id: str, category: str, limit: int = DESC_BATCH) -> dict:
    """저장된 대표 프레임 중 desc가 아직 없는 것 최대 limit장. 이미 라벨이 있으면 size·angle도 싣는다."""
    fm = _video_fm(video_id, category)
    if not any(s.get("frame") for s in (load_cuts(video_id) or {}).get("scenes", [])):
        raise ScriptError("no_frames", f"대표 프레임이 없습니다: {video_id} (cuts를 먼저 실행)")
    labelled = {l["frame"]: l for l in _read_labels(video_id)}
    todo = _undescribed(video_id)
    frames = []
    for s in todo[:limit]:
        item = {"frame": s["frame"], "start": s["start"], "end": s["end"], "camera": s.get("camera"),
                "section": _section_at(fm, (s["start"] + s["end"]) / 2)}
        if s["frame"] in labelled:
            item.update(size=labelled[s["frame"]].get("size"), angle=labelled[s["frame"]].get("angle"))
        frames.append(item)
    return {"id": video_id, "title": fm.get("title"), "frames": frames, "remaining": len(todo),
            "vocab": {"size": SHOT_SIZES, "angle": SHOT_ANGLES, "flags": SHOT_FLAGS}}


def apply_desc(video_id: str, category: str, labels_path: Path) -> dict:
    """프레임별 desc를 라벨 파일에 병합한다. 라벨이 있는 프레임은 desc만, 없는 프레임은 새 라벨로 더한다."""
    fm = _video_fm(video_id, category)
    stored = {s["frame"]: s for s in (load_cuts(video_id) or {}).get("scenes", []) if s.get("frame")}
    try:
        labels = json.loads(Path(labels_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScriptError("invalid_shots", f"설명 JSON을 읽을 수 없습니다: {e}") from e
    if not isinstance(labels, list) or not labels:
        raise _invalid("설명이 비어 있습니다")
    old = {l["frame"]: l for l in _read_labels(video_id)}
    merged = dict(old)
    for lab in labels:
        if not isinstance(lab, dict) or lab.get("frame") not in stored:
            raise _invalid(f"이 영상의 대표 프레임이 아닙니다: {lab.get('frame') if isinstance(lab, dict) else lab!r}")
        desc = _check_desc(lab)
        if lab["frame"] in old:
            merged[lab["frame"]] = {**old[lab["frame"]], "desc": desc}
            continue
        _check_shot(lab)
        scene = stored[lab["frame"]]
        merged[lab["frame"]] = {**{k: lab[k] for k in ("frame", "size", "angle", *SHOT_FLAGS)},
                                "section": _section_at(fm, (scene["start"] + scene["end"]) / 2), "desc": desc}
    _write_labels(video_id, list(merged.values()))
    Path(labels_path).unlink(missing_ok=True)
    return {"status": "applied", "id": video_id, "described": len(labels), "remaining": len(_undescribed(video_id))}


def desc_status(category: str) -> dict:
    """카테고리 레퍼런스의 저장 프레임 수, 설명이 있는 수, 아직 남은 영상(mine은 세지 않는다)."""
    videos = refstore.existing_category_dir(category) / "videos"
    total = done = 0
    todo = []
    for p in sorted(videos.glob("*.md")):
        if refstore.read_md(p)[0].get("role", "ref") == "mine":
            continue
        frames = _stored_frames(p.stem)
        left = len(_undescribed(p.stem))
        total += len(frames)
        done += len(frames) - left
        if left:
            todo.append(p.stem)
    return {"frames": total, "described": done, "videos_todo": todo}


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
    dc = sub.add_parser("describe-context")
    dc.add_argument("video_id")
    dc.add_argument("--category", required=True)
    ad = sub.add_parser("apply-desc")
    ad.add_argument("video_id")
    ad.add_argument("--category", required=True)
    ad.add_argument("--labels", required=True)
    sub.add_parser("desc-status").add_argument("category")
    args = parser.parse_args(argv)
    if args.cmd == "pending":
        return pending(args.category)
    if args.cmd == "context":
        return context(args.video_id, args.category)
    if args.cmd == "describe-context":
        return describe_context(args.video_id, args.category)
    if args.cmd == "desc-status":
        return desc_status(args.category)
    path = Path(args.labels)
    path = path if path.is_absolute() else work_root() / path
    if args.cmd == "apply-desc":
        return apply_desc(args.video_id, args.category, path)
    return apply(args.video_id, args.category, path)


if __name__ == "__main__":
    run(main)
