"""Public spot, futures and funding history for independent strategy research."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

HOUR = 3_600_000
DAY = 24 * HOUR
END = 1794096000000
START = END - 420 * DAY
SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
)


def validate(frame: pd.DataFrame, start: int = START, end: int = END) -> None:
    expected = np.arange(start, end, HOUR)
    if not np.array_equal(frame.open_ms.to_numpy(), expected):
        raise ValueError("hourly history missing, duplicated or out of order")
    values = frame[["open", "high", "low", "close", "volume"]].to_numpy()
    if (
        not np.isfinite(values).all()
        or (values[:, :4] <= 0).any()
        or (values[:, 4] < 0).any()
        or (frame.high < frame[["open", "close", "low"]].max(axis=1)).any()
        or (frame.low > frame[["open", "close", "high"]].min(axis=1)).any()
    ):
        raise ValueError("invalid hourly price or volume")


async def fetch(folder: Path, start: int = START, end: int = END) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=40) as client:

        async def request(url: str, params: dict):
            for attempt in range(5):
                response = await client.get(url, params=params)
                if response.status_code in {429, 418, 500, 502, 503, 504}:
                    await asyncio.sleep(min(30, 2 ** (attempt + 1)))
                    continue
                response.raise_for_status()
                return response.json()
            raise ValueError("public history endpoint remained unavailable")

        async def candles(symbol: str, market: str):
            async with semaphore:
                path = folder / f"{symbol}.{market}.parquet"
                if path.exists():
                    validate(pd.read_parquet(path), start, end)
                    return
                host, endpoint = (
                    ("https://fapi.binance.com", "/fapi/v1/klines")
                    if market == "futures"
                    else ("https://data-api.binance.vision", "/api/v3/klines")
                )
                cursor, records = start, []
                while cursor < end:
                    rows = await request(
                        host + endpoint,
                        {
                            "symbol": symbol,
                            "interval": "1h",
                            "startTime": cursor,
                            "endTime": end - 1,
                            "limit": 1000,
                        },
                    )
                    if not rows or int(rows[-1][0]) < cursor:
                        raise ValueError("hourly pagination stalled")
                    records.extend(
                        {
                            "open_ms": int(r[0]),
                            "open": float(r[1]),
                            "high": float(r[2]),
                            "low": float(r[3]),
                            "close": float(r[4]),
                            "volume": float(r[7]),
                        }
                        for r in rows
                    )
                    cursor = int(rows[-1][0]) + HOUR
                    await asyncio.sleep(0.15)
                frame = pd.DataFrame(records)
                validate(frame, start, end)
                frame.to_parquet(path.with_suffix(".tmp"), index=False)
                path.with_suffix(".tmp").replace(path)
                print(f"{symbol} {market}: {len(frame)} verified hourly candles", flush=True)

        async def funding(symbol: str):
            async with semaphore:
                path = folder / f"{symbol}.funding.json"
                if path.exists():
                    records = json.loads(path.read_text())
                else:
                    cursor, records = start, []
                    while cursor < end:
                        rows = await request(
                            "https://fapi.binance.com/fapi/v1/fundingRate",
                            {
                                "symbol": symbol,
                                "startTime": cursor,
                                "endTime": end - 1,
                                "limit": 1000,
                            },
                        )
                        if not rows:
                            break
                        records.extend(
                            {
                                "timestamp_ms": int(r["fundingTime"]),
                                "funding_rate": float(r["fundingRate"]),
                                "mark": float(r["markPrice"]),
                            }
                            for r in rows
                        )
                        advance = max(r["timestamp_ms"] for r in records) + 1
                        if advance <= cursor:
                            raise ValueError("funding pagination stalled")
                        cursor = advance
                        await asyncio.sleep(0.2)
                times = np.array([r["timestamp_ms"] for r in records])
                values = np.array([[r["funding_rate"], r["mark"]] for r in records])
                if (
                    not len(times)
                    or times[0] > start + DAY
                    or times[-1] < end - DAY
                    or (np.diff(times) <= 0).any()
                    or (np.diff(times) > DAY).any()
                    or not np.isfinite(values).all()
                    or (values[:, 1] <= 0).any()
                ):
                    raise ValueError("incomplete or invalid funding history")
                path.write_text(json.dumps(records))
                print(f"{symbol}: {len(records)} verified public funding events", flush=True)

        await asyncio.gather(*(candles(s, m) for s in SYMBOLS for m in ("futures", "spot")))
        await asyncio.gather(*(funding(s) for s in SYMBOLS))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--end-ms", type=int, default=END)
    parser.add_argument("--days", type=int, default=420)
    args = parser.parse_args()
    if args.end_ms % DAY or args.days < 61:
        parser.error("complete UTC days and more than 60 warmup days required")
    asyncio.run(fetch(args.folder, args.end_ms - args.days * DAY, args.end_ms))
