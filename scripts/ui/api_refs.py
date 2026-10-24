"""레퍼런스 API: 카테고리의 영상 목록, 영상 하나의 상세, 라벨 교정 저장.

- 목록은 `refs/ref_<카테고리>/videos/*.md`의 프런트매터만 읽어 요약을 만든다(role이 ref든 mine이든 전부).
  프런트매터가 깨진 파일은 목록에서 빼고 `skipped`로 센다(사용자가 고치는 중일 수 있다).
- 상세는 요약 + 본문(`## ` 제목별 조각) + 장면 프레임 + md 경로. 본문은 HTML을 해석하지 않고 문자열로만 내려준다.
- 쓰기는 `POST /api/refs/<id>/labels` 하나뿐이고, `videomd.relabel`이 프런트매터만 고친다.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, timezone
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
    return _json_safe(value) if isinstance(value, dict) else None


def _finite(value: Any) -> float:
    """Number -> float, but NaN/inf (YAML `.nan`/`.inf`) are rejected so the response stays strict JSON."""
    try:
        number = float(value or 0)
    except OverflowError as e:            # YAML 정수가 float 범위를 넘는 경우(예: 10**400)
        raise ValueError("number out of range") from e
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _text(value: Any) -> str | None:
    """Frontmatter scalar -> display string (YAML may load `2024-05-01` as a date or `1984` as an int)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, date)) and not isinstance(value, bool):
        return str(value)
    return None


def _duration(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _json_safe(value: Any) -> Any:
    """Recursively drop non-finite numbers and stringify keys/dates so json.dumps(allow_nan=False) cannot fail."""
    if isinstance(value, dict):
        cleaned = {str(k): _json_safe(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v is not _DROP}
    if isinstance(value, list):
        return [v for v in (_json_safe(i) for i in value) if v is not _DROP]
    if isinstance(value, float) and not math.isfinite(value):
        return _DROP
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


_DROP: Any = object()


def _sections(fm: dict) -> list[dict]:
    out = []
    raw = fm.get("sections")
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        item: dict[str, Any] = {"label": _text(s.get("label")), "start": _finite(s.get("start")),
                                "end": _finite(s.get("end"))}
        if s.get("sub"):
            item["sub"] = _text(s["sub"])
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
            start, end = _finite(scene.get("start")), _finite(scene.get("end"))
        except (OSError, ValueError, TypeError):
            continue
        out.append({"frame": frame, "start": start, "end": end})
    return out


def _summary(path: Path, fm: dict, frames: list[dict]) -> dict:
    added = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": path.stem, "role": _text(fm.get("role")), "title": _text(fm.get("title")),
        "channel": _text(fm.get("channel")), "duration_sec": _duration(fm.get("duration_sec")),
        "format": _text(fm.get("format")), "hook_type": _text(fm.get("hook_type")),
        "narrative_type": _text(fm.get("narrative_type")),
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
        if not ID_RE.fullmatch(path.stem):
            skipped += 1                  # 상세가 400으로 거절할 id는 목록에도 싣지 않는다
            continue
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
    try:
        fm, body = refstore.read_md(path)
    except ScriptError:
        raise
    except (OSError, ValueError) as e:
        raise ScriptError("bad_md", f"영상 md를 읽지 못했습니다: {video_id}") from e
    frames = _scene_frames(video_id)
    try:
        summary = _summary(path, fm, frames)
        for f in frames:
            f["section"] = _section_at(summary["sections"], (f["start"] + f["end"]) / 2)
    except (ValueError, TypeError, AttributeError) as e:
        raise ScriptError("bad_md", f"영상 md의 값이 올바르지 않습니다({video_id}): 섹션 시작/끝은 숫자여야 합니다") from e
    video = {**summary, "url": _youtube_url(fm.get("url")), "pacing": _dict_or_none(fm.get("pacing")),
             "camera": _dict_or_none(fm.get("camera"))}
    try:
        md = path.relative_to(work_root()).as_posix()
    except ValueError:
        md = path.resolve().relative_to(work_root().resolve()).as_posix()
    vocab = {"label": videomd.LABELS, "body_sub": videomd.BODY_SUBS,
             "hook_type": videomd.HOOK_TYPES, "narrative_type": videomd.NARRATIVE_TYPES}
    return {"video": video, "frames": frames, "body": _body_parts(body), "md": md,
            "hash": hashlib.sha256(path.read_bytes()).hexdigest(), "vocab": vocab}


@route("POST", r"/api/refs/(?P<video_id>[^/?]+)/labels")
def _labels(req: Request, video_id: str) -> dict:
    if not ID_RE.fullmatch(video_id):
        raise ScriptError("bad_request", f"잘못된 영상 id입니다: {video_id!r}")
    category = _category(req)
    refstore.existing_category_dir(category)
    body = req.json
    expected = body.get("hash")
    if not isinstance(expected, str) or not expected:
        raise ScriptError("bad_request", "hash가 필요합니다")
    changes = {k: body[k] for k in ("sections", "hook_type", "narrative_type") if k in body}
    result = videomd.relabel(video_id, category, changes, expected)
    path = videomd.video_md_path(video_id, category)
    try:
        fm, _ = refstore.read_md(path)
        video: dict | None = _summary(path, fm, _scene_frames(video_id))
    except (ScriptError, ValueError, TypeError, AttributeError, OSError):
        video = None                      # 저장은 끝났다. 화면은 상세를 다시 불러온다
    return {**result, "video": video}
