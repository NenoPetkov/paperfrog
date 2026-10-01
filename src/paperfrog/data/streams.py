from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pandas as pd

from paperfrog.data.lake import Lake
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.storage.database import Store


async def collect_stream(
    adapter: BinanceAdapter, lake: Lake, store: Store, symbol: str, suffix: str
) -> None:
    """Immutable event batches; sequence diagnostics, no invented historical book reconstruction."""
    buffer = []
    last_flush = time.monotonic()
    async for event in adapter.stream(symbol.lower() + "@" + suffix):
        if event.get("e") == "disconnect":
            store.audit("market_stream_disconnected", {"symbol": symbol, "stream": suffix})
            continue
        payload = json.dumps(event, sort_keys=True)
        identity = hashlib.sha256(payload.encode()).hexdigest()
        timestamp = int(event.get("E", event.get("T", time.time() * 1000)))
        buffer.append({"timestamp_ms": timestamp, "event_id": identity, "payload": payload})
        if "u" in event:
            previous = store.get("sequence:" + symbol + ":" + suffix)
            if previous is not None and event.get("pu") is not None and event["pu"] != previous:
                store.audit(
                    "depth_sequence_gap",
                    {"symbol": symbol, "previous": previous, "current": event["u"]},
                )
            store.put("sequence:" + symbol + ":" + suffix, event["u"])
        if len(buffer) >= 500 or time.monotonic() - last_flush >= 30:
            kind = "stream_" + suffix.lower().replace("@", "_")
            lake.append(kind, symbol, pd.DataFrame(buffer), "binance_websocket")
            buffer.clear()
            last_flush = time.monotonic()
            store.put("market_stream_at:" + symbol, time.time())


async def collect_all(
    adapter: BinanceAdapter, lake: Lake, store: Store, symbols: tuple[str, ...]
) -> None:
    async with asyncio.TaskGroup() as group:
        for symbol in symbols:
            for suffix in ("aggTrade", "markPrice@1s", "forceOrder", "depth20@100ms"):
                group.create_task(collect_stream(adapter, lake, store, symbol, suffix))
