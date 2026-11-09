"""Rank the full liquid universe at a completed boundary with executable quotes."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from paperfrog.domain.types import Quote
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.settings import Settings
from paperfrog.storage.database import Store


def full_universe(s: Settings) -> bool:
    return s.simple_exit_policy == "fixed_roe" and s.simple_entry_profile == "momentum"


def pool_hash(pool: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(pool)).encode()).hexdigest()


def available(store: Store, symbol: str, now_ms: int, maximum: int) -> str | None:
    if symbol in store.get("simple_positions", {}):
        return "position already open"
    if store.get("cooldown:" + symbol, 0) > now_ms / 1000:
        return "symbol cooldown active"
    day = datetime.fromtimestamp(now_ms / 1000, UTC).date().isoformat()
    with store.engine.connect() as c:
        n: int = c.execute(
            text("SELECT count(*) FROM entries WHERE day=:d AND symbol=:s"), {"d": day, "s": symbol}
        ).scalar_one()
    if n >= maximum:
        return "per-coin daily entry limit reached"
    return None


def quality(
    s: Settings, quote: Quote, score: float, side: int, min_notional: float, now_ms: int
) -> tuple[float, str | None]:
    spread = float((quote.ask - quote.bid) / quote.mid)
    depth = (
        float((quote.ask_size if side > 0 else quote.bid_size) * quote.mid) * s.max_participation
    )
    if not 0 <= now_ms - quote.timestamp_ms <= s.market_stale_seconds * 1000:
        return 0.0, "execution quote stale or future dated"
    if spread > s.max_spread:
        return 0.0, "execution spread exceeds cap"
    if depth < min_notional:
        return 0.0, "visible executable depth below exchange minimum"
    costs = 2 * s.fee_rate + 2 * s.slippage_bps / 10000 + spread
    # Reward is the fixed 2.5% price target, before commissions and funding.
    result = score * max(0.0, 1 - costs / 0.025)
    if not math.isfinite(result) or result <= 0:
        return 0.0, "setup does not cover the execution cost assumption"
    return result, None


async def select_best(
    s: Settings,
    store: Store,
    exchange: BinanceAdapter,
    rows: dict[str, dict[str, Any]],
    boundary: int,
    clock: Callable[[], int],
) -> tuple[list[str], dict[str, Any]]:
    import asyncio

    from .data import quote_snapshot
    from .minute import veto
    from .momentum import classify, timing_veto
    from .strategy import execution_rules

    rules = execution_rules(s)
    pool = list(store.get("simple_pool", []))
    rejected: dict[str, str] = {}
    candidates = []
    for symbol in pool:
        row = rows.get(symbol)
        if row is None:
            rejected[symbol] = "completed candle or risk context unavailable"
            continue
        unavailable = available(store, symbol, clock(), s.max_entries_per_symbol_per_day)
        if unavailable:
            rejected[symbol] = unavailable
            continue
        if (
            symbol == "BTCUSDT"
            or (s.simple_carry_enabled and symbol == "ETHUSDT")
            or symbol in s.denylist
        ):
            rejected[symbol] = "symbol reserved or excluded"
            continue
        if float(row.get("daily_volume", 0)) < s.min_daily_quote_volume:
            rejected[symbol] = "trailing liquidity below minimum"
            continue
        _, side, score = classify(row, s.fee_rate + s.slippage_bps / 10000, rules)
        if not side:
            rejected[symbol] = (
                timing_veto(row, rules) or "no confirmed closed-candle momentum setup"
            )
            continue
        candidates.append((symbol, row, side, score))
    sem = asyncio.Semaphore(5)

    async def assess(candidate: tuple[str, dict[str, Any], int, float]) -> dict[str, Any] | None:
        symbol, row, side, _ = candidate
        try:
            async with sem:
                quote = await quote_snapshot(exchange, symbol)
            now = clock()
            problem = veto(row, side, now, float(quote.mark), rules)
            spread = float((quote.ask - quote.bid) / quote.mid)
            _, current_side, score = classify(
                row, s.fee_rate + s.slippage_bps / 10000 + spread / 2, rules
            )
            filters = exchange.filters[symbol]
            adjusted, quality_problem = quality(
                s, quote, score, side, float(filters.min_notional + filters.step * quote.mark), now
            )
            if problem or current_side != side or quality_problem:
                rejected[symbol] = (
                    problem or quality_problem or "current execution costs invalidate setup"
                )
                return None
            return {
                "symbol": symbol,
                "side": side,
                "score": adjusted,
                "momentum_score": score,
                "spread_bps": spread * 10000,
                "quote_ms": quote.timestamp_ms,
                "trend_age_bars": row["momentum_trend_age_bars"],
            }
        except Exception as exc:
            # Only classify unavailable public data; never persist transport URLs
            # or a library exception's possibly sensitive text.
            from paperfrog.domain.types import SafeError

            if not isinstance(
                exc, (SafeError, ValueError, KeyError, TypeError, OSError, TimeoutError)
            ):
                raise
            rejected[symbol] = "fresh executable quote or filters unavailable"
            return None

    assessed = await asyncio.gather(*(assess(c) for c in candidates))
    ranked = sorted(
        (r for r in assessed if r is not None), key=lambda r: (-r["score"], r["symbol"])
    )
    selected = [ranked[0]["symbol"]] if ranked else []
    return selected, {
        "asof_ms": boundary,
        "config_hash": s.config_hash,
        "pool_hash": pool_hash(pool),
        "pool_size": len(pool),
        "usable_size": len(rows),
        "quote_checked_count": len(candidates),
        "selected": selected,
        "ranked": ranked,
        "rejected": rejected,
        "policy": "all liquid pool members assessed; one best eligible setup after fresh quote and cost checks",
    }
