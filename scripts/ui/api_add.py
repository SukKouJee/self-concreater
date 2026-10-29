"""레퍼런스 추가 API: `commands/yt-add.md` 2절 흐름을 백그라운드 작업(kind "collect")으로 돌린다.

작업 줄 모델: 워커 하나(`Collector`)가 줄에서 할 일을 하나씩 꺼낸다(받기·분석은 영상 하나씩).
- collect: fetch → cuts → videomd.context → Claude 분석 → 확인 대기(`state="awaiting", ask="review"`).
  확인을 기다리지 않고 곧바로 다음 할 일로 간다.
- 저장(`POST /api/add/<작업>/review/<항목>` save)은 요청을 받은 HTTP 스레드가 `take_review` 관문을 지나
  바로 md를 쓴다. 샷 분류 옵션이면 그 영상의 shots 할 일을 줄 뒤에 붙인다. 다시 분석(reanalyze)도 줄 뒤에 붙는다.
- 줄이 비고 확인 대기가 없으면 작업이 done으로 끝난다. 작업 중에 넣은 링크는 같은 줄 끝에 붙고(같은 영상은 빼고),
  옵션·카테고리는 항목마다 `opts`로 들고 있다.
- force: fetch reanalyze=True, write force=True (cuts 캐시 유지)
- refetch: fetch force=True, cuts force=True, write force=True
- 자막이 없으면 묻지 않고 `opts.no_transcript`(stt/screen/skip)대로 간다.
- blocked/proxy_failed(수집·STT·컷 어디서든): 그 영상 failed, 줄에 남은 영상 skipped("차단으로 중단"), job stopped.
  확인 대기 영상은 남아 저장·건너뛰기를 계속 받고, 다음 새 작업이 앞쪽으로 옮겨 간다("새 작업으로 옮김").
- claude_missing/claude_login: 같은 방식으로 멈춘다(남은 영상을 받지 않는다). 라우트는 작업 전에 claude를 확인한다.
- 촬영 영상 작업(footage·footage_clips)이 도는 동안에는 새 collect 작업을 시작하거나 도는 줄에 링크를 붙이지 않는다:
  `ScriptError("busy", "촬영 영상 처리가 끝난 뒤 시작해 주세요")`(jobs.EXCLUSIVE). 반대로 collect는 확인만 기다릴 때는
  촬영 영상 처리를 막지 않는다.
- 예상치 못한 예외: 그 영상만 failed("예상치 못한 오류: ...")로 두고 다음 할 일로 간다.
- 분석 호출에는 examples md를 항상 첨부하고, 장면 프레임(최대 8장, 처음·끝 포함)은 sparse이거나 화면 분석을 고른 경우에만 첨부한다(yt-analyze 0단계).
  화면 위주 분석 안내(SCREEN_HINT)도 같은 경우에만 task에 붙인다.
- 단계 사이마다 `job.check_cancelled()`: 서버 종료 시 진행 중인 단계(다운로드 정리 try/finally 포함)를
  끝까지 마친 뒤 다음 단계로 가지 않고 멈춘다.
- 항목마다 `timing`(단계 줄별 진행·예상 시간, `scripts.ui.progress.ItemTiming`)을 게시한다.
  줄은 fetch·cuts·analyze(+shots), 샷 전용 작업은 shots 하나. 확인 대기는 분석 줄이 끝난 뒤라 걸린 시간에
  들어가지 않고, 차단 방지 대기는 줄을 멈춰 뺀다. 진행 표시는 보조 정보라 그 오류로 수집을 멈추지 않는다(`_StepClock`).
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts import claude_cli, cuts, fetch, llm, refstore, shots, stt, timings, videomd
from scripts.common import Progress, ScriptError, work_root, workspace_dir
from scripts.ui.jobs import SINGLE_KINDS, STOP_MESSAGE, Job, JobCancelled, new_item
from scripts.ui.progress import ItemTiming
from scripts.ui.server import Request, route

KIND = "collect"
ROLES = ("ref", "mine")
BLOCK_CODES = frozenset({"blocked", "proxy_failed"})
CLAUDE_CODES = frozenset({"claude_missing", "claude_login", "llm_auth", "llm_not_configured"})
STOP_CODES = BLOCK_CODES | CLAUDE_CODES
REVIEW_ACTIONS = frozenset({"save", "skip", "reanalyze"})
NO_TRANSCRIPT_CHOICES = ("stt", "screen", "skip")
IDLE_POLL_SEC = 0.5   # 줄이 비었을 때 확인 대기가 끝났는지 다시 보는 간격

ANALYZE_FRAME_CAP = 8
ANALYZE_TASK = "yt-analyze 규칙대로 입력 영상의 분석 JSON을 만드세요(5절 출력 형식)."
SCREEN_HINT = "\n자막이 없거나 말이 거의 없는 영상입니다. scenes의 frame 이미지(첨부되었거나 Read로 열 수 있는 이미지)를 보고 화면 위주로 분석하세요(0단계)."
SHOTS_TASK = (
    "yt-shots 규칙대로 frames의 이미지(첨부되었거나 Read로 열 수 있는 이미지)를 하나씩 보고 모든 프레임을 분류하세요. "
    '출력은 4절의 라벨 배열을 {"labels": [...]} 객체 하나로 감싸서 내세요.'
)
DESCRIBE_KIND = "refs_describe"
DESCRIBE_ROW = "describe"
DESCRIBE_TASK = (
    "yt-shots 규칙대로 frames의 이미지(첨부되었거나 Read로 열 수 있는 이미지)를 하나씩 보고 프레임마다 desc(5절)를 쓰세요. "
    "size가 이미 있는 프레임은 desc만, 없는 프레임은 size·angle·표시도 함께 쓰세요. "
    '출력은 5절의 배열을 {"labels": [...]} 객체 하나로 감싸서 내세요.'
)
MSG_NO_PROGRESS = "사진 설명이 저장되지 않아 이 영상은 멈췄어요"
MSG_NOTHING_TO_DESCRIBE = "설명할 레퍼런스 사진이 없어요"
MSG_EXISTS = "이미 분석됨"
MSG_BLOCKED = "차단으로 중단"
MSG_CLAUDE_STOP = "Claude(LLM 엔진)를 쓸 수 없어 중단"
MSG_SKIPPED = "건너뜀"
MSG_ALREADY = "이미 있음"
MSG_NO_TRANSCRIPT_SKIP = "자막이 없어 건너뜀"
MSG_SCREEN_ONLY = "자막이 없어 화면만 보고 분석합니다"
MSG_MOVED = "새 작업으로 옮김"
MSG_SHOTS_STOPPED = "샷 분류는 작업이 멈춰 건너뛰었어요"
MSG_REANALYZE_STOPPED = "작업이 멈춰 다시 분석하지 못했습니다"
MSG_REANALYZE_FAILED = "다시 분석하지 못했습니다"
MSG_JOB_STOPPED = "작업이 멈춰 다시 분석할 수 없습니다. 링크를 다시 넣어 주세요"
MSG_JOB_STOPPING = "작업을 멈추는 중입니다. 잠시 뒤 다시 넣어 주세요"
MSG_USE_REVIEW = "레퍼런스 확인은 /api/add/<작업>/review/<항목> 경로로 보내 주세요"
MSG_REVIEWS_PENDING = "확인을 기다리는 영상이 있어요. 먼저 저장하거나 건너뛰어 주세요"
MSG_NO_FRAMES = "컷 통계가 없어 샷 분류를 건너뜁니다"
COLLECT_ROWS = ("fetch", "cuts", "analyze")
SHOTS_ROW = "shots"
WAIT_PHASE = "wait"   # fetch.wait_turn의 차단 방지 대기: 걸린 시간에 넣지 않는다


def _analyze_rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-analyze" / "SKILL.md"]


def _shots_rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-shots" / "SKILL.md"]


def _split_links(links: str | Iterable[str]) -> list[str]:
    """공백·줄바꿈으로 나눈 모든 조각(빈 값 제외, 중복 유지)."""
    chunks = [links] if isinstance(links, str) else list(links)
    pieces: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, str):
            raise ScriptError("bad_request", "links는 문자열 목록이어야 합니다")
        pieces.extend(chunk.split())
    return pieces


def parse_links(links: str | Iterable[str]) -> list[str]:
    """공백·줄바꿈으로 나누고 빈 값과 중복을 뺀다(처음 나온 순서 유지)."""
    return list(dict.fromkeys(_split_links(links)))


def _row_id(link: str) -> str | None:
    """줄 하나의 영상 id. 영상 링크가 아니면(채널·재생목록 등) None."""
    try:
        return fetch.parse_video_id(link)
    except ScriptError:
        return None


def _saved_state(category: str, video_id: str, role: str) -> tuple[str, str | None] | None:
    """요청한 카테고리의 md만 본다. 없으면 None, 있으면 (exists|other_role, title).

    프런트매터를 못 읽거나 role이 없으면 같은 role로 보고(exists) title은 None.
    """
    path = refstore.category_dir(category) / "videos" / f"{video_id}.md"
    if not path.is_file():
        return None
    try:
        fm = refstore.read_md(path)[0]
    except (ScriptError, OSError):
        return "exists", None
    title = fm.get("title")
    saved_role = fm.get("role")
    status = "other_role" if saved_role and saved_role != role else "exists"
    return status, title if isinstance(title, str) and title else None


def check_links(links: str | Iterable[str], category: str, role: str, force: bool) -> dict:
    """붙여넣은 링크를 줄마다 new/exists/other_role/dup/invalid/reanalyze로 가른다(입력 순서, 빈 줄 제외)."""
    lines = _split_links(links)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in lines:
        video_id = _row_id(link)
        title: str | None = None
        if video_id is None:
            status = "invalid"
        elif video_id in seen:
            status = "dup"
        else:
            seen.add(video_id)
            status = "new"
            saved = _saved_state(category, video_id, role)
            if saved is not None:
                status, title = saved
                if status == "exists" and force:
                    status = "reanalyze"
        rows.append({"link": link, "id": video_id, "status": status, "title": title})
    return {"rows": rows, "count": sum(1 for r in rows if r["status"] in {"new", "reanalyze"})}


@dataclass(frozen=True)
class AddOptions:
    """영상(항목) 하나의 옵션. 작업 중에 링크를 더 넣을 수 있어 항목마다 `opts`로 들고 있다."""

    category: str
    role: str = "ref"
    shots: bool = False
    force: bool = False
    refetch: bool = False
    no_transcript: str = "stt"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def of(cls, item: dict) -> AddOptions:
        return cls(**item["opts"])


def _stop_message(code: str) -> str:
    return MSG_CLAUDE_STOP if code in CLAUDE_CODES else MSG_BLOCKED


def _summary(items: list[dict], stage_text: str | None) -> dict:
    states = [item["state"] for item in items]
    return {"saved": states.count("saved"), "skipped": states.count("skipped"),
            "failed": states.count("failed"), "stage_text": stage_text}


def _join_message(prior: Any, message: str) -> str:
    return f"{prior} / {message}" if prior else message


def _item_video(item: dict) -> str | None:
    """항목의 영상 id: 수집이 알아낸 값, 아니면 링크에서 읽은 값."""
    video_id = item.get("video_id")
    if isinstance(video_id, str) and video_id:
        return video_id
    link = item.get("url")
    return _row_id(link) if isinstance(link, str) else None


def _same_video(old: dict, new: dict) -> bool:
    """같은 링크이거나 같은 영상 id면 같은 영상이다."""
    if old.get("url") is not None and old.get("url") == new.get("url"):
        return True
    old_id = _item_video(old)
    return old_id is not None and old_id == _item_video(new)


def _fresh(items: list[dict], existing: list[dict]) -> list[dict]:
    """existing이나 앞선 항목과 같은 영상인 항목을 뺀다(입력 순서 유지)."""
    kept: list[dict] = []
    for item in items:
        if not any(_same_video(old, item) for old in [*existing, *kept]):
            kept.append(item)
    return kept


def _frame_files(frames: Any) -> list[Path]:
    """frame 경로(작업 폴더 기준)를 파일 경로로 바꾼다. frames는 {"frame": path} 목록."""
    root = work_root()
    return [root / f["frame"] for f in (frames or []) if isinstance(f, dict) and f.get("frame")]


def _analysis_frames(cut_data: dict | None) -> list[Path]:
    """yt-analyze 0단계: 장면 프레임 최대 8장(처음·끝 포함, 나머지는 고르게)."""
    scenes = [s for s in (cut_data or {}).get("scenes", []) if s.get("frame")]
    root = work_root()
    return [root / scenes[i]["frame"] for i in cuts.pick_frame_scenes(len(scenes), ANALYZE_FRAME_CAP)]


def _classify_shots(video_id: str, category: str) -> dict:
    """shots.context → Claude 분류 → tmp에 쓰고 shots.apply. apply가 invalid_shots면 한 번 다시 묻는다."""
    ctx = shots.context(video_id, category)
    tmp = workspace_dir("tmp", f"shots_{video_id}.json")

    def apply(data: dict) -> dict:
        labels = data.get("labels")
        if not isinstance(labels, list) or not labels:
            raise ScriptError("invalid_shots", '샷 분류가 형식에 맞지 않습니다: {"labels": [...]} 배열이 없습니다')
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
        return shots.apply(video_id, category, tmp)

    try:
        _, applied = llm.ask_validated(
            _shots_rules(), ctx, SHOTS_TASK, apply, files=_frame_files(ctx.get("frames")),
        )
    finally:
        tmp.unlink(missing_ok=True)
    return applied


def _write(video_id: str, opts: AddOptions, analysis: dict) -> dict:
    path = workspace_dir("tmp", f"analysis_{video_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        return videomd.write(video_id, opts.category, opts.role, path, opts.force or opts.refetch)
    finally:
        path.unlink(missing_ok=True)


# ---- 단계별 진행 시간 ----------------------------------------------------------------

def _timing_engine() -> str | None:
    """분석·샷 기록을 나눌 엔진 이름. 설정이 깨졌으면 None(기록만 엔진 없이, 수집은 계속)."""
    try:
        return str(llm.config()["engine"])
    except ScriptError:
        return None


def _length(value: Any) -> float | None:
    """유한하고 0보다 큰 숫자만 영상 길이(초)로 쓴다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _raw_length(video_id: str) -> float | None:
    """캐시된 raw의 메타 길이. raw가 없거나 읽을 수 없으면 None(그 경우 수집이 다시 받는다)."""
    try:
        raw = fetch.load_raw(video_id)
    except (ScriptError, OSError, ValueError):
        return None
    meta = raw.get("meta") if isinstance(raw, dict) else None
    return _length(meta.get("duration")) if isinstance(meta, dict) else None


