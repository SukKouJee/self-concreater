"""영상 메타데이터·자막 수집 → workspace/raw/{id}.json

사용법 (작업 폴더에서):
  python -m scripts.fetch <url> [--category <cat>] [--role ref|mine] [--force | --reanalyze]

- --category를 주면 중복을 검사한다: 같은 카테고리·같은 role의 md가 있으면 건너뛰고(status: exists),
  다른 role로 이미 등록된 영상이면 role_conflict.
- raw JSON이 이미 있으면 네트워크 없이 그대로 쓴다(status: cached). --force는 둘 다 무시하고 다시 받는다.
- --reanalyze: md가 있어도 건너뛰지 않되 수집은 캐시를 쓴다(분석 규칙이 바뀌었을 때 다시 분석)
- 자막 우선순위: 원어(수동 → 자동) → 수동 ko → 수동 en → 자동 ko → 자동 en → 그 외 수동 → 그 외 자동
  (원어는 yt-dlp의 language. 번역 자막보다 원어 자동 자막이 분석에 맞다)
- 초당 글자 수가 SPARSE_CHARS_PER_SEC 미만이면 sparse: true (말이 거의 없음, 자동 자막이 엉터리일 수 있음)
- 프록시: YT_PROXY 환경변수 (예: http://user:pass@host:port)
- 요청 간격: 네트워크 수집 사이에 최소 YT_FETCH_INTERVAL초(기본 3) 쉰다

실패 코드: invalid_url, private, unavailable, age_restricted, blocked, proxy_failed, no_transcript,
          role_conflict, category_not_found, fetch_failed, transcript_failed
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts import ctx, keys, limits, refstore
from scripts.common import ArgParser, Progress, ScriptError, run, safe_progress

SHORTS_MAX_SEC = 180
# 공백 제외 글자 수 / 영상 길이(초)가 이보다 낮으면 말이 거의 없는 영상으로 본다
SPARSE_CHARS_PER_SEC = 1.0  # 실측: 말하는 영상은 2.0 이상, "Love." 한마디짜리 33초 쇼츠가 0.67
# 연속 요청 간 최소 간격(초). 짧은 시간에 많이 요청하면 자막 요청이 IP 단위로 막힌다 (YT_FETCH_INTERVAL)
FETCH_INTERVAL_SEC = 3.0

BLOCKED_MESSAGE = (
    "YouTube가 이 네트워크의 요청을 막았습니다. 잠시 후 다시 시도하거나, "
    "YT_PROXY 환경변수에 프록시를 지정하세요 (예: YT_PROXY=http://user:pass@host:port)."
)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"}
_PATH_PREFIXES = ("shorts", "embed", "live", "v")

_META_KEYS = [
    "title", "channel", "channel_id", "uploader", "duration", "width", "height",
    "upload_date", "view_count", "like_count", "comment_count", "description",
    "tags", "chapters", "language",
]


# --- 링크 해석 ----------------------------------------------------------------

def parse_video_id(url: str) -> str:
    text = url.strip()
    if _ID_RE.match(text):
        return text
    if not re.match(r"^https?://", text):
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]

    candidate = None
    if host == "youtu.be" and parts:
        candidate = parts[0]
    elif host in _HOSTS:
        if parts[:1] == ["watch"]:
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
            candidate = parts[1]

    if candidate and _ID_RE.match(candidate):
        return candidate
    raise ScriptError("invalid_url", f"유튜브 영상 링크로 읽을 수 없습니다: {url!r}")


def canonical_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def detect_format(meta: dict, url: str) -> str:
    if "/shorts/" in url:
        return "shorts"
    duration, width, height = meta.get("duration"), meta.get("width"), meta.get("height")
    if duration and width and height and duration <= SHORTS_MAX_SEC and height > width:
        return "shorts"
    return "long"


# --- 네트워크 (테스트에서 교체) -------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_error(message: str) -> str:
    """yt-dlp 에러에서 색상 코드, 'ERROR:' 머리말, 버그 신고 안내를 뗀다."""
    text = _ANSI_RE.sub("", message).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    return re.split(r";\s*please report this issue", text)[0].strip()


class _QuietLogger:
    """yt-dlp가 stderr에 직접 찍지 않게 한다 (에러는 예외로 받아 JSON으로 낸다)."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def ytdlp_options(proxy: str | None, **extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "logger": _QuietLogger(), **extra}
    if proxy:
        opts["proxy"] = proxy
    return opts


def classify_ytdlp_error(message: str) -> str:
    m = message.lower()
    if "proxy" in m:
        return "proxy_failed"
    if "confirm your age" in m or "age-restricted" in m:
        return "age_restricted"
    if "not a bot" in m or "429" in m or "too many requests" in m:
        return "blocked"
    if "private video" in m or "members-only" in m or "join this channel" in m:
        return "private"
    if "unavailable" in m or "removed" in m or "not available" in m:
        return "unavailable"
    return "fetch_failed"


