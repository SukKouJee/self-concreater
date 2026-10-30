import json
import os
import time

import pytest

from scripts import fetch, refstore, videomd
from scripts.common import TEMPLATES_DIR, ScriptError
from tests.helpers.paths import doc_key

VID = "dQw4w9WgXcQ"
CAT = "mukbang"

RAW = {
    "id": VID, "url": f"https://youtu.be/{VID}", "format": "shorts",
    "fetched_at": "2026-10-20T00:00:00+00:00",
    "meta": {"title": "라면 5봉지 먹방!", "channel": "채널", "duration": 30, "language": "ko"},
    "transcript": {"source": "auto", "language": "ko", "segments": [
        {"start": 0.0, "dur": 2.0, "text": "오늘은 라면 다섯 봉지"},
        {"start": 2.5, "dur": 3.0, "text": "물은 많이 넣어요"},
        {"start": 75.4, "dur": 2.0, "text": "구독 좋아요"},
    ]},
}

CUTS = {
    "id": VID, "duration_sec": 30.0, "fps": 30.0, "scene_count": 4, "avg_cut_sec": 7.5,
    "cuts": [3.0, 10.0, 25.0],
    "camera": {"static": 0.9, "pan": 0.1, "zoom": 0.0},
    "frames": [], "scenes": [
        {"start": 0.0, "end": 3.0, "camera": "static", "frame": None},
        {"start": 3.0, "end": 10.0, "camera": "pan", "frame": None},
        {"start": 10.0, "end": 25.0, "camera": "static", "frame": "workspace/frames/x/scene003_17.5s.jpg"},
        {"start": 25.0, "end": 30.0, "camera": "static", "frame": None},
    ],
}

ANALYSIS = {
    "summary": "라면 다섯 봉지를 한 번에 끓여 먹는 쇼츠",
    "synopsis": "결과를 먼저 보여 주고 조리와 먹는 장면으로 이어간다.",
    "hook_type": "result_first",
    "narrative_type": "journey",
    "sections": [
        {"label": "hook", "start": 0, "end": 3.4, "content": "완성된 라면 클로즈업"},
        {"label": "body", "sub": "story", "start": 3.4, "end": 24.6, "content": "끓이고 먹는 과정"},
        {"label": "cta", "start": 24.6, "end": 30, "content": "구독 요청", "shot": "정면 바스트"},
    ],
    "techniques": [
        {"kind": "훅", "name": "결과 선공개", "desc": "완성 장면을 첫 1초에 보여 준다", "at": 0},
        {"kind": "엔딩", "name": "구독 요청 반복", "desc": "마지막 5초에 말과 자막으로 두 번"},
    ],
}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_COACH_HOME", str(tmp_path))
    refstore.init_category(CAT)
    fetch.save_raw(RAW)
    cuts_path = tmp_path / "workspace" / "cuts" / f"{VID}.json"
    cuts_path.parent.mkdir(parents=True, exist_ok=True)
    cuts_path.write_text(json.dumps(CUTS), encoding="utf-8")
    return tmp_path


def write_analysis(home, data=ANALYSIS, name=VID):
    path = home / "workspace" / "tmp" / f"analysis_{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def md_path(home, vid=VID):
    return home / "refs" / f"ref_{CAT}" / "videos" / f"{vid}.md"


# --- 제목 패턴 · 시간 표기 --------------------------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("라면 5봉지 먹방!", ["number", "exclaim"]),
    ("이거 어때?? [ASMR]", ["question", "bracket"]),
    ("SUB)꾸덕매콤 크림진짬뽕", ["bracket"]),
    ("밥과 김치", []),
    ("100만 치트키 (진짜)!?", ["number", "question", "bracket", "exclaim"]),
])
def test_title_patterns(title, expected):
    assert videomd.title_patterns(title) == expected


@pytest.mark.parametrize("sec, text", [(0, "0:00"), (3.4, "0:03"), (75.4, "1:15"), (3725, "1:02:05")])
def test_fmt_time(sec, text):
    assert videomd.fmt_time(sec) == text


# --- 분석 입력(context) -----------------------------------------------------------

def test_context_bundles_transcript_scenes_and_vocab(home):
    ctx = videomd.context(VID, CAT)
    assert ctx["id"] == VID and ctx["title"] == "라면 5봉지 먹방!"
    assert ctx["transcript"].splitlines()[0] == "[0.0] 오늘은 라면 다섯 봉지"
    assert ctx["transcript"].splitlines()[2] == "[75.4] 구독 좋아요"
    assert ctx["scenes"].splitlines()[2] == "[10.0–25.0] static · frame workspace/frames/x/scene003_17.5s.jpg"
    assert ctx["scenes"].splitlines()[0] == "[0.0–3.0] static"
    assert ctx["avg_cut_sec"] == 7.5
    assert ctx["vocab"]["label"] == ["hook", "intro", "body", "demo", "transition", "cta", "outro"]
    assert ctx["examples"] == []
    assert ctx["md_exists"] is False


