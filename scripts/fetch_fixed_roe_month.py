"""Public-only inputs for the fixed-ROE monthly replay; never loads credentials.

Hourly volume bounds prune download work only. Replay membership is subsequently
ranked using exact completed 5m trailing volume, never those future upper bounds.
Current exchange metadata cannot prove historical delisted-contract membership.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from paperfrog.data.universe import STABLE
from paperfrog.simple.data import normalize

FIVE = 300_000
HOUR = 3_600_000
DAY = 24 * HOUR


class PublicHistory:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.lock = asyncio.Lock()
        self.next_request = 0.0

    async def get(self, endpoint: str, params: dict | None = None):
        for attempt in range(5):
            async with self.lock:
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + 0.20
            try:
                response = await self.client.get(endpoint, params=params)
            except httpx.TransportError:
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code in {418, 429}:
                # Respect exchange limits rather than retrying a banned connection.
                delay = int(response.headers.get("retry-after", "60"))
                if response.status_code == 418:
                    raise ValueError("public history rate ban; stop collection")
                await asyncio.sleep(min(300, max(1, delay)))
                continue
            if response.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        raise ValueError("public history retries exhausted")

    async def candles(self, symbol: str, start: int, end: int, interval: str,
                      path: Path, *, mark: bool = False) -> pd.DataFrame:
        step = {"1h": HOUR, "5m": FIVE}[interval]
        if path.exists():
            return pd.read_parquet(path)
        chunks, cursor = [], start
        while cursor < end:
            rows = await self.get("/fapi/v1/markPriceKlines" if mark else "/fapi/v1/klines", {
                "symbol": symbol, "interval": interval, "startTime": cursor,
                "endTime": end - 1, "limit": 1000,
            })
            if not rows:
                break
            frame = normalize(rows, end, step)
            advance = int(frame.timestamp_ms.max())
            if advance <= cursor:
                raise ValueError("public history pagination stalled")
            chunks.append(frame)
            cursor = advance
        if not chunks:
            return pd.DataFrame()
        frame = pd.concat(chunks).drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
        frame = frame[(frame.open_ms >= start) & (frame.timestamp_ms <= end)].reset_index(drop=True)
        if (frame.timestamp_ms.diff().dropna() != step).any():
            raise ValueError(f"noncontinuous public {interval} candles for {symbol}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
        return frame

    async def archive(self, symbol: str, start: int, end: int, path: Path,
                      *, mark: bool = False) -> pd.DataFrame:
        if path.exists():
            return pd.read_parquet(path)
        chunks = []
        cursor = pd.Timestamp(start, unit="ms", tz="UTC").normalize()
        finish = pd.Timestamp(end, unit="ms", tz="UTC")
        kind = "markPriceKlines" if mark else "klines"
        while cursor < finish:
            next_month = cursor.normalize().replace(day=1) + pd.offsets.MonthBegin()
            monthly = next_month <= finish
            period = cursor.strftime("%Y-%m" if monthly else "%Y-%m-%d")
            cadence = "monthly" if monthly else "daily"
            url = f"https://data.binance.vision/data/futures/um/{cadence}/{kind}/{symbol}/5m/{symbol}-5m-{period}.zip"
            for attempt in range(5):
                try:
                    response = await self.client.get(url)
                    if response.status_code < 500:
                        break
                except httpx.TransportError:
                    if attempt == 4:
                        raise
                await asyncio.sleep(2 ** attempt)
            if response.status_code == 404:
                # New listings need not have early-period archives. REST validates
                # the entire requested range afterwards if coverage is incomplete.
                cursor = next_month if monthly else cursor + pd.Timedelta(days=1)
                continue
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                with archive.open(archive.namelist()[0]) as f:
                    raw = pd.read_csv(f, header=None)
            # Futures archives have a header; spot archives sometimes do not.
            raw = raw[pd.to_numeric(raw.iloc[:, 0], errors="coerce").notna()]
            rows = raw.to_numpy().tolist()
            chunks.append(normalize(rows, end, FIVE))
            cursor = next_month if monthly else cursor + pd.Timedelta(days=1)
        if not chunks:
            return await self.candles(symbol, start, end, "5m", path, mark=mark)
        frame = pd.concat(chunks).drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
        frame = frame[(frame.open_ms >= start) & (frame.timestamp_ms <= end)].reset_index(drop=True)
        if frame.empty or (frame.timestamp_ms.diff().dropna() != FIVE).any() or int(frame.timestamp_ms.max()) != end:
            return await self.candles(symbol, start, end, "5m", path, mark=mark)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
        return frame


def eligible_metadata(info: dict, end: int) -> dict[str, dict]:
    return {x["symbol"]: x for x in info["symbols"] if
            x["quoteAsset"] == "USDT" and x["contractType"] == "PERPETUAL"
            and x.get("underlyingType", "COIN") == "COIN"
            and x["baseAsset"] not in STABLE
            and re.fullmatch(r"[A-Z0-9]{2,24}USDT", x["symbol"])
            and x["symbol"] not in {"BTCUSDT", "ETHUSDT"}
            and int(x["onboardDate"]) <= end - 3 * DAY}


def bound_candidates(hourly: dict[str, pd.DataFrame], metadata: dict[str, dict],
                     start: int, end: int) -> set[str]:
    """Superset of every possible top-100 member for every 5m in the interval.

