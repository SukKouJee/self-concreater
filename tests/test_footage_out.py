"""Task 4: 결과물 — 폰용 남은 컷 HTML, 위치 CSV, 컷 파일(재포장 / 재압축 / 원본 없음)."""
import csv
import io
import re
import shutil
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest

from scripts import conti, footage
from scripts import footage_match as fm
from scripts import footage_out
from scripts.common import ScriptError
from tests.test_conti import conti_file, draft_md, home  # noqa: F401 - fixtures
from tests.test_cuts import sample_video  # noqa: F401 - fixture


@pytest.fixture
def saved(draft_md, home):  # noqa: F811
    return home / conti.save(draft_md, conti_file(home))["path"]


@pytest.fixture(scope="module")
def av_video(tmp_path_factory):
    """H.264(1초마다 키프레임) + AAC 소리, 6초. 컷 파일이 영상과 소리를 함께 담는지 본다."""
    path = tmp_path_factory.mktemp("avvideo") / "talk.mp4"
    fps, rate = 30, 48000
    with av.open(str(path), "w") as out:
        v = out.add_stream("libx264", rate=fps)
        v.width, v.height, v.pix_fmt = 320, 240, "yuv420p"
        v.options = {"g": str(fps), "keyint_min": str(fps), "sc_threshold": "0"}
        a = out.add_stream("aac", rate=rate)
        a.layout = "mono"
        for i in range(fps * 6):
            img = np.full((240, 320, 3), (i * 3) % 255, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = i
            out.mux(v.encode(frame))
        out.mux(v.encode())
        samples = 1024
        t = 0
        while t < rate * 6:
            tone = (0.2 * np.sin(2 * np.pi * 440 * (np.arange(samples) + t) / rate)).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(tone.reshape(1, -1), format="fltp", layout="mono")
            frame.sample_rate, frame.pts, frame.time_base = rate, t, Fraction(1, rate)
            out.mux(a.encode(frame))
            t += samples
        out.mux(a.encode())
    return path


def shoot(tmp_path, *sources, names=None):
    """원본 촬영 폴더(작업 폴더 밖)에 영상을 복사한다. -> (폴더, [scan 항목])."""
    folder = tmp_path.parent / f"{tmp_path.name}-src" / "촬영"
    folder.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(sources):
        shutil.copy(src, folder / (names[i] if names else src.name))
    return folder, footage.scan_folder(folder)


def cut_ids(path):
    return [c["id"] for c in conti._load_conti(path)["cuts"]]


def cached(fid, name):
    """그 파일의 처리 캐시(원본이 사라져도 이름을 보여 줄 수 있게)."""
    base = footage.footage_dir(fid)
    base.mkdir(parents=True, exist_ok=True)
    (base / "scenes.json").write_text(
        '{"schema": 1, "fid": "%s", "name": "%s", "status": "done", "duration_sec": 6.0, "scenes": []}'
        % (fid, name), encoding="utf-8")


def seed(path, folder, verdicts):
    state = fm.load_state(path)
    state["folders"] = [str(folder)]
    ids = cut_ids(path)
    state["cuts"] = {ids[i]: v for i, v in verdicts.items()}
    fm.save_state(path, state)


def chosen(fid, start, end, kind="auto"):
    return {"kind": kind, "choice": {"fid": fid, "start": start, "end": end}, "candidates": [],
            "reason": None, "user": kind == "pinned"}


def review(fid, start, end):
    """맞추기가 실제로 남기는 모양(footage_match._entry_from): 후보는 score·why·text(받아쓴 말)."""
    match = {"fid": fid, "start": start, "end": end, "confidence": "medium", "why": "그릇이 비슷해요"}
    return fm._entry_from([match], {fid: [{"start": start, "end": end, "text": "1단계 갑니다 <b>"}]}, None)


@pytest.fixture
def scene_setup(saved, tmp_path, sample_video):  # noqa: F811
    """컷 1 찍음(자동), 2 확인 필요, 3 남은 컷(판정 없음), 4 못 찍음."""
    folder, [f] = shoot(tmp_path, sample_video)
    cached(f["fid"], f["name"])
    seed(saved, folder, {0: chosen(f["fid"], 1.0, 3.0), 1: review(f["fid"], 2.0, 4.0),
                         3: {"kind": "not_filmed", "choice": None, "candidates": [], "user": True}})
    return saved, folder, f


def duration(path):
    with av.open(str(path)) as c:
        return c.duration / av.time_base


def streams(path):
    with av.open(str(path)) as c:
        return [s.type for s in c.streams], c.streams.video[0].codec_context.name


# --- 폰용 남은 컷 ---------------------------------------------------------------------------

def test_remaining_html_lists_only_remaining_and_review_cuts(scene_setup, home):
    path, _, _ = scene_setup
    rel = footage_out.remaining_html(path)
    out = home / rel
    assert rel == "drafts/" + path.name.replace("-conti.md", "-remaining.html")
    text = out.read_text("utf-8")
    assert "찍음 1/4" in text
    assert "4단계 다 먹을 수 있을까?" not in text          # 찍은 컷 1
    assert "1단계부터 갑니다." in text and "확인 필요" in text   # 컷 2
    assert "와 순하다" in text and "남은 컷" in text          # 컷 3
    assert "3초 정지" in text and "못 찍음" in text           # 컷 4(대사 없음, 가이드)
    assert text.count('<article class="cut') == 3
    assert '<svg class="sk"' in text                          # 샷 크기 도식


def test_remaining_html_has_no_external_resources(scene_setup, home):
    path, _, _ = scene_setup
    text = (home / footage_out.remaining_html(path)).read_text("utf-8")
    assert not re.search(r"https?://", text)
    assert "<link" not in text and "<script" not in text
    assert not re.findall(r'src="([^"]*)"', text)         # 그림은 img src가 아니라 CSS 배경으로만 싣는다
    assert "data:image" not in text
    assert 'name="viewport"' in text


def test_remaining_html_marks_missing_source_on_candidates(scene_setup, home):
    path, folder, f = scene_setup
    (folder / f["name"]).unlink()
    text = (home / footage_out.remaining_html(path)).read_text("utf-8")
    assert "원본 없음" in text and f["name"] in text


# --- 위치 CSV ---------------------------------------------------------------------------------

def read_csv(path):
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))