_YTDLP_MESSAGES = {
    "private": "비공개 또는 멤버십 전용 영상이라 볼 수 없습니다.",
    "unavailable": "삭제됐거나 볼 수 없는 영상입니다.",
    "age_restricted": "연령 제한 영상이라 로그인 없이는 수집할 수 없습니다.",
    "blocked": BLOCKED_MESSAGE,
    "proxy_failed": "YT_PROXY에 지정한 프록시에 연결할 수 없습니다. 주소·포트·계정을 확인하세요.",
}


def ytdlp_error(e: Exception, action: str) -> ScriptError:
    code = classify_ytdlp_error(str(e))
    return ScriptError(code, _YTDLP_MESSAGES.get(code, f"{action} 실패: {clean_error(str(e))}"))


def get_metadata(video_id: str, proxy: str | None) -> dict:
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(ytdlp_options(proxy, skip_download=True)) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ytdlp_error(e, "메타데이터 수집") from e
    return {"id": video_id, **{k: info.get(k) for k in _META_KEYS}}


def pick_transcript(transcript_list, original: str | None = None):
    """선호 순서대로 자막 트랙을 고른다. 없으면 None."""
    from youtube_transcript_api import NoTranscriptFound

    preferred = []
    if original:
        preferred = [
            (transcript_list.find_manually_created_transcript, [original]),
            (transcript_list.find_generated_transcript, [original]),
        ]
    for finder, langs in preferred + [
        (transcript_list.find_manually_created_transcript, ["ko"]),
        (transcript_list.find_manually_created_transcript, ["en"]),
        (transcript_list.find_generated_transcript, ["ko"]),
        (transcript_list.find_generated_transcript, ["en"]),
    ]:
        try:
            return finder(langs)
        except NoTranscriptFound:
            pass
    tracks = list(transcript_list)
    manual = [t for t in tracks if not t.is_generated]
    return (manual or tracks or [None])[0]


def get_transcript(video_id: str, proxy: str | None, original: str | None = None) -> dict | None:
    import youtube_transcript_api as yta
    from youtube_transcript_api.proxies import GenericProxyConfig

    config = GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None
    try:
        track = pick_transcript(yta.YouTubeTranscriptApi(proxy_config=config).list(video_id), original)
        if track is None:
            return None
        fetched = track.fetch()
    except (yta.TranscriptsDisabled, yta.NoTranscriptFound):
        return None
    except yta.RequestBlocked as e:  # IpBlocked 포함
        raise ScriptError("blocked", BLOCKED_MESSAGE) from e
    except (yta.VideoUnavailable, yta.VideoUnplayable) as e:
        raise ScriptError("unavailable", _YTDLP_MESSAGES["unavailable"]) from e
    except yta.AgeRestricted as e:
        raise ScriptError("age_restricted", _YTDLP_MESSAGES["age_restricted"]) from e
    except yta.CouldNotRetrieveTranscript as e:
        raise ScriptError("transcript_failed", f"자막 수집 실패: {type(e).__name__}") from e

    return {
        "source": "auto" if track.is_generated else "cc",
        "language": track.language_code,
        "segments": [
            {"start": round(s.start, 2), "dur": round(s.duration, 2), "text": s.text}
            for s in fetched.snippets
        ],
    }


# --- 수집 흐름 ----------------------------------------------------------------

META_TEXT = "영상 정보·자막 받는 중"


# 한 프로세스 안 작업 스레드들이 `.last_fetch`를 동시에 읽고 같은 순간에 받지 않게 한다(서버판은 프로세스 하나라
# 이것이 서버 전체 YouTube 간격이다, 서버 설계 §5.2). 잠금은 순번 예약만 하고, 기다림은 잠금 밖에서 한다.
_TURN_LOCK = threading.Lock()


def _reserve_turn(stamp: Path, interval: float) -> float:
    """다음 수집 시각(마지막 예약 + 간격, 지났으면 지금)을 예약해 `.last_fetch`에 쓰고 돌려준다."""
    with _TURN_LOCK:
        now = time.time()
        turn = now
        if stamp.exists():
            try:
                turn = max(now, float(stamp.read_text("utf-8")) + interval)
            except ValueError:
                turn = now
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(turn), encoding="utf-8")
        return turn


def wait_turn(progress: Progress | None = None) -> None:
    """마지막 네트워크 수집 이후 최소 간격이 지날 때까지 기다린다 (프로세스 간 공유).

    progress가 있으면 1초마다 ("wait", None, "차단 방지 대기 N초")로 남은 대기를 알린다.
    같은 프로세스의 여러 스레드는 순번을 하나씩 예약하므로 간격을 두고 차례로 받는다.
    """
    interval = float(os.environ.get("YT_FETCH_INTERVAL") or FETCH_INTERVAL_SEC)
    stamp = ctx.here().scratch.local_path(".last_fetch")
    report = safe_progress(progress)
    wait = _reserve_turn(stamp, interval) - time.time()
    if wait > 0 and report is None:
        time.sleep(wait)
    while wait > 0 and report is not None:
        report("wait", None, f"차단 방지 대기 {math.ceil(wait)}초")
        step = min(1.0, wait)
        time.sleep(step)
        wait -= step


