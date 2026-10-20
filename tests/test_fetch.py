import json

import pytest

from scripts import fetch, refstore
from scripts.common import ScriptError

VID = "dQw4w9WgXcQ"

META = {
    "id": VID, "title": "제목", "channel": "채널", "channel_id": "UC1",
    "duration": 20, "width": 1080, "height": 1920, "upload_date": "20260101",
    "view_count": 10, "like_count": 1, "comment_count": 0,
    "description": "설명", "tags": ["a"], "chapters": None, "language": "ko",
}
TRANSCRIPT = {"source": "cc", "language": "ko",
              "segments": [{"start": 0.0, "dur": 1.5, "text": "안녕하세요 오늘은 라면 다섯 봉지를 먹어 볼게요"},
                           {"start": 2.0, "dur": 3.0, "text": "물은 많이 넣고 스프는 조금만 넣는 게 제 비법입니다"}]}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    monkeypatch.delenv("YT_PROXY", raising=False)
    return tmp_path


@pytest.fixture
def net(monkeypatch):
    """네트워크 호출을 가짜로 바꾸고 호출 횟수를 센다."""
    calls = {"meta": 0, "transcript": 0, "proxy": []}

    def get_metadata(video_id, proxy):
        calls["meta"] += 1
        calls["proxy"].append(proxy)
        return dict(META, id=video_id)

    def get_transcript(video_id, proxy, original=None):
        calls["transcript"] += 1
        calls["original"] = original
        return calls.get("transcript_value", TRANSCRIPT)

    monkeypatch.setattr(fetch, "get_metadata", get_metadata)
    monkeypatch.setattr(fetch, "get_transcript", get_transcript)
    calls["sleeps"] = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: calls["sleeps"].append(s))
    return calls


# --- video id -----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VID}",
    f"https://youtube.com/watch?v={VID}&t=30s",
    f"https://m.youtube.com/watch?feature=share&v={VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://youtube.com/shorts/{VID}?si=abc",
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?si=xyz",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube.com/live/{VID}",
    f"youtube.com/shorts/{VID}",
    VID,
    f"  {VID}  ",
])
def test_parse_video_id(url):
    assert fetch.parse_video_id(url) == VID


@pytest.mark.parametrize("url", [
    "", "hello", "https://vimeo.com/123456", "https://www.youtube.com/watch?v=short",
    "https://www.youtube.com/@channel", "https://www.youtube.com/playlist?list=PL1",
])
def test_parse_video_id_rejects(url):
    with pytest.raises(ScriptError) as exc:
        fetch.parse_video_id(url)
    assert exc.value.code == "invalid_url"


# --- 형식 판단 ----------------------------------------------------------------

@pytest.mark.parametrize("meta, url, expected", [
    ({"duration": 58, "width": 1080, "height": 1920}, "https://youtu.be/x", "shorts"),
    ({"duration": 170, "width": 720, "height": 1280}, "https://youtu.be/x", "shorts"),
    ({"duration": 58, "width": 1920, "height": 1080}, "https://youtu.be/x", "long"),
    ({"duration": 600, "width": 1080, "height": 1920}, "https://youtu.be/x", "long"),
    ({"duration": 600, "width": None, "height": None}, "https://youtube.com/shorts/x", "shorts"),
])
def test_detect_format(meta, url, expected):
    assert fetch.detect_format(meta, url) == expected


# --- 에러 분류 ----------------------------------------------------------------

@pytest.mark.parametrize("message, code", [
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private"),
    ("ERROR: [youtube] x: Sign in to confirm you’re not a bot.", "blocked"),
    ("ERROR: unable to download webpage: HTTP Error 429: Too Many Requests", "blocked"),
    ("ERROR: [youtube] x: Sign in to confirm your age.", "age_restricted"),
    ("ERROR: [youtube] x: Join this channel to get access to members-only content", "private"),
    ("ERROR: [youtube] x: Video unavailable", "unavailable"),
    ("ERROR: [youtube] x: This video has been removed by the uploader", "unavailable"),
    ("ERROR: Unable to download API page: ('Unable to connect to proxy', ...)", "proxy_failed"),
    ("ERROR: something else", "fetch_failed"),
])
def test_classify_ytdlp_error(message, code):
    assert fetch.classify_ytdlp_error(message) == code


def test_clean_error_strips_ansi_and_report_footer():
    raw = ("\x1b[0;31mERROR:\x1b[0m [youtube] x: boom; please report this issue on "
           "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the template")
    assert fetch.clean_error(raw) == "[youtube] x: boom"


# --- 자막 트랙 선택 -------------------------------------------------------------

class FakeTrack:
    def __init__(self, lang, generated):
        self.language_code = lang
        self.is_generated = generated