def _link_length(link: str) -> float | None:
    try:
        video_id = fetch.parse_video_id(link)
    except ScriptError:
        return None   # 잘못된 링크는 fetch가 알린다
    return _raw_length(video_id)


class _StepClock:
    """한 항목의 ItemTiming을 감싼다. 진행 표시는 보조 정보라 여기서 난 오류는 stderr에 한 번 남기고 계속한다.

    - 지금 달리는 줄과 끝난 줄을 직접 기억해, 실패·취소 때 `fail_running()`으로 달리던 줄을 실패로,
      항목이 끝날 때 `skip_pending()`으로 시작하지 않은 줄을 건너뜀으로 둔다(끝난 카드에 남은 시간이 남지 않게).
    - 영상 길이는 처음 알게 된 값만 쓴다(`offer_video_sec`): raw 캐시 → no_transcript 정보 → 메타 → 컷 통계 순으로 부른다.
    - `progress()`는 수집 함수에 넘길 콜백이다. 차단 방지 대기("wait") 동안은 줄을 멈춰 걸린 시간에서 빼고,
      대기 글은 그대로 보여 준다.
    - 확인 대기 중인 항목이 새 작업으로 옮겨지면 `bind()`로 새 작업·인덱스에 이어서 게시한다.
    """

    def __init__(self, job: Job, index: int, rows: list[str], engine: str | None) -> None:
        self._timing: ItemTiming | None = None
        self._job = job
        self._index = index
        self._rows = list(rows)
        self._running: str | None = None
        self._settled: set[str] = set()
        self._video_sec: float | None = None
        self._warned = False
        try:
            self._timing = ItemTiming(job, index, rows, engine=engine)
        except Exception:  # noqa: BLE001 - 진행 표시가 고장 나도 수집은 계속한다
            self._warn()

    def bind(self, job: Job, index: int) -> None:
        if job is self._job and index == self._index:
            return
        self._job, self._index = job, index
        self._call(lambda t: t.rebind(job, index))

    def offer_video_sec(self, value: Any) -> None:
        sec = _length(value)
        if self._video_sec is None and sec is not None:
            self._video_sec = sec
            self._call(lambda t: t.set_video_sec(sec))

    def start(self, row: str, sub: str | None = None) -> None:
        self._running = row
        self._settled.discard(row)
        self._call(lambda t: t.start(row, sub))

    def done(self, row: str, *, cached: bool = False, **record_kw: Any) -> None:
        self._settle(row)
        self._call(lambda t: t.done(row, cached=cached, **record_kw))

    def fail(self, row: str) -> None:
        """오류로 끝난 줄."""
        self._settle(row)
        self._call(lambda t: t.fail(row))

    def skip(self, row: str) -> None:
        """오류가 아니라 하지 않은 줄(STT 없음, 프레임 없음)."""
        if row not in self._rows:
            return
        self._settle(row)
        self._call(lambda t: t.skip(row))

    def fail_running(self) -> None:
        if self._running is not None:
            self.fail(self._running)

    def skip_pending(self) -> None:
        for row in self._rows:
            if row not in self._settled and row != self._running:
                self.skip(row)

    def pause(self, *, publish: bool = True) -> None:
        self._call(lambda t: t.pause(publish=publish))

    def resume(self, *, publish: bool = True) -> None:
        self._call(lambda t: t.resume(publish=publish))

    @contextmanager
    def progress(self) -> Iterator[Progress]:
        """with 블록 안에서 쓸 진행 콜백. 블록이 끝날 때 대기 중이었으면 다시 시계를 움직인다."""
        waiting = False

        def report(phase: str, fraction: float | None, text: str) -> None:
            nonlocal waiting
            # 바로 뒤의 phase()가 구간 이름이 바뀌어 게시하므로 pause/resume은 따로 게시하지 않는다
            if phase == WAIT_PHASE and not waiting:
                waiting = True
                self.pause(publish=False)
            elif phase != WAIT_PHASE and waiting:
                waiting = False
                self.resume(publish=False)
            self._call(lambda t: t.phase(phase, fraction, text))

        try:
            yield report
        finally:
            if waiting:
                self.resume()

    def _settle(self, row: str) -> None:
        self._settled.add(row)
        if self._running == row:
            self._running = None

    def _call(self, action: Callable[[ItemTiming], None]) -> None:
        if self._timing is None:
            return
        try:
            action(self._timing)
        except Exception:  # noqa: BLE001 - 진행 표시가 고장 나도 수집은 계속한다
            self._warn()

    def _warn(self) -> None:
        if not self._warned:
            self._warned = True
            traceback.print_exc(file=sys.stderr)


