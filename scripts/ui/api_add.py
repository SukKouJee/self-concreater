"""레퍼런스 추가 API: `commands/yt-add.md` 2절 흐름을 백그라운드 작업(kind "collect")으로 돌린다.

영상은 하나씩 처리한다: fetch → cuts → videomd.context → Claude 분석 → 확인 대기 → 저장 → (샷 분류).
- force: fetch reanalyze=True, write force=True (cuts 캐시 유지)
- refetch: fetch force=True, cuts force=True, write force=True
- blocked/proxy_failed(수집·STT·컷 어디서든): 그 영상 failed, 남은 영상 skipped("차단으로 중단"), job stopped
- claude_missing/claude_login: 같은 방식으로 멈춘다(남은 영상을 받지 않는다). 라우트는 작업 전에 claude를 확인한다.
- 예상치 못한 예외: 그 영상만 failed("예상치 못한 오류: ...")로 두고 다음 영상으로 간다.
- 분석 호출에는 examples md를 항상 첨부하고, 장면 프레임(최대 8장, 처음·끝 포함)은 sparse이거나 화면 분석을 고른 경우에만 첨부한다(yt-analyze 0단계).
  화면 위주 분석 안내(SCREEN_HINT)도 같은 경우에만 task에 붙인다.
- 단계 사이마다 `job.check_cancelled()`: 서버 종료 시 진행 중인 단계(다운로드 정리 try/finally 포함)를
  끝까지 마친 뒤 다음 단계로 가지 않고 멈춘다.
"""
from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts import claude_cli, cuts, fetch, llm, refstore, shots, stt, videomd
from scripts.common import ScriptError, work_root, workspace_dir
from scripts.ui.jobs import Job, JobCancelled, new_item
from scripts.ui.server import Request, route

KIND = "collect"
ROLES = ("ref", "mine")
BLOCK_CODES = frozenset({"blocked", "proxy_failed"})
CLAUDE_CODES = frozenset({"claude_missing", "claude_login", "llm_auth", "llm_not_configured"})
STOP_CODES = BLOCK_CODES | CLAUDE_CODES
PREVIEW_ACTIONS = frozenset({"save", "save_all", "skip", "reanalyze"})
TRANSCRIPT_ACTIONS = frozenset({"stt", "screen", "skip"})
SAVE_ACTIONS = frozenset({"save", "save_all"})
ALLOWED_ACTIONS: dict[str | None, frozenset[str]] = {None: PREVIEW_ACTIONS, "no_transcript": TRANSCRIPT_ACTIONS}

ANALYZE_FRAME_CAP = 8
ANALYZE_TASK = "yt-analyze 규칙대로 입력 영상의 분석 JSON을 만드세요(5절 출력 형식)."
SCREEN_HINT = "\n자막이 없거나 말이 거의 없는 영상입니다. scenes의 frame 이미지(첨부되었거나 Read로 열 수 있는 이미지)를 보고 화면 위주로 분석하세요(0단계)."
SHOTS_TASK = (
    "yt-shots 규칙대로 frames의 이미지(첨부되었거나 Read로 열 수 있는 이미지)를 하나씩 보고 모든 프레임을 분류하세요. "
    '출력은 4절의 라벨 배열을 {"labels": [...]} 객체 하나로 감싸서 내세요.'
)
MSG_EXISTS = "이미 분석됨"
MSG_BLOCKED = "차단으로 중단"
MSG_CLAUDE_STOP = "Claude(LLM 엔진)를 쓸 수 없어 중단"
MSG_SKIPPED = "건너뜀"
MSG_NO_FRAMES = "컷 통계가 없어 샷 분류를 건너뜁니다"


def _analyze_rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-analyze" / "SKILL.md"]


def _shots_rules() -> list[Path]:
    return [claude_cli.SKILLS_DIR / "yt-shots" / "SKILL.md"]


