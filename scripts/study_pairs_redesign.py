"""Declared third family expansion: monthly formed relative-value pairs."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from paperfrog.research.pairs import SPECS, replay_pairs
from scripts.study_redesign import PERIODS, bootstrap, load_history, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = {
        "candidates": [asdict(c) for c in SPECS],
        "periods": PERIODS,
        "sources": [
            "https://www.nber.org/papers/w7032",
            "https://ieeexplore.ieee.org/document/9200323/",
        ],
        "selection": "Maximum worse first-two-period return, at least twelve closed pairs in each; positive in both needed.",
        "formation": "Monthly, past 30/60 completed days; log price ratio distance, daily return correlation >=0.5 and estimated mean-reversion half-life 1-20 days. Five closest eligible pairs.",
        "trading": "Entry every six hours at 2/2.5 deviations; exit at 0.5 or zero crossing; divergence stop 4; max14 days; 4% combined gross-notional loss limit; max2 nonoverlapping pairs and 0.8x initial portfolio gross exposure.",
        "caveat": "Exploratory extension after failed preceding families; not cointegration certification or untouched holdout. Intrabar stops use simultaneous adverse extremes conservatively. Two-leg execution is approximate.",
    }
    args.output.with_suffix(".plan.json").write_text(json.dumps(plan, indent=2))
    history = load_history(args.folder)
    results = []
    for c in SPECS:
        row = {
            "key": c.key,
            "candidate": asdict(c),
            "selection_periods": [summary(replay_pairs(history, c, a, b)) for a, b in PERIODS[:2]],
        }
        results.append(row)
        print(json.dumps(row), flush=True)
    eligible = [r for r in results if all(p["trades"] >= 12 for p in r["selection_periods"])]
    selected = (
        max(eligible, key=lambda r: min(p["return"] for p in r["selection_periods"]))
        if eligible
        else None
    )
    args.output.with_suffix(".selection.json").write_text(
        json.dumps(
            {"selected_key": selected["key"] if selected else None, "results": results}, indent=2
        )
    )
    details = {}
    for c, row in zip(SPECS, results, strict=True):
        details[c.key] = replay_pairs(history, c, *PERIODS[2])
        row["audit"] = summary(details[c.key])
        print(json.dumps({"audit": row}), flush=True)
    sensitivity = {}
    if selected:
        c = next(c for c in SPECS if c.key == selected["key"])
        for label, cost, delay in (
            ("double_cost", 2, 0),
            ("one_hour_delay", 1, 1),
            ("combined", 2, 1),
        ):
            sensitivity[label] = summary(
                replay_pairs(history, c, *PERIODS[2], cost_multiplier=cost, delay_hours=delay)
            )
        sensitivity["bootstrap"] = bootstrap(details[c.key])
    args.output.with_suffix(".paths.json").write_text(
        json.dumps(details, indent=2, allow_nan=False)
    )
    args.output.write_text(
        json.dumps(
            {
                "plan": plan,
                "variants": results,
                "selected_key": selected["key"] if selected else None,
                "sensitivity": sensitivity,
            },
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