def test_context_without_transcript_or_cuts(home):
    fetch.save_raw({**RAW, "transcript": None})
    (home / "workspace" / "cuts" / f"{VID}.json").unlink()
    ctx = videomd.context(VID, CAT)
    assert ctx["transcript"] is None
    assert ctx["scenes"] is None
    assert ctx["sparse"] is True


def test_context_examples_are_reviewed_refs_newest_first(home):
    videos = home / "refs" / f"ref_{CAT}" / "videos"
    for i, (reviewed, role) in enumerate([(True, "ref"), (False, "ref"), (True, "mine"),
                                          (True, "ref"), (True, "ref"), (True, "ref")]):
        p = videos / f"v{i}.md"
        p.write_text(f"---\nid: v{i}\nrole: {role}\nreviewed: {str(reviewed).lower()}\n---\n## 내 메모\n",
                     encoding="utf-8")
        os.utime(p, (time.time() + i, time.time() + i))
    examples = videomd.context(VID, CAT)["examples"]
    assert examples == [f"refs/ref_{CAT}/videos/v{i}.md" for i in (5, 4, 3)]


# --- 분석 검증 --------------------------------------------------------------------

def test_validate_snaps_boundaries_to_cuts_and_edges():
    sections = videomd.validate(ANALYSIS, duration=30.0, cut_times=[3.0, 10.0, 25.0])["sections"]
    assert [(s["start"], s["end"]) for s in sections] == [(0.0, 3.0), (3.0, 25.0), (25.0, 30.0)]
    assert sections[1]["sub"] == "story"


def test_validate_makes_sections_contiguous_and_clamps():
    data = {**ANALYSIS, "sections": [
        {"label": "body", "sub": "list_item", "start": 5.5, "end": 18, "content": "b"},
        {"label": "hook", "start": 0.4, "end": 5, "content": "a"},       # 순서 뒤바뀜
        {"label": "outro", "start": 19, "end": 41, "content": "c"},     # 틈 + 길이 초과
    ]}
    sections = videomd.validate(data, duration=30.0, cut_times=[])["sections"]
    assert [(s["label"], s["start"], s["end"]) for s in sections] == [
        ("hook", 0.0, 5.0), ("body", 5.0, 18.0), ("outro", 18.0, 30.0)]


@pytest.mark.parametrize("patch, field", [
    ({"hook_type": "clickbait"}, "hook_type"),
    ({"narrative_type": "story"}, "narrative_type"),
    ({"sections": [{"label": "climax", "start": 0, "end": 30, "content": "x"}]}, "label"),
    ({"sections": [{"label": "body", "sub": "rant", "start": 0, "end": 30, "content": "x"}]}, "sub"),
    ({"sections": [{"label": "hook", "sub": "story", "start": 0, "end": 30, "content": "x"}]}, "sub"),
    ({"sections": []}, "sections"),
    ({"techniques": [{"kind": "음악", "name": "n", "desc": "d"}]}, "kind"),
    ({"summary": ""}, "summary"),
])
def test_validate_rejects_out_of_vocab(patch, field):
    with pytest.raises(ScriptError) as exc:
        videomd.validate({**ANALYSIS, **patch}, duration=30.0, cut_times=[])
    assert exc.value.code == "invalid_analysis"
    assert field in exc.value.message


# --- 섹션별 컷 통계 ---------------------------------------------------------------

def test_pacing_by_section():
    sections = [{"label": "hook", "start": 0, "end": 3}, {"label": "body", "start": 3, "end": 25},
                {"label": "cta", "start": 25, "end": 30}]
    assert videomd.pacing(sections, CUTS) == {
        "avg_cut_sec": 7.5,
        "by_section": {"hook": 3.0, "body": 11.0, "cta": 5.0},   # body: 22초 / (안쪽 컷 1개 + 1)
    }


def test_pacing_merges_repeated_labels():
    sections = [{"label": "body", "start": 0, "end": 10}, {"label": "transition", "start": 10, "end": 12},
                {"label": "body", "start": 12, "end": 30}]
    # body: (10 + 18)초 / ((컷 3.0 → 2구간) + (컷 25.0 → 2구간)) = 7.0
    assert videomd.pacing(sections, CUTS)["by_section"]["body"] == 7.0


