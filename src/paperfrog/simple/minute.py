"""Closed one-minute entry timing; 15-minute risk and BTC context stay independent."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from paperfrog.domain.types import EntryVeto, Order, Quote, SafeError
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .data import normalize
from .strategy import BAR_MS, Rules

MINUTE_MS = 60_000
HISTORY_BARS = 120


def frame(raw: list[list[Any]], context: dict[str, Any], boundary: int) -> pd.DataFrame:
    """Use exactly 120 completed consecutive minutes and already closed slow context."""
    x = normalize(raw, boundary, MINUTE_MS)
    if (
        boundary % MINUTE_MS
        or len(x) != HISTORY_BARS
        or int(x.iloc[0].timestamp_ms) != boundary - (HISTORY_BARS - 1) * MINUTE_MS
        or int(x.iloc[-1].timestamp_ms) != boundary
        or x.timestamp_ms.duplicated().any()
        or (x.timestamp_ms.diff().dropna() != MINUTE_MS).any()
        or not 0 <= boundary - int(context.get("timestamp_ms", 0)) < BAR_MS
    ):
        raise SafeError("one-minute candles or closed risk context incomplete")
    values = x[["open", "high", "low", "close", "quote_volume", "taker_buy_quote"]].to_numpy(
        dtype=float
    )
    if (
        not np.isfinite(values).all()
        or (values[:, :4] <= 0).any()
        or (values[:, 4:] < 0).any()
        or (x.high < x[["open", "close", "low"]].max(axis=1)).any()
        or (x.low > x[["open", "close", "high"]].min(axis=1)).any()
        or (x.taker_buy_quote > x.quote_volume).any()
    ):
        raise SafeError("invalid one-minute OHLC or flow")
    # These values retain their actual 15-minute risk/longer-horizon meaning.
    for key in ("atr", "atr_fraction", "daily_volume", "return_4h", "return_24h"):
        value = float(context.get(key, math.nan))
        if not math.isfinite(value) or (
            key in {"atr", "atr_fraction", "daily_volume"} and value <= 0
        ):
            raise SafeError("one-minute risk context unavailable")
        x[key] = value
    for key, value in context.items():
        if key.startswith("btc_"):
            x[key] = value
    previous = x.close.shift(1)
    tr = pd.concat(
        [x.high - x.low, (x.high - previous).abs(), (x.low - previous).abs()], axis=1
    ).max(axis=1)
    x["minute_atr"] = tr.rolling(14).mean()
    x["fast"] = x.close.ewm(span=5, adjust=False).mean()
    x["slow"] = x.close.ewm(span=20, adjust=False).mean()
    x["fast_change"] = x.fast.diff()
    x["trend_atr"] = (x.fast - x.slow) / x.minute_atr.replace(0, np.nan)
    x["prior_high"] = x.high.shift(1).rolling(5).max()
    x["prior_low"] = x.low.shift(1).rolling(5).min()
    for minutes in (1, 3, 5):
        x[f"return_{minutes}m"] = x.close.pct_change(minutes)
    x["return_1h"] = x.close.pct_change(60)
    x["flow"] = 2 * x.taker_buy_quote / x.quote_volume.replace(0, np.nan) - 1
    x["volume_ratio"] = x.quote_volume / x.quote_volume.shift(1).rolling(60).mean().replace(
        0, np.nan
    )
    delta = x.close.diff()
    gain, loss = delta.clip(lower=0).rolling(14).mean(), (-delta.clip(upper=0)).rolling(14).mean()
    x["rsi_14"] = 100 * gain / (gain + loss).replace(0, np.nan)
    x.loc[(gain == 0) & (loss == 0), "rsi_14"] = 50.0
    x["breakout_up_atr"] = (x.close - x.prior_high) / x.minute_atr.replace(0, np.nan)
    x["breakout_down_atr"] = (x.prior_low - x.close) / x.minute_atr.replace(0, np.nan)
    low, high = x.low.rolling(24).min(), x.high.rolling(24).max()
    x["range_position"] = (x.close - low) / (high - low).replace(0, np.nan)
    x["score"] = x.trend_atr.abs().clip(upper=5) * np.sqrt(x.daily_volume / 10_000_000)
    x["entry_timeframe"] = "1m"
    # Keep a distinct copy for older positions' original trend-reversal exit.
    x.attrs["risk_context"] = context
    return x


def direction(row: dict[str, Any], rules: Rules) -> tuple[int, str]:
    fields = (
        "close",
        "open",
        "fast",
        "slow",
        "fast_change",
        "trend_atr",
        "minute_atr",
        "flow",
        "return_1m",
        "return_3m",
        "return_5m",
    )
    if (
        row.get("entry_timeframe") != "1m"
        or not all(math.isfinite(float(row.get(key, math.nan))) for key in fields)
        or float(row["minute_atr"]) <= 0
    ):
        return 0, "complete closed one-minute slope required"
    side = (
        1
        if float(row["trend_atr"]) >= rules.minimum_trend_atr
        else (-1 if float(row["trend_atr"]) <= -rules.minimum_trend_atr else 0)
    )
    if (
        not side
        or any(
            side * float(row[key]) <= 0
            for key in ("return_1m", "return_3m", "return_5m", "fast_change")
        )
        or side * (float(row["close"]) - float(row["open"])) <= 0
        or side * (float(row["close"]) - float(row["fast"])) <= 0
        or side * float(row["flow"]) < rules.minimum_flow
    ):
        return 0, "one-minute slope reversed, flat or unconfirmed by trading flow"
    return (
        side,
        f"1m {'upward' if side > 0 else 'downward'} slope: EMA 5/20, aligned 1/3/5-minute moves and trading flow",
    )


def veto(row: dict[str, Any], side: int, now_ms: int, mark: float, rules: Rules) -> str | None:
    if not 0 <= now_ms - int(row.get("timestamp_ms", 0)) < MINUTE_MS:
        return "one-minute entry candle missing, future dated or expired"
    observed, reason = direction(row, rules)
    if observed != side:
        return reason
    if (
        not math.isfinite(mark)
        or mark <= 0
        or side * (mark - float(row["close"])) < -0.5 * float(row["minute_atr"])
    ):
        return "current price rebounded against the closed one-minute slope"
    return None


def require_order(s: Settings, store: Store, order: Order, now_ms: int, quote: Quote) -> None:
    if order.reduce_only or s.simple_entry_timeframe != "1m":
        return
    from .strategy import execution_rules

    permission = store.get("simple_minute_order:" + order.client_id, {})
    if (
        permission.get("order") != order.model_dump(mode="json")
        or permission.get("config_hash") != s.config_hash
    ):
        raise EntryVeto("one-minute permission is not bound to this order")
    row = permission.get("row", {})
    if not isinstance(row, dict):
        raise EntryVeto("one-minute entry context unavailable")
    latest = store.get("simple_minute:" + order.symbol, {})
    if latest != row:
        raise EntryVeto("one-minute slope changed before order dispatch")
    if (
        quote.symbol != order.symbol
        or not 0 <= now_ms - quote.timestamp_ms <= s.market_stale_seconds * 1000
    ):
        raise EntryVeto("fresh one-minute execution quote required")
    reason = veto(row, order.side, now_ms, float(quote.mark), execution_rules(s))
    if reason:
        raise EntryVeto(reason)
