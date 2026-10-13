from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError
from paperfrog.features.engineering import CORE_FEATURES
from paperfrog.settings import Settings


def assess_history(s: Settings, start_ms: int, end_ms: int) -> dict[str, Any]:
    """Measure observations against a requested grid; span alone is not coverage."""
    if start_ms >= end_ms or start_ms % 900000 or end_ms % 900000:
        raise SafeError("coverage range must contain aligned closed 15-minute bars")
    grid = pd.DataFrame({"timestamp_ms": np.arange(start_ms + 900000, end_ms + 1, 900000)})
    lake = Lake(s.data_dir)
    results, blocked = {}, []
    for symbol in s.symbols:
        result: dict[str, Any] = {}
        for kind in ("spot_15m", "perp_15m", "funding", "open_interest", "snapshots"):
            frame = lake.read(kind, symbol)
            timestamps = frame.timestamp_ms if not frame.empty else pd.Series(dtype="int64")
            selected = timestamps.between(start_ms, end_ms)
            if kind in {"spot_15m", "perp_15m"}:
                matches = int(grid.timestamp_ms.isin(timestamps).sum())
            else:
                tolerance = {"funding": 43200000, "open_interest": 900000, "snapshots": 60000}[kind]
                matches = 0
                if not frame.empty:
                    # Quotes must have genuine spot/perpetual bid and ask fields.
                    if kind == "snapshots":
                        columns = [
                            "bid",
                            "ask",
                            "spot_bid",
                            "spot_ask",
                            "bid_size",
                            "ask_size",
                            "mark",
                        ]
                        if set(columns) <= set(frame):
                            valid = np.isfinite(frame[columns].to_numpy(dtype=float)).all(axis=1)
                            valid &= (frame[columns].to_numpy(dtype=float) > 0).all(axis=1)
                            valid &= (frame.ask > frame.bid) & (frame.spot_ask > frame.spot_bid)
                            frame = frame[valid]
                        else:
                            frame = frame.iloc[:0]
                    if not frame.empty:
                        joined = pd.merge_asof(
                            grid,
                            frame[["timestamp_ms"]].rename(columns={"timestamp_ms": "observed_ms"}),
                            left_on="timestamp_ms",
                            right_on="observed_ms",
                            direction="backward",
                            tolerance=tolerance,
                        )
                        matches = int(joined.observed_ms.notna().sum())
            result[kind] = {
                "rows_in_window": int(selected.sum()),
                "matched_bars": matches,
                "expected_bars": len(grid),
                "missing_bars": len(grid) - matches,
                "coverage_fraction": matches / len(grid),
            }
            if matches != len(grid):
                blocked.append(f"{symbol}: incomplete {kind} coverage")
        results[symbol] = result
    days = (end_ms - start_ms) / 86400000
    if days < s.min_history_days:
        blocked.append("requested window is shorter than universe history requirement")
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "start_utc": pd.Timestamp(start_ms, unit="ms", tz="UTC").isoformat(),
        "end_utc": pd.Timestamp(end_ms, unit="ms", tz="UTC").isoformat(),
        "history_days": days,
        "symbols": results,
        "raw_coverage_complete": not blocked,
        "blocked_reasons": blocked,
        "additional_requirements": [
            "Complete minute quote windows for funding persistence and feature warm-up.",
            "Point-in-time universe evidence, including historical contract status.",
            "Sufficient eligible training/validation episodes and out-of-sample review.",
            "Model promotion and genuine PAPER/testnet operational acceptance.",
        ],
        "live_permitted": False,
    }


def assemble_panel(s: Settings, universes: list[dict[str, Any]], output: Path) -> pd.DataFrame:
    """Join feature files with retained historical universe snapshots, never current rankings."""
    if output.exists():
        raise SafeError("panel output already exists")
    if not universes:
        raise SafeError("historical universe snapshots required; cannot infer past membership")
    ordered = sorted(universes, key=lambda row: row["asof_ms"])
    previous_end = -1
    for row in ordered:
        if (
            not isinstance(row.get("asof_ms"), int)
            or not isinstance(row.get("expires_ms"), int)
            or row["asof_ms"] < previous_end
            or not row["asof_ms"] < row["expires_ms"] <= row["asof_ms"] + 7 * 86400000
            or not isinstance(row.get("symbols"), list)
            or not isinstance(row.get("inputs"), list)
            or row.get("min_history_days", 180) < s.min_history_days
        ):
            raise SafeError("invalid or overlapping historical universe snapshot")
        evidence = {r["symbol"]: r for r in row["inputs"]}
        for symbol in row["symbols"]:
            r = evidence.get(symbol)
            if (
                not r
                or r.get("observed_ms", row["asof_ms"] + 1) > row["asof_ms"]
                or r.get("valid_history_days", 0) < s.min_history_days
            ):
                raise SafeError("universe membership lacks matching point-in-time evidence")
        previous_end = row["expires_ms"]
    frames = []
    required = set(
        CORE_FEATURES
        + [
            "timestamp_ms",
            "perp_mid",
            "bid",
            "ask",
            "bid_size",
            "ask_size",
            "mark",
            "funding_settlement",
            "liquid",
        ]
    )
    if not {"BTCUSDT", "ETHUSDT"} <= set(s.symbols):
        raise SafeError("BTC and ETH hedge symbols required")
    for symbol in s.symbols:
        path = s.data_dir / "features" / f"{symbol}.parquet"
        if not path.exists():
            raise SafeError("validated feature files required for all configured symbols")
        frame = pd.read_parquet(path).copy()
        if (
            not required <= set(frame)
            or frame.timestamp_ms.duplicated().any()
            or not frame.timestamp_ms.is_monotonic_increasing
        ):
            raise SafeError("feature schema or chronology invalid")
        frame["symbol"] = symbol
        frame["in_universe"] = False
        frame["universe_asof_ms"] = -1
        frame["universe_expires_ms"] = -1
        for row in ordered:
            mask = frame.timestamp_ms.between(row["asof_ms"], row["expires_ms"] - 1)
            frame.loc[mask, "universe_asof_ms"] = row["asof_ms"]
            frame.loc[mask, "universe_expires_ms"] = row["expires_ms"]
            if symbol in row["symbols"]:
                frame.loc[mask, "in_universe"] = True
        if symbol not in {"BTCUSDT", "ETHUSDT"}:
            usable = frame.in_universe & np.isfinite(
                frame[CORE_FEATURES].to_numpy(dtype=float)
            ).all(axis=1)
            if not usable.any():
                raise SafeError(
                    "no eligible target rows with complete features and dated membership"
                )
        frames.append(frame)
    panel = pd.concat(frames, ignore_index=True).sort_values(["timestamp_ms", "symbol"])
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    panel.to_parquet(temporary, index=False)
    temporary.replace(output)
    return panel
