"""Bounded shadow research using public closed candles, never exchange orders.

All variants see identical history. Prices are next-bar opens, stops use adverse
extremes after decisions using only already-known opens, fees/spread/slippage and funding reserve apply.
Missing intervals invalidate a path. Portfolio/BTC tick gates are not simulated.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from . import confidence, learning
from .data import normalize
from .market_trend import DAY_FIELDS, regime_allows
from .minute import direction, validate_ohlc
from .strategy import (
    BAR_MS,
    entry,
    execution_rules,
    exit_reason,
    indicators,
    opposite_entry,
    tighten_stop,
)

FIVE = 300_000
# Fixed research universe: membership is not optimized against observed returns.
SYMBOLS = ("ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT",
           "LINKUSDT", "AVAXUSDT", "LTCUSDT", "BCHUSDT", "DOTUSDT", "TRXUSDT",
           "SUIUSDT", "NEARUSDT", "APTUSDT", "UNIUSDT", "ATOMUSDT", "ARBUSDT", "OPUSDT")


def prepare(raw: pd.DataFrame, s: Settings) -> dict[str, pd.DataFrame]:
    """Same entry indicators as live; all slow context joins point backwards."""
    from .minute import timing_indicators

    validate_ohlc(raw)
    x = raw.sort_values("timestamp_ms").reset_index(drop=True)
    if x.timestamp_ms.duplicated().any() or (x.timestamp_ms.diff().dropna() != FIVE).any():
        raise SafeError("research candles have gaps or duplicates")
    grouped = x.groupby((x.timestamp_ms.astype("int64") - 1) // BAR_MS)
    slow = grouped.agg(timestamp_ms=("timestamp_ms", "last"), open_ms=("open_ms", "first"),
                       open=("open", "first"), high=("high", "max"), low=("low", "min"),
                       close=("close", "last"), quote_volume=("quote_volume", "sum"),
                       taker_buy_quote=("taker_buy_quote", "sum"))
    slow = slow[(grouped.size() == 3) & (slow.timestamp_ms % BAR_MS == 0)].reset_index(drop=True)
    rules = execution_rules(s)
    slow = indicators(slow, rules)
    slow["day_context_ms"] = slow.timestamp_ms
    slow["day_return_4h"] = slow.close.pct_change(16)
    slow["day_return_1h"] = slow.close.pct_change(4)
    times = np.arange(17, dtype=float) - 8
    slow["day_slope_4h"] = slow.close.rolling(17).apply(
        lambda p: float(np.dot(times, p - p.mean()) / np.dot(times, times) * 16 / p.mean()), raw=True)
    slow["day_mean_distance_4h"] = slow.close / slow.close.rolling(16).mean() - 1
    slow["entry_timeframe"] = "15m"
    fast = timing_indicators(x.copy(), 5)
    fields = ["timestamp_ms", "atr", "atr_fraction", "daily_volume", "day_context_ms", *DAY_FIELDS]
    fast = pd.merge_asof(fast, slow[fields], on="timestamp_ms", direction="backward")
    fast["previous_atr_fraction"] = fast.atr_fraction.shift(1)
    fast["entry_timeframe"] = "5m"
    return {"5m": fast, "15m": slow}


def replay(raw: pd.DataFrame, markets: dict[int, dict[str, Any]], s: Settings,
           symbol: str, now_ms: int) -> list[dict[str, Any]]:
    """Independent non-overlapping trades per variant; only fully closed labels."""
    prepared = prepare(raw[raw.timestamp_ms <= now_ms], s)
    rules = execution_rules(s)
    one_way = s.fee_rate + s.slippage_bps / 10000 + rules.assumed_half_spread_bps / 10000
    results = []
    tape = raw[raw.timestamp_ms <= now_ms].to_dict("records")
    for timeframe in learning.TIMEFRAMES:
        rows = {int(r["timestamp_ms"]): r for r in prepared[timeframe].to_dict("records")}
        for strategy in ("current", "opposite"):
            for hold in (h for h in learning.HOLD_HOURS if h <= s.simple_max_hold_hours):
                trade = None
                last_row = None
                used_signal = None
                for bar in tape:
                    opened_at = int(bar["open_ms"])
                    closed_at = int(bar["timestamp_ms"])
                    # Signals at the preceding boundary can execute only at this later open.
                    row = rows.get(opened_at)
                    if row is not None:
                        last_row = row
                    if trade is not None:
                        side = trade["side"]
                        adverse = float(bar["low" if side > 0 else "high"])
                        exit_market = markets.get(opened_at // BAR_MS * BAR_MS)
                        if exit_market and exit_market.get("available_ms", opened_at) > opened_at:
                            exit_market = None
                        budget = learning.position_budget(trade, exit_market, opened_at, s.simple_max_hold_hours)
                        reason = exit_reason(budget, float(bar["open"]), opened_at, last_row, rules)
                        price = float(bar["open"])
                        if not reason and s.simple_confidence_enabled:
                            net = side * (price / trade["entry_price"] - 1) - trade["cost"]
                            if confidence.update_runner(trade, net, confidence.SMALL, opened_at):
                                reason = "profit runner"
                        if not reason:
                            if last_row is not None:
                                trade["stop_price"] = tighten_stop(trade, price, float(last_row["atr"]), rules)
                            if side * (adverse - trade["stop_price"]) <= 0:
                                reason = "stop"
                                price = min(price, trade["stop_price"]) if side > 0 else max(price, trade["stop_price"])
                        if reason:
                            net = side * (price / trade["entry_price"] - 1) - trade["cost"]
                            results.append({
                                "id": hashlib.sha256(f"{symbol}:{strategy}:{timeframe}:{hold}:{trade['opened_ms']}".encode()).hexdigest()[:24],
                                "version": learning.VERSION, "symbol": symbol, "strategy": strategy,
                                "timeframe": timeframe, "hold_hours": hold, "pattern": trade["pattern"],
                                "opened_ms": trade["opened_ms"], "closed_ms": closed_at,
                                "net_return": net, "cost": trade["cost"], "reason": reason,
                            })
                            trade = None
                            continue  # No close/reopen inside the same candle.
                        continue
                    row = last_row
                    interval = FIVE if timeframe == "5m" else BAR_MS
                    if (row is None or opened_at < now_ms - learning.WINDOW
                        or not 0 <= opened_at - row["timestamp_ms"] < interval
                        or row["timestamp_ms"] == used_signal):
                        continue
                    market = markets.get(opened_at // BAR_MS * BAR_MS)
                    context = learning.pattern(market)
                    if market is None or context == "unknown" or market.get("available_ms", opened_at) > opened_at:
                        continue
                    if strategy == "opposite":
                        side, _, level = opposite_entry(row, one_way, rules)
                        normal = -side
                    else:
                        normal, _ = entry(row, one_way, rules=replace(rules, entry_style="trend_follow") if timeframe == "5m" else rules)
                        if timeframe == "5m" and direction(row, rules)[0] != normal:
                            normal = 0
                        side, level = normal, None
                    if not side or not regime_allows(market["regime"], normal, strategy):
                        continue
                    if any(not np.isfinite(row[k]) or normal * row[k] <= 0 for k in DAY_FIELDS):
                        continue
                    if row["daily_volume"] < s.min_daily_quote_volume:
                        continue
                    price = float(bar["open"])
                    if strategy == "opposite" and (level is None or side * (price - level) <= 0):
                        continue
                    distance = min(rules.maximum_stop_fraction, max(rules.minimum_stop_fraction, rules.stop_atr * row["atr_fraction"]))
                    used_signal = row["timestamp_ms"]
                    trade = {
                        "side": side, "decision_side": normal, "opened_ms": opened_at,
                        "entry_price": price, "stop_price": price * (1 - side * distance),
                        "initial_stop_distance": price * distance, "pattern": context,
                        "max_hold_hours": hold, "adaptive_holding": True,
                        # Funding reserve scales with permitted holding, even if closed early.
                        "cost": 2 * one_way + 0.0001 * hold,
                        "opposite_confirmation_version": 1 if strategy == "opposite" else None,
                        "opposite_level": level,
                    }
                    # The first candle's adverse move must also hit the initial stop.
                    adverse = float(bar["low" if side > 0 else "high"])
                    if side * (adverse - trade["stop_price"]) <= 0:
                        results.append({"id": hashlib.sha256(f"{symbol}:{strategy}:{timeframe}:{hold}:{opened_at}".encode()).hexdigest()[:24],
                                        "version": learning.VERSION, "symbol": symbol, "strategy": strategy,
                                        "timeframe": timeframe, "hold_hours": hold, "pattern": context,
                                        "opened_ms": opened_at, "closed_ms": closed_at,
                                        "net_return": -distance - trade["cost"], "cost": trade["cost"], "reason": "initial stop"})
                        trade = None
    return results


def recorded_markets(store: Store, now_ms: int) -> dict[int, dict[str, Any]]:
    import json

    with store.engine.connect() as conn:
        records = conn.execute(text("SELECT ts,payload FROM audit WHERE kind='simple_market_trend' AND ts>=:start AND ts<=:end ORDER BY id"),
                               {"start": now_ms - learning.WINDOW - BAR_MS, "end": now_ms}).fetchall()
    result = {}
    for observed, payload in records:
        r = json.loads(payload)
        # Use only context that was actually available before the next entry bar.
        if "regime" in r and isinstance(r.get("timestamp_ms"), int):
            available = (max(observed, r["timestamp_ms"]) + FIVE - 1) // FIVE * FIVE
            r = {**r, "available_ms": available}
            result.setdefault(r["timestamp_ms"], r)
    return result


async def history(exchange: Any, folder: Path, symbol: str, now_ms: int) -> pd.DataFrame:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (symbol + ".parquet")
    end = now_ms // FIVE * FIVE
    start = end - 10 * learning.DAY
    cached = await asyncio.to_thread(pd.read_parquet, path) if path.exists() else pd.DataFrame()
    cursor = max(start, int(cached.timestamp_ms.max())) if not cached.empty else start
    chunks = [cached] if not cached.empty else []
    while cursor < end:
        raw = await exchange.request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": "5m", "startTime": cursor,
            "endTime": end - 1, "limit": 1000})
        data = normalize(raw, end, FIVE)
        advance = int(data.timestamp_ms.max())
        if advance <= cursor:
            raise SafeError("research history did not advance")
        chunks.append(data)
        cursor = advance
        await asyncio.sleep(0.2)
    if not chunks:
        raise SafeError("research history unavailable")
    frame = pd.concat(chunks).drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
    frame = frame[(frame.timestamp_ms > start) & (frame.timestamp_ms <= end)].reset_index(drop=True)
    validate_ohlc(frame)
    temporary = path.with_suffix(".tmp")
    await asyncio.to_thread(frame.to_parquet, temporary, index=False)
    temporary.replace(path)
    return frame


async def refresh(exchange: Any, store: Store, s: Settings, now_ms: int) -> dict[str, Any]:
    markets = recorded_markets(store, now_ms)
    outcomes = []
    errors = []
    for symbol in SYMBOLS:
        if symbol in s.denylist:
            continue
        try:
            raw = await history(exchange, s.data_dir / ("learning-" + s.trading_mode), symbol, now_ms)
            outcomes.extend(await asyncio.to_thread(replay, raw, markets, s, symbol, now_ms))
        except (SafeError, OSError, ValueError, KeyError) as exc:
            errors.append({"symbol": symbol, "error": type(exc).__name__})
    # A partially failed refresh cannot promote a lucky subset of the universe.
    status = {"asof_ms": now_ms, "outcomes": len(outcomes), "errors": errors,
              "version": learning.VERSION, "config_hash": s.config_hash,
              "symbols": [symbol for symbol in SYMBOLS if symbol not in s.denylist],
              "source": "closed-candle simulation; not live account performance"}
    if not errors and outcomes:
        store.put(learning.EVIDENCE, outcomes)
    store.put("simple_learning_research", status)
    return status