@dataclass(frozen=True)
class _AnalyzeRun:
    analysis: dict
    frames: int       # 첨부한 장면 프레임 수
    text_chars: int   # 대본(자막) 글자 수


class Collector:
    """collect 작업의 워커(JobManager.start의 target). 작업 줄에서 할 일을 하나씩 꺼내 처리한다.

    줄에 오르는 일: `collect`(받기→컷→분석→확인 대기), `reanalyze`(의견을 넣어 다시 분석→확인 대기),
    `shots`(저장된 영상의 샷 분류). 저장·건너뛰기는 HTTP 스레드가 `save_review`/`skip_review`로 바로 처리한다.
    항목마다 `_StepClock`을 들고 있어 다시 분석·샷 분류가 같은 진행 표시에 이어서 기록된다.
    """

    def __init__(self, clocks: dict[int, _StepClock] | None = None) -> None:
        self.stage_text: str | None = None
        self.engine: str | None = None
        self._clocks: dict[int, _StepClock] = dict(clocks or {})
        self._clocks_lock = threading.Lock()

    def __call__(self, job: Job) -> None:
        self.engine = _timing_engine()
        try:
            while True:
                task = job.next_task(IDLE_POLL_SEC)
                if task is None:
                    job.sync_status()
                    if job.close_if_idle(lambda items: _summary(items, self.stage_text)):
                        return
                    continue
                stop = self._run_task(job, task)
                job.sync_status()
                if stop is not None:
                    self._settle_leftovers(job, job.close_queue(), _stop_message(stop.code))
                    job.finish("stopped", _summary(job.items, self.stage_text), stop.message)
                    return
        except JobCancelled:
            # 남은 줄을 먼저 정리한다: 저장된 영상의 샷 분류는 saved로, 다시 분석 대기는 확인 대기로 되돌린다
            self._settle_leftovers(job, job.close_queue(), STOP_MESSAGE)
            raise

    # ---- 진행 시계 ---------------------------------------------------------------------

    def clock(self, job: Job, index: int) -> _StepClock:
        """항목의 진행 시계. 없으면(예상 밖) 분석·샷 줄만으로 새로 만든다. 다른 작업에서 옮겨 왔으면 이 작업에 묶는다."""
        with self._clocks_lock:
            clock = self._clocks.get(index)
            if clock is None:
                opts = AddOptions.of(job.item(index))
                rows = [COLLECT_ROWS[-1], *([SHOTS_ROW] if opts.shots else [])]
                clock = self._clocks[index] = _StepClock(job, index, rows, self.engine)
        clock.bind(job, index)
        return clock

    def take_clock(self, index: int) -> _StepClock | None:
        with self._clocks_lock:
            return self._clocks.pop(index, None)

    def put_clock(self, index: int, clock: _StepClock) -> None:
        with self._clocks_lock:
            self._clocks[index] = clock

    def _new_clock(self, job: Job, index: int, rows: list[str]) -> _StepClock:
        clock = _StepClock(job, index, rows, self.engine)
        with self._clocks_lock:
            self._clocks[index] = clock
        return clock

    # ---- 줄의 할 일 --------------------------------------------------------------------

    def _run_task(self, job: Job, task: dict) -> ScriptError | None:
        """할 일 하나를 처리한다. 작업을 멈춰야 하는 오류(STOP_CODES)면 그 오류를 돌려준다."""
        kind, index = task.get("kind"), task["index"]
        if kind == "collect":
            return self._collect_task(job, index)
        if kind == "reanalyze":
            return self._reanalyze_task(job, index, str(task.get("feedback") or "").strip() or None)
        if kind == "shots":
            self._shots_task(job, index)
            return None
        raise ScriptError("bad_task", f"알 수 없는 할 일입니다: {kind!r}")

    def _settle_leftovers(self, job: Job, tasks: list[dict], message: str) -> None:
        """작업이 멈출 때 줄에 남은 할 일을 정리한다(확인 대기 영상은 그대로 남는다)."""
        for task in tasks:
            index = task["index"]
            if task.get("kind") == "shots":
                self.clock(job, index).skip(SHOTS_ROW)
                prior = job.item(index)["message"]
                job.update(index, state="saved", step="done", message=_join_message(prior, MSG_SHOTS_STOPPED))
            elif task.get("kind") == "reanalyze":
                job.update(index, state="awaiting", ask="review", step="confirm", message=MSG_REANALYZE_STOPPED)
            else:
                job.update(index, state="skipped", message=message)

    def _collect_task(self, job: Job, index: int) -> ScriptError | None:
        clock: _StepClock | None = None

        def settle_failed() -> None:
            if clock is not None:
                clock.fail_running()   # 실패·취소: 달리던 줄은 기록하지 않고 실패로 둔다
                clock.skip_pending()

        try:
            # 옵션 읽기도 영상 하나의 일이다: 항목 하나의 opts가 깨져도 그 영상만 실패한다
            opts = AddOptions.of(job.item(index))
            clock = self._new_clock(job, index, [*COLLECT_ROWS, *([SHOTS_ROW] if opts.shots else [])])
            in_review = self._collect(job, index, clock, opts)
        except JobCancelled:
            settle_failed()
            raise
        except ScriptError as e:
            settle_failed()
            job.update(index, state="failed", message=e.message)
            return e if e.code in STOP_CODES else None
        except Exception as e:  # noqa: BLE001 - 한 영상의 버그가 남은 영상까지 막지 않게 한다
            traceback.print_exc(file=sys.stderr)
            settle_failed()
            job.update(index, state="failed", message=f"예상치 못한 오류: {type(e).__name__}: {e}")
            return None
        if not in_review:
            clock.skip_pending()   # 항목이 끝났으니 시작하지 않은 줄은 건너뜀
        return None

    def _collect(self, job: Job, index: int, clock: _StepClock, opts: AddOptions) -> bool:
        """영상 하나를 받고·자르고·분석해 확인 대기로 둔다. 확인 대기로 갔으면 True, 건너뛰었으면 False."""
        notes: list[str] = []
        job.update(index, state="running", step="fetch")
        link = job.item(index)["url"]
        screen = False
        clock.offer_video_sec(_link_length(link))
        clock.start("fetch")
        try:
            with clock.progress() as report:
                info = fetch.fetch(link, opts.category, opts.role, force=opts.refetch, reanalyze=opts.force,
                                   progress=report)
        except ScriptError as e:
            if e.code != "no_transcript":
                raise
            clock.offer_video_sec(e.details.get("duration_sec"))
            video_id = self._identify(job, index, e.details.get("id"), e.details.get("title"))
            clock.done("fetch", cached=e.details.get("raw_status") == "cached")
            if opts.no_transcript == "skip":
                job.update(index, state="skipped", step="done", message=MSG_NO_TRANSCRIPT_SKIP)
                return False
            if opts.no_transcript == "stt":
                screen = not self._transcribe(job, index, video_id, notes, clock)
            else:
                screen = True
                notes.append(MSG_SCREEN_ONLY)
        else:
            clock.offer_video_sec(info.get("duration_sec"))
            video_id = self._identify(job, index, info.get("id"), info.get("title"))
            clock.done("fetch", cached=info.get("status") in {"cached", "exists"})
            if info.get("status") == "exists":
                job.update(index, state="skipped", step="done", message=MSG_EXISTS)
                return False

        job.check_cancelled()
        job.update(index, step="cuts")
        clock.start("cuts")
        try:
            with clock.progress() as report:
                stats = cuts.cut_stats(video_id, force=opts.refetch, progress=report)
        except ScriptError as e:
            clock.fail("cuts")
            if e.code in STOP_CODES:
                raise
            notes.append(f"컷 통계 없이 계속합니다: {e.message}")
        else:
            clock.offer_video_sec(stats.get("duration_sec"))
            clock.done("cuts", cached=stats.get("status") == "cached")
            job.update(index, frames=list(stats.get("frames") or []))
        note = " / ".join(notes) or None
        job.update(index, message=note, note=note, screen=screen)

        job.check_cancelled()
        job.update(index, step="analyze")
        preview = self._timed_analyze(clock, opts.category, video_id, screen, None)
        job.check_cancelled()   # 분석 중에 멈추면 이 영상은 확인 대기로 보내지 않는다(진행 중인 영상만 멈춘다)
        job.update(index, state="awaiting", ask="review", step="confirm", preview=preview)
        return True

    def _reanalyze_task(self, job: Job, index: int, feedback: str | None) -> ScriptError | None:
        """의견을 넣어 다시 분석한다. 실패하면 앞의 분석을 그대로 둔 채 확인 대기로 돌아간다(이유는 message)."""
        item = job.item(index)
        opts = AddOptions.of(item)
        job.update(index, state="running", step="analyze")
        clock = self.clock(job, index)
        try:
            preview = self._timed_analyze(clock, opts.category, str(item["video_id"]), bool(item.get("screen")),
                                          feedback)
        except ScriptError as e:
            clock.fail_running()
            job.update(index, state="awaiting", ask="review", step="confirm",
                       message=f"{MSG_REANALYZE_FAILED}: {e.message}")
            return e if e.code in STOP_CODES else None
        except Exception as e:  # noqa: BLE001 - 앞의 분석으로 확인을 이어 가게 하고, 이유는 message에 남긴다
            traceback.print_exc(file=sys.stderr)
            clock.fail_running()
            job.update(index, state="awaiting", ask="review", step="confirm",
                       message=f"{MSG_REANALYZE_FAILED}: 예상치 못한 오류: {type(e).__name__}: {e}")
            return None
        job.update(index, state="awaiting", ask="review", step="confirm", preview=preview, message=item.get("note"))
        return None

    def _shots_task(self, job: Job, index: int) -> None:
        """저장된 영상의 샷 분류. md는 이미 저장됐으므로 어떤 실패도 항목을 saved로 두고 이유만 남긴다."""
        item = job.item(index)
        opts = AddOptions.of(item)
        job.update(index, state="running", step="shots")
        clock = self.clock(job, index)
        try:
            self._shots_after_save(job, index, str(item["video_id"]), opts.category, clock)
        except Exception as e:  # noqa: BLE001 - 저장된 영상을 실패로 바꾸지 않는다(이유는 message)
            traceback.print_exc(file=sys.stderr)
            clock.fail_running()
            prior = job.item(index)["message"]
            job.update(index, message=_join_message(prior, f"샷 분류 실패: 예상치 못한 오류: {type(e).__name__}: {e}"))
        job.update(index, state="saved", step="done")

    # ---- 확인(HTTP 스레드) ------------------------------------------------------------

    def save_review(self, job: Job, item_id: str, edited: dict | None) -> str | None:
        """확인 대기 항목을 저장한다(take_review 관문을 지난 쪽만). 결과 안내 글(없으면 None)을 돌려준다.

        저장이 실패하면 항목을 확인 대기로 되돌리고(고친 분석은 그대로 보여 준다) 이유를 message에 둔 뒤 ScriptError.
        """
        index = job.take_review(item_id)
        item = job.item(index)
        opts = AddOptions.of(item)
        chosen = edited if edited is not None else item["preview"]
        back: dict[str, Any] = {"state": "awaiting", "ask": "review", "step": "confirm"}
        if edited is not None:
            back["preview"] = edited
        try:
            result = _write(str(item["video_id"]), opts, chosen)
        except ScriptError as e:
            job.update(index, message=e.message, **back)
            raise
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            message = f"예상치 못한 오류: {type(e).__name__}: {e}"
            job.update(index, message=message, **back)
            raise ScriptError("save_failed", message) from e

        clock = self.clock(job, index)
        if result.get("status") == "exists":
            job.update(index, state="skipped", step="done", ask=None, message=MSG_ALREADY)
            clock.skip_pending()
            return MSG_ALREADY
        self.stage_text = result.get("stage_text")
        if opts.shots:
            # 샷 분류는 Claude 호출이라 워커 줄 뒤에 붙인다. 줄이 닫혔으면(작업이 멈춤) 건너뛴다
            if job.requeue(index, {"kind": "shots", "index": index}, state="waiting", step="shots", ask=None):
                return None
            clock.skip(SHOTS_ROW)
            job.update(index, state="saved", step="done", ask=None,
                       message=_join_message(item.get("message"), MSG_SHOTS_STOPPED))
            return MSG_SHOTS_STOPPED
        job.update(index, state="saved", step="done", ask=None)
        clock.skip_pending()
        return None

    def skip_review(self, job: Job, item_id: str) -> None:
        index = job.take_review(item_id)
        job.update(index, state="skipped", step="done", ask=None, message=MSG_SKIPPED)
        self.clock(job, index).skip_pending()

    def reanalyze_review(self, job: Job, item_id: str, feedback: str) -> None:
        """다시 분석을 워커 줄 뒤에 붙인다. 작업이 멈췄으면 확인 대기를 그대로 두고 job_stopped."""
        index = job.take_review(item_id)
        task = {"kind": "reanalyze", "index": index, "feedback": feedback}
        if not job.requeue(index, task, state="waiting", step="analyze", ask=None):
            job.update(index, state="awaiting", ask="review", step="confirm")
            raise ScriptError("job_stopped", MSG_JOB_STOPPED)

    def after_review(self, job: Job) -> None:
        """확인 처리 뒤: status를 맞추고, 이미 멈춘 작업이면 요약을 새로 만든다."""
        job.sync_status()
        if not job.is_active():
            job.set_summary(_summary(job.items, self.stage_text))

    # ---- 영상 하나의 단계 -------------------------------------------------------------

    def _identify(self, job: Job, index: int, video_id: Any, title: Any) -> str:
        """링크로 시작한 항목에 영상 id·제목을 붙인다. 다른 항목이 같은 id를 쓰면 항목 id는 링크로 둔다."""
        if not video_id:
            raise ScriptError("fetch_failed", "영상 id를 알 수 없습니다")
        taken = any(item["id"] == video_id for i, item in enumerate(job.items) if i != index)
        fields: dict[str, Any] = {"title": title, "video_id": str(video_id)}
        if not taken:
            fields["id"] = str(video_id)
        job.update(index, **fields)
        return str(video_id)

    def _transcribe(self, job: Job, index: int, video_id: str, notes: list[str], clock: _StepClock) -> bool:
        """STT로 자막을 만든다(자막 줄의 새 회차, 기록 키 stt). stt_unavailable이면 이유를 남기고 False(화면 분석으로)."""
        job.update(index, message="음성 인식 중")
        clock.start("fetch", sub="stt")
        try:
            with clock.progress() as report:
                result = stt.transcribe(video_id, progress=report)
        except ScriptError as e:
            if e.code != "stt_unavailable":
                clock.fail("fetch")
                raise
            clock.skip("fetch")   # STT가 없을 뿐 오류가 아니다: 화면 분석으로 간다
            notes.append(f"{e.message} — 화면만 보고 분석합니다")
            return False
        clock.done("fetch", cached=result.get("status") == "exists")
        return True

    def _timed_analyze(self, clock: _StepClock, category: str, video_id: str, screen: bool,
                       feedback: str | None) -> dict:
        """분석 줄을 새 회차로 시작해 분석하고 첨부 프레임 수·대본 글자 수와 함께 기록한다."""
        clock.start("analyze")
        run = self._analyze(category, video_id, screen, feedback)
        clock.done("analyze", frames=run.frames, text_chars=run.text_chars)
        return run.analysis

    def _analyze(self, category: str, video_id: str, screen: bool, feedback: str | None) -> _AnalyzeRun:
        ctx = videomd.context(video_id, category)
        # duration·cut_times는 videomd.write와 같은 방식으로 구한다
        raw = fetch.load_raw(video_id)
        cut_data = videomd.load_cuts(video_id)
        duration = float(raw["meta"].get("duration") or (cut_data or {}).get("duration_sec") or 0)
        cut_times = cut_data["cuts"] if cut_data else []
        need_frames = bool(ctx.get("sparse") or screen)
        frames = _analysis_frames(cut_data) if need_frames else []
        files = [*frames, *[work_root() / e for e in ctx.get("examples") or []]]
        task = ANALYZE_TASK + (SCREEN_HINT if need_frames else "")
        if feedback:
            task += f"\n사용자 의견: {feedback}"
        _, analysis = llm.ask_validated(
            _analyze_rules(), ctx, task, lambda d: videomd.validate(d, duration, cut_times), files=files,
        )
        return _AnalyzeRun(analysis, frames=len(frames), text_chars=_transcript_chars(raw))

    def _shots_after_save(self, job: Job, index: int, video_id: str, category: str, clock: _StepClock) -> None:
        """md는 이미 저장됐으므로 샷 분류 실패는 항목을 실패로 바꾸지 않고 이유만 남긴다(샷 줄만 실패)."""
        clock.start(SHOTS_ROW)
        try:
            _classify_shots(video_id, category)
        except ScriptError as e:
            if e.code == "no_frames":
                clock.skip(SHOTS_ROW)
            else:
                clock.fail(SHOTS_ROW)
            message = MSG_NO_FRAMES if e.code == "no_frames" else f"샷 분류 실패: {e.message}"
            prior = job.item(index)["message"]
            job.update(index, message=_join_message(prior, message))
            return
        clock.done(SHOTS_ROW)