def test_pacing_without_cuts():
    assert videomd.pacing([{"label": "hook", "start": 0, "end": 3}], None) == {
        "avg_cut_sec": None, "by_section": {}}


# --- 저장 -------------------------------------------------------------------------

def test_write_creates_md_matching_template_schema(home):
    out = videomd.write(VID, CAT, "ref", write_analysis(home))
    assert out["status"] == "written"
    assert out["md"] == f"refs/ref_{CAT}/videos/{VID}.md"

    fm, body = refstore.read(doc_key(md_path(home)))
    template = TEMPLATES_DIR / "video.md"                     # 플러그인 폴더(작업 폴더 밖)
    template_keys = list(refstore.parse_md(template.read_bytes(), template)[0])
    assert list(fm) == template_keys
    assert fm["role"] == "ref" and fm["format"] == "shorts" and fm["transcript_source"] == "auto"
    assert fm["sections"] == [
        {"label": "hook", "start": 0.0, "end": 3.0},
        {"label": "body", "sub": "story", "start": 3.0, "end": 25.0},
        {"label": "cta", "start": 25.0, "end": 30.0},
    ]
    assert fm["title_pattern"] == ["number", "exclaim"]
    assert fm["pacing"]["by_section"] == {"hook": 3.0, "body": 11.0, "cta": 5.0}
    assert fm["camera"] == {"static": 0.9, "pan": 0.1, "zoom": 0.0}
    assert fm["shots"] is None and fm["reviewed"] is False and fm["schema_version"] == 1

    assert "## 한 줄 요약\n라면 다섯 봉지를 한 번에 끓여 먹는 쇼츠\n" in body
    assert "| 0:00–0:03 | hook | 완성된 라면 클로즈업 | 고정 · 컷 3.0초 |" in body
    assert "| 0:03–0:25 | body/story | 끓이고 먹는 과정 | 고정·패닝 · 컷 11.0초 |" in body
    assert "| 0:25–0:30 | cta | 구독 요청 | 정면 바스트 |" in body
    assert "- **훅 · 결과 선공개** (0:00): 완성 장면을 첫 1초에 보여 준다" in body
    assert body.rstrip().endswith("## 내 메모")


def test_write_removes_analysis_file_and_updates_stage(home):
    path = write_analysis(home)
    out = videomd.write(VID, CAT, "ref", path)
    assert not path.exists()
    assert out["stage"] == {"ref": 1, "mine": 0, "stage": 1}
    _, index = refstore.read(doc_key(home / "refs" / f"ref_{CAT}" / "index.md"))
    assert "ref: 1개" in index


def test_write_skips_existing_md(home):
    videomd.write(VID, CAT, "ref", write_analysis(home))
    out = videomd.write(VID, CAT, "ref", write_analysis(home))
    assert out["status"] == "exists"


def test_force_rewrite_keeps_user_memo(home):
    videomd.write(VID, CAT, "ref", write_analysis(home))
    p = md_path(home)
    p.write_text(p.read_text("utf-8") + "훅이 좋아서 따라 해 볼 것\n- 다음엔 자막 색도 볼 것\n",
                 encoding="utf-8", newline="")
    videomd.write(VID, CAT, "ref", write_analysis(home, {**ANALYSIS, "summary": "새 요약"}), force=True)
    _, body = refstore.read(doc_key(p))
    assert "새 요약" in body
    assert body.endswith("## 내 메모\n훅이 좋아서 따라 해 볼 것\n- 다음엔 자막 색도 볼 것\n")


def test_force_rewrite_keeps_crlf_file_consistent(home):
    videomd.write(VID, CAT, "ref", write_analysis(home))
    p = md_path(home)
    crlf = (p.read_text("utf-8") + "윈도 메모장에서 쓴 메모\n").replace("\n", "\r\n")
    p.write_bytes(crlf.encode("utf-8"))
    videomd.write(VID, CAT, "ref", write_analysis(home), force=True)
    raw = p.read_bytes().decode("utf-8")
    assert "윈도 메모장에서 쓴 메모\r\n" in raw
    assert "\n" not in raw.replace("\r\n", "")


def test_write_requires_raw(home):
    with pytest.raises(ScriptError) as exc:
        videomd.write("aaaaaaaaaaa", CAT, "ref", write_analysis(home))
    assert exc.value.code == "raw_not_found"