def test_csv_has_bom_columns_and_every_cut(scene_setup, home):
    path, folder, f = scene_setup
    rel = footage_out.csv_export(path)
    assert rel == "drafts/" + path.name.replace("-conti.md", "-footage.csv")
    rows = read_csv(home / rel)
    assert rows[0] == ["컷", "섹션", "판정", "파일", "시작", "끝", "원본 경로"]
    assert [r[0] for r in rows[1:]] == ["1", "2", "3", "4"]
    first = rows[1]
    assert first[1] == "hook" and first[2] == "찍음(자동)"
    assert first[3] == f["name"] and first[4] == "0:01.0" and first[5] == "0:03.0"
    assert first[6] == str(folder / f["name"])
    assert [r[2] for r in rows[2:]] == ["확인 필요", "남은 컷", "못 찍음"]
    assert rows[2][3:] == ["", "", "", ""]


def test_csv_shows_missing_source(scene_setup, home):
    path, folder, f = scene_setup
    (folder / f["name"]).unlink()
    rows = read_csv(home / footage_out.csv_export(path))
    assert rows[1][3] == f["name"] and rows[1][6] == "원본 없음"


def test_clock_formats_minutes_and_hours():
    assert footage_out.clock(0) == "0:00.0"
    assert footage_out.clock(75.25) == "1:15.2"
    assert footage_out.clock(3725.0) == "1:02:05.0"


# --- 컷 파일 ---------------------------------------------------------------------------------

def test_export_clips_remuxes_chosen_range(scene_setup, home):
    path, _, f = scene_setup
    result = footage_out.export_clips(path)
    [clip] = result["clips"]
    out = home / clip["file"]
    assert clip["n"] == 1 and out.name == "01_sample.mp4" and out.parent.name.endswith("-cuts")
    assert clip["reencoded"] is False
    assert 2.0 - 0.1 <= duration(out) <= 3.0 + 0.2             # 키프레임 때문에 조금 앞당겨질 수 있다
    assert clip["start"] <= 1.0 + 1e-6
    assert result["skipped"] == []
    assert result["dir"] == clip["file"].rsplit("/", 1)[0]


