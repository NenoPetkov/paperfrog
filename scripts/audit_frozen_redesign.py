"""Frozen post-selection channel strategy: continuous year and unseen older data.

The current year was used to choose this candidate. Its positive return, if any,
is a discovery result, not untouched validation. Earlier data are a separate
retrospective robustness check, not a future forward test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from paperfrog.research.redesign import DAY, FRICTION, FUTURE_FEE, Candidate, replay
from scripts.study_redesign import bootstrap, load_history, summary

FROZEN = Candidate("channel", 55, 20, 3.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.with_suffix(".plan.json").write_text(
        json.dumps(
            {
                "candidate": asdict(FROZEN),
                "status": "frozen after viewing 22 redesign configurations",
                "reason": "Positive combined split performance and strongest final-period directional result. This is post-selection research.",
                "tests": "Continuous 360 days plus three 120-day blocks; base, double cost, delayed execution, combined stress; no further parameter search.",
            },
            indent=2,
        )
    )
    h = load_history(args.folder)
    start, end = h.end - 360 * DAY, h.end
    results, paths = {}, {}
    for label, cost, delay in (
        ("base", 1, 0),
        ("double_cost", 2, 0),
        ("one_hour_delay", 1, 1),
        ("combined", 2, 1),
    ):
        r = replay(h, FROZEN, start, end, cost_multiplier=cost, delay_hours=delay)
        paths[label] = r
        results[label] = summary(r)
        print(json.dumps({label: results[label]}), flush=True)
    blocks = [summary(replay(h, FROZEN, a, a + 120 * DAY)) for a in range(start, end, 120 * DAY)]
    i = (start - h.start) // 3_600_000
    # Fully paid spot benchmarks, 10bps commission + 3.5bps friction per side.
    spot_roundtrip = (1 - 0.001 - FRICTION) / (1 + 0.001 + FRICTION)
    passive = h.spot["close"][-1] / h.spot["open"][i] * spot_roundtrip - 1
    results["bootstrap"] = bootstrap(paths["base"])
    args.output.with_suffix(".paths.json").write_text(json.dumps(paths, indent=2, allow_nan=False))
    args.output.write_text(
        json.dumps(
            {
                "frozen": asdict(FROZEN),
                "period": [start, end],
                "results": results,
                "blocks": blocks,
                "spot_buy_hold_benchmarks": {
                    "BTC": float(passive[h.symbols.index("BTCUSDT")]),
                    "equal_weight_ten": float(np.mean(passive)),
                },
                "cost_assumptions": {
                    "futures_fee_per_side": FUTURE_FEE,
                    "friction_per_side": FRICTION,
                },
                "history_sha256": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(args.folder.iterdir())
                    if p.suffix in {".parquet", ".json"}
                },
                "limitations": [
                    "Current-year candidate chosen after viewing its final block; not untouched validation.",
                    "Earlier-year check is untouched for this new candidate but runs backward in calendar time; not forward evidence.",
                    "Fixed current surviving universe, simplified fills, hourly stop bars, no exchange-specific liquidation simulation.",
                ],
            },
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
