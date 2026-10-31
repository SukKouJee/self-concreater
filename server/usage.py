"""LLM 사용량과 일 한도(서버 설계 §6): `PgUsageMeter`가 `scripts.stores.UsageMeter`를 구현한다.

- 가격표 `PRICES`: 모델 id → 백만 토큰당 USD(입력·출력·캐시 읽기·캐시 쓰기). **2026-09-25 기준 Anthropic 공식 가격**
  (claude-api 스킬의 모델 표·prompt-caching 문서). 캐시 쓰기는 5분 TTL 단가(입력의 1.25배) — `llm_anthropic`은
  `cache_control`을 쓰지 않아 자동 5분 캐시만 생긴다. 캐시 읽기는 모델마다 다르다(Opus 5.5 0.05배, Fable 5.1 0.025배, 그 밖 0.1배).
  가격이 바뀌면 이 표를 고친다. 표에 없는 모델은 시작을 거부한다(`server.config.LlmSettings`).
- `check()`: 오늘(Asia/Seoul 0시~다음 0시, 고정 +09:00) `sum(cost_usd) >= coalesce(users.daily_usd_limit, YTC_DAILY_USD)`이면
  `ScriptError("llm_limit", …)`. 검사 자체가 DB 오류로 실패하면 그 오류를 그대로 올린다(비용을 모르는 호출은 하지 않는다).
- `record()`: `llm_usage`에 토큰·비용(numeric(10,6), 반올림)과 시각(검사와 같은 시계)을 쓴다. **최선만**: DB 오류나 가격 없음은 로그(`ytc.usage`)로
  남기고 올리지 않는다 — 기록 실패가 LLM 결과나 원래 오류를 가리지 않는다(T4 m-1). 그 호출의 비용은 한도에 안 잡힌다.
- 초과 한계(설계 §6): `llm.ask`는 llm 슬롯을 잡은 뒤에 검사하므로, 검사를 통과하고 아직 기록하지 않은 호출은 프로세스 전체에서
  `YTC_LLM_SLOTS`개 이하다. 한 사용자의 하루 사용은 한도 + (그 사용자의 동시 호출 수, 최대 `YTC_LLM_SLOTS`) × 호출 하나의 최대 비용까지 갈 수 있다.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final

import psycopg
import psycopg_pool

from scripts.common import ScriptError

if TYPE_CHECKING:
    from server.db import Db

log = logging.getLogger("ytc.usage")

KST: Final = timezone(timedelta(hours=9), "KST")       # Asia/Seoul has no DST; no tzdata needed
MILLION: Final = Decimal(1_000_000)
COST_STEP: Final = Decimal("0.000001")                 # llm_usage.cost_usd numeric(10,6)
CENT: Final = Decimal("0.01")
PRICES_AS_OF: Final = "2026-09-25"
CACHE_WRITE_5M: Final = Decimal("1.25")


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


def _p(inp: str, out: str, read: str) -> Price:
    base = Decimal(inp)
    return Price(base, Decimal(out), Decimal(read), base * CACHE_WRITE_5M)


PRICES: Final[Mapping[str, Price]] = {
    "claude-fable-5-1": _p("10", "50", "0.25"),
    "claude-fable-5": _p("10", "50", "1.00"),
    "claude-opus-5-5": _p("4", "20", "0.20"),
    "claude-opus-5": _p("5", "25", "0.50"),
    "claude-opus-4-8": _p("5", "25", "0.50"),
    "claude-opus-4-7": _p("5", "25", "0.50"),
    "claude-opus-4-6": _p("5", "25", "0.50"),
    "claude-sonnet-5-5": _p("2", "10", "0.20"),
    "claude-sonnet-5": _p("2", "10", "0.20"),
    "claude-sonnet-4-6": _p("3", "15", "0.30"),
    "claude-haiku-4-5": _p("1", "5", "0.10"),
}


def price(model: str) -> Price:
    found = PRICES.get(model)
    if found is None:
        raise ScriptError("llm_price_unknown", f"가격표에 없는 모델입니다: {model!r} (server/usage.py PRICES)")
    return found


def _tokens(usage: Mapping[str, int], field: str) -> int:
    value = usage.get(field, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def cost_usd(model: str, usage: Mapping[str, int]) -> Decimal:
    """`llm_anthropic`의 usage dict(input/output/cache_read_input/cache_creation_input_tokens) → USD, 소수 6자리."""
    p = price(model)
    total = (_tokens(usage, "input_tokens") * p.input
             + _tokens(usage, "output_tokens") * p.output
             + _tokens(usage, "cache_read_input_tokens") * p.cache_read
             + _tokens(usage, "cache_creation_input_tokens") * p.cache_write)
    return (total / MILLION).quantize(COST_STEP, rounding=ROUND_HALF_UP)


def kst_day(now: datetime) -> tuple[datetime, datetime]:
    """now가 속한 한국 시간 하루의 [0시, 다음 0시)."""
    local = now.astimezone(KST)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def limit_error(used: Decimal, limit: Decimal, resets_at: datetime) -> ScriptError:
    u, lim = _cents(used), _cents(limit)
    return ScriptError(
        "llm_limit",
        f"오늘 쓸 수 있는 AI 사용량({lim}달러)을 다 썼습니다. 내일 0시(한국 시간)에 다시 쓸 수 있습니다. 오늘 사용: {u}달러",
        used=float(u), limit=float(lim), resets_at=resets_at.isoformat(),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PgUsageMeter:
    """한 사용자의 사용량 계량. 저장은 `Db.user_tx(uid)`(RLS: 그 사용자 행만)."""

    def __init__(self, db: Db, user_id: uuid.UUID | str, daily_usd: Decimal,
                 *, now: Callable[[], datetime] = _utc_now) -> None:
        self.db = db
        self.user_id = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
        self.daily_usd = daily_usd
        self._now = now

    def check(self, job_kind: str | None = None) -> None:
        start, end = kst_day(self._now())
        with self.db.user_tx(self.user_id) as conn:
            row = conn.execute(
                "SELECT u.daily_usd_limit, "
                "(SELECT coalesce(sum(l.cost_usd), 0) FROM llm_usage l WHERE l.user_id = u.id AND l.at >= %s AND l.at < %s) "
                "FROM users u WHERE u.id = %s", (start, end, self.user_id)).fetchone()
        if row is None:
            raise ScriptError("login_required", "로그인이 필요합니다")
        limit = Decimal(row[0]) if row[0] is not None else self.daily_usd
        used = Decimal(row[1])
        if used >= limit:
            raise limit_error(used, limit, end)

    def record(self, model: str, usage: Mapping[str, int], ok: bool) -> None:
        try:
            cost = cost_usd(model, usage)
            with self.db.user_tx(self.user_id) as conn:
                conn.execute(
                    "INSERT INTO llm_usage (user_id, at, job_kind, model, input_tokens, output_tokens, cache_read_tokens, "
                    "cache_write_tokens, cost_usd, ok) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s)",
                    (self.user_id, self._now(), model, _tokens(usage, "input_tokens"), _tokens(usage, "output_tokens"),
                     _tokens(usage, "cache_read_input_tokens"), _tokens(usage, "cache_creation_input_tokens"),
                     cost, ok))
        except (psycopg.Error, psycopg_pool.PoolTimeout, OSError, ScriptError) as e:
            # best effort: the LLM result (or the original error) must win; the spend is lost from the daily sum
            log.error("llm_usage record failed (user %s, model %s, ok=%s, usage=%s): %s: %s",
                      self.user_id, model, ok, dict(usage), type(e).__name__, e)
