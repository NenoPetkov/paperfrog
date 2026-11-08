"""Predeclared daily portfolio comparison, independent of the old trading rules."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from paperfrog.research.redesign import CANDIDATES, DAY, History, replay

END = 1794096000000
PERIODS = [(END - n * DAY, END - (n - 120) * DAY) for n in (360, 240, 120)]
SOURCES = {
    "carry": "https://www.bis.org/publications/working-paper-1087-crypto-carry",
    "perpetual_carry_mechanics": "https://www.binance.com/en-AE/support/faq/detail/f330e17d6fc04679b9b21d6f9350e787",
    "trend": "https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing",
    "relative_reversal": "https://digitalcommons.fairfield.edu/business-facultypubs/246/",
    "relative_momentum": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3913263",
}


def load_history(folder: Path) -> History:
    symbols = sorted(p.name.split(".")[0] for p in folder.glob("*.futures.parquet"))
    if len(symbols) != 10:
        raise ValueError("exactly ten fixed research coins required")
    futures = {s: pd.read_parquet(folder / f"{s}.futures.parquet") for s in symbols}
    spot = {s: pd.read_parquet(folder / f"{s}.spot.parquet") for s in symbols}
    funding = {s: json.loads((folder / f"{s}.funding.json").read_text()) for s in symbols}
    earliest = int(next(iter(futures.values())).open_ms.iloc[0])
    latest = int(next(iter(futures.values())).open_ms.iloc[-1]) + 3_600_000
    for records in funding.values():
        if (
            not records
            or records[0]["timestamp_ms"] > earliest + DAY
            or records[-1]["timestamp_ms"] < latest - DAY
            or any(
                b["timestamp_ms"] - a["timestamp_ms"] > DAY
                for a, b in zip(records, records[1:], strict=False)
            )
        ):
            raise ValueError("incomplete historical funding")
    return History(futures, spot, funding)


def summary(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in {"trade_records", "daily_equity"}}


def bootstrap(result: dict) -> dict:
    """Descriptive circular seven-day block bootstrap; not multiplicity-adjusted."""
    equity = np.array([10000, *result["daily_equity"]])
    returns = equity[1:] / equity[:-1] - 1
    rng = np.random.default_rng(20261108)
    starts = rng.integers(0, len(returns), size=(4000, (len(returns) + 6) // 7))
    indices = (starts[:, :, None] + np.arange(7)) % len(returns)
    samples = returns[indices.reshape(4000, -1)[:, : len(returns)]]
    outcomes = np.prod(1 + samples, axis=1) - 1
    low, high = np.quantile(outcomes, [0.025, 0.975])
    return {
        "period_return_95_interval": [float(low), float(high)],
        "positive_fraction": float(np.mean(outcomes > 0)),
        "note": "Descriptive, dependent on this sample, not corrected for strategy search.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    plan = {
        "candidates": [asdict(c) for c in CANDIDATES],
        "periods": PERIODS,
        "sources": SOURCES,
        "selection": "Maximum worse return of first two periods; at least six trades per period. Positive in both required to pass discovery.",
        "stress": "Frozen candidate only: double all fees/friction and funding debits, one-hour delayed entries, and combined stress.",
        "limits": "Futures 0.8x initial gross equity exposure; carry three equal-quantity hedges, 12% equity spot each plus 18% reserved futures margin each.",
        "caveat": "Fixed surviving universe. Final period overlaps previously viewed data. Literature motivates families, not these precise parameters or guaranteed results.",
    }
    args.output.with_suffix(".plan.json").write_text(json.dumps(plan, indent=2))
    history = load_history(args.folder)
    results = []
    for candidate in CANDIDATES:
        periods = [replay(history, candidate, start, end) for start, end in PERIODS[:2]]
        row = {
            "key": candidate.key,
            "candidate": asdict(candidate),
            "selection_periods": [summary(r) for r in periods],
        }
        results.append(row)
        print(json.dumps(row), flush=True)
    eligible = [r for r in results if all(p["trades"] >= 6 for p in r["selection_periods"])]
    selected = (
        max(eligible, key=lambda r: min(p["return"] for p in r["selection_periods"]))
        if eligible
        else None
    )
    frozen = {
        "selected_key": selected["key"] if selected else None,
        "passes_discovery": bool(
            selected and all(p["return"] > 0 for p in selected["selection_periods"])
        ),
        "selection_results": results,
    }
    args.output.with_suffix(".selection.json").write_text(json.dumps(frozen, indent=2))
    audit_details = {}
    for candidate, row in zip(CANDIDATES, results, strict=True):
        result = replay(history, candidate, *PERIODS[2])
        row["audit"] = summary(result)
        audit_details[candidate.key] = result
        print(json.dumps({"audit": row}), flush=True)
    sensitivity = {}
    if selected:
        candidate = next(c for c in CANDIDATES if c.key == selected["key"])
        for name, multiplier, delay in (
            ("double_cost", 2, 0),
            ("one_hour_delay", 1, 1),
            ("combined", 2, 1),
        ):
            sensitivity[name] = summary(
                replay(
                    history, candidate, *PERIODS[2], cost_multiplier=multiplier, delay_hours=delay
                )
            )
        sensitivity["bootstrap"] = bootstrap(audit_details[candidate.key])
    audit_file = args.output.with_suffix(".paths.json")
    audit_file.write_text(json.dumps(audit_details, indent=2, allow_nan=False))
    report = {
        "plan": plan,
        "variants": results,
        "selected_key": frozen["selected_key"],
        "passes_discovery": frozen["passes_discovery"],
        "sensitivity": sensitivity,
        "history_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(args.folder.iterdir())
            if p.suffix in {".json", ".parquet"}
        },
        "limitations": [
            "No live orders or automatic promotion.",
            "Hourly fills approximate executable quotes and simultaneous hedge execution.",
            "Funding on a stop bar charged through its close; intrabar event order is unknown.",
            "No liquidation engine, exchange outages, tax or account-specific fee tier simulation.",
            "Carry uses fully paid spot, separate reserved margin, no spot borrowing, both legs' basis PnL and four fill costs.",
            "Relative portfolios close and reopen on scheduled rebalance, conservatively charging costs on unchanged legs.",
            "No automatic strategy switching or fitting until a positive number appears.",
            "Results cannot establish future profitability; forward testing is separate.",
        ],
    }
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False))
    print(
        json.dumps(
            {
                "selected": frozen["selected_key"],
                "discovery_pass": frozen["passes_discovery"],
                "sensitivity": sensitivity,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
