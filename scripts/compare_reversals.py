"""Bounded follow-up to the two retained losing studies; no exchange connections/orders."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError
from paperfrog.settings import load_settings
from paperfrog.simple.backtest import evaluate, load_history, replay
from paperfrog.simple.strategy import require_paper


def digest(frame: pd.DataFrame) -> str:
    return hashlib.sha256(
        pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes()
    ).hexdigest()


async def compare(mode: str, root: Path) -> None:
    s = load_settings()
    require_paper(s)
    protocol_path = root / "protocol.json"
    if not protocol_path.exists():
        raise SafeError("write the frozen comparison protocol before starting")
    protocol = json.loads(protocol_path.read_text())
    if mode not in protocol["candidates"]:
        raise SafeError("candidate outside the frozen comparison")
    output = root / mode
    if output.exists():
        raise SafeError("use an immutable new candidate output directory")
    output.mkdir(parents=True)
    candidate = s.model_copy(update={"simple_entry_mode": mode})
    original = s.model_copy(update={"simple_entry_mode": "trend"})
    collection = json.loads(Path("reports/simple-collection-30d.json").read_text())
    samples = (
        (
            "recent_97_coin",
            s.data_dir / "simple",
            collection["usable_symbols"],
            Path("reports/simple-pool-30d"),
        ),
        (
            "two_year_three_coin",
            s.data_dir,
            ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            Path("reports/simple-trend-v1-verified"),
        ),
    )
    comparisons: dict[str, Any] = {}
    for name, lake_path, symbols, reference in samples:
        reference_manifest = json.loads((reference / "manifest.json").read_text())
        start_ms, end_ms = reference_manifest["start_ms"], reference_manifest["end_ms"]
        frames, funding = load_history(Lake(lake_path), symbols, start_ms, end_ms)
        # Newly derived rejection columns cannot change the original observations/features.
        for symbol in symbols:
            legacy = frames[symbol].drop(
                columns=["failed_up_breakout", "failed_down_breakout", "rejection_level"]
            )
            if (
                digest(legacy) != reference_manifest["input_hashes"][symbol]
                or digest(funding[symbol]) != reference_manifest["funding_hashes"][symbol]
            ):
                raise SafeError("reference history changed; do not compare different inputs")
        old_config = reference_manifest["configuration"]
        if any(s.public().get(key) != value for key, value in old_config.items()):
            raise SafeError("reference costs or configuration changed; rerun the original study")
        reference_results = json.loads((reference / "evaluation.json").read_text())["evaluations"]
        if name == "recent_97_coin":
            reproduced = await replay(
                frames, funding, original, start_ms, end_ms, output / "original_regression"
            )
            previous = reference_results["baseline"]
            if (
                reproduced["trades"] != previous["trades"]
                or abs(reproduced["net_return"] - previous["net_return"]) > 1e-10
            ):
                raise SafeError("original regression changed; review before evaluating candidates")
        result = await evaluate(frames, funding, candidate, start_ms, end_ms, output / name)
        comparisons[name] = {
            "reference_report": str(reference / "evaluation.json"),
            "reference_inputs_identical": True,
            "original": reference_results,
            "candidate": result["evaluations"],
        }
        print(
            json.dumps(
                {
                    "candidate": mode,
                    "sample_complete": name,
                    "net_returns": {
                        case: value["net_return"] for case, value in result["evaluations"].items()
                    },
                },
                allow_nan=False,
            ),
            flush=True,
        )
    reasons = []
    gate = protocol["paper_candidate_criteria"]
    for name, study in comparisons.items():
        cases = study["candidate"]
        minimum = (
            gate["minimum_recent_trades"]
            if name == "recent_97_coin"
            else gate["minimum_long_trades"]
        )
        if cases["baseline"]["trades"] < minimum:
            reasons.append(name + ": too few baseline trades")
        pf = cases["baseline"].get("profit_factor")
        if pf is None or pf < gate["minimum_baseline_profit_factor"]:
            reasons.append(name + ": baseline profit factor fails")
        for case in ("baseline", "2x_cost", "extra_bar_delay"):
            if cases[case]["net_return"] <= 0:
                reasons.append(name + ": " + case + " did not beat cash")
            if cases[case]["max_drawdown"] > gate["maximum_drawdown"]:
                reasons.append(name + ": " + case + " drawdown fails")
    result = {
        "candidate": mode,
        "finished_at": datetime.now(UTC).isoformat(),
        "samples": comparisons,
        "paper_candidate_criteria_passed": not reasons,
        "rejection_reasons": reasons,
        "historical_data_previously_viewed": True,
        "prospective_confirmation_required": True,
        "live_permitted": False,
    }
    (output / "comparison.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "candidate": mode,
                "paper_candidate_criteria_passed": not reasons,
                "rejection_reasons": reasons,
                "live_permitted": False,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=["reverse", "failed_breakout"])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(compare(args.mode, args.output))
    except SafeError as exc:
        parser.exit(2, str(exc) + "\n")
