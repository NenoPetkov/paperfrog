"""Fixed-scenario 90-day momentum study with public historical funding.

Selection uses the first two 30-day periods. The final 30 days audit the frozen
choice. Parts of that final period were seen in the prior study: this is further
retrospective research, not a pristine out-of-sample claim or profit guarantee.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from paperfrog.settings import Settings
from paperfrog.simple.learning_replay import FIVE, SYMBOLS, prepare
from paperfrog.simple.minute import validate_history, validate_ohlc
from paperfrog.simple.momentum import PROFILES, classify
from paperfrog.simple.strategy import RULES
from scripts.study_holding_momentum import DAY, FEE, portfolio

FRICTION = 0.00035  # 1.5bps half-spread + 2bps slippage; observed p90 full spread 2.64bps.
VARIANTS = [(profile, hold) for profile in sorted(PROFILES) for hold in (1, 2, 4, 8)]

FIELDS = (
    "timestamp_ms",
    "entry_timeframe",
    "open",
    "close",
    "fast",
    "slow",
    "fast_change",
    "trend_atr",
    "previous_trend_atr",
    "minute_atr",
    "atr",
    "atr_fraction",
    "flow",
    "return_5m",
    "return_15m",
    "return_25m",
    "return_1h",
    "path_efficiency_1h",
    "prior_high",
    "prior_low",
    "previous_close",
    "previous_fast",
    "previous_flow",
    "volume_ratio",
    "daily_volume",
    "day_context_ms",
    "day_return_4h",
    "day_return_1h",
    "day_slope_4h",
    "day_mean_distance_4h",
)


class TimeRows:
    """Array-backed timestamp lookup; avoid millions of duplicated Python dicts."""

    def __init__(self, frame, key):
        self.data = frame.to_records(index=False)
        self.names = tuple(frame.columns)
        self.start = int(frame[key].iloc[0])
        self.extra = {}

    def __getitem__(self, timestamp):
        i, remainder = divmod(timestamp - self.start, FIVE)
        if remainder or not 0 <= i < len(self.data):
            raise KeyError(timestamp)
        record = self.data[i]
        if self.extra and not self.extra["momentum_side"][i]:
            row = {
                "momentum_side": 0,
                "trend_atr": float(record["trend_atr"]),
                "previous_trend_atr": float(record["previous_trend_atr"]),
                "atr": float(record["atr"]),
            }
            if "rsi_14" in self.names:
                row["rsi_14"] = float(record["rsi_14"])
            return row
        row = {
            name: value.item() if isinstance(value, np.generic) else value
            for name, value in zip(self.names, record, strict=True)
        }
        row.update({key: value[i].item() for key, value in self.extra.items()})
        return row

    def get(self, timestamp, default=None):
        try:
            return self[timestamp]
        except KeyError:
            return default

    def signals(self, rules):
        from copy import copy

        result = copy(self)
        sides, scores = np.zeros(len(self.data), dtype=int), np.zeros(len(self.data))
        for i in range(len(self.data)):
            _, sides[i], scores[i] = classify(self[self.start + i * FIVE], FEE + FRICTION, rules)
        result.extra = {"momentum_side": sides, "momentum_score": scores}
        return result


async def fetch_funding(folder: Path, start: int, end: int) -> None:
    async with httpx.AsyncClient(base_url="https://fapi.binance.com", timeout=30) as client:
        for symbol in SYMBOLS:
            path = folder / f"{symbol}.funding.json"
            if path.exists():
                cached = json.loads(path.read_text())
                if cached["start_ms"] <= start and cached["end_ms"] >= end:
                    continue
            records, cursor = [], start
            while cursor < end:
                response = await client.get(
                    "/fapi/v1/fundingRate",
                    params={
                        "symbol": symbol,
                        "startTime": cursor,
                        "endTime": end - 1,
                        "limit": 1000,
                    },
                )
                response.raise_for_status()
                rows = response.json()
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
            if (
                not records
                or any(
                    not math.isfinite(r["funding_rate"])
                    or not math.isfinite(r["mark"])
                    or r["mark"] <= 0
                    for r in records
                )
                or len({r["timestamp_ms"] for r in records}) != len(records)
                or records[0]["timestamp_ms"] > start + DAY
                or records[-1]["timestamp_ms"] < end - DAY
                or any(
                    b["timestamp_ms"] - a["timestamp_ms"] > DAY
                    for a, b in zip(records, records[1:], strict=False)
                )
            ):
                raise ValueError("funding history incomplete or invalid")
            path.write_text(json.dumps({"start_ms": start, "end_ms": end, "records": records}))
            print(f"{symbol}: {len(records)} public funding events", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    end = 1794096000000  # 2026-11-08 00:00 UTC; frozen before looking at results.
    start = end - 90 * DAY
    periods = [
        (start, start + 30 * DAY),
        (start + 30 * DAY, start + 60 * DAY),
        (start + 60 * DAY, end),
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".plan.json").write_text(
        json.dumps(
            {
                "variants": VARIANTS,
                "periods": periods,
                "fee_rate": FEE,
                "friction": FRICTION,
                "funding": "historical exchange events",
                "selection": "maximize worse return of first two periods; >=20 trades in each",
                "max_positions": 2,
                "daily_entry_cap": 12,
                "per_coin_cap": 2,
            },
            indent=2,
        )
    )
    asyncio.run(fetch_funding(args.folder, start, end))
    s = Settings(
        _env_file=None,
        **{
            name: field.get_default(call_default_factory=True)
            for name, field in Settings.model_fields.items()
        },
    )
    prepared, tapes, funding = {}, {}, {}
    for symbol in SYMBOLS:
        raw = pd.read_parquet(args.folder / f"{symbol}.parquet")
        validate_history(raw, end, FIVE, 93 * DAY // FIVE)
        validate_ohlc(raw)
        prepared[symbol] = TimeRows(prepare(raw, s)["5m"][list(FIELDS)], "timestamp_ms")
        tapes[symbol] = TimeRows(raw[["open_ms", "open", "high", "low", "close"]], "open_ms")
        funding[symbol] = json.loads((args.folder / f"{symbol}.funding.json").read_text())[
            "records"
        ]
        print(f"Prepared {symbol}", flush=True)
    results, feature_sets = [], {}
    options = {
        "friction": FRICTION,
        "funding": funding,
        "max_positions": 2,
        "daily_cap": 12,
        "symbol_cap": 2,
    }
    for profile in sorted(PROFILES):
        rules = replace(RULES, version=f"abc-{profile}-v1", entry_style="trend_follow")
        features = {symbol: frame.signals(rules) for symbol, frame in prepared.items()}
        feature_sets[profile] = features
        for hold in (1, 2, 4, 8):
            # Compute only first two periods until the selection is frozen.
            r = {
                "profile": profile,
                "hold_hours": hold,
                "selection_periods": [
                    portfolio(tapes, features, a, b, hold, "confirmed", **options)
                    for a, b in periods[:2]
                ],
            }
            results.append(r)
            print(json.dumps(r), flush=True)
    adequate = [r for r in results if all(p["trades"] >= 20 for p in r["selection_periods"])]
    if not adequate:
        raise ValueError("no candidate has enough closed trades to compare")
    selected = max(adequate, key=lambda r: min(p["return"] for p in r["selection_periods"]))
    selected_key = (selected["profile"], selected["hold_hours"])
    args.output.with_suffix(".selection.json").write_text(
        json.dumps({"selected": selected, "selection_results": results}, indent=2)
    )
    for r in results:
        r["audit"] = portfolio(
            tapes, feature_sets[r["profile"]], *periods[2], r["hold_hours"], "confirmed", **options
        )
    frozen = next(r for r in results if (r["profile"], r["hold_hours"]) == selected_key)
    sensitivity = {
        "double_cost": portfolio(
            tapes,
            feature_sets[selected_key[0]],
            *periods[2],
            selected_key[1],
            "confirmed",
            stress=2,
            **options,
        ),
        "four_x": portfolio(
            tapes,
            feature_sets[selected_key[0]],
            *periods[2],
            selected_key[1],
            "confirmed",
            leverage=4,
            **options,
        ),
    }
    report = {
        "periods": periods,
        "variants": results,
        "frozen_selection": frozen,
        "sensitivity": sensitivity,
        "history_hashes": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(args.folder.iterdir())
            if p.suffix in {".json", ".parquet"}
        },
        "positive_selection_periods": sum(
            all(p["return"] > 0 for p in r["selection_periods"]) for r in results
        ),
        "positive_audit_variants": sum(r["audit"]["return"] > 0 for r in results),
        "caveats": [
            "Fixed surviving 19-coin universe; not historical top-100 membership.",
            "Further retrospective study after viewing the earlier 21-day experiment.",
            "Next-open fills, entry-bar stops, no positions across splits; conservative intrabar drawdown.",
            "Historical funding marks; entry-instant funding excluded, exit-instant funding included.",
            "Fee 5bps/side, friction 3.5bps/side; stress doubles fees/friction/funding debits, not credits.",
            "Margin 10% per position, 2x, two positions, twelve entries/day and two per coin/day.",
            "No liquidation, historical order-book or full live loss-lock replay; returns are screening estimates.",
            "This study does not activate live trading or establish future profitability.",
        ],
    }
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({"selected": frozen, "sensitivity": sensitivity}), flush=True)


if __name__ == "__main__":
    main()
