"""Single fixed BTC/ETH hedge hypothesis, after 22 other redesign configurations.

No coin ranking, signal hunting or rotation. This retrospective check cannot
certify future returns, and the capital cannot be treated as risk-free cash.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from paperfrog.research.redesign import DAY, Candidate, replay
from scripts.study_redesign import load_history, summary

SPEC = Candidate("static_carry", 30, 360)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--earlier", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = {
        "candidate": asdict(SPEC),
        "research_attempt": 23,
        "mechanism": "Fully paid BTC/ETH spot plus equal coin quantities short perpetual futures; collect variable funding. No direction prediction.",
        "allocation": "18% equity spot per asset, 27% reserved futures margin per asset, 10% residual cash. Maximum 36% spot and 36% short-futures initial exposure.",
        "entry": "Once at period start, only if prior30-day settled funding positive, adequate prior volume and acceptable spot/futures basis. No reentry or rotation.",
        "exit": "End of declared horizon, 3% hedge-notional net basis loss or conservative futures margin-buffer breach. Brief negative funding does not close the hedge.",
        "tests": "Both 360-day periods, four cost/delay scenarios each; also six separate 120-day blocks to expose entry-date sensitivity.",
        "caveat": "Chosen after earlier carry/trend failures, both periods already viewed for other strategies; retrospective hypothesis, not independent validation.",
        "source": "https://www.bis.org/publications/working-paper-1087-crypto-carry",
    }
    args.output.with_suffix(".plan.json").write_text(json.dumps(plan, indent=2))
    results, paths = {}, {}
    for period, folder in (("current", args.current), ("earlier", args.earlier)):
        h = load_history(folder)
        start, end = h.end - 360 * DAY, h.end
        results[period] = {}
        for label, cost, delay in (
            ("base", 1, 0),
            ("double_cost", 2, 0),
            ("one_hour_delay", 1, 1),
            ("combined", 2, 1),
        ):
            r = replay(h, SPEC, start, end, cost_multiplier=cost, delay_hours=delay)
            results[period][label] = summary(r)
            paths[period + ":" + label] = r
            print(json.dumps({"period": period, label: summary(r)}), flush=True)
        results[period]["blocks"] = [
            summary(replay(h, SPEC, a, a + 120 * DAY)) for a in range(start, end, 120 * DAY)
        ]
    args.output.with_suffix(".paths.json").write_text(json.dumps(paths, indent=2, allow_nan=False))
    args.output.write_text(
        json.dumps({"plan": plan, "results": results}, indent=2, allow_nan=False)
    )


if __name__ == "__main__":
    main()
