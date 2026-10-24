"""레퍼런스 API(읽기 전용): 카테고리의 영상 목록과 영상 하나의 상세.

- 목록은 `refs/ref_<카테고리>/videos/*.md`의 프런트매터만 읽어 요약을 만든다(role이 ref든 mine이든 전부).
  프런트매터가 깨진 파일은 목록에서 빼고 `skipped`로 센다(사용자가 고치는 중일 수 있다).
- 상세는 요약 + 본문(`## ` 제목별 조각) + 장면 프레임 + md 경로. 본문은 HTML을 해석하지 않고 문자열로만 내려준다.
- 어떤 파일도 쓰지 않는다.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import refstore, videomd
from scripts.common import ScriptError, work_root
from scripts.ui.server import Request, route

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
FRAMES_PREFIX = "workspace/frames/"
YOUTUBE_PREFIXES = (
    "https://www.youtube.com/", "https://youtube.com/", "https://m.youtube.com/", "https://youtu.be/",
)
HEADING_RE = re.compile(r"^## (.*)$", re.MULTILINE)


def _category(req: Request) -> str:
    category = req.query.get("category")
    if not category:
        raise ScriptError("bad_request", "category가 필요합니다")
    return str(category)


def _dict_or_none(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _sections(fm: dict) -> list[dict]:
    out = []
    raw = fm.get("sections")
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        item: dict[str, Any] = {"label": s.get("label"), "start": float(s.get("start") or 0),
                                "end": float(s.get("end") or 0)}
        if s.get("sub"):
            item["sub"] = s["sub"]
        out.append(item)
    return out


def _scene_frames(video_id: str) -> list[dict]:
    """컷 json의 장면 중 프레임 파일이 실제로 있는 것만. 깨진 컷 json은 프레임 없음으로 본다."""
    try:
        cuts = videomd.load_cuts(video_id)
    except (ValueError, OSError):
        return []
    scenes = cuts.get("scenes") if isinstance(cuts, dict) else None
    if not isinstance(scenes, list):
        return []
    root = work_root()
    base = (root / "workspace" / "frames").resolve()
    out = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        frame = scene.get("frame")
        if not isinstance(frame, str) or not frame.startswith(FRAMES_PREFIX) or "\\" in frame or "\x00" in frame:
            continue
        try:
            target = (root / frame).resolve()
            if not target.is_relative_to(base) or not target.is_file():
                continue
            start, end = float(scene.get("start") or 0), float(scene.get("end") or 0)
        except (OSError, ValueError, TypeError):
            continue
        out.append({"frame": frame, "start": start, "end": end})
    return out


def _summary(path: Path, fm: dict, frames: list[dict]) -> dict:
    added = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": path.stem, "role": fm.get("role"), "title": fm.get("title"), "channel": fm.get("channel"),
        "duration_sec": fm.get("duration_sec"), "format": fm.get("format"),
        "hook_type": fm.get("hook_type"), "narrative_type": fm.get("narrative_type"),
        "reviewed": bool(fm.get("reviewed")), "sections": _sections(fm),
        "thumb": frames[0]["frame"] if frames else None, "added": added,
    }


def _section_at(sections: list[dict], t: float) -> str | None:
    for i, s in enumerate(sections):
        last = i == len(sections) - 1
        if s["start"] <= t < s["end"] or (last and t == s["end"]):
            return s["label"]
    return None


def _body_parts(body: str) -> list[dict]:
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    marks = list(HEADING_RE.finditer(text))
    parts = []
    head = text[: marks[0].start()] if marks else text
    if head.strip():
        parts.append({"heading": "", "text": head.strip()})
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        parts.append({"heading": m.group(1).strip(), "text": text[m.end():end].strip()})
    return parts


def _youtube_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith(YOUTUBE_PREFIXES) else None


@route("GET", "/api/refs")
def _list(req: Request) -> dict:
    category = _category(req)
    folder = refstore.existing_category_dir(category) / "videos"
    videos = []
    skipped = 0
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            fm, _ = refstore.read_md(path)
            videos.append(_summary(path, fm, _scene_frames(path.stem)))
        except (ScriptError, ValueError, TypeError, AttributeError, OSError):
            skipped += 1                  # 한 파일이 깨졌다고 목록 전체를 막지 않는다
    return {"category": category, "videos": videos, "skipped": skipped}


@route("GET", r"/api/refs/(?P<video_id>[^/?]+)")
def _detail(req: Request, video_id: str) -> dict:
    if not ID_RE.fullmatch(video_id):
        raise ScriptError("bad_request", f"잘못된 영상 id입니다: {video_id!r}")
    category = _category(req)
    path = refstore.existing_category_dir(category) / "videos" / f"{video_id}.md"
    if not path.is_file():
        raise ScriptError("video_not_found", f"영상 md가 없습니다: {video_id}")
    fm, body = refstore.read_md(path)
    frames = _scene_frames(video_id)
    try:
        summary = _summary(path, fm, frames)
        for f in frames:
            f["section"] = _section_at(summary["sections"], (f["start"] + f["end"]) / 2)
    except (ValueError, TypeError, AttributeError) as e:
        raise ScriptError("bad_md", f"영상 md의 값이 올바르지 않습니다({video_id}): {e}") from e
    video = {**summary, "url": _youtube_url(fm.get("url")), "pacing": _dict_or_none(fm.get("pacing")),
             "camera": _dict_or_none(fm.get("camera"))}
    try:
        md = path.relative_to(work_root()).as_posix()
    except ValueError:
        md = path.resolve().relative_to(work_root().resolve()).as_posix()
    return {"video": video, "frames": frames, "body": _body_parts(body), "md": md}
