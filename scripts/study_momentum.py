"""Bounded chronological momentum search using the guarded PAPER engine, offline only."""

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

from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError
from paperfrog.settings import load_settings
from paperfrog.simple.backtest import load_history, replay
from paperfrog.simple.research import candidates, monthly_performance
from paperfrog.simple.strategy import BAR_MS, Rules, indicators, require_paper


def write_json(path: Path, value: Any) -> None:
    with path.open("x") as file:
        file.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def digest(frame: pd.DataFrame) -> str:
    return hashlib.sha256(
        pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes()
    ).hexdigest()


def stamp(date: str) -> int:
    return int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp() * 1000)


def trial(task: dict[str, Any]) -> dict[str, Any]:
    s = load_settings().model_copy(update={"simple_entry_mode": "trend"})
    require_paper(s)
    if s.config_hash != task["configuration_hash"]:
        raise SafeError("configuration changed during the study")
    rules = Rules(**task["rules"])
    start, end = task["start_ms"], task["end_ms"]
    frames, funding = {}, {}
    for symbol in task["symbols"]:
        folder = Path(task["input"])
        raw = pd.read_parquet(folder / f"{symbol}-candles.parquet")
        frame = raw[raw.timestamp_ms.between(start - rules.warmup_bars * BAR_MS + BAR_MS, end)]
        if len(frame) != (end - start) // BAR_MS + rules.warmup_bars:
            raise SafeError("study snapshot coverage changed")
        frames[symbol] = indicators(frame, rules)
        settlements = pd.read_parquet(folder / f"{symbol}-funding.parquet")
        funding[symbol] = settlements[settlements.timestamp_ms.between(start - 16 * 3600000, end)]
    output = Path(task["output"])
    result = asyncio.run(
        replay(
            frames,
            funding,
            s,
            start,
            end,
            output,
            cost_multiplier=task.get("cost_multiplier", 1),
            delay_bars=task.get("delay_bars", 1),
            rules=rules,
            memory_ledger=True,
        )
    )
    months, summary = monthly_performance(pd.read_parquet(output / "equity.parquet"))
    months.to_csv(output / "monthly.csv", index=False)
    result.update(summary)
    write_json(output / "study-metrics.json", result)
    return {"candidate": rules.version, "case": task["case"], "metrics": result}


