"""촬영 영상 결과물: 폰용 남은 컷 HTML, 컷별 위치 CSV, 컷별로 잘라 낸 파일

사용법 (작업 폴더에서):
  python -m scripts.footage_out remaining <conti.md>    → drafts/<기획안>-remaining.html
  python -m scripts.footage_out csv <conti.md>          → drafts/<기획안>-footage.csv
  python -m scripts.footage_out clips <conti.md>        → drafts/<기획안>-cuts/NN_<원본 이름>.mp4

- 모두 지금 콘티에 있는 컷만 쓴다(footage_match.visible_cuts). 판정은 workspace/footage_match/의 상태 파일.
- 원본이 지금 연결 폴더 바로 아래에 있는지는 폴더를 다시 훑어 지문으로 확인한다. 없으면 `원본 없음`
  (HTML 후보·CSV 원본 경로·컷 파일 건너뜀 이유). 같은 파일이 돌아오면(같은 지문) 다시 쓰인다.
- 남은 컷 HTML: 외부 자원 없는 한 파일(이미지 base64, 스토리보드 HTML과 같은 토큰·도식). 맨 위에 만든 시각과
  "찍음 N/M", 그 아래 확인 필요·남은 컷·못 찍음 컷만. 폰 폭 한 줄.
- CSV: UTF-8 BOM(엑셀에서 한글이 깨지지 않게), 열은 CSV_COLUMNS. 시각은 m:ss.s(1시간 넘으면 h:mm:ss.s).
- 컷 파일: 고른 구간(choice)이 있는 컷만. 원본 시각 = 장면 시각 + 영상 트랙 시작 시각(장면 시각은 첫 프레임 기준).
  1) 재포장: 시작 이전의 키프레임부터 끝까지 영상·소리 패킷을 그대로 옮긴다(다시 압축 없음, 원본 화질).
     시작이 키프레임까지 앞당겨질 수 있다(결과의 start). .mov 등도 .mp4 컨테이너로 담는다.
  2) 재포장이 실패한 파일만 그 구간을 PyAV로 다시 압축한다(H.264 + AAC, reencoded: true, 시작은 정확).
  출력 폴더(drafts/<기획안>-cuts/)는 이 기능 전용: 만들기 전에 그 폴더의 이전 컷 파일(NN_*.mp4 등)만 지운다.
  원본은 읽기만 한다. 파일은 임시 이름으로 쓴 뒤 바꿔 끼운다(중간에 멈춰도 반쯤 쓴 파일이 남지 않게).
  다른 프로그램이 컷 파일을 열어 두었으면(윈도 잠금) 그 컷만 failed('열려 있어요'), 예상치 못한 오류도 그 컷만 실패하고
  나머지 컷은 계속 만든다. 탐색이 고른 시작을 SEEK_SLACK 넘게 지나치면(색인 없는 mkv) 다시 압축한다.
  HEVC는 hvc1 태그로 담는다. CSV는 =,+,-,@로 시작하는 칸 앞에 '를 붙인다(수식 주입 방지).
"""
from __future__ import annotations

import contextlib
import csv
import io
import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from scripts import conti, footage
from scripts import footage_match as fmatch
from scripts.common import ArgParser, ScriptError, run, work_root
from scripts.report import _vars
from scripts.videomd import LABELS

KIND_LABELS = {"auto": "찍음(자동)", "pinned": "찍음(고정)", "review": "확인 필요", "missing": "남은 컷",
               "not_filmed": "못 찍음"}
FILMED = frozenset({"auto", "pinned"})
REMAINING = ("review", "missing", "not_filmed")    # 남은 컷 HTML에 싣는 판정
CSV_COLUMNS = ["컷", "섹션", "판정", "파일", "시작", "끝", "원본 경로"]
MISSING_SOURCE = "원본 없음"
MSG_LOCKED = "{name}이(가) 다른 프로그램에서 열려 있어요. 닫고 다시 해 주세요"
MSG_NO_CLIPS = "고른 구간이 있는 컷이 없습니다. 먼저 촬영 영상을 맞추거나 구간을 골라 주세요"
CLIP_EXT = ".mp4"
CLIP_NAME_RE = re.compile(r"\d{2,}_.+\.(?:mp4|mov|m4v|mkv)", re.IGNORECASE)   # 이 기능이 만든 컷 파일 이름
REENCODE_CRF = "18"                               # 다시 압축할 때 화질(낮을수록 좋음): 원본에 가깝게
REENCODE_PRESET = "veryfast"
AUDIO_RATE = 48000

