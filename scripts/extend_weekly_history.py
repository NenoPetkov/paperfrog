"""Fetch public candle warm-up before the original recent study, without credentials/orders."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.simple.data import candles
from paperfrog.simple.research_budget import OfflineResearchSettings
from paperfrog.simple.strategy import BAR_MS


async def collect() -> None:
    output = Path("reports/weekly-warmup-collection-v1.json")
    if output.exists():
        raise SafeError("warm-up collection report already exists")
    original = json.loads(Path("reports/simple-collection-30d.json").read_text())
    manifest = json.loads(Path("reports/simple-pool-30d/manifest.json").read_text())
    lake = Lake(Path("data/simple"))
    required_start = manifest["start_ms"] - 3072 * BAR_MS + BAR_MS
    adapter = BinanceAdapter(OfflineResearchSettings.model_validate({}))
    semaphore = asyncio.Semaphore(3)

    async def one(symbol: str) -> tuple[str, bool]:
        async with semaphore:
            try:
                raw = lake.read("perp_15m", symbol)
                earliest = int(raw.timestamp_ms.min())
                if earliest > required_start:
                    await candles(adapter, lake, symbol, required_start - BAR_MS, earliest - BAR_MS)
                frame = lake.read("perp_15m", symbol)
                frame = frame[frame.timestamp_ms.between(required_start, manifest["end_ms"])]
                expected = (manifest["end_ms"] - manifest["start_ms"]) // BAR_MS + 3072
                if len(frame) != expected or (frame.timestamp_ms.diff().dropna() != BAR_MS).any():
                    raise SafeError("extended warm-up coverage incomplete")
                print(json.dumps({"warmup_complete": symbol}), flush=True)
                return symbol, True
            except (SafeError, ValueError, KeyError):
                print(json.dumps({"warmup_unavailable": symbol}), flush=True)
                return symbol, False

    try:
        results = await asyncio.gather(*(one(symbol) for symbol in original["usable_symbols"]))
    finally:
        await adapter.close()
    report = {
        "original_collection": "reports/simple-collection-30d.json",
        "requested_symbols": original["usable_symbols"],
        "usable_symbols": [symbol for symbol, valid in results if valid],
        "excluded_incomplete_warmup": [symbol for symbol, valid in results if not valid],
        "warmup_bars": 3072,
        "start_ms": manifest["start_ms"],
        "end_ms": manifest["end_ms"],
        "selection_uses_data_coverage_only": True,
        "historical_membership_proven": False,
        "live_permitted": False,
        "runtime_state_modified": False,
    }
    with output.open("x") as file:
        file.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps({"requested": len(results), "usable": len(report["usable_symbols"])}), flush=True
    )


if __name__ == "__main__":
    try:
        asyncio.run(collect())
    except SafeError as exc:
        raise SystemExit(str(exc)) from None
