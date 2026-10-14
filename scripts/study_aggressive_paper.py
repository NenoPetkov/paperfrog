"""Evaluate frozen signals at a higher offline PAPER budget; no runtime/exchange writes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from paperfrog.domain.types import SafeError
from paperfrog.settings import load_settings
from paperfrog.simple.backtest import replay
from paperfrog.simple.research import monthly_performance
from paperfrog.simple.research_budget import offline_budget
from paperfrog.simple.strategy import BAR_MS, Rules, indicators, require_paper


def run_case(task: dict[str, Any]) -> dict[str, Any]:
    base = load_settings().model_copy(update={"simple_entry_mode": "trend"})
    require_paper(base)
    settings = offline_budget(base)
    if settings.config_hash != task["configuration_hash"]:
        raise SafeError("offline risk configuration changed during evaluation")
    rules = Rules(**task["rules"])
    frames, funding = {}, {}
    folder = Path(task["input"])
    hashes = json.loads((folder / "hashes.json").read_text())
    start, end = task["start_ms"], task["end_ms"]
    for symbol in task["symbols"]:
        raw = pd.read_parquet(folder / f"{symbol}-candles.parquet")
        settlements = pd.read_parquet(folder / f"{symbol}-funding.parquet")
        for name, frame in (("candles", raw), ("funding", settlements)):
            digest = hashlib.sha256(
                pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes()
            ).hexdigest()
            if digest != hashes[symbol][name]:
                raise SafeError("frozen offline study input changed")
        raw = raw[raw.timestamp_ms.between(start - rules.warmup_bars * BAR_MS + BAR_MS, end)]
        if len(raw) != (end - start) // BAR_MS + rules.warmup_bars:
            raise SafeError("offline study needs complete history and warm-up")
        frames[symbol] = indicators(raw, rules)
        funding[symbol] = settlements[settlements.timestamp_ms.between(start - 16 * 3600000, end)]
    output = Path(task["output"])
    result = asyncio.run(
        replay(
            frames,
            funding,
            settings,
            start,
            end,
            output,
            cost_multiplier=task["cost_multiplier"],
            delay_bars=task["delay_bars"],
            rules=rules,
            memory_ledger=True,
        )
    )
    months, summary = monthly_performance(pd.read_parquet(output / "equity.parquet"))
    months.to_csv(output / "monthly.csv", index=False)
    result.update(summary)
    (output / "study-metrics.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return {"case": task["case"], "metrics": result}


def study(source: Path, output: Path, workers: int) -> None:
    require_paper(load_settings())
    if output.exists() or not 1 <= workers <= 3:
        raise SafeError("use a new immutable offline output directory and 1 to 3 workers")
    selection = json.loads((source / "frozen-selection.json").read_text())
    previous = json.loads((source / "protocol.json").read_text())
    recent = json.loads(Path("reports/simple-pool-30d/manifest.json").read_text())
    collection = json.loads(
        Path(
            previous.get("recent_pool_collection", "reports/simple-collection-30d.json")
        ).read_text()
    )
    rules = selection["rules"]
    s = offline_budget(load_settings().model_copy(update={"simple_entry_mode": "trend"}))
    output.mkdir(parents=True)
    paths = sorted(Path("src/paperfrog").rglob("*.py")) + [Path(__file__)]
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    protocol = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_study": str(source),
        "signal_rules_frozen_before_risk_evaluation": rules,
        "configuration": s.public(),
        "source_hashes": source_hashes,
        "offline_only": True,
        "profit_guard_scale": s.risk_per_trade / 0.002,
        "live_permitted": False,
        "runtime_state_modified": False,
        "signal_parameter_search": False,
        "previous_search_trials": 28 if previous["family"] == "position_breakout" else 24,
        "loss_thresholds_are_not_guaranteed_monthly_loss_caps": True,
        "historical_data_previously_viewed": True,
        "independent_holdout_claim": False,
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2, allow_nan=False) + "\n")
    tasks = []
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for name, begin, end, folder, assets in (
        (
            "continuous_23_month"
            if previous["family"] in {"weekly_trend", "position_breakout"}
            else "continuous_two_year",
            previous["calibration"][0],
            previous["reserved_final_period"][1],
            source / "inputs/two_year",
            symbols,
        ),
        (
            "reserved_final",
            previous["reserved_final_period"][0],
            previous["reserved_final_period"][1],
            source / "inputs/two_year",
            symbols,
        ),
        (
            "recent_pool",
            recent["start_ms"],
            recent["end_ms"],
            source / "inputs/recent",
            collection["usable_symbols"],
        ),
    ):
        for case, cost, delay in (("baseline", 1, 1), ("2x_cost", 2, 1), ("extra_bar_delay", 1, 2)):
            label = name + "_" + case
            tasks.append(
                {
                    "rules": rules,
                    "configuration_hash": s.config_hash,
                    "symbols": assets,
                    "input": str(folder),
                    "start_ms": begin,
                    "end_ms": end,
                    "output": str(output / label),
                    "case": label,
                    "cost_multiplier": cost,
                    "delay_bars": delay,
                }
            )
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(run_case, task) for task in tasks]):
            result = future.result()
            results.append(result)
            m = result["metrics"]
            print(
                json.dumps(
                    {
                        "case": result["case"],
                        "net_return": m["net_return"],
                        "max_drawdown": m["max_drawdown"],
                        "trades": m["trades"],
                        "best_month": m["best_complete_month"],
                        "worst_month": m["worst_complete_month"],
                    }
                ),
                flush=True,
            )
    if any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != h for p, h in source_hashes.items()):
        raise SafeError("source changed during offline risk study")
    result = {
        "finished_at": datetime.now(UTC).isoformat(),
        "rules": rules,
        "evaluations": sorted(results, key=lambda row: row["case"]),
        "all_cases_positive": all(r["metrics"]["net_return"] > 0 for r in results),
        "monthly_target_supported": all(r["metrics"]["monthly_target_supported"] for r in results),
        "live_permitted": False,
        "runtime_state_modified": False,
        "prospective_confirmation_required": True,
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "offline_risk_study_complete": True,
                "all_cases_positive": result["all_cases_positive"],
                "monthly_target_supported": result["monthly_target_supported"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    try:
        study(args.source, args.output, args.workers)
    except SafeError as exc:
        parser.exit(2, str(exc) + "\n")