def test_export_clips_keeps_audio_with_video(saved, tmp_path, av_video, home):
    folder, [f] = shoot(tmp_path, av_video)
    seed(saved, folder, {1: chosen(f["fid"], 2.2, 4.0, "pinned")})
    [clip] = footage_out.export_clips(saved)["clips"]
    out = home / clip["file"]
    kinds, codec = streams(out)
    assert out.name == "02_talk.mp4" and sorted(kinds) == ["audio", "video"] and codec == "h264"
    assert clip["reencoded"] is False
    assert abs(clip["start"] - 2.0) < 0.05                    # 2.2초 앞의 키프레임(2초)부터
    assert 1.9 <= duration(out) <= 2.3


def test_export_clips_mov_source_goes_into_mp4(saved, tmp_path, av_video, home):
    folder, [f] = shoot(tmp_path, av_video, names=["IMG_0001.MOV"])
    seed(saved, folder, {0: chosen(f["fid"], 0.0, 1.0)})
    [clip] = footage_out.export_clips(saved)["clips"]
    assert clip["file"].endswith("/01_IMG_0001.mp4")


def test_export_clips_reencodes_when_remux_fails(saved, tmp_path, av_video, home, monkeypatch):
    folder, [f] = shoot(tmp_path, av_video)
    seed(saved, folder, {2: chosen(f["fid"], 2.5, 4.5)})

    def broken(*args, **kw):
        raise av.error.InvalidDataError(1094995529, "Invalid data found when processing input")

    monkeypatch.setattr(footage_out, "_remux", broken)
    [clip] = footage_out.export_clips(saved)["clips"]
    out = home / clip["file"]
    kinds, codec = streams(out)
    assert clip["reencoded"] is True and codec == "h264" and sorted(kinds) == ["audio", "video"]
    assert clip["start"] == pytest.approx(2.5)
    assert 1.8 <= duration(out) <= 2.2
    assert not list(out.parent.glob("*.tmp*"))


def test_export_clips_skips_missing_source_with_reason(scene_setup, home):
    path, folder, f = scene_setup
    (folder / f["name"]).unlink()
    result = footage_out.export_clips(path)
    assert result["clips"] == []
    [skip] = result["skipped"]
    assert skip["n"] == 1 and skip["name"] == f["name"] and "원본 없음" in skip["reason"]


def test_export_clips_overwrites_only_inside_cuts_folder(scene_setup, home):
    path, folder, f = scene_setup
    out_dir = path.with_name(path.name.replace("-conti.md", "-cuts"))
    out_dir.mkdir()
    (out_dir / "07_old.mp4").write_bytes(b"stale")
    (out_dir / "내 메모.txt").write_text("keep", encoding="utf-8")
    footage_out.export_clips(path)
    names = sorted(p.name for p in out_dir.iterdir())
    assert names == ["01_sample.mp4", "내 메모.txt"]
    before = (folder / f["name"]).read_bytes()
    footage_out.export_clips(path)
    assert (folder / f["name"]).read_bytes() == before         # 원본은 읽기만


def test_export_clips_reports_progress_per_cut(scene_setup):
    path, _, _ = scene_setup
    events = []
    footage_out.export_clips(path, progress=lambda i, event, info: events.append((i, event)))
    assert events == [(0, "start"), (0, "done")]


def test_clip_targets_only_cuts_with_choice(scene_setup):
    path, _, f = scene_setup
    [t] = footage_out.clip_targets(path)
    assert t["n"] == 1 and t["fid"] == f["fid"] and t["source"] is not None


def test_export_clips_without_choices_is_an_error(saved):
    with pytest.raises(ScriptError) as e:
        footage_out.export_clips(saved)
    assert e.value.code == "no_clips"


