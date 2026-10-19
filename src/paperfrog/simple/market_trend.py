"""Independent closed-day trend and broad market vetoes for new entries only."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .strategy import BAR_MS

DAY_BARS = 96
MIN_MARKETS = 20
MIN_COVERAGE = 0.8
DAY_FIELDS = ("day_return_24h", "day_return_4h", "day_slope_24h", "day_mean_distance")


def finite(row: dict[str, Any], keys: tuple[str, ...]) -> bool:
    try:
        return all(math.isfinite(float(row[key])) for key in keys)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def regime(index: float, median: float, btc: float, eth: float) -> str:
    if index > 50 and min(median, btc, eth) > 0:
        return "BULL"
    if index < 50 and max(median, btc, eth) < 0:
        return "BEAR"
    return "MIXED"


def day_context(frame: pd.DataFrame, boundary: int) -> dict[str, float | int]:
    """Use 97 closed quarter-hour prices: exactly 24 hours, without future bars."""
    x = frame[frame.timestamp_ms <= boundary].tail(DAY_BARS + 1)
    if (
        boundary % BAR_MS
        or len(x) != DAY_BARS + 1
        or int(x.iloc[-1].timestamp_ms) != boundary
        or int(x.iloc[0].timestamp_ms) != boundary - DAY_BARS * BAR_MS
        or (x.timestamp_ms.diff().dropna() != BAR_MS).any()
    ):
        raise SafeError("complete consecutive closed 24-hour trend history required")
    prices = x.close.to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise SafeError("invalid 24-hour trend prices")
    times = np.arange(DAY_BARS + 1, dtype=float) - DAY_BARS / 2
    slope = float(np.dot(times, prices - prices.mean()) / np.dot(times, times))
    return {
        "day_context_ms": boundary,
        "day_return_24h": float(prices[-1] / prices[0] - 1),
        "day_return_4h": float(prices[-1] / prices[-17] - 1),
        "day_slope_24h": slope * DAY_BARS / float(prices.mean()),
        "day_mean_distance": float(prices[-1] / prices[1:].mean() - 1),
    }


def market_context(
    rows: dict[str, dict[str, Any]], pool: list[str], boundary: int
) -> dict[str, Any]:
    """Equal-weight breadth over the liquidity pool BEFORE directional selection.

    Bull index = percent of covered pool coins up over 24h. BULL requires an
    advancing majority, positive median return and positive BTC and ETH returns.
    BEAR is symmetric. Conflicting evidence is MIXED and permits neither side.
    """
    members = list(dict.fromkeys(pool))

    def valid(row: dict[str, Any]) -> bool:
        return row.get("day_context_ms") == boundary and finite(row, DAY_FIELDS)

    usable = [rows[symbol] for symbol in members if symbol in rows and valid(rows[symbol])]
    if (
        boundary % BAR_MS
        or len(usable) < MIN_MARKETS
        or len(usable) < MIN_COVERAGE * len(members)
        or any(symbol not in rows or not valid(rows[symbol]) for symbol in ("BTCUSDT", "ETHUSDT"))
    ):
        raise SafeError("broad market trend needs BTC, ETH and at least 80% of 20+ pool coins")
    returns = [float(row["day_return_24h"]) for row in usable]
    bull_index = 100 * sum(value > 0 for value in returns) / len(returns)
    median = float(np.median(returns))
    btc, eth = (float(rows[symbol]["day_return_24h"]) for symbol in ("BTCUSDT", "ETHUSDT"))
    return {
        "timestamp_ms": boundary,
        "regime": regime(bull_index, median, btc, eth),
        "bull_index": bull_index,
        "median_return_24h": median,
        "median_return_4h": float(np.median([row["day_return_4h"] for row in usable])),
        "btc_return_24h": btc,
        "eth_return_24h": eth,
        "covered_coins": len(usable),
        "pool_coins": len(members),
    }


def entry_gate(
    s: Settings, store: Store, row: dict[str, Any], side: int, now_ms: int
) -> str | None:
    if not s.simple_market_trend_enabled:
        return None
    if side not in (-1, 1):
        return "invalid day-trend entry direction"
    label = "LONG" if side > 0 else "SHORT"
    try:
        expected = int(row["timestamp_ms"]) // BAR_MS * BAR_MS
        context_at = int(row["day_context_ms"])
        complete = all(math.isfinite(float(row[key])) for key in DAY_FIELDS)
    except (KeyError, TypeError, ValueError, OverflowError):
        return "coin 24-hour trend missing or invalid; new entries blocked"
    if not complete or context_at != expected or not 0 <= now_ms - context_at < BAR_MS:
        return "coin 24-hour trend stale or future dated; new entries blocked"
    for key, description in (
        ("day_return_24h", "24-hour return"),
        ("day_slope_24h", "24-hour regression trend"),
        ("day_mean_distance", "price versus the 24-hour mean"),
        ("day_return_4h", "four-hour return"),
    ):
        if side * float(row[key]) <= 0:
            return f"coin {description} opposes {label} ({float(row[key]):+.2%})"
    market = store.get("simple_market_trend")
    if (
        not isinstance(market, dict)
        or market.get("config_hash") != s.config_hash
        or market.get("timestamp_ms") != context_at
        or not 0 <= now_ms - int(market.get("timestamp_ms", 0)) < BAR_MS
    ):
        return "broad market trend missing or stale; new entries blocked"
    fields = (
        "bull_index",
        "median_return_24h",
        "btc_return_24h",
        "eth_return_24h",
        "covered_coins",
        "pool_coins",
    )
    if not finite(market, fields):
        return "broad market trend incomplete; new entries blocked"
    if (
        not 0 <= float(market["bull_index"]) <= 100
        or not MIN_MARKETS <= market["covered_coins"] <= market["pool_coins"]
        or market["covered_coins"] < MIN_COVERAGE * market["pool_coins"]
        or market.get("regime") != regime(*(float(market[key]) for key in fields[:4]))
    ):
        return "broad market trend invalid; new entries blocked"
    if market.get("regime") != ("BULL" if side > 0 else "BEAR"):
        return f"market {market.get('regime', 'UNKNOWN')}; {label} requires aligned market breadth, BTC and ETH"
    return None


def require_order(s: Settings, store: Store, order: Order, now_ms: int) -> None:
    if order.reduce_only or not s.simple_market_trend_enabled:
        return
    record = store.get("simple_market_order:" + order.client_id, {})
    if (
        record.get("order") != order.model_dump(mode="json")
        or record.get("config_hash") != s.config_hash
    ):
        raise EntryVeto("day/market trend permission is not bound to this order")
    reason = entry_gate(s, store, record.get("row", {}), order.side, now_ms)
    if reason:
        raise EntryVeto(reason)