For H <= t < H+1h, 23 whole hours are certainly inside (t-24h,t].
The two boundary hours bound the rest, even if their volume arrives later.
These bounds control acquisition, not decisions. Missing rows cannot certify
pruning, so a symbol with missing hours is always retained.
"""
    symbols = sorted(hourly)
    hours = np.arange(start - DAY, end, HOUR, dtype=np.int64)
    values = np.array([hourly[s].set_index("open_ms").quote_volume.reindex(hours).to_numpy()
                       for s in symbols])
    result = set()
    for j, h in enumerate(hours):
        if h < start:
            continue
        valid = np.array([int(metadata[s]["onboardDate"]) <= h - 3 * DAY for s in symbols])
        lower = np.nansum(values[:, j - 23:j], axis=1)
        upper = lower + values[:, j - 24] + values[:, j]
        ranked = sorted(lower[valid], reverse=True)
        threshold = max(10_000_000, ranked[99] if len(ranked) >= 100 else 0)
        missing = np.isnan(values[:, j - 24:j + 1]).any(axis=1)
        result.update(s for i, s in enumerate(symbols)
                      if valid[i] and (missing[i] or upper[i] >= threshold))
    return result


async def collect(folder: Path, start: int, end: int, stage: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url="https://fapi.binance.com", timeout=45) as client:
        api = PublicHistory(client)
        info_path = folder / "exchange-info.json"
        if not info_path.exists():
            info_path.write_text(json.dumps(await api.get("/fapi/v1/exchangeInfo")))
        info = json.loads(info_path.read_text())
        metadata = eligible_metadata(info, end)
        sem = asyncio.Semaphore(4)
        hourly, unavailable = {}, []

        async def hour(symbol):
            async with sem:
                try:
                    frame = await api.candles(symbol, start - 3 * DAY, end, "1h",
                                              folder / "hourly" / f"{symbol}.parquet")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 400 and exc.response.json().get("code") in {-1121, -1122}:
                        unavailable.append(symbol)
                        return
                    raise
                if frame.empty:
                    unavailable.append(symbol)
                else:
                    hourly[symbol] = frame

        if stage in {"hourly", "all"}:
            tasks = [asyncio.create_task(hour(s)) for s in metadata]
            for i, task in enumerate(asyncio.as_completed(tasks), 1):
                await task
                if i % 50 == 0:
                    print(f"hourly universe {i}/{len(tasks)}", flush=True)
            candidates = sorted(bound_candidates(hourly, metadata, start, end))
            manifest = {"start_ms": start, "end_ms": end, "warmup_start_ms": start - 3 * DAY,
                        "metadata_contracts": len(metadata), "unavailable": sorted(unavailable),
                        "candidate_symbols": candidates,
                        "historical_delisted_membership_proven": False,
                        "pool_pruning": "23h lower/25h upper bounds; final membership uses closed 5m volume"}
            (folder / "input-manifest.json").write_text(json.dumps(manifest, indent=2))
            print(f"candidates {len(candidates)}, unavailable {len(unavailable)}", flush=True)
        if stage == "hourly":
            return
        manifest = json.loads((folder / "input-manifest.json").read_text())
        symbols = [*manifest["candidate_symbols"], "BTCUSDT"]

        download_sem = asyncio.Semaphore(16)

        async def five(symbol):
            async with download_sem:
                frame = await api.archive(symbol, start - 3 * DAY, end,
                                          folder / "candles" / f"{symbol}.parquet")
                if frame.empty:
                    raise ValueError(f"candidate has no public history: {symbol}")
                print(f"5m {symbol}: {len(frame)}", flush=True)

        await asyncio.gather(*(five(s) for s in symbols))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--stage", choices=["hourly", "candles", "all"], default="all")
    args = p.parse_args()
    start, end = (int(pd.Timestamp(v, tz="UTC").timestamp() * 1000) for v in (args.start, args.end))
    if start % DAY or end % DAY or end <= start:
        p.error("increasing UTC daily boundaries required")
    asyncio.run(collect(args.folder, start, end, args.stage))