class _Turned:
    """세로 촬영(디스플레이 행렬 90도) 프레임 대역."""
    rotation, pts, time_base = 90, 5, Fraction(1, 30)

    def to_ndarray(self, format):
        img = np.zeros((2, 4, 3), dtype=np.uint8)
        img[0, 3] = 255                      # 오른쪽 위 점
        return img


def test_rotate_applies_display_matrix_before_reencode():
    out = footage_out._rotate(_Turned(), av)
    img = out.to_ndarray(format="rgb24")
    assert (out.width, out.height) == (2, 4) and out.pts == 5
    assert img[0, 0].tolist() == [255, 255, 255]   # 반시계 90도: 오른쪽 위 → 왼쪽 위


def test_rotate_keeps_upright_frame():
    frame = av.VideoFrame(4, 2, "rgb24")
    assert footage_out._rotate(frame, av) is frame


def test_remaining_html_links_each_reference_image_once(scene_setup, home, monkeypatch):
    path, _, _ = scene_setup
    frame = home / "workspace" / "frames" / "same.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"\xff\xd8same-image-bytes")
    ref = {"frame": "workspace/frames/same.jpg", "title": "같은 장면", "match": "content"}
    monkeypatch.setattr(conti, "cut_refs", lambda fm: {c["n"]: ref for c in fm["cuts"]})
    text = (home / footage_out.remaining_html(path)).read_text("utf-8")
    assert text.count("url(../workspace/frames/same.jpg)") == 1  # 컷 3개가 같은 사진: 규칙 하나만
    assert "data:image" not in text
    assert text.count('class="ref img0"') == 3 and "참고 · 같은 장면" in text


# --- Fix round 1 ------------------------------------------------------------------------------

@pytest.fixture
def two_cuts(saved, tmp_path, av_video):
    folder, [f] = shoot(tmp_path, av_video)
    seed(saved, folder, {0: chosen(f["fid"], 0.0, 1.0), 1: chosen(f["fid"], 2.0, 3.0)})
    return saved