ClipProgress = Callable[[int, str, dict[str, Any]], None]


def _conti_path(value: str | Path) -> Path:
    path = conti.safe_draft_path(str(value), conti_file=True)
    if not path.is_file():
        raise ScriptError("conti_not_found", f"콘티가 없습니다: {value}")
    return path


def _base(path: Path) -> str:
    return path.stem[: -len(conti.CONTI_SUFFIX)]


def _rel(path: Path) -> str:
    return path.resolve().relative_to(work_root().resolve()).as_posix()


def clock(sec: float) -> str:
    """초 → m:ss.s (1시간 넘으면 h:mm:ss.s). 소수 첫째 자리는 내림."""
    tenths = int(round(max(0.0, sec) * 1000)) // 100
    whole, tenth = divmod(tenths, 10)
    hours, rest = divmod(whole, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{tenth}"
    return f"{minutes}:{seconds:02d}.{tenth}"


# ---- 원본 찾기 ---------------------------------------------------------------------------

def present_sources(folders: list[str]) -> dict[str, Path]:
    """연결 폴더 바로 아래에 지금 있는 영상: 지문 → 실제 경로. 폴더가 없거나 읽을 수 없으면 건너뛴다.
    링크로 폴더 밖을 가리키는 파일은 넣지 않는다."""
    found: dict[str, Path] = {}
    for folder in folders:
        try:
            root = Path(folder).resolve(strict=True)
            files = footage.scan_folder(root)
        except ScriptError as e:
            if e.code != "folder_not_found":
                raise
            continue
        except OSError:
            continue                                  # 드라이브 분리·접근 거부: 그 폴더의 원본은 없는 것으로
        for f in files:
            real = Path(f["path"])
            if real.parent == root:
                found.setdefault(f["fid"], real)
    return found


class _Names:
    """fid → 파일 이름(지금 있으면 그 이름, 없으면 처리 캐시의 이름)."""

    def __init__(self, present: dict[str, Path]) -> None:
        self.present = present
        self._cache: dict[str, str | None] = {}

    def __call__(self, fid: Any) -> str | None:
        if not isinstance(fid, str):
            return None
        if fid in self.present:
            return self.present[fid].name
        if fid not in self._cache:
            cached = footage.cached_result(fid) if footage.valid_fid(fid) else None
            self._cache[fid] = cached.get("name") if cached else None
        return self._cache[fid]


def _load(path: Path) -> tuple[dict, list[dict], dict[str, Path]]:
    """(콘티 fm, 지금 컷 + 판정, 지금 있는 원본)."""
    state = fmatch.load_state(path)
    fm = conti._load_conti(path)
    cuts = fmatch.visible_cuts(path, state)
    return fm, cuts, present_sources(state["folders"])


# ---- 폰용 남은 컷 -------------------------------------------------------------------------

REMAINING_CSS = """
main{max-width:560px;padding:16px 12px 48px}.board{grid-template-columns:minmax(0,1fr)}.cut{min-width:0}
.cut header{flex-wrap:wrap}dd{overflow-wrap:anywhere}
.top{position:sticky;top:0;background:var(--page);padding:8px 0;border-bottom:1px solid var(--grid);z-index:1}
.count{font-size:20px;font-weight:700;margin:4px 0 0}.badge{display:inline-block;font-size:12px;font-weight:600;
padding:1px 8px;border-radius:999px;border:1px solid var(--axis);color:var(--ink2)}
.badge.review{color:var(--warn);border-color:var(--warn)}.pics figure:first-child{flex:0 0 38%}
.cands{margin:6px 0 0;padding:0;list-style:none;font-size:13px;color:var(--ink2)}.cands li{margin-top:2px}
.miss{color:var(--warn);font-weight:600}
div.ref{background-size:cover;background-position:center}.empty{color:var(--ink2);padding:24px 0}
"""


def _section_color(label: str) -> str:
    return f"var(--s{LABELS.index(label) + 1})" if label in LABELS else "var(--muted)"


def _candidate_lines(verdict: dict, names: _Names, present: dict[str, Path]) -> str:
    items = []
    for cand in verdict.get("candidates") or []:
        fid = cand.get("fid")
        name = names(fid) or "알 수 없는 파일"
        missing = f' <span class="miss">{MISSING_SOURCE}</span>' if fid not in present else ""
        speech = f" · “{conti._esc(cand['text'])}”" if cand.get("text") else ""
        items.append(f"<li>{conti._esc(name)} {clock(float(cand.get('start') or 0))}–"
                     f"{clock(float(cand.get('end') or 0))}{speech}{missing}</li>")
    return '<ul class="cands">' + "".join(items) + "</ul>" if items else ""


# 참고 사진이 없는 남은 컷 칸(콘티 스토리보드는 이때 도식을 그리지만 남은 컷 목록은 빈 칸 안내를 둔다)
NO_REF = '<figure><div class="noref">같은 샷의 레퍼런스 프레임 없음</div><figcaption>&nbsp;</figcaption></figure>'


class _Images:
    """참고 사진을 한 번씩만 싣는다: 같은 사진은 CSS 클래스(배경 이미지) 하나를 함께 쓴다
    (긴 콘티는 같은 레퍼런스 프레임을 여러 컷이 써서, 컷마다 base64를 넣으면 수십 MB가 된다)."""

    def __init__(self) -> None:
        self.classes: dict[str, str] = {}
        self.rules: list[str] = []

    def figure(self, ref: dict | None) -> str:
        uri = conti._data_uri(ref["frame"]) if ref else None
        if not uri or not ref:
            return NO_REF
        cls = self.classes.get(ref["frame"])
        if cls is None:
            cls = self.classes[ref["frame"]] = f"img{len(self.classes)}"
            self.rules.append(f".{cls}{{background-image:url({uri})}}")
        title = conti._esc(ref["title"])
        caption = conti._esc(ref.get("desc") or ref["title"])
        return (f'<figure><div class="ref {cls}" role="img" aria-label="참고 프레임: {title}"></div>'
                f'<figcaption title="{title}">{conti.ref_tag(ref["match"])} · {caption}</figcaption></figure>')


def render_remaining(fm: dict, cuts: list[dict], present: dict[str, Path], now: float | None = None) -> str:
    script = fm["script"]
    vertical = fm["format"] == "shorts"
    refs = conti.cut_refs(fm)
    names = _Names(present)
    images = _Images()
    filmed = sum(c["match"]["kind"] in FILMED for c in cuts)
    made = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y-%m-%d %H:%M")
    head = (f'<div class="top"><h1>{conti._esc(fm["title"])}</h1>'
            f'<p class="count">찍음 {filmed}/{len(cuts)}</p>'
            f'<p class="sub">남은 컷 · {made} 만듦</p></div>')
    cards = []
    for c in cuts:
        verdict = c["match"]
        kind = verdict["kind"]
        if kind not in REMAINING:
            continue
        label = script[c["section"]]["label"]
        reason = f'<p class="warn">{conti._esc(verdict["reason"])}</p>' if verdict.get("reason") else ""
        rows = [("대사", conti._esc(c["line"]) or "—", "line"), ("화면", conti._esc(c["action"]), ""),
                ("가이드", conti._esc(c["guide"]), "")]
        cards.append(
            f'<article class="cut" style="--sc:{_section_color(label)}"><header><b>#{c["n"]}</b>'
            f'<span>{conti._esc(label)} · {c["sec"]:g}초</span>'
            f'<span class="badge {kind}">{KIND_LABELS[kind]}</span></header>'
            f'<div class="pics"><figure>{conti.sketch(c["size"], vertical)}'
            f'<figcaption>{conti._esc(c["size"])}</figcaption></figure>{images.figure(refs.get(c["n"]))}</div>'
            + "<dl>" + "".join(f'<dt>{k}</dt><dd class="{cls}">{v}</dd>' for k, v, cls in rows) + "</dl>"
            + reason + _candidate_lines(verdict, names, present) + "</article>")
    board = ('<div class="board">' + "".join(cards) + "</div>" if cards
             else '<p class="empty">남은 컷이 없어요. 모두 찍었습니다.</p>')
    css = conti.CSS % (_vars(0), _vars(1), _vars(1))
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{conti._esc(fm['title'])} 남은 컷</title><style>{css}{REMAINING_CSS}{"".join(images.rules)}"
            f"main{{--ar:{'9/16' if vertical else '16/9'}}}</style></head>"
            f"<body><main>{head}{board}</main></body></html>")


