"""Deterministic BTC reference; not a tradable candidate or an LLM input.

Frozen, exploratory thresholds. A lagged relationship is estimated from past
closed bars and checked on a later window; this does not establish a trading edge.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .rotation import decision_side
from .strategy import BAR_MS, Rules

BTC = "BTCUSDT"
MINUTE_MS = 60_000
MINUTE_BARS = 120
PAIR_SAMPLES = 192
VALIDATION_SAMPLES = 64
MIN_CORRELATION = 0.25
MIN_LAG_CORRELATION = 0.10
MIN_VALIDATION_SKILL = 0.01


def closed_reference(frame: pd.DataFrame, rules: Rules) -> dict[str, Any]:
    if len(frame) < 192:
        raise SafeError("BTC reference history incomplete")
    row = frame.iloc[-1]
    trend = float(row.trend_atr)
    returns = [float(frame.close.iloc[-1] / frame.close.iloc[-1 - bars] - 1) for bars in (1, 4)]
    if not all(math.isfinite(v) for v in [trend, *returns]):
        raise SafeError("BTC reference indicators unavailable")
    direction = (
        1
        if trend >= rules.minimum_trend_atr and min(returns) > 0
        else (-1 if trend <= -rules.minimum_trend_atr and max(returns) < 0 else 0)
    )
    return {
        "timestamp_ms": int(row.timestamp_ms),
        "direction": direction,
        "trend_atr": trend,
        "return_15m": returns[0],
        "return_1h": returns[1],
    }


def pair_features(alt: pd.DataFrame, btc: pd.DataFrame, asof_ms: int) -> dict[str, Any]:
    """Predict the next alt 15m return using the most recent BTC 15m return.

    Each training label is already observed: BTC return at t-1 -> alt return
    at t. The latest BTC return is reserved for the forward prediction.
    No lag search, future bars, LLM opinion or strategy parameter changes.
    """
    invalid: dict[str, Any] = {"btc_pair_valid": False, "btc_reference_score": 0.0}
    alt = alt[alt.timestamp_ms <= asof_ms]
    btc = btc[btc.timestamp_ms <= asof_ms]
    if asof_ms % BAR_MS:
        return invalid
    if alt.empty or btc.empty or int(alt.iloc[-1].timestamp_ms) != int(btc.iloc[-1].timestamp_ms):
        return invalid
    if int(alt.iloc[-1].timestamp_ms) != asof_ms:
        return invalid
    joined = (
        alt[["timestamp_ms", "close"]]
        .merge(
            btc[["timestamp_ms", "close"]],
            on="timestamp_ms",
            suffixes=("_alt", "_btc"),
            validate="one_to_one",
        )
        .sort_values("timestamp_ms")
    )
    if len(joined) < PAIR_SAMPLES + 2 or (joined.timestamp_ms.diff().dropna() != BAR_MS).any():
        return invalid
    a = joined.close_alt.pct_change()
    b = joined.close_btc.pct_change()
    pairs = pd.DataFrame({"x": b.shift(1), "y": a, "same_btc": b}).dropna().tail(PAIR_SAMPLES)
    values = pairs.to_numpy(dtype=float)
    if len(pairs) != PAIR_SAMPLES or not np.isfinite(values).all():
        return invalid
    x, y, same = values[:, 0], values[:, 1], values[:, 2]
    if min(float(np.std(v)) for v in (x, y, same)) < 1e-8:
        return invalid
    correlation = float(np.corrcoef(same, y)[0, 1])
    lag_correlation = float(np.corrcoef(x, y)[0, 1])
    contemporary_beta = float(np.cov(same, y, ddof=0)[0, 1] / np.var(same))
    cut = len(x) - VALIDATION_SAMPLES
    variance = float(np.var(x[:cut]))
    if variance < 1e-16:
        return invalid
    train_beta = float(np.cov(x[:cut], y[:cut], ddof=0)[0, 1] / variance)
    intercept = float(np.mean(y[:cut]) - train_beta * np.mean(x[:cut]))
    predicted = intercept + train_beta * x[cut:]
    baseline_error = float(np.mean((y[cut:] - np.mean(y[:cut])) ** 2))
    if baseline_error < 1e-16:
        return invalid
    validation_skill = 1 - float(np.mean((y[cut:] - predicted) ** 2)) / baseline_error
    beta = float(np.cov(x, y, ddof=0)[0, 1] / np.var(x))
    mean = float(np.mean(y) - beta * np.mean(x))
    expected = mean + beta * float(b.iloc[-1])
    if not all(
        math.isfinite(v)
        for v in (
            correlation,
            lag_correlation,
            contemporary_beta,
            train_beta,
            validation_skill,
            beta,
            expected,
        )
    ):
        return invalid
    valid = correlation >= MIN_CORRELATION and 0 < contemporary_beta <= 3
    lag_valid = (
        valid
        and lag_correlation >= MIN_LAG_CORRELATION
        and 0 < train_beta <= 3
        and 0 < beta <= 3
        and validation_skill >= MIN_VALIDATION_SKILL
        and all(math.isfinite(v) for v in (expected, beta, validation_skill))
    )
    return {
        "btc_pair_valid": valid,
        "btc_pair_asof_ms": int(joined.iloc[-1].timestamp_ms),
        "btc_correlation": correlation,
        "btc_beta": contemporary_beta,
        "btc_lag_correlation": lag_correlation,
        "btc_lag_beta": beta,
        "btc_validation_skill": validation_skill,
        "btc_lag_valid": lag_valid,
        "btc_expected_return_15m": expected if lag_valid else 0.0,
        "btc_reference_score": 1
        + max(0, correlation)
        + (max(0, lag_correlation) if lag_valid else 0)
        if valid
        else 0.0,
    }


def fast_reference(raw: list[list[Any]], boundary: int) -> dict[str, Any]:
    """Use exactly the latest 120 CLOSED one-minute futures candles."""
    records = [
        {
            "timestamp_ms": int(r[6]) + 1,
            "open_ms": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
        }
        for r in raw
        if int(r[6]) + 1 <= boundary
    ]
    x = pd.DataFrame(records)
    if (
        boundary % MINUTE_MS
        or len(x) != MINUTE_BARS
        or int(x.iloc[-1].timestamp_ms) != boundary
        or int(x.iloc[0].timestamp_ms) != boundary - (MINUTE_BARS - 1) * MINUTE_MS
        or (x.timestamp_ms.diff().dropna() != MINUTE_MS).any()
        or (x.open_ms != x.timestamp_ms - MINUTE_MS).any()
    ):
        raise SafeError("BTC minute candles missing, duplicated, future or out of order")
    values = x[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if (
        not np.isfinite(values).all()
        or (values <= 0).any()
        or (x.high < x[["open", "low", "close"]].max(axis=1)).any()
        or (x.low > x[["open", "high", "close"]].min(axis=1)).any()
    ):
        raise SafeError("BTC minute OHLC invalid")
    tr = pd.concat(
        [x.high - x.low, (x.high - x.close.shift(1)).abs(), (x.low - x.close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = float(tr.tail(14).mean())
    if not math.isfinite(atr) or atr <= 0:
        raise SafeError("BTC minute volatility unavailable")
    trend = float(
        (
            x.close.ewm(span=5, adjust=False).mean().iloc[-1]
            - x.close.ewm(span=20, adjust=False).mean().iloc[-1]
        )
        / atr
    )
    r1, r5, r15 = [float(x.close.iloc[-1] / x.close.iloc[-1 - n] - 1) for n in (1, 5, 15)]
    if not all(math.isfinite(v) for v in (trend, r1, r5, r15)):
        raise SafeError("BTC minute indicators invalid")
    direction = 1 if trend >= 0.25 and r5 > 0 else (-1 if trend <= -0.25 and r5 < 0 else 0)
    return {
        "timestamp_ms": boundary,
        "direction": direction,
        "trend_atr": trend,
        "return_1m": r1,
        "return_5m": r5,
        "return_15m": r15,
        "atr_fraction": atr / float(x.close.iloc[-1]),
        "shock": abs(r1) >= 0.01 or abs(r5) >= 0.02,
    }


def direction_gate(s: Settings, store: Store, side: int, now_ms: int) -> str | None:
    """Mandatory independently refreshed BTC context, also checked at dispatch."""
    if side not in (-1, 1):
        return "invalid altcoin entry direction"
    for key, age in (("simple_btc_closed", BAR_MS), ("simple_btc_fast", 2 * MINUTE_MS)):
        value = store.get(key)
        if (
            not isinstance(value, dict)
            or value.get("config_hash") != s.config_hash
            or not 0 <= now_ms - int(value.get("timestamp_ms", 0)) < age
        ):
            return "BTC reference missing or stale; new altcoin entries blocked"
        if value.get("shock"):
            return "BTC short-term shock; new altcoin entries blocked"
        direction = value.get("direction")
        if type(direction) is not int or direction not in (-1, 0, 1):
            return "BTC reference direction missing or invalid; new altcoin entries blocked"
        neutral_allowed = s.simple_btc_direction_policy == "neutral_allowed" and direction == 0
        if direction != side and not neutral_allowed:
            return (
                "BTC direction opposes the proposed altcoin entry"
                if direction == -side
                else "BTC trend and short-term momentum must agree with the altcoin direction"
            )
        if (
            key == "simple_btc_fast"
            and not neutral_allowed
            and side * float(value.get("return_1m", 0)) < -0.5 * float(value.get("atr_fraction", 0))
        ):
            return "BTC latest minute moved against the proposed altcoin entry"
    return None


def entry_gate(
    s: Settings, store: Store, row: dict[str, Any], side: int, now_ms: int
) -> str | None:
    reason = direction_gate(s, store, side, now_ms)
    if reason:
        return reason
    expected_pair_asof = int(row["timestamp_ms"])
    if s.simple_entry_timeframe in {"1m", "5m", "8m"}:
        expected_pair_asof = expected_pair_asof // BAR_MS * BAR_MS
    if (
        row.get("btc_pair_valid") is not True
        or int(row.get("btc_pair_asof_ms", 0)) != expected_pair_asof
        or not 0 <= now_ms - int(row["btc_pair_asof_ms"]) < BAR_MS
    ):
        return "BTC/altcoin lag evidence missing, weak or stale"
    if row.get("btc_lag_valid") is True:
        expected = float(row.get("btc_expected_return_15m", math.nan))
        if not math.isfinite(expected) or side * expected <= 0:
            return "BTC-based next-bar estimate disagrees with the altcoin entry"
    return None


def require_order(s: Settings, store: Store, order: Order, now_ms: int) -> None:
    if order.reduce_only:
        return
    if order.symbol == BTC:
        raise EntryVeto("BTC is reference-only; new BTC positions are forbidden")
    if not s.simple_btc_reference_enabled:
        return
    record = store.get("simple_btc_order:" + order.client_id, {})
    if record.get("order") != order.model_dump(mode="json"):
        raise EntryVeto("BTC entry permission is not bound to this order")
    reason = entry_gate(s, store, record.get("context", {}), decision_side(store, order), now_ms)
    if reason:
        raise EntryVeto(reason)
