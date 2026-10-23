"""레퍼런스 추가 API: `commands/yt-add.md` 2절 흐름을 백그라운드 작업(kind "collect")으로 돌린다.

영상은 하나씩 처리한다: fetch → cuts → videomd.context → Claude 분석 → 확인 대기 → 저장 → (샷 분류).
- force: fetch reanalyze=True, write force=True (cuts 캐시 유지)
- refetch: fetch force=True, cuts force=True, write force=True
- blocked/proxy_failed(수집·STT·컷 어디서든): 그 영상 failed, 남은 영상 skipped("차단으로 중단"), job stopped
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts import claude_cli, cuts, fetch, refstore, shots, stt, videomd
from scripts.common import ScriptError, workspace_dir
from scripts.ui.jobs import Job, new_item
from scripts.ui.server import Request, route

KIND = "collect"
ROLES = ("ref", "mine")
STOP_CODES = frozenset({"blocked", "proxy_failed"})
PREVIEW_ACTIONS = frozenset({"save", "save_all", "skip", "reanalyze"})
TRANSCRIPT_ACTIONS = frozenset({"stt", "screen", "skip"})

ANALYZE_TASK = "yt-analyze 규칙대로 입력 영상의 분석 JSON을 만드세요(5절 출력 형식)."
SCREEN_HINT = "\n자막이 없거나 말이 거의 없는 영상입니다. scenes의 frame 이미지를 Read로 보고 화면 위주로 분석하세요(0단계)."
SHOTS_TASK = (
    "yt-shots 규칙대로 frames의 이미지를 하나씩 Read로 보고 모든 프레임을 분류하세요. "
    '출력은 4절의 라벨 배열을 {"labels": [...]} 객체 하나로 감싸서 내세요.'
)
MSG_EXISTS = "이미 분석됨"
MSG_BLOCKED = "차단으로 중단"
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


def _summary(job: Job, stage_text: str | None) -> dict:
    states = [item["state"] for item in job.items]
    return {"saved": states.count("saved"), "skipped": states.count("skipped"),
            "failed": states.count("failed"), "stage_text": stage_text}


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
        _, applied = claude_cli.ask_validated(_shots_rules(), ctx, SHOTS_TASK, apply, tools=["Read"])
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
            except ScriptError as e:
                job.update(index, state="failed", message=e.message)
                if e.code in STOP_CODES:
                    job.skip_rest(index + 1, MSG_BLOCKED)
                    job.finish("stopped", _summary(job, self.stage_text), e.message)
                    return
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

        job.update(index, step="cuts")
        try:
            stats = cuts.cut_stats(video_id, force=opts.refetch)
            job.update(index, frames=list(stats.get("frames") or []))
        except ScriptError as e:
            if e.code in STOP_CODES:
                raise
            notes.append(f"컷 통계 없이 계속합니다: {e.message}")
        job.update(index, message=" / ".join(notes) or None)

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
        tools = ["Read"] if (ctx.get("sparse") or screen) else None
        task = ANALYZE_TASK + (SCREEN_HINT if tools else "")
        if feedback:
            task += f"\n사용자 의견: {feedback}"
        _, analysis = claude_cli.ask_validated(
            _analyze_rules(), ctx, task, lambda d: videomd.validate(d, duration, cut_times), tools=tools,
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
                job.update(index, state="running", step="analyze")
                feedback = str(decision.get("feedback") or "").strip() or None
                preview = self._analyze(video_id, screen, feedback)
                continue
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
                else:
                    job.update(index, state="failed", message=e.message)
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
    items = [new_item(link, url=link) for link in links]
    job = req.app.jobs.start(KIND, Collector(opts), items=items, category=category)
    return {"job": job.snapshot()}


@route("POST", "/api/add/shots-only")
def _shots_only(req: Request) -> dict:
    category = _category(req.json)
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


@route("POST", r"/api/jobs/(?P<job_id>[^/]+)/items/(?P<item_id>.+)/decision")
def _decision(req: Request, job_id: str, item_id: str) -> dict:
    job = req.app.jobs.get(job_id)
    body = req.json
    action = body.get("action")
    item = job.find(item_id)
    allowed = TRANSCRIPT_ACTIONS if item.get("ask") == "no_transcript" else PREVIEW_ACTIONS
    if action not in allowed:
        raise ScriptError("bad_action", f"이 항목에서 할 수 없는 결정입니다: {action!r} (가능: {', '.join(sorted(allowed))})")
    decision: dict[str, Any] = {"action": action}
    if action == "reanalyze":
        decision["feedback"] = str(body.get("feedback") or "")
    if action in {"save", "save_all"} and body.get("analysis") is not None:
        if not isinstance(body["analysis"], dict):
            raise ScriptError("bad_request", "analysis는 JSON 객체여야 합니다")
        decision["analysis"] = body["analysis"]
    job.decide(item_id, decision)
    return {"job": job.snapshot()}