def _write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding=encoding, newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def remaining_html(conti_file: str | Path, now: float | None = None) -> str:
    """drafts/<기획안>-remaining.html을 쓰고 작업 폴더 기준 상대 경로를 돌려준다."""
    path = _conti_path(conti_file)
    fm, cuts, present = _load(path)
    out = path.with_name(f"{_base(path)}-remaining.html")
    _write_text(out, render_remaining(fm, cuts, present, now))
    return _rel(out)


# ---- 위치 CSV ------------------------------------------------------------------------------

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: str) -> str:
    """엑셀이 수식으로 읽지 않게: =, +, -, @, 탭, CR로 시작하는 칸은 앞에 '를 붙인다(파일 이름 등 사용자 문자열)."""
    return "'" + value if value.startswith(FORMULA_START) else value


def csv_rows(fm: dict, cuts: list[dict], present: dict[str, Path]) -> list[list[str]]:
    names = _Names(present)
    rows = [CSV_COLUMNS]
    for c in cuts:
        verdict = c["match"]
        choice = verdict.get("choice")
        row = [str(c["n"]), fm["script"][c["section"]]["label"], KIND_LABELS.get(verdict["kind"], verdict["kind"])]
        if choice:
            fid = choice.get("fid")
            source = present.get(fid) if isinstance(fid, str) else None
            row += [names(fid) or "", clock(float(choice["start"])), clock(float(choice["end"])),
                    str(source) if source else MISSING_SOURCE]
        else:
            row += ["", "", "", ""]
        rows.append([csv_cell(cell) for cell in row])
    return rows