def run_tasks(tasks: list[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(trial, task) for task in tasks]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            metrics = result["metrics"]
            print(
                json.dumps(
                    {
                        "candidate": result["candidate"],
                        "case": result["case"],
                        "net_return": metrics["net_return"],
                        "trades": metrics["trades"],
                        "profit_factor": metrics["profit_factor"],
                        "max_drawdown": metrics["max_drawdown"],
                    },
                    allow_nan=False,
                ),
                flush=True,
            )
    return sorted(results, key=lambda row: (row["candidate"], row["case"]))


def rank(row: dict[str, Any]) -> tuple[bool, float, str]:
    m = row["metrics"]
    qualified = m["net_return"] > 0 and (m["profit_factor"] or 0) >= 1.05 and m["trades"] >= 30
    return qualified, m["net_return"] / (0.01 + m["max_drawdown"]), row["candidate"]


def study(
    root: Path,
    workers: int,
    family: str = "breakout",
    collection_path: Path = Path("reports/simple-collection-30d.json"),
) -> None:
    s = load_settings().model_copy(update={"simple_entry_mode": "trend"})
    require_paper(s)
    if root.exists() or not 1 <= workers <= 4:
        raise SafeError("use a new immutable study directory and 1 to 4 workers")
    root.mkdir(parents=True)
    rules = candidates(family)
    source_paths = sorted(Path("src/paperfrog").rglob("*.py")) + [Path(__file__)]
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    start, train_end, validation_end, end = map(
        stamp,
        (
            "2024-10-15",
            "2025-10-13",
            "2026-04-13",
            "2026-10-13",
        ),
    )
    if family in {"weekly_trend", "position_breakout"}:
        start = stamp("2024-11-15")
    protocol = {
        "created_at": datetime.now(UTC).isoformat(),
        "family": family,
        "recent_pool_collection": str(collection_path),
        "candidates": [r.public() for r in rules],
        "candidate_count": len(rules),
        "calibration": [start, train_end],
        "validation": [train_end, validation_end],
        "reserved_final_period": [validation_end, end],
        "selection": "Top two calibration return/(0.01+drawdown), positive/PF>=1.05/30-trade candidates first. Then choose by the same validation ranking. Freeze before final/stress evaluation; no retuning.",
        "risk": "All Settings risk/exposure limits and PAPER loss/cooldown locks unchanged. Separate studies start with equal cash; continuous two-year replay never resets risk history.",
        "monthly_goal": {"minimum_return": 0.20, "tolerated_loss": -0.20},
        "configuration": s.public(),
        "configuration_hash": s.config_hash,
        "source_hashes": source_hashes,
        "historical_data_previously_viewed": True,
        "independent_holdout_claim": False,
        "live_permitted": False,
        "runtime_state_modified": False,
        "limitations": [
            "Parameter search and previously viewed history; selection bias remains.",
            "Fixed symbol pools omit historical membership and delistings.",
            "Assumed spread/depth, fractional filters, adverse candle-extreme stop fills; no historical order book.",
            "Actual funding used; costs and additional entry delay stressed.",
            "No prospective performance evidence or guaranteed monthly loss cap/return.",
        ],
    }
    write_json(root / "protocol.json", protocol)

    def snapshot(lake: Path, symbols: list[str], begin: int, finish: int, name: str) -> Path:
        frames, funding = load_history(Lake(lake), symbols, begin, finish, rules[0])
        folder = root / "inputs" / name
        folder.mkdir(parents=True)
        hashes = {}
        for symbol, frame in frames.items():
            frame.to_parquet(folder / f"{symbol}-candles.parquet", index=False)
            funding[symbol].to_parquet(folder / f"{symbol}-funding.parquet", index=False)
            hashes[symbol] = {"candles": digest(frame), "funding": digest(funding[symbol])}
        write_json(folder / "hashes.json", hashes)
        return folder

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    long_input = snapshot(s.data_dir, symbols, start, end, "two_year")

    def task(
        rule: Rules,
        case: str,
        begin: int,
        finish: int,
        folder: Path = long_input,
        assets: list[str] = symbols,
        cost: float = 1,
        delay: int = 1,
    ) -> dict[str, Any]:
        return {
            "rules": rule.public(),
            "case": case,
            "start_ms": begin,
            "end_ms": finish,
            "input": str(folder),
            "symbols": assets,
            "configuration_hash": s.config_hash,
            "output": str(root / case / rule.version),
            "cost_multiplier": cost,
            "delay_bars": delay,
        }

    calibration = run_tasks([task(r, "calibration", start, train_end) for r in rules], workers)
    write_json(root / "calibration.json", calibration)
    by_name = {r.version: r for r in rules}
    finalists = [by_name[r["candidate"]] for r in sorted(calibration, key=rank, reverse=True)[:2]]
    write_json(root / "finalists.json", [r.public() for r in finalists])
    validation = run_tasks(
        [task(r, "validation", train_end, validation_end) for r in finalists], workers
    )
    write_json(root / "validation.json", validation)
    chosen = sorted(validation, key=rank, reverse=True)[0]
    winner = by_name[chosen["candidate"]]
    write_json(
        root / "frozen-selection.json",
        {
            "rules": winner.public(),
            "selection_qualified": rank(chosen)[0],
            "frozen_before_final_evaluation": datetime.now(UTC).isoformat(),
            "live_permitted": False,
        },
    )
    collection = json.loads(collection_path.read_text())
    # The collection start includes the warm-up; original replay boundaries are in its manifest.
    recent_manifest = json.loads(Path("reports/simple-pool-30d/manifest.json").read_text())
    recent_start, recent_end = recent_manifest["start_ms"], recent_manifest["end_ms"]
    recent_symbols = collection["usable_symbols"]
    recent_input = snapshot(
        s.data_dir / "simple", recent_symbols, recent_start, recent_end, "recent"
    )
    final_tasks = []
    for name, begin, finish, folder, assets in (
        ("reserved_final", validation_end, end, long_input, symbols),
        (
            "continuous_23_month"
            if family in {"weekly_trend", "position_breakout"}
            else "continuous_two_year",
            start,
            end,
            long_input,
            symbols,
        ),
        ("recent_pool", recent_start, recent_end, recent_input, recent_symbols),
    ):
        for case, cost, delay in (("baseline", 1, 1), ("2x_cost", 2, 1), ("extra_bar_delay", 1, 2)):
            final_tasks.append(
                task(winner, f"{name}_{case}", begin, finish, folder, assets, cost, delay)
            )
    results = run_tasks(final_tasks, workers)
    if any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != h for p, h in source_hashes.items()):
        raise SafeError("source changed during study; results cannot be accepted")
    reasons = []
    if not rank(chosen)[0]:
        reasons.append("validation selection did not meet positive/PF/trade criteria")
    for result in results:
        m = result["metrics"]
        if m["net_return"] <= 0:
            reasons.append(result["case"] + ": did not beat cash")
        if m["max_drawdown"] > 0.06:
            reasons.append(result["case"] + ": exceeded existing drawdown threshold")
        if result["case"].endswith("baseline") and (m["profit_factor"] or 0) < 1.1:
            reasons.append(result["case"] + ": profit factor below 1.1")
        if m["risk_lock_at_end"] and m["risk_lock_at_end"].get("until") is None:
            reasons.append(result["case"] + ": persistent risk lock")
    report = {
        "finished_at": datetime.now(UTC).isoformat(),
        "selected_rules": winner.public(),
        "calibration": calibration,
        "validation": validation,
        "evaluations": results,
        "paper_candidate_criteria_passed": not reasons,
        "rejection_reasons": reasons,
        "cash_return": 0.0,
        "historical_data_previously_viewed": True,
        "prospective_confirmation_required": True,
        "live_permitted": False,
        "runtime_state_modified": False,
    }
    write_json(root / "summary.json", report)
    print(
        json.dumps(
            {
                "study_complete": True,
                "selected": winner.version,
                "paper_candidate_criteria_passed": not reasons,
                "rejection_reasons": reasons,
                "live_permitted": False,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--collection", type=Path, default=Path("reports/simple-collection-30d.json")
    )
    parser.add_argument(
        "--family",
        choices=["breakout", "sustained_trend", "weekly_trend", "position_breakout"],
        default="breakout",
    )
    args = parser.parse_args()
    try:
        study(args.output, args.workers, args.family, args.collection)
    except SafeError as exc:
        parser.exit(2, str(exc) + "\n")
