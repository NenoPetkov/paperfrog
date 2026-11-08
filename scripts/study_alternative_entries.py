"""Second registered family comparison after the failed momentum-only study.

Reuses the same chronological periods and costs, explicitly acknowledging reused
history. No search over coins, dates or trading hours. No live mutations.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from paperfrog.settings import Settings
from paperfrog.simple.learning_replay import FIVE, SYMBOLS, prepare
from paperfrog.simple.minute import direction, validate_history, validate_ohlc
from paperfrog.simple.momentum import classify
from paperfrog.simple.strategy import RULES
from scripts.study_holding_momentum import DAY, FEE, HOUR, portfolio
from scripts.study_momentum_live import FIELDS, FRICTION, TimeRows

FAMILIES = ("hour_cross", "hour_sample", "rsi_recovery", "rsi_exhaustion", "inverse_diagnostic")
VARIANTS = [(family, hold) for family in FAMILIES for hold in (1, 4, 8)]


def hourly(raw):
    grouped = raw.groupby((raw.timestamp_ms.astype("int64") - 1) // HOUR)
    hours = grouped.agg(timestamp_ms=("timestamp_ms", "last"), close=("close", "last"))
    hours = hours[(grouped.size() == 12) & (hours.timestamp_ms % HOUR == 0)].reset_index(drop=True)
    for name, span in (("hour_fast", 8), ("hour_slow", 24)):
        weights = (1 - 2 / (span + 1)) ** np.arange(47, -1, -1)
        weights /= weights.sum()
        hours[name] = hours.close.rolling(48).apply(
            lambda p, w=weights: float(np.dot(p, w)), raw=True
        )
    hours["hour_trend"] = hours.hour_fast - hours.hour_slow
    hours["previous_hour_trend"] = hours.hour_trend.shift(1)
    hours["hour_return"] = hours.close.pct_change()
    return hours[["timestamp_ms", "hour_trend", "previous_hour_trend", "hour_return"]]


def signal(row, family):
    keys = ("atr_fraction", "flow", "open", "close", "minute_atr", "rsi_14", "previous_rsi_14")
    if (
        not all(math.isfinite(float(row[k])) for k in keys)
        or not RULES.min_atr_fraction <= row["atr_fraction"] <= RULES.max_atr_fraction
        or min(0.02, 2 * row["atr_fraction"]) <= 2 * (FEE + FRICTION) + 0.0035
    ):
        return 0, 0.0
    side = 0
    if family == "hour_cross":
        if int(row["timestamp_ms"]) % HOUR or not all(
            math.isfinite(float(row[k]))
            for k in ("hour_trend", "previous_hour_trend", "hour_return")
        ):
            return 0, 0.0
        side = (
            1
            if row["hour_trend"] > 0 >= row["previous_hour_trend"]
            else -1
            if row["hour_trend"] < 0 <= row["previous_hour_trend"]
            else 0
        )
        if side * row["hour_return"] <= 0 or direction(row, RULES)[0] != side:
            return 0, 0.0
        return side, abs(row["hour_return"]) / row["atr_fraction"]
    if family in {"hour_sample", "inverse_diagnostic"}:
        _, side, score = classify(
            row, FEE + FRICTION, replace(RULES, version="abc-momentum_selective-v1")
        )
        if family == "hour_sample":
            return (side, score) if int(row["timestamp_ms"]) % HOUR == 0 else (0, 0.0)
        return -side, score
    previous, current = row["previous_rsi_14"], row["rsi_14"]
    if family == "rsi_recovery":
        side = 1 if previous < 30 <= current < 50 else -1 if previous > 70 >= current > 50 else 0
    elif family == "rsi_exhaustion":
        side = (
            1
            if previous < 25 and previous < current < 40
            else -1
            if previous > 75 and previous > current > 60
            else 0
        )
    if (
        not side
        or side * (row["close"] - row["open"]) <= 0
        or side * row["flow"] < RULES.minimum_flow
    ):
        return 0, 0.0
    return side, abs(current - previous) * abs(previous - 50)


def schedule(features, start, end):
    result = {}
    for symbol, rows in features.items():
        indices = np.flatnonzero(rows.extra["momentum_side"])
        for i in indices:
            ts = rows.start + int(i) * FIVE
            if start <= ts < end:
                row = rows[ts]
                if row["daily_volume"] >= 10_000_000:
                    result.setdefault(ts, []).append((row["momentum_score"], symbol, row))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    end = 1794096000000
    periods = [(end - n * DAY, end - (n - 30) * DAY) for n in (90, 60, 30)]
    args.output.with_suffix(".plan.json").write_text(
        json.dumps(
            {
                "variants": VARIANTS,
                "periods": periods,
                "selection": "best worst return across earlier two periods; at least 12 trades each; inverse is diagnostic only",
            },
            indent=2,
        )
    )
    s = Settings(
        _env_file=None,
        **{k: f.get_default(call_default_factory=True) for k, f in Settings.model_fields.items()},
    )
    prepared, tapes, funding = {}, {}, {}
    for symbol in SYMBOLS:
        raw = pd.read_parquet(args.folder / f"{symbol}.parquet")
        validate_history(raw, end, FIVE, 93 * DAY // FIVE)
        validate_ohlc(raw)
        fast = prepare(raw, s)["5m"]
        fast["previous_rsi_14"] = fast.rsi_14.shift(1)
        fast = pd.merge_asof(fast, hourly(raw), on="timestamp_ms", direction="backward")
        prepared[symbol] = TimeRows(
            fast[
                [
                    *FIELDS,
                    "rsi_14",
                    "previous_rsi_14",
                    "hour_trend",
                    "previous_hour_trend",
                    "hour_return",
                ]
            ],
            "timestamp_ms",
        )
        tapes[symbol] = TimeRows(raw[["open_ms", "open", "high", "low", "close"]], "open_ms")
        funding[symbol] = json.loads((args.folder / f"{symbol}.funding.json").read_text())[
            "records"
        ]
    feature_sets, results = {}, []
    options = {
        "friction": FRICTION,
        "funding": funding,
        "max_positions": 2,
        "daily_cap": 12,
        "symbol_cap": 2,
    }
    for family in FAMILIES:
        from copy import copy

        features = {}
        for symbol, rows in prepared.items():
            target = copy(rows)
            sides, scores = np.zeros(len(rows.data), dtype=int), np.zeros(len(rows.data))
            for i in range(len(rows.data)):
                sides[i], scores[i] = signal(rows[rows.start + i * FIVE], family)
            target.extra = {"momentum_side": sides, "momentum_score": scores}
            features[symbol] = target
        feature_sets[family] = features
        for hold in (1, 4, 8):
            policy = "rsi" if family.startswith("rsi") else "stop_only"
            r = {
                "family": family,
                "hold_hours": hold,
                "policy": policy,
                "selection_periods": [
                    portfolio(
                        tapes,
                        features,
                        a,
                        b,
                        hold,
                        policy,
                        schedule=schedule(features, a, b),
                        **options,
                    )
                    for a, b in periods[:2]
                ],
            }
            results.append(r)
            print(json.dumps(r), flush=True)
    eligible = [
        r
        for r in results
        if r["family"] != "inverse_diagnostic"
        and all(p["trades"] >= 12 for p in r["selection_periods"])
    ]
    selected = (
        max(eligible, key=lambda r: min(p["return"] for p in r["selection_periods"]))
        if eligible
        else None
    )
    args.output.with_suffix(".selection.json").write_text(
        json.dumps({"frozen_selection": selected, "selection_results": results}, indent=2)
    )
    for r in results:
        features = feature_sets[r["family"]]
        a, b = periods[2]
        r["audit"] = portfolio(
            tapes,
            features,
            a,
            b,
            r["hold_hours"],
            r["policy"],
            schedule=schedule(features, a, b),
            **options,
        )
        print(json.dumps({"audit": r}), flush=True)
    stress = None
    if selected:
        features = feature_sets[selected["family"]]
        stress = portfolio(
            tapes,
            features,
            *periods[2],
            selected["hold_hours"],
            selected["policy"],
            schedule=schedule(features, *periods[2]),
            stress=2,
            **options,
        )
    args.output.write_text(
        json.dumps(
            {
                "periods": periods,
                "variants": results,
                "frozen_selection": selected,
                "double_cost": stress,
                "caveat": "Repeated retrospective family exploration after failed momentum experiments. No guaranteed live edge; no historical order-book or full live risk replay.",
            },
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