def csv_export(conti_file: str | Path) -> str:
    """drafts/<기획안>-footage.csv(UTF-8 BOM)를 쓰고 상대 경로를 돌려준다."""
    path = _conti_path(conti_file)
    fm, cuts, present = _load(path)
    buf = io.StringIO()
    csv.writer(buf).writerows(csv_rows(fm, cuts, present))
    out = path.with_name(f"{_base(path)}-footage.csv")
    _write_text(out, buf.getvalue(), encoding="utf-8-sig")
    return _rel(out)


# ---- 컷 파일 --------------------------------------------------------------------------------

def _av() -> Any:
    try:
        import av
    except ImportError as e:
        raise ScriptError("pyav_missing", "PyAV가 설치돼 있지 않아 컷 파일을 만들 수 없습니다 (pip install av)") from e
    return av


def check_ready() -> None:
    """컷 파일을 만들 수 있는지(PyAV). 없으면 ScriptError("pyav_missing")."""
    _av()


def clips_dir(conti_file: Path) -> Path:
    return conti_file.with_name(f"{_base(conti_file)}-cuts")


def clip_targets(conti_file: str | Path) -> list[dict[str, Any]]:
    """고른 구간이 있는 컷(콘티 순서). source는 지금 연결 폴더에서 찾은 원본(없으면 None)."""
    path = _conti_path(conti_file)
    state = fmatch.load_state(path)
    present = present_sources(state["folders"])
    names = _Names(present)
    out = []
    for c in fmatch.visible_cuts(path, state):
        choice = c["match"].get("choice")
        if not choice or not isinstance(choice.get("fid"), str):
            continue
        fid = choice["fid"]
        out.append({"cut": c["id"], "n": c["n"], "fid": fid, "name": names(fid),
                    "start": float(choice["start"]), "end": float(choice["end"]), "source": present.get(fid)})
    return out


def _seconds(value: Any, base: Any) -> float:
    return float(value * base)


