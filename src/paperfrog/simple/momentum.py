"""Deterministic per-coin A/B/C momentum; no cross-coin direction dependency."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from .minute import direction
from .strategy import Rules, entry

VERSION = "abc-momentum-v1"


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