class FakeList:
    def __init__(self, tracks):
        self.tracks = tracks

    def __iter__(self):
        return iter(self.tracks)

    def _find(self, langs, generated):
        for lang in langs:
            for t in self.tracks:
                if t.language_code == lang and t.is_generated == generated:
                    return t
        from youtube_transcript_api import NoTranscriptFound
        raise NoTranscriptFound("x", langs, None)

    def find_manually_created_transcript(self, langs):
        return self._find(langs, False)

    def find_generated_transcript(self, langs):
        return self._find(langs, True)


@pytest.mark.parametrize("tracks, expected", [
    ([("en", False), ("ko", True), ("ko", False)], ("ko", False)),   # 수동 ko 최우선
    ([("ko", True), ("en", False)], ("en", False)),                  # 수동 en > 자동 ko
    ([("ja", False), ("ko", True), ("en", True)], ("ko", True)),      # 자동 ko > 다른 언어 수동
    ([("ja", True), ("en", True)], ("en", True)),
    ([("ja", True), ("fr", False)], ("fr", False)),                  # 그 외: 수동 우선
    ([("ja", True)], ("ja", True)),
    ([], None),
])
def test_pick_transcript_order(tracks, expected):
    picked = fetch.pick_transcript(FakeList([FakeTrack(*t) for t in tracks]))
    got = None if picked is None else (picked.language_code, picked.is_generated)
    assert got == expected


@pytest.mark.parametrize("original, tracks, expected", [
    # 원어(ko) 자동 자막이 번역된 수동 en보다 우선
    ("ko", [("en", False), ("ja", False), ("ko", True)], ("ko", True)),
    # 원어 수동 > 원어 자동
    ("ja", [("ja", True), ("ja", False), ("ko", False)], ("ja", False)),
    # 원어 트랙이 없으면 기본 순서
    ("ja", [("en", False), ("ko", True)], ("en", False)),
    (None, [("en", False), ("ko", True)], ("en", False)),
])
def test_pick_transcript_prefers_original_language(original, tracks, expected):
    picked = fetch.pick_transcript(FakeList([FakeTrack(*t) for t in tracks]), original)
    got = None if picked is None else (picked.language_code, picked.is_generated)
    assert got == expected


# --- fetch 흐름 ---------------------------------------------------------------

def test_fetch_writes_raw_json(home, net):
    out = fetch.fetch(f"https://youtube.com/shorts/{VID}")
    raw = json.loads((home / "workspace" / "raw" / f"{VID}.json").read_text("utf-8"))
    assert raw["id"] == VID
    assert raw["url"] == f"https://youtu.be/{VID}"
    assert raw["format"] == "shorts"
    assert raw["meta"]["title"] == "제목"
    assert raw["transcript"]["segments"][0]["text"].startswith("안녕하세요")
    assert out == {
        "id": VID, "status": "fetched", "raw": f"workspace/raw/{VID}.json",
        "title": "제목", "channel": "채널", "duration_sec": 20, "format": "shorts",
        "transcript_source": "cc", "language": "ko", "segments": 2,
        "chars_per_sec": 2.05, "sparse": False, "also_in": [],
    }


def test_fetch_passes_original_language(home, net):
    fetch.fetch(VID)
    assert net["original"] == "ko"


def test_sparse_transcript_is_flagged(home, net):
    net["transcript_value"] = {"source": "auto", "language": "en",
                               "segments": [{"start": 1.0, "dur": 1.0, "text": "Ki"},
                                            {"start": 9.0, "dur": 1.0, "text": "M ah"}]}
    out = fetch.fetch(VID)
    assert out["sparse"] is True
    assert out["chars_per_sec"] == 0.25


@pytest.mark.parametrize("chars, duration, sparse", [(0, 30, True), (29, 30, True), (30, 30, False), (10, None, False)])
def test_is_sparse(chars, duration, sparse):
    segments = [{"start": 0, "dur": 1, "text": "가" * chars}]
    assert fetch.speech_density(segments, duration)[1] is sparse


def test_fetch_uses_cache_without_network(home, net):
    fetch.fetch(VID)
    out = fetch.fetch(VID)
    assert out["status"] == "cached"
    assert net["meta"] == 1 and net["transcript"] == 1


def test_fetch_force_refetches(home, net):
    fetch.fetch(VID)
    assert fetch.fetch(VID, force=True)["status"] == "fetched"
    assert net["meta"] == 2


def test_fetch_passes_proxy(home, net, monkeypatch):
    monkeypatch.setenv("YT_PROXY", "http://proxy:8080")
    fetch.fetch(VID)
    assert net["proxy"] == ["http://proxy:8080"]


