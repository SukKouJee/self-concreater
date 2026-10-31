"""server.usage(서버 설계 §6) 단위 테스트: 가격표·비용 계산·한국 시간 하루·한도 오류 문구·LLM 설정·기록 실패. DB 없이 돈다.

가격은 2026-09-25 기준 Anthropic 공식 가격(USD / 백만 토큰). 캐시 쓰기는 5분 TTL(입력의 1.25배) — 엔진이 1시간 TTL을 쓰지 않는다.
실제 Anthropic 호출은 없다(가짜 usage dict).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

pytest.importorskip("psycopg")

from scripts import llm_anthropic  # noqa: E402
from scripts.common import ScriptError  # noqa: E402
from server import usage  # noqa: E402
from server.config import LlmSettings  # noqa: E402

KST = timezone(timedelta(hours=9))


# --- 가격표·비용 ---------------------------------------------------------------------------

def test_cost_of_plain_input_and_output_tokens() -> None:
    # sonnet 5.5: $2 in / $10 out per MTok
    cost = usage.cost_usd("claude-sonnet-5-5", {"input_tokens": 1_000_000, "output_tokens": 100_000})
    assert cost == Decimal("3.000000")


def test_cost_includes_cache_read_and_cache_write_tokens() -> None:
    # opus 5.5: $4 in, $20 out, cache read $0.20, cache write (5m) = 1.25 x $4 = $5
    u = {"input_tokens": 10_000, "output_tokens": 2_000, "cache_read_input_tokens": 50_000,
         "cache_creation_input_tokens": 20_000}
    expected = (Decimal(10_000) * 4 + Decimal(2_000) * 20 + Decimal(50_000) * Decimal("0.20")
                + Decimal(20_000) * 5) / Decimal(1_000_000)
    assert usage.cost_usd("claude-opus-5-5", u) == expected.quantize(Decimal("0.000001"))
    assert usage.cost_usd("claude-opus-5-5", u) == Decimal("0.190000")


def test_cost_rounds_to_numeric_10_6_and_tolerates_missing_fields() -> None:
    assert usage.cost_usd("claude-haiku-4-5", {"input_tokens": 1}) == Decimal("0.000001")   # $1/MTok
    assert usage.cost_usd("claude-haiku-4-5", {}) == Decimal("0.000000")


@pytest.mark.parametrize("model, inp, out, read, write", [
    ("claude-opus-5-5", "4", "20", "0.20", "5"),
    ("claude-sonnet-5-5", "2", "10", "0.20", "2.5"),
    ("claude-haiku-4-5", "1", "5", "0.10", "1.25"),
    ("claude-fable-5-1", "10", "50", "0.25", "12.5"),
    ("claude-opus-5", "5", "25", "0.50", "6.25"),
])
def test_price_table_matches_official_prices(model: str, inp: str, out: str, read: str, write: str) -> None:
    p = usage.price(model)
    assert (p.input, p.output, p.cache_read, p.cache_write) == (Decimal(inp), Decimal(out), Decimal(read), Decimal(write))


def test_engine_default_model_is_in_the_price_table() -> None:
    assert llm_anthropic.DEFAULT_MODEL in usage.PRICES


def test_unknown_model_has_no_price() -> None:
    with pytest.raises(ScriptError) as e:
        usage.cost_usd("claude-unknown-9", {"input_tokens": 1})
    assert e.value.code == "llm_price_unknown"


# --- 한국 시간 하루와 오류 문구 ----------------------------------------------------------------

def test_kst_day_bounds() -> None:
    now = datetime(2026, 11, 1, 16, 30, tzinfo=timezone.utc)         # = 2026-11-02 01:30 KST
    start, end = usage.kst_day(now)
    assert start.isoformat() == "2026-11-02T00:00:00+09:00"
    assert end.isoformat() == "2026-11-03T00:00:00+09:00"
    start, end = usage.kst_day(datetime(2026, 11, 1, 14, 59, tzinfo=timezone.utc))   # 23:59 KST
    assert start.isoformat() == "2026-11-01T00:00:00+09:00" and end.isoformat() == "2026-11-02T00:00:00+09:00"


def test_limit_error_is_the_design_text_and_fields() -> None:
    err = usage.limit_error(Decimal("1.02"), Decimal("1.00"), datetime(2026, 11, 2, tzinfo=KST))
    assert err.code == "llm_limit"
    assert err.message == ("오늘 쓸 수 있는 AI 사용량(1.00달러)을 다 썼습니다. 내일 0시(한국 시간)에 다시 쓸 수 있습니다. "
                           "오늘 사용: 1.02달러")
    assert err.details == {"used": 1.02, "limit": 1.0, "resets_at": "2026-11-02T00:00:00+09:00"}


# --- LLM 설정(env) -----------------------------------------------------------------------------

def test_llm_settings_defaults() -> None:
    s = LlmSettings.from_env({})
    assert s.engine == "anthropic-api"
    assert s.model == llm_anthropic.DEFAULT_MODEL
    assert s.max_tokens == 16000
    assert s.daily_usd == Decimal("1.00")
    assert (s.media_slots, s.llm_slots) == (2, 4)


def test_llm_settings_from_env() -> None:
    s = LlmSettings.from_env({"YTC_LLM_ENGINE": "anthropic-api", "YTC_LLM_MODEL": "claude-opus-5-5",
                              "YTC_LLM_MAX_TOKENS": "4000", "YTC_DAILY_USD": "2.5", "YTC_MEDIA_SLOTS": "1",
                              "YTC_LLM_SLOTS": "8"})
    assert (s.model, s.max_tokens, s.daily_usd, s.media_slots, s.llm_slots) == \
        ("claude-opus-5-5", 4000, Decimal("2.5"), 1, 8)


def test_model_not_in_price_table_refuses_start() -> None:
    with pytest.raises(ScriptError) as e:
        LlmSettings.from_env({"YTC_LLM_MODEL": "claude-made-up-1"})
    assert e.value.code == "config_invalid" and "가격표" in e.value.message


@pytest.mark.parametrize("env", [
    {"YTC_LLM_ENGINE": "claude-cli"},
    {"YTC_LLM_MAX_TOKENS": "0"},
    {"YTC_LLM_MAX_TOKENS": "lots"},
    {"YTC_DAILY_USD": "-1"},
    {"YTC_DAILY_USD": "abc"},
    {"YTC_DAILY_USD": "NaN"},
    {"YTC_DAILY_USD": "1e9"},
    {"YTC_MEDIA_SLOTS": "0"},
    {"YTC_LLM_SLOTS": "0"},
])
def test_bad_llm_settings_are_config_invalid(env: dict[str, str]) -> None:
    with pytest.raises(ScriptError) as e:
        LlmSettings.from_env(env)
    assert e.value.code == "config_invalid"


def test_server_settings_include_llm_and_refuse_unknown_model() -> None:
    from server.config import ServerSettings

    env = {"YTC_DB_DSN": "host=x", "YTC_S3_ENDPOINT": "http://m", "YTC_S3_ACCESS_KEY": "a", "YTC_S3_SECRET_KEY": "b",
           "YTC_PUBLIC_URL": "https://ytc.example.com", "YTC_SESSION_SECRET": "s" * 40}
    assert ServerSettings.from_env(env).llm.model == llm_anthropic.DEFAULT_MODEL
    with pytest.raises(ScriptError) as e:
        ServerSettings.from_env({**env, "YTC_LLM_MODEL": "gpt-x"})
    assert e.value.code == "config_invalid"


# --- 기록은 최선만(DB 실패가 결과·원래 오류를 가리지 않는다) -----------------------------------------

class _BrokenDb:
    """user_tx에 들어가면 DB 오류를 낸다(연결 끊김·풀 시간 초과 흉내)."""

    def __init__(self) -> None:
        self.calls = 0

    @contextmanager
    def user_tx(self, user_id: Any) -> Iterator[Any]:
        import psycopg

        self.calls += 1
        raise psycopg.OperationalError("connection lost")
        yield  # pragma: no cover


UID = "00000000-0000-4000-8000-000000000001"


def test_record_db_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    db = _BrokenDb()
    meter = usage.PgUsageMeter(db, UID, Decimal("1.00"))  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="ytc.usage"):
        meter.record("claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": 5}, True)
    assert db.calls == 1
    assert any("llm_usage" in r.getMessage() for r in caplog.records)


def test_record_unknown_model_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    meter = usage.PgUsageMeter(_BrokenDb(), UID, Decimal("1.00"))  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="ytc.usage"):
        meter.record("claude-unknown-9", {"input_tokens": 10}, False)
    assert caplog.records


def test_check_db_failure_is_not_swallowed() -> None:
    """검사가 실패하면 호출을 막는다(비용 모르는 호출 금지): 오류가 그대로 올라간다."""
    import psycopg

    meter = usage.PgUsageMeter(_BrokenDb(), UID, Decimal("1.00"))  # type: ignore[arg-type]
    with pytest.raises(psycopg.OperationalError):
        meter.check()


def _fake_anthropic(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
    import json

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(body).encode("utf-8")

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-TEST-NOT-A-REAL-KEY")
    monkeypatch.setattr(llm_anthropic.urllib.request, "urlopen", lambda *a, **k: _Resp())


class _OkCheckBrokenRecord(usage.PgUsageMeter):
    def check(self, job_kind: str | None = None) -> None:
        return None


@pytest.fixture
def pinned() -> Iterator[None]:
    from scripts import llm

    llm.pin(engine="anthropic-api", model="claude-sonnet-5-5", max_tokens=1000)
    yield
    llm.unpin()


def _meter_ctx(tmp_path: Any, meter: Any) -> Any:
    from scripts.ctx import Ctx

    c = Ctx(root=tmp_path, user_id=UID)
    c.__dict__["usage"] = meter
    return c


def test_llm_result_survives_a_record_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, pinned: None) -> None:
    from scripts import ctx, llm

    _fake_anthropic(monkeypatch, {"content": [{"type": "tool_use", "name": "respond", "input": {"a": 1}}],
                                  "usage": {"input_tokens": 3, "output_tokens": 4}})
    meter = _OkCheckBrokenRecord(_BrokenDb(), UID, Decimal("1.00"))  # type: ignore[arg-type]
    with ctx.use(_meter_ctx(tmp_path, meter)):
        assert llm.ask([], {}, "t", schema={"type": "object"}) == {"a": 1}


def test_original_llm_error_survives_a_record_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
                                                      pinned: None) -> None:
    from scripts import ctx, llm

    _fake_anthropic(monkeypatch, {"content": [], "stop_reason": "max_tokens",
                                  "usage": {"input_tokens": 3, "output_tokens": 1000}})
    meter = _OkCheckBrokenRecord(_BrokenDb(), UID, Decimal("1.00"))  # type: ignore[arg-type]
    with ctx.use(_meter_ctx(tmp_path, meter)), pytest.raises(ScriptError) as e:
        llm.ask([], {}, "t", schema={"type": "object"})
    assert e.value.code == "llm_failed"