def _transcript_chars(raw: Any) -> int:
    """raw 자막의 글자 수(분석 기록용). 자막이 없거나 모양이 다르면 0."""
    transcript = raw.get("transcript") if isinstance(raw, dict) else None
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    return sum(len(s["text"]) for s in segments or [] if isinstance(s, dict) and isinstance(s.get("text"), str))


def run_shots_only(category: str) -> Callable[[Job], None]:
    def target(job: Job) -> None:
        engine = _timing_engine()
        for index, item in enumerate(job.items):
            job.check_cancelled()
            job.update(index, state="running", step="shots")
            clock = _StepClock(job, index, [SHOTS_ROW], engine)
            clock.offer_video_sec(_raw_length(item["id"]))
            clock.start(SHOTS_ROW)
            try:
                _classify_shots(item["id"], category)
            except ScriptError as e:
                if e.code == "no_frames":
                    clock.skip(SHOTS_ROW)
                    job.update(index, state="skipped", step="done", message=MSG_NO_FRAMES)
                    continue
                clock.fail(SHOTS_ROW)
                job.update(index, state="failed", message=e.message)
                if e.code in CLAUDE_CODES:
                    job.skip_rest(index + 1, MSG_CLAUDE_STOP)
                    job.finish("stopped", _summary(job.items, None), e.message)
                    return
                continue
            except BaseException:
                clock.fail_running()   # 예상치 못한 오류·취소: 기록하지 않는다
                raise
            clock.done(SHOTS_ROW)
            job.update(index, state="saved", step="done")
        job.finish("done", _summary(job.items, None))

    return target


