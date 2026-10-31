"""T11 자원·한도(서버 설계 §3.3·§5.2·§6) — 실제 PG 16 + MinIO. 환경이 없으면 skip.

- UsageMeter: 가짜 llm_usage 행으로 일 한도 거절(한국 시간 하루, 사용자별 한도 우선, 남의 행·어제 행 제외), record가 비용과 함께 행을 쓴다.
- 같은 영상 수집 잠금(PG advisory lock, bigint 한 개짜리 키 공간): 두 스레드가 같은 영상이면 차례로, 다른 영상이면 동시에.
- 슬롯: 시작 때 설치한 media·llm 세마포어가 동시 실행 수를 묶는다.
- 두 사용자가 같은 영상을 동시에 넣어도 한 번만 받고(공유 캐시), 어느 쪽도 job_running을 받지 않는다(ASGI 앱 + dev 로그인).
실제 Anthropic·YouTube 호출은 없다(가짜 usage·가짜 다운로드).
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts import ctx, limits, llm, refstore
from scripts.common import ScriptError
from scripts.ctx import Ctx
from tests.helpers.pg import pg_user

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 1, 15, 0, tzinfo=KST)
VID = "Q8PSsikq8PA"
CAT = "travel"


def _owner(owner_dsn: str, sql: str, *params: Any) -> None:
    import psycopg

    with psycopg.connect(owner_dsn) as conn:
        conn.execute(sql, params)


def _usage_row(owner_dsn: str, uid: uuid.UUID, at: datetime, cost: str) -> None:
    _owner(owner_dsn, "INSERT INTO llm_usage (user_id, at, model, input_tokens, output_tokens, cost_usd, ok) "
                      "VALUES (%s, %s, 'claude-sonnet-5-5', 1, 1, %s, true)", uid, at, Decimal(cost))


@pytest.fixture
def users(migrated_db: Any) -> tuple[str, uuid.UUID, uuid.UUID]:
    owner = migrated_db.owner_dsn
    return owner, pg_user(owner, "a@example.com"), pg_user(owner, "b@example.com")


def _meter(pg_db: Any, uid: uuid.UUID, daily: str = "1.00") -> Any:
    from server.usage import PgUsageMeter

    return PgUsageMeter(pg_db, uid, Decimal(daily), now=lambda: NOW)


# --- 일 한도 -----------------------------------------------------------------------------------

def test_check_refuses_with_the_design_message_when_today_reaches_the_limit(pg_db: Any, users: Any) -> None:
    owner, a, b = users
    _usage_row(owner, a, NOW.replace(hour=10), "0.60")
    _usage_row(owner, a, NOW.replace(hour=0, minute=0), "0.42")                 # today 00:00 KST counts
    _usage_row(owner, a, NOW.replace(hour=0, minute=0) - timedelta(seconds=1), "5")   # yesterday 23:59:59 KST
    _usage_row(owner, b, NOW.replace(hour=11), "9")                             # another user's spend
    with pytest.raises(ScriptError) as e:
        _meter(pg_db, a).check()
    assert e.value.code == "llm_limit"
    assert e.value.message == ("오늘 쓸 수 있는 AI 사용량(1.00달러)을 다 썼습니다. 내일 0시(한국 시간)에 다시 쓸 수 있습니다. "
                               "오늘 사용: 1.02달러")
    assert e.value.details == {"used": 1.02, "limit": 1.0, "resets_at": "2026-11-02T00:00:00+09:00"}


def test_check_passes_below_the_limit_and_refuses_at_exactly_the_limit(pg_db: Any, users: Any) -> None:
    owner, a, _ = users
    _usage_row(owner, a, NOW, "0.99")
    _meter(pg_db, a).check()
    _usage_row(owner, a, NOW, "0.01")
    with pytest.raises(ScriptError):
        _meter(pg_db, a).check()


def test_user_limit_overrides_the_server_default(pg_db: Any, users: Any) -> None:
    owner, a, b = users
    _usage_row(owner, a, NOW, "1.50")
    _usage_row(owner, b, NOW, "0.10")
    _owner(owner, "UPDATE users SET daily_usd_limit = 2.00 WHERE id = %s", a)
    _owner(owner, "UPDATE users SET daily_usd_limit = 0.10 WHERE id = %s", b)
    _meter(pg_db, a).check()                                    # 1.50 < 2.00 (env default 1.00 ignored)
    with pytest.raises(ScriptError) as e:
        _meter(pg_db, b).check()
    assert e.value.details["limit"] == 0.1


def test_record_writes_tokens_and_cost_and_check_then_sees_it(pg_db: Any, users: Any) -> None:
    import psycopg

    owner, a, _ = users
    meter = _meter(pg_db, a, "0.50")
    # opus 5.5: 100k in x $4 + 10k out x $20 + 50k cache read x $0.20 + 10k cache write x $5 = 0.66
    meter.record("claude-opus-5-5", {"input_tokens": 100_000, "output_tokens": 10_000,
                                     "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 10_000}, False)
    with psycopg.connect(owner) as conn:
        rows = conn.execute("SELECT user_id, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                            "cost_usd, ok, job_kind FROM llm_usage").fetchall()
    assert rows == [(a, "claude-opus-5-5", 100_000, 10_000, 50_000, 10_000, Decimal("0.660000"), False, None)]
    with pytest.raises(ScriptError) as e:
        _meter(pg_db, a, "0.50").check()
    assert e.value.details["used"] == 0.66


def test_backend_usage_is_the_pg_meter_with_the_configured_daily_limit(pg_db: Any, s3_bucket: Any, users: Any,
                                                                       tmp_path: Path) -> None:
    from server.backend import ServerBackend
    from server.usage import PgUsageMeter

    owner, a, _ = users
    backend = ServerBackend(pg_db, s3_bucket, scratch_dir=tmp_path / "t", state_dir=tmp_path / "s",
                            daily_usd=Decimal("0.25"))
    meter = Ctx(user_id=str(a), backend=backend).usage
    assert isinstance(meter, PgUsageMeter) and meter.daily_usd == Decimal("0.25")
    _usage_row(owner, a, datetime.now(KST), "0.30")
    with pytest.raises(ScriptError) as e:
        meter.check()
    assert e.value.code == "llm_limit"


# --- 같은 영상 수집 잠금 -------------------------------------------------------------------------

def test_collect_lock_serializes_the_same_video_and_reports_the_wait(pg_db: Any) -> None:
    order: list[str] = []
    waited: list[str] = []
    inside = threading.Event()
    release = threading.Event()

    def first() -> None:
        with pg_db.collect_lock(VID, None):
            order.append("a-in")
            inside.set()
            release.wait(5)
            order.append("a-out")

    def second() -> None:
        with pg_db.collect_lock(VID, lambda: waited.append("b")):
            order.append("b-in")

    t1 = threading.Thread(target=first)
    t1.start()
    assert inside.wait(5)
    t2 = threading.Thread(target=second)
    t2.start()
    time.sleep(0.3)
    assert order == ["a-in"] and waited == ["b"]                # B is blocked on A's lock and said so
    release.set()
    t1.join(5)
    t2.join(5)
    assert order == ["a-in", "a-out", "b-in"]


def test_collect_lock_different_videos_do_not_block_and_same_thread_reenters(pg_db: Any) -> None:
    with pg_db.collect_lock(VID, None):
        with pg_db.collect_lock(VID, lambda: pytest.fail("re-entry must not wait")):
            pass
        done = threading.Event()

        def other() -> None:
            with pg_db.collect_lock("AAAAAAAAAAA", lambda: pytest.fail("must not wait")):
                done.set()

        t = threading.Thread(target=other)
        t.start()
        assert done.wait(5)
        t.join(5)


def test_collect_lock_key_space_is_separate_from_object_locks(pg_db: Any) -> None:
    """T7의 객체 잠금은 두 int 형식(class, hashtext) — 수집 잠금은 bigint 한 개 형식이라 같은 숫자여도 겹치지 않는다."""
    import psycopg

    from server.db import collect_lock_key

    key = collect_lock_key(VID)
    hi, lo = (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF
    to_int4 = (lambda v: v - (1 << 32) if v >= (1 << 31) else v)
    with psycopg.connect(pg_db.app_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s, %s)", (to_int4(hi), to_int4(lo)))
        try:
            with pg_db.collect_lock(VID, lambda: pytest.fail("two-int lock must not block the bigint lock")):
                pass
        finally:
            conn.execute("SELECT pg_advisory_unlock_all()")


def test_collect_lock_gives_up_after_the_deadline_with_collect_busy(pg_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg_db, "collect_wait_max", 0.6)
    monkeypatch.setattr(pg_db, "collect_poll", 0.1)
    inside, release = threading.Event(), threading.Event()

    def holder() -> None:
        with pg_db.collect_lock(VID, None):
            inside.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert inside.wait(5)
        waited: list[int] = []
        started = time.monotonic()
        with pytest.raises(ScriptError) as e:
            with pg_db.collect_lock(VID, lambda: waited.append(1)):
                pytest.fail("must not get the lock")
        assert e.value.code == "collect_busy"
        assert e.value.message == "다른 사용자가 같은 영상을 오래 받고 있습니다. 잠시 뒤 다시 시도하세요."
        assert 0.5 <= time.monotonic() - started < 5 and waited == [1]
    finally:
        release.set()
        t.join(5)
    with pg_db.collect_lock(VID, lambda: pytest.fail("free again")):
        pass


def test_collect_lock_connects_with_timeout_and_keepalives(pg_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    from server import db as db_mod

    seen: dict[str, Any] = {}
    real = psycopg.connect

    def spy(*a: Any, **k: Any) -> Any:
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(db_mod.psycopg, "connect", spy)
    with pg_db.collect_lock(VID, None):
        pass
    assert seen["connect_timeout"] == 10 and seen["keepalives"] == 1 and seen["keepalives_idle"] == 60
    assert seen["autocommit"] is True


# --- 슬롯 ----------------------------------------------------------------------------------------

@pytest.fixture
def runtime(pg_db: Any) -> Iterator[Any]:
    from server.config import LlmSettings
    from server.jobs_registry import install_runtime, uninstall_runtime

    settings = LlmSettings.from_env({"YTC_MEDIA_SLOTS": "2", "YTC_LLM_SLOTS": "3", "YTC_LLM_MAX_TOKENS": "1234"})
    install_runtime(settings, pg_db)
    try:
        yield settings
    finally:
        uninstall_runtime()


def _peak(name: str, workers: int) -> int:
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def work() -> None:
        with limits.slot(name):                                 # type: ignore[arg-type]
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.15)
            with lock:
                state["now"] -= 1

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return state["peak"]


def test_installed_slots_bound_concurrency_and_pin_the_llm(runtime: Any) -> None:
    assert _peak("media", 6) == 2
    assert _peak("llm", 6) == 3
    assert llm.config() == {"engine": "anthropic-api", "model": runtime.model, "max_tokens": 1234}


def test_uninstall_restores_local_no_op(pg_db: Any) -> None:
    from server.config import LlmSettings
    from server.jobs_registry import install_runtime, uninstall_runtime

    install_runtime(LlmSettings.from_env({"YTC_MEDIA_SLOTS": "1"}), pg_db)
    uninstall_runtime()
    assert _peak("media", 3) == 3
    with limits.collecting(VID):
        pass


# --- 두 사용자가 같은 영상을 동시에 수집 -----------------------------------------------------------

@pytest.fixture
def fake_youtube(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str]]:
    """메타·자막·영상 받기를 가짜로(느리게) 바꾸고 횟수를 센다. 분석(LLM)은 실패로 끝내 작업을 닫는다."""
    from scripts import cuts, fetch
    from scripts.ui import api_add
    from tests.test_cuts import make_video

    sample = make_video(tmp_path_factory.mktemp("video") / "sample.mp4", seconds_per_scene=1)
    calls: dict[str, list[str]] = {"meta": [], "download": []}
    lock = threading.Lock()

    def get_metadata(video_id: str, proxy: str | None) -> dict[str, Any]:
        with lock:
            calls["meta"].append(video_id)
        time.sleep(0.5)                                         # long enough for the other user to arrive
        return {"id": video_id, "title": "제목", "channel": "채널", "duration": 3, "width": 320, "height": 240,
                "language": "ko"}

    def get_transcript(video_id: str, proxy: str | None, original: str | None = None) -> dict[str, Any]:
        return {"source": "cc", "language": "ko", "segments": [{"start": 0.0, "dur": 2.0, "text": "안녕하세요 반갑습니다"}]}

    def download_video(video_id: str, out_dir: Path, proxy: str | None, progress: Any = None) -> Path:
        with lock:
            calls["download"].append(video_id)
        time.sleep(0.5)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{video_id}.mp4"
        shutil.copy(sample, dest)
        return dest

    def no_llm(*a: Any, **k: Any) -> Any:
        raise ScriptError("fake_analysis_stop", "테스트: 분석은 하지 않는다")

    monkeypatch.setenv("YT_FETCH_INTERVAL", "0")
    monkeypatch.setattr(fetch, "get_metadata", get_metadata)
    monkeypatch.setattr(fetch, "get_transcript", get_transcript)
    monkeypatch.setattr(cuts, "download_video", download_video)
    monkeypatch.setattr(api_add.llm, "check", lambda: {"engine": "anthropic-api", "ready": True})
    monkeypatch.setattr(api_add.llm, "ask_validated", no_llm)
    return calls


def test_two_users_collecting_the_same_video_download_once_and_neither_gets_job_running(
        asgi_stack: Any, fake_youtube: dict[str, list[str]]) -> None:
    from server.config import LlmSettings
    from server.jobs_registry import install_runtime, uninstall_runtime

    db = asgi_stack.backend.db
    waits: list[str] = []

    class _Watched:
        """install_runtime에 넘길 Db 대리: 실제 PG 잠금을 쓰되 누가 기다렸는지 기록한다."""

        @staticmethod
        def collect_lock(video_id: str, on_wait: Any) -> Any:
            def noted() -> None:
                waits.append(video_id)
                if on_wait is not None:
                    on_wait()
            return db.collect_lock(video_id, noted)

    install_runtime(LlmSettings.from_env({}), _Watched())  # type: ignore[arg-type]
    try:
        clients = {}
        for email in ("a@example.com", "b@example.com"):
            clients[email] = asgi_stack.login(email)
            assert clients[email].post("/api/init", json={}).status_code == 200
            with ctx.use(Ctx(user_id=str(asgi_stack.users[email]), backend=asgi_stack.backend)):
                refstore.init_category(CAT)

        replies: dict[str, Any] = {}

        def add(email: str) -> None:
            replies[email] = clients[email].post("/api/add", json={"category": CAT, "links": [f"https://youtu.be/{VID}"]})

        threads = [threading.Thread(target=add, args=(email,)) for email in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        for email, r in replies.items():
            assert r.status_code == 200, (email, r.text)        # no job_running across users
        jobs = {email: asgi_stack.registry.for_user(asgi_stack.users[email]).get(r.json()["job"]["id"])
                for email, r in replies.items()}
        deadline = time.monotonic() + 60
        while any(j.is_active() for j in jobs.values()) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not any(j.is_active() for j in jobs.values())
        assert fake_youtube["meta"] == [VID]                    # raw fetched once, the other used the shared cache
        assert fake_youtube["download"] == [VID]                # video downloaded once
        assert VID in waits                                     # really concurrent: one waited on the other's lock
        for email, job in jobs.items():
            item = job.snapshot()["items"][0]
            assert item["message"] == "테스트: 분석은 하지 않는다", (email, item)   # both reached analysis
        with ctx.use(Ctx(user_id=str(asgi_stack.users["b@example.com"]), backend=asgi_stack.backend)):
            from scripts import keys
            assert ctx.here().shared.exists(keys.cuts(VID))
    finally:
        uninstall_runtime()
