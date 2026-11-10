"""Predeclared tighter-entry search; real RiskEngine and unchanged exits/risk.

Ranking sees the first 20 days only. Later-period/full-month evaluation must be
reported separately. Previously reviewed history is not a pristine holdout.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from paperfrog.simple.learning_replay import prepare
from paperfrog.simple.momentum import classify
from paperfrog.simple.strategy import execution_rules
from scripts.fetch_fixed_roe_month import DAY
from scripts.replay_fixed_roe_month import load_inputs, run, settings


@dataclass(frozen=True)
class EntryFilter:
    name: str
    confirmation: str = "none"
    side: str = "both"
    minimum_score: float = 0
    maximum_age: int = 12
    minimum_efficiency: float = 0.3
    minimum_trend_atr: float = 0.3
    minimum_volume_ratio: float = 0
    minimum_flow: float = 0

    def accepts(self, row: dict) -> bool:
        side = int(row["side"])
        # Cached score includes the configured uniform execution-cost discount.
        raw_score = float(row["score"]) / (1 - 0.0017 / 0.025)
        if (self.side == "short" and side > 0) or (self.side == "long" and side < 0):
            return False
        if not (raw_score >= self.minimum_score
                and row["momentum_trend_age_bars"] <= self.maximum_age
                and row["path_efficiency_15m"] >= self.minimum_efficiency
                and abs(row["trend_atr"]) >= self.minimum_trend_atr
                and row["volume_ratio"] >= self.minimum_volume_ratio
                and side * row["flow"] >= self.minimum_flow):
            return False
        if self.confirmation in {"hour", "hour_day"} and not (
            side * row["return_1h"] > 0 and row["path_efficiency_1h"] >= 0.3
        ):
            return False
        if self.confirmation in {"day", "hour_day"} and not (
            side * row["day_return_4h"] > 0 and side * row["day_slope_4h"] > 0
        ):
            return False
        return True


def variants() -> list[EntryFilter]:
    result = [EntryFilter("baseline")]
    for side in ("both", "short"):
        for confirmation in ("none", "hour", "day", "hour_day"):
            if (side, confirmation) != ("both", "none"):
                result.append(EntryFilter(f"{side}-{confirmation}", confirmation, side))
            result.append(EntryFilter(f"{side}-{confirmation}-strong", confirmation, side,
                                      minimum_score=1.5, minimum_efficiency=0.6))
        result.append(EntryFilter(f"{side}-day-young", "day", side, maximum_age=6))
        result.append(EntryFilter(f"{side}-day-trend", "day", side, minimum_trend_atr=0.75))
    return result


def volume_flow_variants() -> list[EntryFilter]:
    """Second predeclared family: stronger participation, flow and fresh trend."""
    return [EntryFilter(
        f"{side}-{confirmation}-volume{volume:g}-flow{flow:g}".replace(".", "p"),
        confirmation, side, minimum_score=1, maximum_age=6,
        minimum_efficiency=0.5, minimum_trend_atr=0.5,
        minimum_volume_ratio=volume, minimum_flow=flow,
    ) for side in ("both", "short") for confirmation in ("none", "day", "hour_day")
        for volume in (1.25, 2) for flow in (0.25, 0.5)]


def prepare_context(source: Path) -> None:
    """Shared live features at cached signal times, with atomic research writes."""
    manifest = json.loads((source / "input-manifest.json").read_text())
    s = settings()
    rules = execution_rules(s)
    fields = ["timestamp_ms", "close", "minute_atr", "trend_atr", "flow", "return_1h", "return_15m",
              "path_efficiency_1h", "path_efficiency_15m", "rsi_14", "momentum_trend_age_bars",
              "day_context_ms", "day_return_4h", "day_return_1h", "day_slope_4h",
              "day_mean_distance_4h", "volume_ratio", "daily_volume"]
    basic = source / "signals"
    enriched = source / "signal-context"
    basic.mkdir(exist_ok=True)
    enriched.mkdir(exist_ok=True)
    for n, symbol in enumerate(manifest["candidate_symbols"], 1):
        target = enriched / f"{symbol}.parquet"
        if target.exists() and set(fields) <= set(pd.read_parquet(target).columns):
            continue
        raw = pd.read_parquet(source / "candles" / f"{symbol}.parquet")
        frame = prepare(raw, s)["5m"]
        path = basic / f"{symbol}.parquet"
        if path.exists():
            cached = pd.read_parquet(path)
            values = cached[["timestamp_ms", "side", "score"]].merge(frame[fields], on="timestamp_ms", how="left", validate="one_to_one")
        else:
            frame = frame[(frame.timestamp_ms >= manifest["start_ms"]) &
                          (frame.timestamp_ms < manifest["end_ms"]) &
                          (frame.daily_volume >= s.min_daily_quote_volume)]
            rows = []
            for row in frame.to_dict("records"):
                _, side, score = classify(row, 0.00085, rules)
                if side:
                    rows.append({k: row[k] for k in fields} | {"side": side, "score": score * .932})
            values = pd.DataFrame(rows, columns=fields + ["side", "score"])
            temp = path.with_suffix(".tmp")
            values[["timestamp_ms", "close", "minute_atr", "side", "score"]].to_parquet(temp, index=False)
            temp.replace(path)
        temp = target.with_suffix(".tmp")
        values.to_parquet(temp, index=False)
        temp.replace(target)
        if n % 25 == 0:
            print(f"context {n}/{len(manifest['candidate_symbols'])}", flush=True)


INPUTS: dict = {}
SOURCE = Path()
OUTPUT = Path()


def trial(spec: EntryFilter) -> dict:
    manifest = INPUTS["manifest"]
    split = manifest["start_ms"] + 20*DAY
    with contextlib.redirect_stdout(io.StringIO()):
        result = run(SOURCE, inputs=INPUTS, accept=spec.accepts, end_ms=split,
                     label=spec.name + "-train", output_folder=OUTPUT)
    result["filter"] = asdict(spec)
    # This ranking does not inspect later-period results.
    result["selection_score"] = result["return_pct"] - 0.25 * result["max_drawdown_pct_5m"]
    result["minimum_sample_met"] = result["closed_trades"] >= 40
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--workers", type=int, choices=(1, 2), default=2)
    p.add_argument("--prepare-only", action="store_true")
    args = p.parse_args()
    SOURCE, OUTPUT = args.source, args.output
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if args.prepare_only:
        prepare_context(SOURCE)
        raise SystemExit(0)
    specs = variants()
    plan = {"filters": [asdict(s) for s in specs], "development_days": 20,
            "validation_days": 10, "minimum_development_closed_trades": 40,
            "ranking": "development return_pct minus 0.25*development drawdown_pct",
            "risk_and_exit_changes": False,
            "history_previously_reviewed": True,
            "forbidden": ["coin winner whitelist", "higher leverage", "wider stops", "reset risk locks"]}
    (OUTPUT / "search-plan.json").write_text(json.dumps(plan, indent=2))
    INPUTS = load_inputs(SOURCE, context=True)
    results = []
    with ProcessPoolExecutor(max_workers=args.workers,
                             mp_context=multiprocessing.get_context("fork")) as executor:
        futures = {executor.submit(trial, spec): spec for spec in specs}
        for f in as_completed(futures):
            result = f.result()
            results.append(result)
            print(json.dumps({k: result[k] for k in ("filter", "return_pct", "max_drawdown_pct_5m", "closed_trades", "selection_score")}), flush=True)
            (OUTPUT / "development-results.json").write_text(json.dumps(results, indent=2))
    ranked = sorted((r for r in results if r["minimum_sample_met"]),
                    key=lambda r: (-r["selection_score"], r["filter"]["name"]))
    (OUTPUT / "development-ranking.json").write_text(json.dumps(ranked, indent=2))
    print("DEVELOPMENT COMPLETE", flush=True)
