"""Declared second-stage carry experiment after short-horizon screening failed.

Longer amortization follows carry economics, but this is additional exploration
on viewed history and not an independent validation claim.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from paperfrog.research.redesign import Candidate, replay
from scripts.study_redesign import PERIODS, bootstrap, load_history, summary

CANDIDATES = tuple(Candidate("carry", lookback, hold) for lookback in (7, 30) for hold in (60, 90))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = {
        "candidates": [asdict(c) for c in CANDIDATES],
        "periods": PERIODS,
        "selection": "Highest worse return of first two periods; at least three closed hedges in each. This small sample is exploratory only.",
        "reason": "Check whether 60-90 day funding income amortizes four execution costs better than 7-30 days.",
        "caveat": "Second-stage exploration after examining the first fourteen configurations; not pristine holdout.",
    }
    args.output.with_suffix(".plan.json").write_text(json.dumps(plan, indent=2))
    history = load_history(args.folder)
    results = []
    for c in CANDIDATES:
        row = {
            "key": c.key,
            "candidate": asdict(c),
            "selection_periods": [summary(replay(history, c, a, b)) for a, b in PERIODS[:2]],
        }
        results.append(row)
        print(json.dumps(row), flush=True)
    eligible = [r for r in results if all(p["trades"] >= 3 for p in r["selection_periods"])]
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
    for c, row in zip(CANDIDATES, results, strict=True):
        details[c.key] = replay(history, c, *PERIODS[2])
        row["audit"] = summary(details[c.key])
        print(json.dumps({"audit": row}), flush=True)
    sensitivity = {}
    if selected:
        c = next(c for c in CANDIDATES if c.key == selected["key"])
        for label, cost, delay in (
            ("double_cost", 2, 0),
            ("one_hour_delay", 1, 1),
            ("combined", 2, 1),
        ):
            sensitivity[label] = summary(
                replay(history, c, *PERIODS[2], cost_multiplier=cost, delay_hours=delay)
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
