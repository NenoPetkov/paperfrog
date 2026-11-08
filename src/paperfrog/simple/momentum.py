"""Deterministic per-coin A/B/C momentum; no cross-coin direction dependency."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from paperfrog.domain.types import EntryVeto, Order, Quote
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .minute import direction
from .strategy import BAR_MS, Rules, entry

VERSION = "abc-momentum-v1"
PROFILES = {"momentum", "momentum_selective", "momentum_breakout", "momentum_pullback"}


def classify(row: dict[str, Any], cost: float, rules: Rules) -> tuple[str, int, float]:
    """A = persistent uptrend, C = persistent downtrend, B = wait.

    Rank by the absolute one-hour move divided by 15m ATR, multiplied by
    one-hour path efficiency. Liquidity is an eligibility filter, not momentum.
    Inputs are completed candles only. Missing/invalid observations fail closed.
    """
    keys = ("return_1h", "path_efficiency_1h", "atr_fraction", "previous_trend_atr")
    try:
        if not all(math.isfinite(float(row[k])) for k in keys):
            return "B", 0, 0.0
        if not math.isfinite(cost) or cost < 0 or row["atr_fraction"] <= 0:
            return "B", 0, 0.0
        if not 0.3 <= row["path_efficiency_1h"] <= 1:
            return "B", 0, 0.0
        side, _ = entry(row, cost, rules=replace(rules, entry_style="trend_follow"))
        if row.get("entry_timeframe") == "5m":
            if direction(row, rules)[0] != side:
                return "B", 0, 0.0
        elif row.get("entry_timeframe") != "15m":
            return "B", 0, 0.0
        if (
            not side
            or side * row["return_1h"] <= 0
            or side * row["previous_trend_atr"] < rules.minimum_trend_atr
        ):
            return "B", 0, 0.0
        setup = rules.version.removeprefix("abc-").removesuffix("-v1")
        if setup in {"momentum_selective", "momentum_breakout", "momentum_pullback"}:
            if int(row["day_context_ms"]) != int(row["timestamp_ms"]) // BAR_MS * BAR_MS:
                return "B", 0, 0.0
            fields = (
                "day_return_4h",
                "day_return_1h",
                "day_slope_4h",
                "day_mean_distance_4h",
                "previous_close",
                "previous_fast",
                "previous_flow",
                "minute_atr",
                "volume_ratio",
            )
            if not all(math.isfinite(float(row[k])) for k in fields):
                return "B", 0, 0.0
            if (
                any(side * row[k] <= 0 for k in fields[:4])
                or row["minute_atr"] <= 0
                or side * (row["close"] - row["fast"]) > 2 * row["minute_atr"]
            ):
                return "B", 0, 0.0
            if setup == "momentum_selective" and (
                row["path_efficiency_1h"] < 0.6
                or side * row["previous_flow"] < 0.05
                or abs(row["trend_atr"]) < 1
                or row["volume_ratio"] < 1
            ):
                return "B", 0, 0.0
            if setup == "momentum_breakout" and (
                row["path_efficiency_1h"] < 0.5
                or row["volume_ratio"] < 1.5
                or side * (row["close"] - row["prior_high" if side > 0 else "prior_low"]) <= 0
            ):
                return "B", 0, 0.0
            if setup == "momentum_pullback" and (
                side * (row["previous_close"] - row["previous_fast"]) > 0
                or side * (row["close"] - row["fast"]) <= 0
            ):
                return "B", 0, 0.0
        score = abs(row["return_1h"]) / row["atr_fraction"] * row["path_efficiency_1h"]
        return ("A" if side > 0 else "C"), side, float(score)
    except (KeyError, ValueError, TypeError, OverflowError):
        return "B", 0, 0.0


def reversal(position: dict[str, Any], row: dict[str, Any]) -> bool:
    """Two completed opposite EMA observations; stop protection never waits."""
    try:
        values = [float(row[k]) for k in ("trend_atr", "previous_trend_atr")]
        return all(math.isfinite(v) and position["side"] * v < 0 for v in values)
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def require_order(
    s: Settings, store: Store, order: Order, now_ms: int, quote: Quote | None
) -> None:
    """Recompute the exact live order's momentum permission at dispatch."""
    from . import rotation
    from .learning_replay import SYMBOLS
    from .minute import veto
    from .strategy import execution_rules

    if order.reduce_only or s.simple_entry_profile not in PROFILES:
        return
    if order.symbol not in SYMBOLS:
        raise EntryVeto("momentum symbol is outside the researched universe")
    permission = store.get("simple_minute_order:" + order.client_id, {})
    row = permission.get("row", {})
    state = rotation.selection(store)
    if (
        permission.get("order") != order.model_dump(mode="json")
        or permission.get("config_hash") != s.config_hash
        or row != store.get("simple_minute:" + order.symbol)
        or state["auto"]
        or state["active"] != "current"
        or quote is None
        or quote.symbol != order.symbol
        or not 0 <= now_ms - quote.timestamp_ms <= s.market_stale_seconds * 1000
    ):
        raise EntryVeto("momentum permission missing, changed or not bound to this order")
    rules = execution_rules(s)
    reason = veto(row, order.side, now_ms, float(quote.mark), rules)
    if reason:
        raise EntryVeto(reason)
    spread = float((quote.ask - quote.bid) / quote.mid)
    if (
        spread > s.max_spread
        or classify(row, s.fee_rate + s.slippage_bps / 10000 + spread / 2, rules)[1] != order.side
    ):
        raise EntryVeto("momentum setup or cost filter no longer permits this order")
    if (
        s.simple_entry_profile != "momentum"
        and not 0 <= now_ms - int(row.get("day_context_ms", 0)) < BAR_MS
    ):
        raise EntryVeto("coin trend context expired before momentum dispatch")
