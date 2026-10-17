"""Observational trade metadata; never supplies signals or changes risk settings."""

from __future__ import annotations

import math
from typing import Any

from paperfrog.domain.types import Fill, Quote

# Explicit numeric allowlist: never persist arbitrary row text, settings or credentials.
INDICATORS = (
    "timestamp_ms",
    "close",
    "trend_atr",
    "flow",
    "atr",
    "atr_fraction",
    "return_1m",
    "return_3m",
    "return_5m",
    "return_15m",
    "return_25m",
    "return_1h",
    "return_4h",
    "return_24h",
    "rsi_14",
    "volume_ratio",
    "breakout_up_atr",
    "breakout_down_atr",
    "range_position",
    "prior_high",
    "prior_low",
    "daily_volume",
    "score",
    "btc_correlation",
    "btc_beta",
    "btc_lag_correlation",
    "btc_lag_beta",
    "btc_validation_skill",
    "btc_expected_return_15m",
    "btc_pair_asof_ms",
)


def indicators(row: dict[str, Any]) -> dict[str, float]:
    result = {}
    for name in INDICATORS:
        try:
            value = float(row[name])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if math.isfinite(value):
            result[name] = value
    return result


def reference(quote: Quote) -> dict[str, float | int]:
    return {
        "timestamp_ms": quote.timestamp_ms,
        "bid": float(quote.bid),
        "ask": float(quote.ask),
        "mid": float(quote.mid),
        "mark": float(quote.mark),
        "spread_bps": float((quote.ask - quote.bid) / quote.mid) * 10000,
    }


def execution_cost(fill: Fill, quote: dict[str, Any] | None) -> dict[str, float] | None:
    """Signed price shortfall; already embedded in fill PnL, never subtract twice."""
    if not quote:
        return None
    touch = float(quote["ask" if fill.side > 0 else "bid"])
    price, quantity = float(fill.price), float(fill.quantity)
    return {
        "spread_cost": (touch - float(quote["mid"])) * fill.side * quantity,
        "slippage_cost": (price - touch) * fill.side * quantity,
        "slippage_bps": (price / touch - 1) * fill.side * 10000,
    }


def observe(trade: dict[str, Any], price: float, timestamp_ms: int) -> None:
    """Sampled excursions, not intrabar extremes; missing legacy history stays missing."""
    if trade.get("journal_version") != 1 or timestamp_ms < trade["opened_ms"]:
        return
    movement = trade["side"] * (price / trade["entry_price"] - 1)
    trade["mfe_fraction"] = max(trade.get("mfe_fraction", 0.0), movement)
    trade["mae_fraction"] = min(trade.get("mae_fraction", 0.0), movement)
    trade["last_observed_ms"] = max(timestamp_ms, trade.get("last_observed_ms", 0))
