"""Per-entry routing from closed observations and earlier realized outcomes.

A preference for opposite is never permission to invert an unconfirmed breakout.
"""
from __future__ import annotations

import math
from statistics import mean
from typing import Any

from paperfrog.storage.database import Store

from .strategy import BAR_MS

VERSION = 1
LOOKBACK_MS = 30 * 86_400_000


def features(row: dict[str, Any]) -> list[float] | None:
    """Direction-normalized setup, so long and short failures can be compared."""
    try:
        trend = float(row["trend_atr"])
        side = 1 if trend > 0 else -1
        atr = float(row["atr_fraction"])
        price = float(row["close"])
        bound = float(row["prior_high" if side > 0 else "prior_low"])
        vector = [abs(trend), side * float(row["previous_trend_atr"]),
                  side * float(row["flow"]), side * float(row["previous_flow"]),
                  side * float(row["day_return_1h"]) / atr,
                  side * float(row["day_return_4h"]) / atr,
                  side * (price - bound) / (price * atr)]
        return vector if atr > 0 and price > 0 and all(math.isfinite(v) for v in vector) else None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def loss_matches(store: Store, row: dict[str, Any], now_ms: int) -> dict[str, Any]:
    """Include similar winners too; exclude future labels and operational exits."""
    target = features(row)
    empty = {"matched": False, "samples": 0, "losses": 0, "ids": []}
    if target is None:
        return empty
    matches = []
    config_hash = (store.get("simple_market_trend") or {}).get("config_hash")
    seen = set()
    for trade in store.get("simple_closed", []):
        try:
            context = trade.get("entry_context") or {}
            opened, closed = int(trade["entry_ms"]), int(trade["timestamp_ms"])
            identifier = trade["trade_id"]
            if (identifier in seen or not now_ms - LOOKBACK_MS <= opened < closed < now_ms
                    or trade.get("strategy_choice") != "current"
                    or context.get("entry_timeframe", "15m") != row.get("entry_timeframe", "15m")
                    or context.get("config_hash") != config_hash
                    or not any(label in trade.get("reason", "") for label in
                               ("stop", "holding limit", "profit runner", "trend reversed"))):
                continue
            vector = features(context.get("indicators", {}))
            if vector is None:
                continue
            # Direction-normalized neighborhood; thresholds are heuristic, not a profit claim.
            distances = [abs(a-b)/scale for a, b, scale in zip(
                target, vector, (1., 1., .4, .4, 2., 4., .5), strict=True)]
            if max(distances) > 1.5 or mean(distances) > .5:
                continue
            notional = float(trade["entry_price"]) * float(trade["quantity"])
            net = float(trade.get("verified_net_pnl", trade["pnl"])) / notional
            reverse = (-float(trade["gross_pnl"]) - float(trade["fees"])) / notional
            if not all(math.isfinite(v) for v in (net, reverse)):
                continue
            seen.add(identifier)
            matches.append((mean(distances), identifier, net, reverse, opened // (4 * 3_600_000)))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
    matches = sorted(matches)[:20]
    if not matches:
        return empty
    losses = sum(item[2] < 0 for item in matches)
    net = mean(item[2] for item in matches)
    reverse = mean(item[3] for item in matches)
    blocks = len({item[4] for item in matches})
    return {"matched": len(matches) >= 3 and blocks >= 2 and losses / len(matches) >= 2/3
            and net < 0 and reverse > 0,
            "samples": len(matches), "losses": losses, "blocks": blocks,
            "mean_net_return": net, "same_exit_inverse_estimate": reverse,
            "ids": [item[1] for item in matches]}


def choose(store: Store, row: dict[str, Any], now_ms: int) -> dict[str, Any]:
    from .learning import pattern

    market = store.get("simple_market_trend") or {}
    context = pattern(market)
    history = loss_matches(store, row, now_ms)
    reasons = []
    try:
        trend, previous, flow, previous_flow, one, four = (
            float(row[key]) for key in ("trend_atr", "previous_trend_atr", "flow", "previous_flow",
                                       "day_return_1h", "day_return_4h"))
        efficiency = float(row["path_efficiency_1h"])
        changes = float(row["direction_changes_1h"])
        if not all(math.isfinite(v) for v in (trend, previous, flow, previous_flow, one, four, efficiency, changes)):
            raise ValueError("nonfinite")
        if not 0 <= efficiency <= 1 or changes < 0:
            raise ValueError("invalid path")
    except (KeyError, TypeError, ValueError):
        return {"version": VERSION, "choice": None, "reason": "closed trend-stability observations missing",
                "asof_ms": row.get("timestamp_ms"), "market_ms": market.get("timestamp_ms"), "history": history}
    if not context.endswith(":persistent"):
        reasons.append("broad trend is mixed or fading")
    if one * four <= 0 or trend * previous <= 0:
        reasons.append("trend direction disagrees across horizons")
    if abs(previous) < .3:
        reasons.append("trend has not persisted across two closed candles")
    vector = features(row)
    if row.get("entry_timeframe", "15m") == "15m" and vector is not None and 0 <= vector[-1] < .15:
        reasons.append("breakout is less than 0.15 ATR beyond its range")
    if flow * previous_flow <= 0:
        reasons.append("trading flow changed direction")
    if efficiency < .55:
        reasons.append("price path is choppy")
    if history["matched"]:
        reasons.append(f"similar setups lost {history['losses']}/{history['samples']} closed trades")
    choice = "opposite" if reasons else "current"
    return {"version": VERSION, "choice": choice,
            "reason": "; ".join(reasons) if reasons else "aligned persistent trend, consistent flow and directional price path",
            "asof_ms": row.get("timestamp_ms"), "market_ms": market.get("timestamp_ms"),
            "pattern": context, "efficiency_1h": efficiency, "direction_changes_1h": changes,
            "history": history}


def permitted(store: Store, choice: str) -> bool:
    """Strict auto requires evidence for the actual variant, never its other side."""
    decision = store.get("simple_learning", {})
    if decision.get("entry_policy") == "probe":
        return True
    return any(r.get("eligible") and r.get("strategy") == choice
               and r.get("timeframe") == decision.get("timeframe")
               and r.get("hold_hours") == decision.get("hold_hours")
               for r in decision.get("ranking", []))


def valid(store: Store, route: dict[str, Any], row: dict[str, Any], now_ms: int, choice: str) -> bool:
    interval = 300_000 if row.get("entry_timeframe") == "5m" else BAR_MS
    try:
        return bool(route.get("version") == VERSION and route.get("choice") == choice
                    and route.get("asof_ms") == row["timestamp_ms"]
                    and 0 <= now_ms - int(row["timestamp_ms"]) < interval
                    and route.get("market_ms") == (store.get("simple_market_trend") or {}).get("timestamp_ms")
                    and choose(store, row, now_ms).get("choice") == choice and permitted(store, choice))
    except (TypeError, ValueError, KeyError):
        return False
