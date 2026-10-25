"""단계별 실측 시간 기록(workspace/timings.json)과 예상 시간 계산.

기록·읽기 실패는 조용히 기본값으로 대체한다(진행률 표시가 본 작업을 막지 않게).
"""
from __future__ import annotations

import json
import os
import statistics
import time
from typing import Any

from scripts.common import workspace_dir

STEPS = ("fetch", "stt", "cuts", "analyze", "shots")
KEEP = 20
ENGINE_STEPS = ("analyze", "shots")

# 기본값(기록 없을 때). 컷: 2026-10-25 이 PC에서 43분 31초(2611초) 영상이 약 490초
# (360p 받기 + PySceneDetect + 장면별 카메라 흐름) -> 0.185 * 2611 + 7 ~ 490.
# stt는 보수적으로 영상 길이의 0.3배, analyze/shots는 Claude 호출이라 고정 추정값(가정).
DEFAULTS: dict[str, dict[str, float]] = {
    "fetch": {"per_video_sec": 0.0, "fixed_sec": 5.0},
    "stt": {"per_video_sec": 0.3, "fixed_sec": 10.0},
    "cuts": {"per_video_sec": 0.185, "fixed_sec": 7.0},
    "analyze": {"per_video_sec": 0.0, "fixed_sec": 90.0},
    "shots": {"per_video_sec": 0.0, "fixed_sec": 60.0},
}


def _check(step: str) -> None:
    if step not in STEPS:
        raise ValueError(f"알 수 없는 단계: {step!r}")


def _key(step: str, engine: str | None) -> str:
    return f"{step}:{engine}" if engine and step in ENGINE_STEPS else step


def _path():
    return workspace_dir("timings.json")


def _load() -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, list)}


def record(
    step: str,
    *,
    elapsed_sec: float,
    video_sec: float | None = None,
    engine: str | None = None,
    frames: int | None = None,
    text_chars: int | None = None,
) -> None:
    """정상 종료한 단계의 걸린 시간을 남긴다. 저장 실패는 무시한다(알 수 없는 단계만 ValueError)."""
    _check(step)
    entry: dict[str, Any] = {"elapsed_sec": float(elapsed_sec), "at": time.time()}
    if video_sec is not None:
        entry["video_sec"] = float(video_sec)
    if frames is not None:
        entry["frames"] = frames
    if text_chars is not None:
        entry["text_chars"] = text_chars
    try:
        data = _load()
        key = _key(step, engine)
        data[key] = [*data.get(key, []), entry][-KEEP:]
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return  # 진행률 기록은 부가 기능: 쓰기 실패가 본 작업을 막지 않는다


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def expected(step: str, *, video_sec: float | None, engine: str | None = None) -> float | None:
    """단계의 예상 소요 시간(초). 길이 비례 단계인데 길이를 모르면 None."""
    _check(step)
    default = DEFAULTS[step]
    proportional = default["per_video_sec"] > 0
    if proportional and not video_sec:
        return None
    default_value = default["per_video_sec"] * (video_sec or 0.0) + default["fixed_sec"]

    samples: list[float] = []
    for item in _load().get(_key(step, engine), []):
        if not isinstance(item, dict):
            continue
        elapsed = _num(item.get("elapsed_sec"))
        if elapsed is None:
            continue
        if proportional:
            length = _num(item.get("video_sec"))
            if not length or length <= 0:
                continue
            samples.append(elapsed / length)
        else:
            samples.append(elapsed)

    if len(samples) >= 3:
        median = statistics.median(samples)
        return median * video_sec if proportional else median  # type: ignore[operator]
    if not samples:
        return default_value
    values = [elapsed * video_sec if proportional else elapsed for elapsed in samples]  # type: ignore[operator]
    return statistics.fmean([default_value, *values])
