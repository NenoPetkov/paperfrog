from __future__ import annotations

import numpy as np
import pandas as pd

from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError
from paperfrog.features.engineering import align_inputs, build_features, funding_persistence
from paperfrog.settings import Settings


def build_symbol(s: Settings, symbol: str) -> pd.DataFrame:
    lake = Lake(s.data_dir)
    perp, spot, oi, funding, snapshots = [
        lake.read(kind, symbol)
        for kind in ("perp_15m", "spot_15m", "open_interest", "funding", "snapshots")
    ]
    if snapshots.empty or not {"spot_bid", "spot_ask"} <= set(snapshots):
        raise SafeError(
            "actual spot and perpetual midpoint history required; klines cannot substitute"
        )
    inputs = align_inputs(perp, spot, oi, funding, snapshots)
    quotes = snapshots[
        [
            "timestamp_ms",
            "bid",
            "ask",
            "spot_bid",
            "spot_ask",
            "bid_size",
            "ask_size",
            "mark",
            "next_funding_ms",
        ]
    ].copy()
    quotes["perp_mid"] = (quotes.bid + quotes.ask) / 2
    quotes["spot_mid"] = (quotes.spot_bid + quotes.spot_ask) / 2
    inputs = inputs.drop(columns=["perp_mid", "spot_mid", "bid", "ask"])
    inputs = pd.merge_asof(
        inputs,
        quotes.sort_values("timestamp_ms"),
        on="timestamp_ms",
        direction="backward",
        tolerance=60000,
    )
    # One completed minute observation per settlement-window minute. No long-gap fill.
    q = quotes.copy()
    q["timestamp_ms"] = ((q.timestamp_ms // 60000) + 1) * 60000
    q = q.groupby("timestamp_ms", as_index=False).last()
    q["basis"] = q.perp_mid / q.spot_mid - 1
    result = build_features(inputs, funding_persistence(q, funding))
    event_times = funding.timestamp_ms.to_numpy()
    cumulative = np.r_[0.0, funding.funding_rate.to_numpy().cumsum()]
    right = np.searchsorted(event_times, result.timestamp_ms.to_numpy(), side="right")
    for hours in (8, 24, 72):
        left = np.searchsorted(
            event_times, result.timestamp_ms.to_numpy() - hours * 3600000, side="right"
        )
        result[f"funding_sum_{hours}h"] = cumulative[right] - cumulative[left]
        if hours == 24:
            result["funding_events_24h"] = right - left
    settlements = funding[["timestamp_ms"]].copy()
    settlements["last_funding_ms"] = settlements.timestamp_ms
    settlements["funding_interval_hours"] = settlements.timestamp_ms.diff() / 3600000
    result = pd.merge_asof(
        result, settlements, on="timestamp_ms", direction="backward", tolerance=12 * 3600000
    )
    result["time_since_funding_minutes"] = (result.timestamp_ms - result.last_funding_ms) / 60000
    result["time_to_next_funding_minutes"] = (result.next_funding_ms - result.timestamp_ms) / 60000
    result["funding_settlement"] = result.timestamp_ms.isin(funding.timestamp_ms)
    result["basis_change_sigma"] = result.basis.diff().rolling(96).std()
    target = s.data_dir / "features" / f"{symbol}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    result.replace([np.inf, -np.inf], np.nan).to_parquet(temporary, index=False)
    temporary.replace(target)
    return result