def _track_start(stream: Any) -> float:
    """영상 트랙의 시작 시각(초). 장면 시각은 첫 프레임 기준이라 이것을 더해야 원본 시각이 된다."""
    if stream.start_time is None or stream.time_base is None:
        return 0.0
    return _seconds(stream.start_time, stream.time_base)


SEEK_SLACK = 0.05        # 재포장 시작이 고른 시작보다 이만큼 넘게 늦으면(탐색이 지나침) 다시 압축한다


def _seek(container: Any, stream: Any, seconds: float) -> None:
    """seconds(원본 시각) 이전의 키프레임으로 간다."""
    container.seek(int(seconds / stream.time_base), stream=stream, backward=True, any_frame=False)


def _tag_hevc(out_stream: Any, source: Any) -> None:
    """HEVC는 mp4에 hvc1 태그로 담는다(ffmpeg 기본 hev1은 QuickTime·사진 앱·CapCut 일부에서 안 열림).
    코덱은 원본 트랙(디코더 이름)으로 본다 — 템플릿으로 만든 출력 트랙의 이름은 인코더(libx265 등)다."""
    if source.codec_context.name == "hevc":
        out_stream.codec_context.codec_tag = "hvc1"


def _remux(src: Path, dst: Path, start: float, end: float) -> float:
    """시작 이전의 키프레임부터 끝까지 영상·소리 패킷을 그대로 옮긴다. -> 실제 시작(장면 시각 기준, 초)."""
    av = _av()
    with av.open(str(src)) as inp:
        if not inp.streams.video:
            raise ValueError("영상 트랙이 없습니다")
        video = inp.streams.video[0]
        audio = inp.streams.audio[0] if inp.streams.audio else None
        origin = _track_start(video)
        begin, stop = origin + start, origin + end
        with av.open(str(dst), "w") as out:
            ov = out.add_stream_from_template(video)
            _tag_hevc(ov, video)
            oa = out.add_stream_from_template(audio) if audio is not None else None
            _seek(inp, video, begin)
            offset: float | None = None          # 첫 키프레임의 dts(초): 모든 패킷 시각을 이만큼 당긴다
            first_pts = 0.0
            video_done = False
            audio_done = audio is None
            written = 0
            for packet in inp.demux([s for s in (video, audio) if s is not None]):
                if packet.dts is None or packet.pts is None:
                    continue                     # 끝을 알리는 빈 패킷
                tb = packet.time_base
                if packet.stream.index == video.index:
                    if video_done:
                        continue
                    if offset is None:
                        if not packet.is_keyframe:
                            continue
                        offset = _seconds(packet.dts, tb)
                        first_pts = _seconds(packet.pts, tb)
                        if first_pts > begin + SEEK_SLACK:
                            raise ValueError(f"키프레임 탐색이 시작을 지나쳤습니다({first_pts - begin:.2f}초)")
                    if _seconds(packet.dts, tb) >= stop:
                        video_done = True
                    else:
                        _shift(packet, offset)
                        packet.stream = ov
                        out.mux(packet)
                        written += 1
                else:
                    if audio_done or offset is None:
                        continue
                    t = _seconds(packet.pts, tb)
                    if t >= stop:
                        audio_done = True
                    elif t >= first_pts:
                        _shift(packet, offset)
                        packet.stream = oa
                        out.mux(packet)
                if video_done and audio_done:
                    break
            if written == 0:
                raise ValueError("옮길 영상 패킷이 없습니다")
        return first_pts - origin


def _shift(packet: Any, offset: float) -> None:
    ticks = int(round(Fraction(offset) / packet.time_base))
    packet.dts -= ticks
    packet.pts -= ticks


def _even(n: int) -> int:
    return max(2, n - n % 2)


