from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from paperfrog.data.lake import Lake
from paperfrog.data.universe import STABLE
from paperfrog.domain.types import SafeError
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.settings import Settings

from .strategy import BAR_MS, indicators, require_execution


def normalize(rows: list[list[Any]], end_ms: int) -> pd.DataFrame:
    records = [
        {
            "timestamp_ms": int(r[6]) + 1,
            "open_ms": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "quote_volume": float(r[7]),
            "taker_buy_quote": float(r[10]),
        }
        for r in rows
        if int(r[6]) + 1 <= end_ms
    ]
    if not records:
        raise SafeError("no closed futures candles returned")
    x = pd.DataFrame(records)
    if not (x.open_ms == x.timestamp_ms - BAR_MS).all():
        raise SafeError("invalid futures candle availability")
    return x


async def scan(adapter: BinanceAdapter, s: Settings) -> dict[str, Any]:
    require_execution(s)
    info = await adapter.exchange_info()
    tickers = await adapter.request("GET", "/fapi/v1/ticker/24hr")
    now_ms = int(time.time() * 1000) + adapter.offset_ms
    metadata = {
        x["symbol"]: x
        for x in info["symbols"]
        if x["symbol"] in adapter.filters
        and x["symbol"] != "BTCUSDT"
        and re.fullmatch(r"[A-Z0-9]{2,24}USDT", x["symbol"])
        and x["baseAsset"] not in STABLE
        and x.get("underlyingType", "COIN") == "COIN"
        and x["symbol"] not in s.denylist
        and int(x["onboardDate"]) <= now_ms - 3 * 86400000
    }
    usable = []
    for row in tickers:
        value = float(row["quoteVolume"])
        if (
            row["symbol"] in metadata
            and math.isfinite(value)
            and value >= s.min_daily_quote_volume
            and 0 <= now_ms - int(row["closeTime"]) <= 120000
        ):
            usable.append(row)
    usable.sort(key=lambda r: (-float(r["quoteVolume"]), r["symbol"]))
    pool = usable[: s.simple_scan_limit]
    if not pool:
        raise SafeError("no fresh liquid USDT perpetual crypto contracts")
    return {
        "observed_ms": now_ms,
        "observed_at": datetime.fromtimestamp(now_ms / 1000, UTC).isoformat(),
        "symbols": [r["symbol"] for r in pool],
        "metadata": {r["symbol"]: metadata[r["symbol"]] for r in pool},
        "ticker_volume": {r["symbol"]: float(r["quoteVolume"]) for r in pool},
        "historical_membership_proven": False,
        "live_permitted": False,
    }


async def candles(
    adapter: BinanceAdapter, lake: Lake, symbol: str, start_ms: int, end_ms: int
) -> pd.DataFrame:
    lake.bind_environment(adapter.base)
    cursor = start_ms
    chunks = []
    while cursor < end_ms:
        rows = await adapter.request(
            "GET",
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": "15m",
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": 499,
            },
        )
        if not rows:
            raise SafeError("requested simple history missing")
        x = normalize(rows, end_ms)
        chunks.append(x)
        await asyncio.to_thread(lake.append, "perp_15m", symbol, x, "binance_simple_closed_klines")
        next_ms = int(rows[-1][6]) + 1
        if next_ms <= cursor:
            raise SafeError("simple candle pagination stalled")
        cursor = next_ms
    frame = (
        pd.concat(chunks)
        .drop_duplicates("timestamp_ms")
        .sort_values("timestamp_ms")
        .reset_index(drop=True)
    )
    if (
        len(frame) != (end_ms - start_ms) // BAR_MS
        or int(frame.timestamp_ms.iloc[0]) != start_ms + BAR_MS
        or int(frame.timestamp_ms.iloc[-1]) != end_ms
    ):
        raise SafeError("simple candle coverage incomplete")
    await asyncio.to_thread(indicators, frame)  # Validate integrity without blocking monitoring.
    return frame


async def funding(
    adapter: BinanceAdapter,
    lake: Lake,
    symbol: str,
    start_ms: int,
    end_ms: int,
    allow_empty: bool = False,
) -> int:
    records = []
    cursor = start_ms
    while cursor < end_ms:
        rows = await adapter.request(
            "GET",
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms - 1, "limit": 1000},
        )
        if not rows:
            break
        for r in rows:
            rate, mark = float(r["fundingRate"]), float(r.get("markPrice", 0))
            if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                raise SafeError("actual funding marks missing or invalid")
            records.append(
                {"timestamp_ms": int(r["fundingTime"]), "funding_rate": rate, "mark": mark}
            )
        next_ms = int(rows[-1]["fundingTime"]) + 1
        if next_ms <= cursor:
            raise SafeError("funding pagination stalled")
        cursor = next_ms
    if not records:
        if allow_empty:
            return 0
        raise SafeError("actual funding history unavailable")
    return lake.append("funding", symbol, pd.DataFrame(records), "binance_simple_funding_history")


async def collect(
    adapter: BinanceAdapter, lake: Lake, snapshot: dict[str, Any], start_ms: int, end_ms: int
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(3)

    async def one(symbol: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                frame = await candles(adapter, lake, symbol, start_ms, end_ms)
                count = await funding(adapter, lake, symbol, start_ms, end_ms)
                return symbol, {"candles": len(frame), "funding_written": count}
            except (SafeError, ValueError, KeyError):
                return symbol, {"error": "incomplete or rejected history; excluded"}

    results = dict(await asyncio.gather(*(one(symbol) for symbol in snapshot["symbols"])))
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "results": results,
        "historical_membership_proven": False,
        "live_permitted": False,
    }
