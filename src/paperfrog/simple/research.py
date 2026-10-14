"""Retrospective research helpers; never change runtime risk or active strategy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from paperfrog.domain.types import SafeError

from .strategy import RULES, Rules


def candidates(family: str = "breakout") -> list[Rules]:
    """A bounded, declared family that seeks longer moves without increasing exposure."""
    if family == "position_breakout":
        return [
            replace(
                RULES,
                version=f"position-breakout-b{breakout}-t{trail}-v1",
                fast_ema=192,
                slow_ema=768,
                warmup_bars=3072,
                breakout_bars=breakout,
                atr_bars=14,
                atr_timeframe_bars=96,
                min_atr_fraction=0.005,
                max_atr_fraction=0.10,
                minimum_trend_atr=0.5,
                minimum_flow=0.05,
                stop_atr=2.0,
                trailing_atr=float(trail),
                minimum_stop_fraction=0.02,
                maximum_stop_fraction=0.10,
                maximum_hold_bars=2688,
            )
            for breakout in (672, 1920)
            for trail in (2, 3)
        ]
    if family == "weekly_trend":
        return [
            replace(
                RULES,
                version=f"weekly-breakout-{side}-s{stop}-v1",
                fast_ema=192,
                slow_ema=768,
                warmup_bars=3072,
                breakout_bars=192,
                atr_bars=96,
                minimum_trend_atr=2.0,
                minimum_flow=0.0,
                stop_atr=float(stop),
                trailing_atr=float(stop),
                minimum_stop_fraction=0.02,
                maximum_stop_fraction=0.06,
                maximum_hold_bars=2688,
                allowed_side=side,
            )
            for side in ("both", "long")
            for stop in (8, 12)
        ]
    if family == "sustained_trend":
        return [
            replace(
                RULES,
                version=f"sustained-{style}-{side}-s{stop}-v1",
                fast_ema=48,
                slow_ema=192,
                breakout_bars=96,
                minimum_trend_atr=2.0,
                stop_atr=float(stop),
                trailing_atr=float(stop),
                minimum_stop_fraction=0.012,
                maximum_stop_fraction=0.06,
                maximum_hold_bars=672,
                entry_style=style,
                allowed_side=side,
            )
            for style in ("breakout", "trend_follow")
            for side in ("both", "long")
            for stop in (6, 10)
        ]
    if family != "breakout":
        raise SafeError("unknown research family")
    return [
        replace(
            RULES,
            version=f"momentum-b{breakout}-h{hold}-t{trail}-v1",
            fast_ema=24,
            slow_ema=96,
            breakout_bars=breakout,
            minimum_trend_atr=1.5,
            stop_atr=4.0,
            trailing_atr=float(trail),
            maximum_hold_bars=hold,
        )
        for breakout in (24, 48, 96)
        for hold in (96, 288)
        for trail in (3, 5)
    ]


def monthly_performance(equity: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Account returns, including fees/funding and cash months; flag partial months."""
    if len(equity) < 2:
        raise SafeError("monthly returns require an initial and ending equity")
    if (
        not np.isfinite(equity[["timestamp_ms", "equity"]].to_numpy()).all()
        or (equity.equity <= 0).any()
        or (equity.timestamp_ms.diff().dropna() <= 0).any()
    ):
        raise SafeError("monthly returns require finite chronological equity")
    x = equity.copy()
    x["time"] = pd.to_datetime(x.timestamp_ms, unit="ms", utc=True)
    intervals = x.iloc[1:].copy()
    # An interval ending at midnight on the first belongs to the preceding month.
    intervals["month"] = (intervals.time - pd.Timedelta(1, "ns")).dt.strftime("%Y-%m")
    previous_equity = float(x.equity.iloc[0])
    previous_time = x.time.iloc[0]
    records = []
    for month, group in intervals.groupby("month", sort=True):
        ending = float(group.equity.iloc[-1])
        end = group.time.iloc[-1]
        month_start = pd.Timestamp(str(month) + "-01", tz="UTC")
        month_end = month_start + pd.offsets.MonthBegin(1)
        records.append(
            {
                "month": month,
                "start": previous_time.isoformat(),
                "end": end.isoformat(),
                "start_equity": previous_equity,
                "end_equity": ending,
                "net_return": ending / previous_equity - 1,
                "complete_month": previous_time == month_start and end == month_end,
            }
        )
        previous_equity, previous_time = ending, end
    months = pd.DataFrame(records)
    complete = months.loc[months.complete_month, "net_return"]
    summary = {
        "complete_months": len(complete),
        "positive_complete_months": int((complete > 0).sum()),
        "complete_months_at_least_20pct": int((complete >= 0.20).sum()),
        "worst_complete_month": float(complete.min()) if len(complete) else None,
        "best_complete_month": float(complete.max()) if len(complete) else None,
        "median_complete_month": float(complete.median()) if len(complete) else None,
        "monthly_target_supported": bool(len(complete) and (complete >= 0.20).all()),
    }
    return months, summary