def test_export_clips_contains_locked_output_to_its_cut(two_cuts, home, monkeypatch):
    import os
    real = os.replace

    def replace(src, dst):
        if Path(dst).name.startswith("01_"):
            raise PermissionError(13, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    result = footage_out.export_clips(two_cuts)
    [failed] = result["failed"]
    assert failed["n"] == 1 and "다른 프로그램에서 열려 있어요" in failed["reason"]
    assert [c["n"] for c in result["clips"]] == [2] and (home / result["clips"][0]["file"]).is_file()


def test_clear_clips_ignores_locked_old_clip(two_cuts, home, monkeypatch):
    out_dir = home / "drafts" / (two_cuts.name.replace("-conti.md", "") + "-cuts")
    out_dir.mkdir()
    (out_dir / "07_old.mp4").write_bytes(b"x")
    real = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == "07_old.mp4":
            raise PermissionError(13, "locked")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = footage_out.export_clips(two_cuts)
    assert [c["n"] for c in result["clips"]] == [1, 2]


def test_export_clips_unexpected_error_fails_only_that_cut(two_cuts, monkeypatch):
    real = footage_out.cut_clip

    def flaky(target, out_dir):
        if target["n"] == 1:
            raise RuntimeError("boom")
        return real(target, out_dir)

    monkeypatch.setattr(footage_out, "cut_clip", flaky)
    result = footage_out.export_clips(two_cuts)
    assert result["failed"][0]["reason"] == "예상치 못한 오류: RuntimeError: boom"
    assert [c["n"] for c in result["clips"]] == [2]


def test_remux_seek_overshoot_falls_back_to_reencode(two_cuts, monkeypatch):
    real, calls = footage_out._seek, []

    def overshoot(container, stream, seconds):
        calls.append(seconds)                        # 컷마다 재포장(홀수 번째) → 다시 압축(짝수 번째)
        real(container, stream, seconds + (1.0 if len(calls) % 2 else 0.0))   # 색인 없는 mkv처럼 지나친다

    monkeypatch.setattr(footage_out, "_seek", overshoot)
    result = footage_out.export_clips(two_cuts)
    assert all(c["reencoded"] for c in result["clips"]) and len(result["clips"]) == 2


@pytest.mark.parametrize("value, expected", [
    ("=cmd|' /C calc'!A0", "'=cmd|' /C calc'!A0"), ("+1", "'+1"), ("-x", "'-x"), ("@sum", "'@sum"),
    ("\t=1+1", "'\t=1+1"), ("\r=1+1", "'\r=1+1"), ("a.mp4", "a.mp4"), ("", ""),
])
def test_csv_cell_neutralizes_formulas(value, expected):
    assert footage_out.csv_cell(value) == expected


def test_csv_escapes_formula_file_name(saved, tmp_path, sample_video, home):  # noqa: F811
    folder, [f] = shoot(tmp_path, sample_video, names=["=HYPERLINK.mp4"])
    seed(saved, folder, {0: chosen(f["fid"], 1.0, 2.0)})
    rows = read_csv(home / footage_out.csv_export(saved))
    assert rows[1][3] == "'=HYPERLINK.mp4"


@pytest.fixture(scope="module")
def hevc_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("hevc") / "hevc.mp4"
    with av.open(str(path), "w") as out:
        v = out.add_stream("libx265", rate=30)
        v.width, v.height, v.pix_fmt = 160, 120, "yuv420p"
        v.options = {"x265-params": "log-level=none:keyint=30"}
        for i in range(60):
            frame = av.VideoFrame.from_ndarray(np.full((120, 160, 3), i * 4, dtype=np.uint8), format="rgb24")
            frame.pts = i
            out.mux(v.encode(frame))
        out.mux(v.encode())
    return path


def test_remux_tags_hevc_as_hvc1(saved, tmp_path, hevc_video, home):
    folder, [f] = shoot(tmp_path, hevc_video)
    seed(saved, folder, {0: chosen(f["fid"], 0.0, 1.5)})
    [clip] = footage_out.export_clips(saved)["clips"]
    assert clip["reencoded"] is False
    with av.open(str(home / clip["file"])) as c:
        v = c.streams.video[0]
        assert v.codec_context.name == "hevc" and v.codec_context.codec_tag == "hvc1"


def test_remaining_html_keeps_not_filmed_cuts(scene_setup, home):
    path, _, _ = scene_setup
    text = (home / footage_out.remaining_html(path)).read_text("utf-8")
    assert 'class="badge not_filmed">못 찍음' in text


def test_remaining_html_shows_candidate_speech(scene_setup, home):
    path, _, _ = scene_setup
    text = (home / footage_out.remaining_html(path)).read_text("utf-8")
    assert "“1단계 갑니다 &lt;b&gt;”" in text             # 후보의 받아쓴 말(text), 이스케이프


def test_remaining_html_pinned_caption_has_no_file_name(scene_setup, home, monkeypatch):
    path, _, _ = scene_setup
    frame = home / "workspace" / "frames" / "pin.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"\xff\xd8pinned")
    ref = {"frame": "workspace/frames/pin.jpg", "title": "pin.jpg", "match": "pinned", "desc": None}
    monkeypatch.setattr(conti, "cut_refs", lambda fm: {c["n"]: ref for c in fm["cuts"]})
    text = (home / footage_out.remaining_html(path)).read_text("utf-8")
    assert "참고(고정)</figcaption>" in text
    assert "pin.jpg" not in re.sub(r"url\([^)]*\)", "", text)       # 파일 이름은 이미지 링크에만 있고 글에는 없다


def test_remaining_html_embed_option_keeps_one_file(scene_setup, home, monkeypatch):
    path, _, _ = scene_setup
    frame = home / "workspace" / "frames" / "same.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"\xff\xd8same-image-bytes")
    ref = {"frame": "workspace/frames/same.jpg", "title": "같은 장면", "match": "content"}
    monkeypatch.setattr(conti, "cut_refs", lambda fm: {c["n"]: ref for c in fm["cuts"]})
    text = (home / footage_out.remaining_html(path, embed=True)).read_text("utf-8")
    assert text.count("data:image/jpeg;base64,") == 1 and "../workspace/" not in text
