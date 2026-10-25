"""Analyze a ledger snapshot and compare guarded variants using public candles.

Run against a SQLite backup, not the active ledger. No trading API mutation,
strategy activation, risk reset or account authorization is performed here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.settings import load_settings
from paperfrog.simple import learning, learning_replay
from paperfrog.simple.analysis import analysis
from paperfrog.storage.database import Store


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    s = load_settings()
    with sqlite3.connect(f"file:{args.ledger.resolve()}?mode=ro", uri=True) as conn:
        trades = json.loads(conn.execute("SELECT value FROM kv WHERE key='simple_closed'").fetchone()[0])
        end = conn.execute("SELECT max(ts) FROM audit").fetchone()[0]
    now = datetime.fromtimestamp(end / 1000, ZoneInfo(s.display_timezone))
    monday = now.date() - timedelta(days=now.weekday())
    previous = datetime.combine(monday - timedelta(days=7), datetime.min.time(), now.tzinfo)
    finish = datetime.combine(monday, datetime.min.time(), now.tzinfo)
    periods = {
        "previous_calendar_week": (int(previous.timestamp() * 1000), int(finish.timestamp() * 1000)),
        "last_seven_days": (end - learning.WINDOW, end),
        "since_last_review": (int(datetime(2026, 10, 24, 14, tzinfo=UTC).timestamp() * 1000), end),
    }
    report = {"asof": now.isoformat(), "currency": "inspect per-period currency groups",
              "periods": {name: {"start_ms": a, "end_ms": b,
                                  **analysis([t for t in trades if a <= t["timestamp_ms"] < b])}
                          for name, (a, b) in periods.items()}}
    args.output.mkdir(parents=True, exist_ok=True)
    if args.replay:
        store = Store(args.ledger)
        exchange = BinanceAdapter(s)
        try:
            status = await learning_replay.refresh(exchange, store, s, end)
            records = store.get(learning.EVIDENCE, [])
            patterns = sorted({r["pattern"] for r in records})
            report["shadow_replay"] = {
                "status": status, "variants_by_pattern": {p: learning.score(records, end, p) for p in patterns},
                "notes": ["Same fixed research universe and closed history for all variants.",
                          "Simulated next-open execution; open-time exits precede intrabar stop checks.",
                          "Fees, assumed spread/slippage and conservative funding reserve included.",
                          "Purged chronological holdout; four-hour blocks reduce correlated-coin sample inflation.",
                          "Portfolio constraints, exchange fills and real-time BTC gates are not replayed.",
                          "No evidence for periods lacking recorded broad-market context.",
                          "This is screening research, not realized or guaranteed future profit."],
            }
            (args.output / "learning-evidence.json").write_text(json.dumps({"status": status, "outcomes": records}, allow_nan=False))
        finally:
            await exchange.close()
            store.engine.dispose()
    (args.output / "analysis.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({"report": str(args.output / "analysis.json"),
                      "periods": {k: v["summary"]["trades"] for k, v in report["periods"].items()},
                      "research": report.get("shadow_replay", {}).get("status")}))


if __name__ == "__main__":
    asyncio.run(main())