def parse_links(links: str | Iterable[str]) -> list[str]:
    """공백·줄바꿈으로 나누고 빈 값과 중복을 뺀다(처음 나온 순서 유지)."""
    chunks = [links] if isinstance(links, str) else list(links)
    seen: dict[str, None] = {}
    for chunk in chunks:
        if not isinstance(chunk, str):
            raise ScriptError("bad_request", "links는 문자열 목록이어야 합니다")
        for link in chunk.split():
            seen.setdefault(link, None)
    return list(seen)


@dataclass(frozen=True)
class AddOptions:
    category: str
    role: str = "ref"
    shots: bool = False
    force: bool = False
    refetch: bool = False


def _stop_message(code: str) -> str:
    return MSG_CLAUDE_STOP if code in CLAUDE_CODES else MSG_BLOCKED


def _summary(job: Job, stage_text: str | None) -> dict:
    states = [item["state"] for item in job.items]
    return {"saved": states.count("saved"), "skipped": states.count("skipped"),
            "failed": states.count("failed"), "stage_text": stage_text}


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


class Collector:
    """링크 목록을 하나씩 처리하는 작업 함수(JobManager.start의 target)."""

    def __init__(self, opts: AddOptions) -> None:
        self.opts = opts
        self.save_all = False
        self.stage_text: str | None = None

    def __call__(self, job: Job) -> None:
        for index in range(len(job.items)):
            job.check_cancelled()
            try:
                self._one(job, index)
            except JobCancelled:
                raise
            except ScriptError as e:
                job.update(index, state="failed", message=e.message)
                if e.code in STOP_CODES:
                    job.skip_rest(index + 1, _stop_message(e.code))
                    job.finish("stopped", _summary(job, self.stage_text), e.message)
                    return
            except Exception as e:  # noqa: BLE001 - 한 영상의 버그가 남은 영상까지 막지 않게 한다
                traceback.print_exc(file=sys.stderr)
                job.update(index, state="failed", message=f"예상치 못한 오류: {type(e).__name__}: {e}")
        job.finish("done", _summary(job, self.stage_text))

    # ---- 영상 하나 -------------------------------------------------------------------

    def _one(self, job: Job, index: int) -> None:
        opts = self.opts
        notes: list[str] = []
        job.update(index, state="running", step="fetch")
        link = job.item(index)["url"]
        screen = False
        try:
            info = fetch.fetch(link, opts.category, opts.role, force=opts.refetch, reanalyze=opts.force)
        except ScriptError as e:
            if e.code != "no_transcript":
                raise
            video_id = self._identify(job, index, e.details.get("id"), e.details.get("title"))
            choice = self._wait(job, index, ask="no_transcript", message=e.message)["action"]
            if choice == "skip":
                job.update(index, state="skipped", step="done", ask=None, message=MSG_SKIPPED)
                return
            if choice not in {"stt", "screen"}:
                raise ScriptError("bad_action", f"알 수 없는 결정입니다: {choice!r}")
            job.update(index, ask=None, message=None)
            if choice == "stt":
                screen = not self._transcribe(job, index, video_id, notes)
            else:
                screen = True
        else:
            video_id = self._identify(job, index, info.get("id"), info.get("title"))
            if info.get("status") == "exists":
                job.update(index, state="skipped", step="done", message=MSG_EXISTS)
                return

        job.check_cancelled()
        job.update(index, step="cuts")
        try:
            stats = cuts.cut_stats(video_id, force=opts.refetch)
            job.update(index, frames=list(stats.get("frames") or []))
        except ScriptError as e:
            if e.code in STOP_CODES:
                raise
            notes.append(f"컷 통계 없이 계속합니다: {e.message}")
        job.update(index, message=" / ".join(notes) or None)

        job.check_cancelled()
        self._analyze_and_save(job, index, video_id, screen)

    def _identify(self, job: Job, index: int, video_id: Any, title: Any) -> str:
        """링크로 시작한 항목에 영상 id·제목을 붙인다. 다른 항목이 같은 id를 쓰면 항목 id는 링크로 둔다."""
        if not video_id:
            raise ScriptError("fetch_failed", "영상 id를 알 수 없습니다")
        taken = any(item["id"] == video_id for i, item in enumerate(job.items) if i != index)
        fields: dict[str, Any] = {"title": title}
        if not taken:
            fields["id"] = str(video_id)
        job.update(index, **fields)
        return str(video_id)

    def _wait(self, job: Job, index: int, **fields: Any) -> dict:
        job.update(index, state="awaiting", **fields)
        return job.wait_decision(job.item(index)["id"])

    def _transcribe(self, job: Job, index: int, video_id: str, notes: list[str]) -> bool:
        """STT로 자막을 만든다. stt_unavailable이면 이유를 남기고 False(화면 분석으로)."""
        job.update(index, message="음성 인식 중")
        try:
            stt.transcribe(video_id)
        except ScriptError as e:
            if e.code != "stt_unavailable":
                raise
            notes.append(f"{e.message} — 화면만 보고 분석합니다")
            return False
        return True

    def _analyze(self, video_id: str, screen: bool, feedback: str | None) -> dict:
        ctx = videomd.context(video_id, self.opts.category)
        # duration·cut_times는 videomd.write와 같은 방식으로 구한다
        raw = fetch.load_raw(video_id)
        cut_data = videomd.load_cuts(video_id)
        duration = float(raw["meta"].get("duration") or (cut_data or {}).get("duration_sec") or 0)
        cut_times = cut_data["cuts"] if cut_data else []
        need_frames = bool(ctx.get("sparse") or screen)
        files = [
            *(_analysis_frames(cut_data) if need_frames else []),
            *[work_root() / e for e in ctx.get("examples") or []],
        ]
        task = ANALYZE_TASK + (SCREEN_HINT if need_frames else "")
        if feedback:
            task += f"\n사용자 의견: {feedback}"
        _, analysis = llm.ask_validated(
            _analyze_rules(), ctx, task, lambda d: videomd.validate(d, duration, cut_times), files=files,
        )
        return analysis

    def _analyze_and_save(self, job: Job, index: int, video_id: str, screen: bool) -> None:
        job.update(index, step="analyze")
        preview = self._analyze(video_id, screen, None)
        note = job.item(index)["message"]
        while True:
            if self.save_all:
                decision: dict = {"action": "save"}
                job.update(index, preview=preview)
            else:
                decision = self._wait(job, index, step="confirm", preview=preview, message=note)
            action = decision["action"]
            if action == "skip":
                job.update(index, state="skipped", step="done", message=MSG_SKIPPED)
                return
            if action == "reanalyze":
                job.check_cancelled()
                job.update(index, state="running", step="analyze")
                feedback = str(decision.get("feedback") or "").strip() or None
                preview = self._analyze(video_id, screen, feedback)
                continue
            if action not in SAVE_ACTIONS:
                # 저장 버튼을 누르지 않은 결정으로는 절대 쓰지 않는다
                raise ScriptError("bad_action", f"알 수 없는 결정입니다: {action!r}")
            if action == "save_all":
                self.save_all = True
            edited = decision.get("analysis")
            chosen = edited if isinstance(edited, dict) else preview
            job.update(index, state="running", step="confirm")
            try:
                result = self._write(video_id, chosen)
            except ScriptError as e:
                # 사용자가 고친 분석이 형식에 어긋나면 고친 내용을 그대로 다시 보여 주고 기다린다
                if e.code != "invalid_analysis" or not isinstance(edited, dict):
                    raise
                preview, note = chosen, e.message
                self.save_all = False
                continue
            break

        if result.get("status") == "exists":
            job.update(index, state="skipped", step="done", message="이미 있음")
            return
        self.stage_text = result.get("stage_text")
        if self.opts.shots:
            job.check_cancelled()
            job.update(index, step="shots")
            self._shots_after_save(job, index, video_id)
        job.update(index, state="saved", step="done")

    def _write(self, video_id: str, analysis: dict) -> dict:
        opts = self.opts
        path = workspace_dir("tmp", f"analysis_{video_id}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            return videomd.write(video_id, opts.category, opts.role, path, opts.force or opts.refetch)
        finally:
            path.unlink(missing_ok=True)

    def _shots_after_save(self, job: Job, index: int, video_id: str) -> None:
        """md는 이미 저장됐으므로 샷 분류 실패는 항목을 실패로 바꾸지 않고 이유만 남긴다."""
        try:
            _classify_shots(video_id, self.opts.category)
        except ScriptError as e:
            message = MSG_NO_FRAMES if e.code == "no_frames" else f"샷 분류 실패: {e.message}"
            prior = job.item(index)["message"]
            job.update(index, message=f"{prior} / {message}" if prior else message)


def run_shots_only(category: str) -> Any:
    def target(job: Job) -> None:
        for index, item in enumerate(job.items):
            job.check_cancelled()
            job.update(index, state="running", step="shots")
            try:
                _classify_shots(item["id"], category)
            except ScriptError as e:
                if e.code == "no_frames":
                    job.update(index, state="skipped", step="done", message=MSG_NO_FRAMES)
                    continue
                job.update(index, state="failed", message=e.message)
                if e.code in CLAUDE_CODES:
                    job.skip_rest(index + 1, MSG_CLAUDE_STOP)
                    job.finish("stopped", _summary(job, None), e.message)
                    return
                continue
            job.update(index, state="saved", step="done")
        job.finish("done", _summary(job, None))

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


@route("GET", "/api/categories")
def _categories(req: Request) -> dict:
    return {"categories": refstore.list_categories()}


@route("POST", "/api/add")
def _add(req: Request) -> dict:
    body = req.json
    role = body.get("role", "ref")
    if role not in ROLES:
        raise ScriptError("bad_request", f"role은 {'/'.join(ROLES)} 중 하나여야 합니다")
    category = _category(body)
    links = parse_links(body.get("links") or [])
    if not links:
        raise ScriptError("no_links", "분석할 유튜브 링크를 넣어 주세요")
    opts = AddOptions(category=category, role=role, shots=_flag(body, "shots"),
                      force=_flag(body, "force"), refetch=_flag(body, "refetch"))
    llm.check()              # claude가 없으면 영상을 받기 전에 400으로 알린다
    items = [new_item(link, url=link) for link in links]
    job = req.app.jobs.start(KIND, Collector(opts), items=items, category=category)
    return {"job": job.snapshot()}


@route("POST", "/api/add/shots-only")
def _shots_only(req: Request) -> dict:
    category = _category(req.json)
    llm.check()
    pending = shots.pending(category)["pending"]
    items = [new_item(vid, step="shots") for vid in pending]
    job = req.app.jobs.start(KIND, run_shots_only(category), items=items, category=category)
    return {"job": job.snapshot()}


@route("GET", "/api/jobs/current")
def _current(req: Request) -> dict:
    job = req.app.jobs.current(req.query.get("kind") or KIND)
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
    job = req.app.jobs.get(job_id)
    body = req.json
    action = body.get("action")
    decision: dict[str, Any] = {"action": action}
    if action == "reanalyze":
        decision["feedback"] = str(body.get("feedback") or "")
    if action in {"save", "save_all"} and body.get("analysis") is not None:
        if not isinstance(body["analysis"], dict):
            raise ScriptError("bad_request", "analysis는 JSON 객체여야 합니다")
        decision["analysis"] = body["analysis"]
    job.decide(item_id, decision, allowed=ALLOWED_ACTIONS)
    return {"job": job.snapshot()}