def load_raw(video_id: str) -> dict:
    shared = ctx.here().shared
    key = keys.raw(video_id)
    if not shared.exists(key):
        raise ScriptError("raw_not_found", f"수집 데이터가 없습니다: {video_id} (먼저 fetch)")
    return json.loads(shared.get(key).decode("utf-8"))


def save_raw(data: dict) -> str:
    """수집 데이터를 공유 캐시에 쓴다. 돌려주는 값은 키(`workspace/raw/<id>.json`)."""
    key = keys.raw(data["id"])
    # write_text 기본 줄바꿈(Windows CRLF)과 같은 바이트
    ctx.here().shared.put(key, refstore.text_bytes(json.dumps(data, ensure_ascii=False, indent=2)))
    return key


def check_duplicates(video_id: str, category: str, role: str) -> tuple[dict | None, list[str]]:
    refstore.require_category(category)
    found = refstore.find_video(video_id)
    conflict = [f for f in found if f["role"] != role]
    if conflict:
        f = conflict[0]
        raise ScriptError(
            "role_conflict",
            f"이 영상은 이미 {f['category']} 카테고리에 {f['role']}로 등록돼 있습니다. "
            "같은 영상을 ref와 mine에 동시에 넣을 수 없습니다.",
            md=f["md"],
        )
    same = next((f for f in found if f["category"] == category), None)
    others = [f["category"] for f in found if f["category"] != category]
    return same, others


def speech_density(segments: list[dict], duration: float | None) -> tuple[float | None, bool]:
    """(초당 글자 수, sparse 여부). 길이를 모르면 (None, False)."""
    if not duration:
        return None, False
    chars = sum(len(re.sub(r"\s", "", s["text"])) for s in segments)
    return round(chars / duration, 2), chars / duration < SPARSE_CHARS_PER_SEC


def _summary(data: dict, status: str, also_in: list[str]) -> dict:
    meta, transcript = data["meta"], data["transcript"] or {}
    cps, sparse = speech_density(transcript.get("segments", []), meta.get("duration"))
    return {
        "id": data["id"],
        "status": status,
        "raw": keys.raw(data["id"]),
        "title": meta.get("title"),
        "channel": meta.get("channel"),
        "duration_sec": meta.get("duration"),
        "format": data["format"],
        "transcript_source": transcript.get("source"),
        "language": transcript.get("language"),
        "segments": len(transcript.get("segments", [])),
        "chars_per_sec": cps,
        "sparse": sparse,
        "also_in": also_in,
    }


def fetch(url: str, category: str | None = None, role: str = "ref", force: bool = False,
          reanalyze: bool = False, progress: Progress | None = None) -> dict:
    """progress가 있으면 네트워크로 받을 때만 차단 방지 대기("wait")와 메타·자막 받기("meta")를 알린다."""
    video_id = parse_video_id(url)

    also_in: list[str] = []
    if category:
        same, also_in = check_duplicates(video_id, category, role)
        if same and not (force or reanalyze):
            return {"id": video_id, "status": "exists", "md": same["md"], "also_in": also_in}

    report = safe_progress(progress)
    # 캐시 확인 → 받기 → 저장을 한 잠금 안에서: 같은 영상을 동시에 넣은 다른 사용자는 기다렸다가 캐시를 쓴다
    with limits.collecting(video_id, on_wait=limits.collect_waiter(report)):
        if ctx.here().shared.exists(keys.raw(video_id)) and not force:
            data, status = load_raw(video_id), "cached"
        else:
            proxy = os.environ.get("YT_PROXY") or None
            wait_turn(report)
            if report is not None:
                report("meta", None, META_TEXT)
            meta = get_metadata(video_id, proxy)
            transcript = get_transcript(video_id, proxy, meta.get("language"))
            data = {
                "id": video_id,
                "url": canonical_url(video_id),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "format": detect_format(meta, url),
                "meta": meta,
                "transcript": transcript,
            }
            save_raw(data)
            status = "fetched"

    summary = _summary(data, status, also_in)
    if not data["transcript"]:
        raise ScriptError(
            "no_transcript",
            "이 영상에는 쓸 수 있는 자막이 없습니다. 음성 인식(STT)으로 만들 수 있습니다: "
            f"python -m scripts.stt {video_id}",
            raw_status=status,
            **{k: summary[k] for k in ["id", "raw", "title", "duration_sec", "format"]},
        )
    return summary


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.fetch")
    parser.add_argument("url")
    parser.add_argument("--category")
    parser.add_argument("--role", choices=["ref", "mine"], default="ref")
    parser.add_argument("--force", action="store_true", help="캐시를 무시하고 다시 수집")
    parser.add_argument("--reanalyze", action="store_true", help="md가 있어도 건너뛰지 않음 (수집은 캐시 사용)")
    args = parser.parse_args(argv)
    return fetch(args.url, args.category, args.role, args.force, args.reanalyze)


if __name__ == "__main__":
    run(main)