def test_fetch_without_transcript_saves_meta_and_reports(home, net):
    net["transcript_value"] = None
    with pytest.raises(ScriptError) as exc:
        fetch.fetch(VID)
    assert exc.value.code == "no_transcript"
    assert exc.value.details["raw"] == f"workspace/raw/{VID}.json"
    raw = json.loads((home / "workspace" / "raw" / f"{VID}.json").read_text("utf-8"))
    assert raw["transcript"] is None
    # 캐시에서 다시 읽어도 같은 안내
    with pytest.raises(ScriptError) as exc:
        fetch.fetch(VID)
    assert exc.value.code == "no_transcript"
    assert net["meta"] == 1


def test_blocked_has_proxy_guidance(home, monkeypatch):
    def blocked(video_id, proxy):
        raise ScriptError("blocked", fetch.BLOCKED_MESSAGE)
    monkeypatch.setattr(fetch, "get_metadata", blocked)
    with pytest.raises(ScriptError) as exc:
        fetch.fetch(VID)
    assert exc.value.code == "blocked"
    assert "YT_PROXY" in exc.value.message
    assert not (home / "workspace" / "raw" / f"{VID}.json").exists()


# --- 중복 검사 ----------------------------------------------------------------

def add_video_md(home, category, role):
    refstore.init_category(category)
    path = home / "refs" / f"ref_{category}" / "videos" / f"{VID}.md"
    path.write_text(f"---\nid: {VID}\nrole: {role}\n---\n## 내 메모\n", encoding="utf-8")


def test_fetch_existing_md_in_same_category_is_skipped(home, net):
    add_video_md(home, "cooking", "ref")
    out = fetch.fetch(VID, category="cooking", role="ref")
    assert out["status"] == "exists"
    assert out["md"] == f"refs/ref_cooking/videos/{VID}.md"
    assert net["meta"] == 0


def test_fetch_existing_md_with_force_refetches(home, net):
    add_video_md(home, "cooking", "ref")
    assert fetch.fetch(VID, category="cooking", role="ref", force=True)["status"] == "fetched"


def test_fetch_reanalyze_skips_exists_but_uses_cache(home, net):
    add_video_md(home, "cooking", "ref")
    fetch.save_raw({"id": VID, "url": f"https://youtu.be/{VID}", "format": "shorts",
                    "meta": dict(META), "transcript": TRANSCRIPT})
    out = fetch.fetch(VID, category="cooking", role="ref", reanalyze=True)
    assert out["status"] == "cached"
    assert net["meta"] == 0


def test_fetch_role_conflict(home, net):
    add_video_md(home, "cooking", "ref")
    with pytest.raises(ScriptError) as exc:
        fetch.fetch(VID, category="cooking", role="mine")
    assert exc.value.code == "role_conflict"
    assert net["meta"] == 0


def test_fetch_role_conflict_across_categories(home, net):
    add_video_md(home, "vlog", "mine")
    refstore.init_category("cooking")
    with pytest.raises(ScriptError) as exc:
        fetch.fetch(VID, category="cooking", role="ref")
    assert exc.value.code == "role_conflict"


def test_fetch_reports_other_categories(home, net):
    add_video_md(home, "vlog", "ref")
    refstore.init_category("cooking")
    out = fetch.fetch(VID, category="cooking", role="ref")
    assert out["status"] == "fetched"
    assert out["also_in"] == ["vlog"]


def test_fetch_unknown_category(home, net):
    with pytest.raises(ScriptError) as exc:
        fetch.fetch(VID, category="nope")
    assert exc.value.code == "category_not_found"


def test_main_cli_options(home, net):
    refstore.init_category("cooking")
    out = fetch.main([VID, "--category", "cooking", "--role", "ref"])
    assert out["status"] == "fetched"
    assert fetch.main([VID, "--category", "cooking", "--reanalyze"])["status"] == "cached"
    with pytest.raises(ScriptError) as exc:
        fetch.main([VID, "--role", "other"])
    assert exc.value.code == "usage"


# --- 요청 간격 ----------------------------------------------------------------

def test_network_requests_are_spaced(home, net, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(fetch.time, "time", lambda: clock[0])
    fetch.fetch(VID)
    assert net["sleeps"] == []
    clock[0] += 1.0
    fetch.fetch(VID, force=True)
    assert net["sleeps"] == [pytest.approx(2.0)]


def test_cache_hits_do_not_wait(home, net):
    fetch.fetch(VID)
    fetch.fetch(VID)
    assert net["sleeps"] == []


def test_interval_from_env(home, net, monkeypatch):
    monkeypatch.setenv("YT_FETCH_INTERVAL", "10")
    clock = [1000.0]
    monkeypatch.setattr(fetch.time, "time", lambda: clock[0])
    fetch.fetch(VID)
    fetch.fetch(VID, force=True)
    assert net["sleeps"] == [pytest.approx(10.0)]