def test_write_rejects_role_conflict(home):
    videomd.write(VID, CAT, "ref", write_analysis(home))
    with pytest.raises(ScriptError) as exc:
        videomd.write(VID, CAT, "mine", write_analysis(home), force=True)
    assert exc.value.code == "role_conflict"


def test_write_without_cuts_leaves_pacing_empty(home):
    (home / "workspace" / "cuts" / f"{VID}.json").unlink()
    videomd.write(VID, CAT, "ref", write_analysis(home))
    fm, body = refstore.read(doc_key(md_path(home)))
    assert fm["pacing"] == {"avg_cut_sec": None, "by_section": {}}
    assert fm["camera"] is None
    assert "| 0:00–0:03 | hook | 완성된 라면 클로즈업 | - |" in body


# --- 기법 카탈로그 ----------------------------------------------------------------

def test_techniques_are_appended_under_kind_and_merged_by_name(home):
    videomd.write(VID, CAT, "ref", write_analysis(home))
    other = "bbbbbbbbbbb"
    fetch.save_raw({**RAW, "id": other})
    out = videomd.write(other, CAT, "ref", write_analysis(home, {**ANALYSIS, "techniques": [
        {"kind": "훅", "name": "결과 선공개", "desc": "다른 설명"},
        {"kind": "촬영", "name": "탑뷰 손 클로즈업", "desc": "위에서 손만"},
    ]}, name=other))
    assert out["techniques_added"] == 1 and out["techniques_merged"] == 1

    _, body = refstore.read(doc_key(home / "refs" / f"ref_{CAT}" / "techniques.md"))
    hook = body.split("## 훅")[1].split("## 전환")[0]
    assert hook.count("**결과 선공개**") == 1
    assert f"[{VID}](videos/{VID}.md), [{other}](videos/{other}.md)" in hook
    assert "**구독 요청 반복**" in body.split("## 엔딩")[1].split("## 촬영")[0]
    assert "**탑뷰 손 클로즈업**" in body.split("## 촬영")[1]


def test_rewrite_drops_stale_techniques_of_that_video(home):
    other = "bbbbbbbbbbb"
    fetch.save_raw({**RAW, "id": other})
    videomd.write(VID, CAT, "ref", write_analysis(home))          # 결과 선공개, 구독 요청 반복
    videomd.write(other, CAT, "ref", write_analysis(home, name=other))   # 같은 두 기법에 출처 추가
    videomd.write(VID, CAT, "ref", write_analysis(home, {**ANALYSIS, "techniques": [
        {"kind": "촬영", "name": "탑뷰", "desc": "위에서"}]}), force=True)
    _, body = refstore.read(doc_key(home / "refs" / f"ref_{CAT}" / "techniques.md"))
    hook = body.split("## 훅")[1].split("## 전환")[0]
    assert f"[{VID}]" not in hook                                  # 예전 분석의 출처는 빠지고
    assert f"**결과 선공개**" in hook and f"[{other}]" in hook       # 다른 영상 출처는 남는다
    assert f"**탑뷰**" in body and body.count(f"[{VID}]") == 1


def test_stale_technique_line_removed_when_no_sources_left(home):
    videomd.write(VID, CAT, "ref", write_analysis(home))
    videomd.write(VID, CAT, "ref", write_analysis(home, {**ANALYSIS, "techniques": []}), force=True)
    _, body = refstore.read(doc_key(home / "refs" / f"ref_{CAT}" / "techniques.md"))
    assert "결과 선공개" not in body and "구독 요청 반복" not in body


def test_rewrite_does_not_duplicate_technique_sources(home):
    videomd.write(VID, CAT, "ref", write_analysis(home))
    videomd.write(VID, CAT, "ref", write_analysis(home), force=True)
    _, body = refstore.read(doc_key(home / "refs" / f"ref_{CAT}" / "techniques.md"))
    assert body.count(f"[{VID}](videos/{VID}.md)") == 2   # 훅 1, 엔딩 1


# --- CLI --------------------------------------------------------------------------

def test_main_routes_subcommands(home):
    assert videomd.main(["context", VID, "--category", CAT])["id"] == VID
    out = videomd.main(["write", VID, "--category", CAT, "--analysis", str(write_analysis(home))])
    assert out["status"] == "written"


def test_main_resolves_relative_analysis_path_against_work_root(home, monkeypatch, tmp_path_factory):
    write_analysis(home)
    monkeypatch.chdir(tmp_path_factory.mktemp("plugin_root"))   # ytc는 플러그인 폴더에서 실행된다
    out = videomd.main(["write", VID, "--category", CAT, "--analysis", f"workspace/tmp/analysis_{VID}.json"])
    assert out["status"] == "written"
