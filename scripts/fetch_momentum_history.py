"""Download public futures candles for offline research; no credentials or orders."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
import pandas as pd

from paperfrog.simple.data import normalize
from paperfrog.simple.learning_replay import FIVE, SYMBOLS
from paperfrog.simple.minute import validate_history, validate_ohlc


async def fetch(folder: Path, start: int, end: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(2)
    async with httpx.AsyncClient(base_url="https://fapi.binance.com", timeout=30) as client:

        async def one(symbol: str) -> None:
            async with semaphore:
                path = folder / f"{symbol}.parquet"
                cached = pd.read_parquet(path) if path.exists() else pd.DataFrame()
                chunks = [cached] if not cached.empty else []
                intervals = (
                    [(start, end)]
                    if cached.empty
                    else [
                        (start, min(end, int(cached.open_ms.min()))),
                        (max(start, int(cached.timestamp_ms.max())), end),
                    ]
                )
                for cursor, finish in intervals:
                    while cursor < finish:
                        response = await client.get(
                            "/fapi/v1/klines",
                            params={
                                "symbol": symbol,
                                "interval": "5m",
                                "startTime": cursor,
                                "endTime": finish - 1,
                                "limit": 1000,
                            },
                        )
                        response.raise_for_status()
                        frame = normalize(response.json(), finish, FIVE)
                        advance = int(frame.timestamp_ms.max())
                        if advance <= cursor:
                            raise ValueError("public history pagination stalled")
                        chunks.append(frame)
                        cursor = advance
                        await asyncio.sleep(0.25)
                x = pd.concat(chunks).drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
                x = x[(x.timestamp_ms > start) & (x.timestamp_ms <= end)].reset_index(drop=True)
                validate_history(x, end, FIVE, (end - start) // FIVE)
                validate_ohlc(x)
                temporary = path.with_suffix(".tmp")
                x.to_parquet(temporary, index=False)
                temporary.replace(path)
                print(f"{symbol}: {len(x)} verified closed candles", flush=True)

        await asyncio.gather(*(one(symbol) for symbol in SYMBOLS))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--start-ms", type=int, required=True)
    parser.add_argument("--end-ms", type=int, required=True)
    args = parser.parse_args()
    if args.start_ms % FIVE or args.end_ms % FIVE or args.end_ms <= args.start_ms:
        parser.error("start and end must be increasing five-minute boundaries")
    asyncio.run(fetch(args.folder, args.start_ms, args.end_ms))
