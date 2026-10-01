from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from paperfrog.domain.types import SafeError
from paperfrog.exchange.binance import BinanceAdapter


class Lake:
    def __init__(self, root: Path):
        self.root = root

    def bind_environment(self, environment: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "market-environment.json"
        if path.exists():
            if json.loads(path.read_text())["environment"] != environment:
                raise SafeError("data lake belongs to another exchange environment")
        else:
            try:
                with path.open("x") as file:
                    json.dump({"environment": environment}, file)
            except FileExistsError:
                self.bind_environment(environment)

    def append(self, kind: str, symbol: str, frame: pd.DataFrame, source: str) -> int:
        if not re.fullmatch(r"[a-z0-9_]+", kind) or not re.fullmatch(r"[A-Z0-9]+", symbol):
            raise SafeError("invalid partition")
        if frame.empty:
            return 0
        key = "event_id" if "event_id" in frame else "timestamp_ms"
        frame = frame.sort_values("timestamp_ms").drop_duplicates(key).copy()
        frame["utc"] = pd.to_datetime(frame.timestamp_ms, unit="ms", utc=True)
        written = 0
        for day, group in frame.groupby(frame.utc.dt.strftime("%Y-%m-%d")):
            digest = hashlib.sha256(
                group.to_json(date_unit="ms", date_format="iso").encode()
            ).hexdigest()
            folder = self.root / "raw" / kind / symbol / str(day)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (digest + ".parquet")
            if target.exists():
                continue
            group["source"], group["collected_at_ms"], group["schema_version"] = (
                source,
                int(time.time() * 1000),
                1,
            )
            temporary = target.with_suffix(f".{os.getpid()}.tmp")
            group.to_parquet(temporary, index=False)
            os.replace(temporary, target)
            written += len(group)
        return written

    def read(self, kind: str, symbol: str) -> pd.DataFrame:
        if not re.fullmatch(r"[a-z0-9_]+", kind) or not re.fullmatch(r"[A-Z0-9]+", symbol):
            raise SafeError("invalid partition")
        paths = list((self.root / "raw" / kind / symbol).glob("*/*.parquet"))
        if not paths:
            return pd.DataFrame()
        with duckdb.connect() as connection:
            frame = connection.execute(
                "SELECT * FROM read_parquet(?, union_by_name=true)", [[str(p) for p in paths]]
            ).df()
        key = "event_id" if "event_id" in frame else "timestamp_ms"
        return (
            frame.sort_values(["timestamp_ms", "collected_at_ms"])
            .drop_duplicates(key, keep="first")
            .reset_index(drop=True)
        )


def validate(frame: pd.DataFrame, interval_ms: int | None = None) -> dict[str, Any]:
    problems = []
    if frame.empty:
        return {"valid": False, "rows": 0, "problems": ["empty dataset"]}
    if frame.timestamp_ms.duplicated().any():
        problems.append("duplicate timestamps")
    if not frame.timestamp_ms.is_monotonic_increasing:
        problems.append("out-of-order timestamps")
    if interval_ms and (frame.timestamp_ms.diff().dropna() != interval_ms).any():
        problems.append("missing or irregular bars")
    for column in ("open", "high", "low", "close", "perp_mid", "spot_mid", "open_interest"):
        if column in frame and ((frame[column] <= 0) | ~np.isfinite(frame[column])).any():
            problems.append("impossible " + column)
    if {"high", "low", "open", "close"} <= set(frame):
        if (
            (frame.high < frame[["open", "close", "low"]].max(axis=1))
            | (frame.low > frame[["open", "close", "high"]].min(axis=1))
        ).any():
            problems.append("invalid OHLC bounds")
    return {
        "valid": not problems,
        "rows": len(frame),
        "start_ms": int(frame.timestamp_ms.min()),
        "end_ms": int(frame.timestamp_ms.max()),
        "problems": problems,
    }


async def backfill(
    adapter: BinanceAdapter,
    lake: Lake,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str = "15m",
) -> dict[str, int]:
    if interval not in {"1m", "15m"} or end_ms <= start_ms:
        raise SafeError("invalid backfill range")
    lake.bind_environment(adapter.base)
    counts = {}
    for market, spot in (("spot", True), ("perp", False)):
        cursor, count = start_ms, 0
        while cursor < end_ms:
            rows = await adapter.request(
                "GET",
                "/api/v3/klines" if spot else "/fapi/v1/klines",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": 1000,
                },
                spot=spot,
            )
            if not rows:
                break
            normalized = []
            now = int(time.time() * 1000)
            for r in rows:
                close = int(r[6]) + 1
                if close > min(now, end_ms):
                    continue
                normalized.append(
                    {
                        "timestamp_ms": close,
                        "open_ms": int(r[0]),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                        "quote_volume": float(r[7]),
                        "taker_buy_quote": float(r[10]),
                    }
                )
            if normalized:
                count += lake.append(
                    market + "_" + interval,
                    symbol,
                    pd.DataFrame(normalized),
                    "binance_rest_closed_klines",
                )
            next_cursor = int(rows[-1][6]) + 1
            if next_cursor <= cursor:
                raise SafeError("backfill pagination did not advance")
            cursor = next_cursor
        counts[market + "_" + interval] = count
    cursor = start_ms
    funding = []
    while cursor < end_ms:
        rows = await adapter.request(
            "GET",
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms - 1, "limit": 1000},
        )
        if not rows:
            break
        funding.extend(
            {
                "timestamp_ms": int(r["fundingTime"]),
                "funding_rate": float(r["fundingRate"]),
                "mark": float(r.get("markPrice", 0)),
            }
            for r in rows
        )
        cursor = int(rows[-1]["fundingTime"]) + 1
    counts["funding"] = (
        lake.append("funding", symbol, pd.DataFrame(funding), "binance_funding_history")
        if funding
        else 0
    )
    # Official endpoint only offers recent history; never synthesize older OI.
    oi_start = max(start_ms, int(time.time() * 1000) - 29 * 86400000)
    oi = []
    cursor = oi_start
    while cursor < end_ms:
        rows = await adapter.request(
            "GET",
            "/futures/data/openInterestHist",
            {
                "symbol": symbol,
                "period": "15m",
                "startTime": cursor,
                "endTime": min(end_ms - 1, cursor + 499 * 900000),
                "limit": 500,
            },
        )
        if not rows:
            break
        oi.extend(
            {
                "timestamp_ms": int(r["timestamp"]),
                "open_interest": float(r["sumOpenInterest"]),
                "oi_notional": float(r["sumOpenInterestValue"]),
            }
            for r in rows
        )
        cursor = int(rows[-1]["timestamp"]) + 900000
    counts["open_interest"] = (
        lake.append("open_interest", symbol, pd.DataFrame(oi), "binance_recent_oi") if oi else 0
    )
    return counts


async def collect_snapshot(adapter: BinanceAdapter, lake: Lake, symbol: str) -> dict[str, Any]:
    import asyncio

    lake.bind_environment(adapter.base)
    book, premium, oi, spot = await asyncio.gather(
        adapter.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}),
        adapter.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}),
        adapter.request("GET", "/fapi/v1/openInterest", {"symbol": symbol}),
        adapter.request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol}, spot=True),
    )
    now = int(time.time() * 1000)
    frame = pd.DataFrame(
        [
            {
                "timestamp_ms": int(book.get("time", now)),
                "bid": float(book["bidPrice"]),
                "ask": float(book["askPrice"]),
                "bid_size": float(book["bidQty"]),
                "ask_size": float(book["askQty"]),
                "mark": float(premium["markPrice"]),
                "index": float(premium["indexPrice"]),
                "funding_rate": float(premium["lastFundingRate"]),
                "next_funding_ms": int(premium["nextFundingTime"]),
                "open_interest": float(oi["openInterest"]),
                "spot_bid": float(spot["bidPrice"]),
                "spot_ask": float(spot["askPrice"]),
            }
        ]
    )
    lake.append("snapshots", symbol, frame, "binance_rest_snapshot")
    return dict(frame.iloc[0])