def _rotate(frame: Any, av: Any) -> Any:
    """디스플레이 행렬(세로 촬영 등)을 화소에 적용한다(다시 압축하면 회전 정보가 따라가지 않으므로)."""
    code = round(getattr(frame, "rotation", 0) or 0) % 360
    if code == 0:
        return frame
    image = frame.to_ndarray(format="rgb24")
    turned = np.ascontiguousarray(np.rot90(image, k=code // 90))   # 반시계 각도 = rot90(k)
    out = av.VideoFrame.from_ndarray(turned, format="rgb24")
    out.pts, out.time_base = frame.pts, frame.time_base
    return out


def _reencode(src: Path, dst: Path, start: float, end: float) -> float:
    """그 구간을 다시 압축한다(H.264 + AAC). -> 실제 시작(= start)."""
    av = _av()
    with av.open(str(src)) as inp:
        if not inp.streams.video:
            raise ScriptError("clip_failed", "영상 트랙이 없습니다")
        video = inp.streams.video[0]
        video.thread_type = "AUTO"
        audio = inp.streams.audio[0] if inp.streams.audio else None
        origin = _track_start(video)
        begin, stop = origin + start, origin + end
        rate = video.average_rate or video.guessed_rate or Fraction(30)
        with av.open(str(dst), "w") as out:
            ov = out.add_stream("libx264", rate=rate)
            ov.pix_fmt = "yuv420p"
            ov.options = {"crf": REENCODE_CRF, "preset": REENCODE_PRESET}
            ov.time_base = Fraction(1, 1000)
            ov.codec_context.time_base = Fraction(1, 1000)
            oa = None
            resampler = None
            if audio is not None:
                oa = out.add_stream("aac", rate=AUDIO_RATE)
                oa.layout = "stereo"
                resampler = av.AudioResampler(format="fltp", layout="stereo", rate=AUDIO_RATE,
                                              frame_size=oa.codec_context.frame_size or 1024)
            _seek(inp, video, begin)
            last_pts = -1
            samples = 0
            sized = False
            held: list[Any] = []
            for frame in inp.decode(*[s for s in (video, audio) if s is not None]):
                if frame.time is None:
                    continue
                if isinstance(frame, av.VideoFrame):
                    if frame.time < begin - 1e-6 or frame.time >= stop:
                        continue
                    frame = _rotate(frame, av)
                    if not sized:                 # 크기는 코덱이 열리기 전(첫 mux 전)에 정해야 한다
                        ov.width, ov.height = _even(frame.width), _even(frame.height)
                        sized = True
                    pic = frame.reformat(width=ov.width, height=ov.height, format="yuv420p")
                    pts = max(last_pts + 1, int(round((frame.time - begin) * 1000)))
                    pic.pts, pic.time_base, last_pts = pts, Fraction(1, 1000), pts
                    out.mux(ov.encode(pic))
                    for packets in held:
                        out.mux(packets)
                    held.clear()
                elif oa is not None and resampler is not None:
                    end_t = frame.time + frame.samples / (frame.sample_rate or AUDIO_RATE)
                    if end_t <= begin or frame.time >= stop:
                        continue
                    frame.pts = None
                    for chunk in resampler.resample(frame):
                        chunk.pts, chunk.time_base = samples, Fraction(1, AUDIO_RATE)
                        samples += chunk.samples
                        if sized:
                            out.mux(oa.encode(chunk))
                        else:
                            held.append(oa.encode(chunk))   # 첫 영상 프레임 전 소리: 영상 크기를 정한 뒤 쓴다
            if last_pts < 0:
                raise ScriptError("clip_failed", "고른 구간에서 영상 프레임을 읽지 못했습니다")
            out.mux(ov.encode())
            if oa is not None and resampler is not None:
                for chunk in resampler.resample(None):
                    chunk.pts, chunk.time_base = samples, Fraction(1, AUDIO_RATE)
                    samples += chunk.samples
                    out.mux(oa.encode(chunk))
                out.mux(oa.encode())
    return start


def _remux_errors() -> tuple[type[Exception], ...]:
    av = _av()
    return (av.error.FFmpegError, ValueError)


@contextlib.contextmanager
def _temp_for(dst: Path) -> Iterator[Path]:
    """dst 자리에 바꿔 끼울 임시 파일. 실패하면 지운다."""
    tmp = dst.with_name(f"{dst.stem}.part{dst.suffix}")
    try:
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def cut_clip(target: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """컷 하나를 잘라 out_dir에 쓴다. 재포장이 실패하면 그 구간만 다시 압축한다.
    -> {"cut", "n", "name", "file"(상대 경로), "start", "end", "reencoded"}"""
    src: Path = target["source"]
    dst = out_dir / f"{target['n']:02d}_{src.stem}{CLIP_EXT}"
    start, end = target["start"], target["end"]
    reencoded = False
    with _temp_for(dst) as tmp:
        try:
            actual = _remux(src, tmp, start, end)
        except _remux_errors() as e:
            if isinstance(e, OSError) and not os.path.isfile(src):
                raise ScriptError("source_missing", f"{MISSING_SOURCE}: {src.name}") from e
            tmp.unlink(missing_ok=True)
            reencoded = True
            try:
                actual = _reencode(src, tmp, start, end)
            except _remux_errors() as e2:
                raise ScriptError("clip_failed", f"컷 파일을 만들지 못했습니다: {src.name}: {e2}") from e2
        try:
            os.replace(tmp, dst)
        except OSError as e:
            raise ScriptError("clip_failed", MSG_LOCKED.format(name=dst.name)) from e
    return {"cut": target["cut"], "n": target["n"], "name": src.name, "file": _rel(dst),
            "start": round(max(0.0, actual), 3), "end": end, "reencoded": reencoded}


def clear_clips(out_dir: Path) -> None:
    """이전에 만든 컷 파일만 지운다(그 폴더의 다른 파일은 둔다)."""
    if not out_dir.is_dir():
        return
    for entry in out_dir.iterdir():
        if entry.is_file() and CLIP_NAME_RE.fullmatch(entry.name):
            try:
                entry.unlink()
            except OSError:
                pass   # 다른 프로그램이 열어 둠: 같은 이름이면 그 컷에서 '열려 있어요'로 알리고, 아니면 남는다


def export_clips(conti_file: str | Path, progress: ClipProgress | None = None,
                 targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """고른 구간이 있는 컷마다 drafts/<기획안>-cuts/NN_<원본 이름>.mp4를 만든다.
    progress(i, event, info): 컷마다 "start" 다음 "done" | "skipped" | "failed"(info = 결과).
    "start" 콜백이 예외를 내면(작업 취소) 거기서 멈춘다.
    targets: 미리 구한 clip_targets(작업 진행 항목과 순서를 맞출 때). 없으면 지금 구한다.
    -> {"dir", "clips": [...], "skipped": [{"cut","n","name","reason"}], "failed": [...]}"""
    path = _conti_path(conti_file)
    _av()
    if targets is None:
        targets = clip_targets(path)
    if not targets:
        raise ScriptError("no_clips", MSG_NO_CLIPS)
    out_dir = clips_dir(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_clips(out_dir)
    result: dict[str, Any] = {"dir": _rel(out_dir), "clips": [], "skipped": [], "failed": []}
    for i, target in enumerate(targets):
        if progress:
            progress(i, "start", target)
        base = {"cut": target["cut"], "n": target["n"], "name": target["name"]}
        if target["source"] is None:
            info = {**base, "reason": f"{MISSING_SOURCE}: {target['name'] or target['fid']}"}
            result["skipped"].append(info)
            event = "skipped"
        else:
            try:
                info = cut_clip(target, out_dir)
            except Exception as e:  # noqa: BLE001 - 컷 하나의 오류로 나머지 컷을 멈추지 않는다
                reason = e.message if isinstance(e, ScriptError) else f"예상치 못한 오류: {type(e).__name__}: {e}"
                info = {**base, "reason": reason}
                if isinstance(e, ScriptError) and e.code == "source_missing":
                    result["skipped"].append(info)
                    event = "skipped"
                else:
                    result["failed"].append(info)
                    event = "failed"
            else:
                result["clips"].append(info)
                event = "done"
        if progress:
            progress(i, event, info)
    return result


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.footage_out")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("remaining", "csv", "clips"):
        sub.add_parser(name).add_argument("conti")
    args = parser.parse_args(argv)
    if args.cmd == "remaining":
        return {"path": remaining_html(args.conti)}
    if args.cmd == "csv":
        return {"path": csv_export(args.conti)}
    return export_clips(args.conti)


if __name__ == "__main__":
    run(main)