# ---- 레퍼런스 사진 설명 --------------------------------------------------------------

def _describe_batch(video_id: str, category: str) -> dict | None:
    """설명이 없는 프레임 한 묶음(최대 shots.DESC_BATCH장)을 Claude에 묻고 저장한다. 남은 프레임이 없으면 None."""
    ctx = shots.describe_context(video_id, category, limit=shots.DESC_BATCH)
    if not ctx["frames"]:
        return None
    tmp = workspace_dir("tmp", f"desc_{video_id}.json")

    def apply(data: dict) -> dict:
        labels = data.get("labels")
        if not isinstance(labels, list) or not labels:
            raise ScriptError("invalid_shots", '사진 설명이 형식에 맞지 않습니다: {"labels": [...]} 배열이 없습니다')
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
        return shots.apply_desc(video_id, category, tmp)

    try:
        _, applied = llm.ask_validated(
            _shots_rules(), ctx, DESCRIBE_TASK, apply, files=_frame_files(ctx["frames"]),
        )
    finally:
        tmp.unlink(missing_ok=True)
    return applied


def _batches(frames: int) -> int:
    return -(-frames // shots.DESC_BATCH)


def run_describe(category: str) -> Callable[[Job], None]:
    """카테고리의 남은 레퍼런스 프레임을 묶음으로 Claude에 보내 desc를 채운다. 멈추기는 묶음 사이에서만 듣는다.

    묶음마다 바로 저장하므로 중간에 멈추거나 한 묶음이 실패해도 앞선 묶음은 남는다. Claude 로그인·설정 문제면 남은 영상을 건너뛰고 멈춘다.
    남은 시간은 이 작업에서 지금까지 걸린 묶음 평균(없으면 기록된 예상)에 남은 묶음 수를 곱한다.
    """
    def target(job: Job) -> None:
        eng = _timing_engine()
        durations: list[float] = []
        described = 0
        left = [int(i["total"]) for i in job.items]

        def eta() -> float | None:
            avg = sum(durations) / len(durations) if durations else timings.expected(DESCRIBE_ROW, video_sec=None, engine=eng)
            return round(avg * sum(_batches(n) for n in left), 1) if avg is not None else None

        for index, item in enumerate(job.items):
            job.check_cancelled()
            total = int(item["total"])
            job.update(index, state="running", step=DESCRIBE_ROW, done=0, eta_sec=eta())
            clock = _StepClock(job, index, [DESCRIBE_ROW], eng)
            try:
                while left[index] > 0:
                    job.check_cancelled()
                    clock.start(DESCRIBE_ROW)
                    began = time.monotonic()
                    applied = _describe_batch(item["id"], category)
                    if applied is None:
                        clock.skip(DESCRIBE_ROW)
                        left[index] = 0
                        break
                    clock.done(DESCRIBE_ROW)
                    durations.append(time.monotonic() - began)
                    now_left = int(applied["remaining"])
                    if now_left >= left[index]:
                        raise ScriptError("no_progress", MSG_NO_PROGRESS)
                    described += left[index] - now_left
                    left[index] = now_left
                    job.update(index, done=total - now_left, eta_sec=eta())
            except ScriptError as e:
                clock.fail_running()
                if e.code == "no_frames":
                    job.update(index, state="skipped", step="done", message=MSG_NO_FRAMES)
                    left[index] = 0
                    continue
                job.update(index, state="failed", step="done", message=e.message)
                left[index] = 0
                if e.code in CLAUDE_CODES:
                    job.skip_rest(index + 1, MSG_CLAUDE_STOP)
                    job.finish("stopped", {**_summary(job.items, None), "described": described}, e.message)
                    return
                continue
            except BaseException:
                clock.fail_running()   # 예상치 못한 오류·취소: 기록하지 않는다
                raise
            job.update(index, state="saved", step="done", eta_sec=eta())
        job.finish("done", {**_summary(job.items, None), "described": described})

    return target


# ---- 라우트 -------------------------------------------------------------------------

def _flag(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ScriptError("bad_request", f"{key}는 true/false여야 합니다")
    return value


def _category(body: dict) -> str:
    category = body.get("category")
    if not isinstance(category, str) or not category:
        raise ScriptError("bad_request", "category가 필요합니다")
    refstore.existing_category_dir(category)
    return category


def _no_transcript(body: dict) -> str:
    value = body.get("no_transcript", "stt")
    if value not in NO_TRANSCRIPT_CHOICES:
        raise ScriptError("bad_request", f"no_transcript는 {'/'.join(NO_TRANSCRIPT_CHOICES)} 중 하나여야 합니다")
    return str(value)


def _collector(job: Job) -> Collector | None:
    return job.target if isinstance(job.target, Collector) else None


def _has_leftover_reviews(job: Job) -> bool:
    return not job.is_active() and _collector(job) is not None and not job.review_done()


def _start_collect(app: Any, items: list[dict], category: str) -> Job:
    """새 collect 작업을 시작한다. 확인 대기가 남은 가장 최근의 멈춘 collect 작업에서 그 영상들을 앞쪽으로 옮겨 온다.

    가장 최근 작업만 보지 않는 까닭: 그 사이에 샷 분류 보강(같은 kind)이 돌았어도 확인 대기를 잃지 않게.
    """
    previous = app.jobs.latest_where(KIND, _has_leftover_reviews)
    old = _collector(previous) if previous is not None else None
    released = previous.release_reviews(MSG_MOVED) if previous is not None and old is not None else []
    adopted = [item for _, item in released]
    fresh = _fresh(items, adopted)
    clocks: dict[int, _StepClock] = {}
    for new_index, (old_index, _) in enumerate(released):
        clock = old.take_clock(old_index) if old is not None else None
        if clock is not None:
            clocks[new_index] = clock
    tasks = [{"kind": "collect", "index": i} for i in range(len(adopted), len(adopted) + len(fresh))]
    try:
        return app.jobs.start(KIND, Collector(clocks), items=[*adopted, *fresh], category=category, tasks=tasks)
    except ScriptError:
        # 다른 요청이 먼저 작업을 시작했다: 옮기려던 확인 대기를 원래 작업에 되돌린다
        if previous is not None and old is not None:
            for new_index, (old_index, item) in enumerate(released):
                previous.update(old_index, state="awaiting", ask="review", message=item["message"])
                if new_index in clocks:
                    old.put_clock(old_index, clocks[new_index])
        raise


@route("GET", "/api/categories")
def _categories(req: Request) -> dict:
    return {"categories": refstore.list_categories()}


@route("POST", "/api/add")
def _add(req: Request) -> dict:
    """링크를 넣는다. 도는 collect 작업이 있으면 그 줄 끝에 붙이고(같은 영상은 빼고), 없으면 새 작업을 시작한다."""
    body = req.json
    role = body.get("role", "ref")
    if role not in ROLES:
        raise ScriptError("bad_request", f"role은 {'/'.join(ROLES)} 중 하나여야 합니다")
    category = _category(body)
    links = parse_links(body.get("links") or [])
    if not links:
        raise ScriptError("no_links", "분석할 유튜브 링크를 넣어 주세요")
    opts = AddOptions(category=category, role=role, shots=_flag(body, "shots"), force=_flag(body, "force"),
                      refetch=_flag(body, "refetch"), no_transcript=_no_transcript(body))
    llm.check()              # claude가 없으면 영상을 받기 전에 400으로 알린다
    items = [dict(new_item(link, url=link), opts=opts.to_dict()) for link in links]
    current = req.app.jobs.current(KIND)
    if current is not None and current.is_active():
        if _collector(current) is None:
            raise ScriptError("job_running", SINGLE_KINDS[KIND])   # 샷 분류 보강 작업에는 붙이지 않는다
        req.app.jobs.check_exclusive(KIND)   # 확인만 기다리던 줄에 일을 붙이는 것도 촬영 영상 처리와 겹친다
        indexes = current.append_queued(items, "collect", _same_video)
        if indexes is not None:
            snapshot = current.snapshot()
            return {"job": snapshot, "appended": [snapshot["items"][i]["id"] for i in indexes]}
        if current.is_active():
            # 줄이 닫혔는데 아직 진행 중: 취소·중단 코드로 멈추는 중이다(곧 stopped가 된다)
            raise ScriptError("job_stopping", MSG_JOB_STOPPING)
        # 그사이 작업이 끝났다: 새 작업으로 시작한다
    job = _start_collect(req.app, items, category)
    return {"job": job.snapshot()}


@route("POST", "/api/add/check")
def _check(req: Request) -> dict:
    """링크를 받기 전에 줄마다 상태를 알려 준다(Claude·네트워크를 쓰지 않는다)."""
    body = req.json
    role = body.get("role", "ref")
    if role not in ROLES:
        raise ScriptError("bad_request", f"role은 {'/'.join(ROLES)} 중 하나여야 합니다")
    category = _category(body)
    return check_links(body.get("links") or [], category, role, _flag(body, "force"))


@route("GET", "/api/add/shots-pending")
def _shots_pending(req: Request) -> dict:
    category = _category({"category": req.query.get("category")})
    return {"count": len(shots.pending(category)["pending"])}


@route("POST", "/api/add/shots-only")
def _shots_only(req: Request) -> dict:
    category = _category(req.json)
    if req.app.jobs.latest_where(KIND, _has_leftover_reviews) is not None:
        # 샷 분류 보강도 collect 작업이라, 돌기 시작하면 멈춘 작업의 확인 대기 카드가 화면에서 가려진다
        raise ScriptError("reviews_pending", MSG_REVIEWS_PENDING)
    llm.check()
    pending = shots.pending(category)["pending"]
    items = [new_item(vid, step="shots") for vid in pending]
    job = req.app.jobs.start(KIND, run_shots_only(category), items=items, category=category)
    return {"job": job.snapshot()}


def _video_title(video_id: str, category: str) -> str | None:
    """카드에 보일 제목. 읽을 수 없으면 제목 없이 id만 보인다(작업에는 영향 없음)."""
    try:
        title = refstore.read_md(videomd.video_md_path(video_id, category))[0].get("title")
    except (ScriptError, OSError):
        return None
    return title if isinstance(title, str) and title else None


@route("GET", "/api/refs/describe-status")
def _describe_status(req: Request) -> dict:
    """GET /api/refs/describe-status?category= — 저장 프레임 수, 설명이 있는 수, 남은 영상."""
    return shots.desc_status(_category({"category": req.query.get("category")}))


@route("POST", "/api/refs/describe")
def _describe(req: Request) -> dict:
    """남은 레퍼런스 프레임에 한 줄 설명을 붙이는 작업(kind refs_describe)을 시작한다. collect·footage와는 동시에 돌지 않는다."""
    category = _category(req.json)
    llm.check()
    items = []
    for video_id in shots.desc_status(category)["videos_todo"]:
        title = _video_title(video_id, category)
        remaining = shots.describe_context(video_id, category, limit=0)["remaining"]
        items.append(dict(new_item(video_id, title=title, step=DESCRIBE_ROW),
                          total=remaining, done=0, eta_sec=None))
    if not items:
        raise ScriptError("nothing_to_do", MSG_NOTHING_TO_DESCRIBE)
    job = req.app.jobs.start(DESCRIBE_KIND, run_describe(category), items=items, category=category)
    return {"job": job.snapshot()}


@route("POST", r"/api/add/(?P<job_id>[^/]+)/review/(?P<item_id>.+)")
def _review(req: Request, job_id: str, item_id: str) -> dict:
    """확인 대기 영상 하나: save(바로 md를 쓴다) · skip · reanalyze(작업 줄 뒤에 붙는다)."""
    job = req.app.jobs.get(job_id)
    body = req.json
    action = body.get("action")
    if action not in REVIEW_ACTIONS:
        raise ScriptError("bad_action", f"할 수 없는 결정입니다: {action!r} (가능: {', '.join(sorted(REVIEW_ACTIONS))})")
    edited = body.get("analysis")
    if edited is not None and not isinstance(edited, dict):
        raise ScriptError("bad_request", "analysis는 JSON 객체여야 합니다")
    collector = _collector(job)
    if collector is None:
        raise ScriptError("not_awaiting", "이 항목은 확인을 기다리고 있지 않습니다")
    if action == "save":
        collector.save_review(job, item_id, edited)
    elif action == "skip":
        collector.skip_review(job, item_id)
    else:
        collector.reanalyze_review(job, item_id, str(body.get("feedback") or ""))
    collector.after_review(job)
    return {"job": job.snapshot()}


@route("GET", "/api/jobs/current")
def _current(req: Request) -> dict:
    """그 kind의 가장 최근 작업. collect는 끝난 작업이면 확인 대기가 남은 멈춘 작업을 먼저 돌려준다(그 카드를 잃지 않게)."""
    kind = req.query.get("kind") or KIND
    job = req.app.jobs.current(kind)
    if kind == KIND and job is not None and not job.is_active():
        job = req.app.jobs.latest_where(KIND, _has_leftover_reviews) or job
    return {"job": job.snapshot() if job else None}


@route("GET", r"/api/jobs/(?P<job_id>[^/]+)")
def _job(req: Request, job_id: str) -> dict:
    return {"job": req.app.jobs.get(job_id).snapshot()}


@route("POST", r"/api/jobs/(?P<job_id>[^/]+)/cancel")
def _cancel(req: Request, job_id: str) -> dict:
    """진행 중인 작업을 멈추라고 알린다(작업 스레드가 다음 확인 지점에서 멈춘다). 이미 끝난 작업이면 400."""
    job = req.app.jobs.get(job_id)
    if not job.is_active():
        raise ScriptError("not_running", "이미 끝난 작업입니다")
    job.cancel()
    return {"job": job.snapshot()}


@route("POST", r"/api/jobs/(?P<job_id>[^/]+)/items/(?P<item_id>.+)/decision")
def _decision(req: Request, job_id: str, item_id: str) -> dict:
    """작업 스레드가 기다리는 결정을 넣는다. collect는 확인 경로(`/api/add/<작업>/review/<항목>`)를 쓴다."""
    job = req.app.jobs.get(job_id)
    if job.kind == KIND:
        raise ScriptError("bad_action", MSG_USE_REVIEW)
    body = req.json
    decision: dict[str, Any] = {"action": body.get("action")}
    if body.get("feedback") is not None:
        decision["feedback"] = str(body["feedback"])
    if body.get("analysis") is not None:
        if not isinstance(body["analysis"], dict):
            raise ScriptError("bad_request", "analysis는 JSON 객체여야 합니다")
        decision["analysis"] = body["analysis"]
    job.decide(item_id, decision)
    return {"job": job.snapshot()}
