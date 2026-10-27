"""Task 4: 결과물 — 폰용 남은 컷 HTML, 위치 CSV, 컷 파일(재포장 / 재압축 / 원본 없음)."""
import csv
import io
import re
import shutil
from fractions import Fraction

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
    return {"kind": "review", "choice": None, "reason": "확신도 중간",
            "candidates": [{"fid": fid, "start": start, "end": end, "confidence": "medium", "speech": "1단계"}]}


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
    assert all(src.startswith("data:") for src in re.findall(r'src="([^"]*)"', text))
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


def test_remaining_html_embeds_each_reference_image_once(scene_setup, home, monkeypatch):
    path, _, _ = scene_setup
    frame = home / "workspace" / "frames" / "same.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"\xff\xd8same-image-bytes")
    ref = {"frame": "workspace/frames/same.jpg", "title": "같은 장면", "match": "exact"}
    monkeypatch.setattr(conti, "cut_refs", lambda fm: {c["n"]: ref for c in fm["cuts"]})
    text = (home / footage_out.remaining_html(path)).read_text("utf-8")
    assert text.count("data:image/jpeg;base64,") == 1            # 컷 3개가 같은 사진: 한 번만 싣는다
    assert text.count('class="ref img0"') == 3 and "참고 · 같은 장면" in text
