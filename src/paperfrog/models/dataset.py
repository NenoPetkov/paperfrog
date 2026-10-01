from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from paperfrog.domain.types import SafeError
from paperfrog.features.engineering import CORE_FEATURES
from paperfrog.models.labels import label_episode
from paperfrog.portfolio.sizing import estimate_hedge
from paperfrog.storage.database import Store
from paperfrog.strategy.engine import Episodes


def build_samples(panel: pd.DataFrame, output: Path) -> pd.DataFrame:
    """One sample per episode; PIT membership and completed future paths are mandatory."""
    required = set(
        CORE_FEATURES
        + [
            "symbol",
            "timestamp_ms",
            "perp_mid",
            "in_universe",
            "universe_asof_ms",
            "universe_expires_ms",
        ]
    )
    if not required <= set(panel):
        raise SafeError("training panel missing point-in-time features or universe provenance")
    if output.exists():
        raise SafeError("dataset output already exists")
    output.mkdir(parents=True)
    store = Store(output / "episodes.sqlite")
    episodes = Episodes(store)
    indexed = {
        s: f.sort_values("timestamp_ms").reset_index(drop=True) for s, f in panel.groupby("symbol")
    }
    if not {"BTCUSDT", "ETHUSDT"} <= indexed.keys():
        raise SafeError("BTC and ETH hedge histories required")
    records = []
    for symbol, bars in indexed.items():
        if symbol in {"BTCUSDT", "ETHUSDT"}:
            continue
        for index, row in enumerate(bars.itertuples()):
            universe = (
                {symbol}
                if row.in_universe
                and row.universe_asof_ms <= row.timestamp_ms < row.universe_expires_ms
                else set()
            )
            candidate = episodes.update(symbol, bars.iloc[: index + 1], universe)
            if candidate is None or index + 16 >= len(bars):
                continue
            current = bars.iloc[index]
            if not np.isfinite(current[CORE_FEATURES].to_numpy(dtype=float)).all():
                continue
            prices = pd.DataFrame(
                {
                    s: indexed[s].set_index("timestamp_ms").perp_mid
                    for s in (symbol, "BTCUSDT", "ETHUSDT")
                }
            ).dropna()
            history = prices[prices.index <= row.timestamp_ms].pct_change().dropna().tail(2880)
            try:
                hedge = estimate_hedge(history.to_numpy())
            except SafeError:
                continue
            future = bars.iloc[index + 1 : index + 17].copy()
            if (future.timestamp_ms.diff().dropna() != 900000).any():
                continue
            future = future.merge(
                prices[["BTCUSDT", "ETHUSDT"]], left_on="timestamp_ms", right_index=True, how="left"
            )
            future = future.rename(columns={"BTCUSDT": "btc_mid", "ETHUSDT": "eth_mid"})
            if future[["btc_mid", "eth_mid"]].isna().any().any():
                continue
            p = prices.loc[row.timestamp_ms]
            residual = history[symbol] - hedge.btc * history.BTCUSDT - hedge.eth * history.ETHUSDT
            sigma = float(residual.tail(2880).std() * 4)
            if "funding_settlement" not in future:
                raise SafeError(
                    "label funding requires actual settlement flags for target and hedge legs"
                )
            hedge_events = {}
            for s in ("BTCUSDT", "ETHUSDT"):
                f = indexed[s].set_index("timestamp_ms").reindex(future.timestamp_ms)
                hedge_events[s] = (f.funding_settlement.astype(float) * f.funding_rate).to_numpy()
            direction = -candidate.crowded_side
            future["funding_pnl"] = -direction * (
                future.funding_settlement.astype(float) * future.funding_rate
                - hedge.btc * hedge_events["BTCUSDT"]
                - hedge.eth * hedge_events["ETHUSDT"]
            )
            label = label_episode(
                future,
                sigma,
                current.basis,
                candidate.crowded_side,
                hedge.btc,
                hedge.eth,
                (float(p[symbol]), float(p.BTCUSDT), float(p.ETHUSDT)),
            )
            record = {k: float(current[k]) for k in CORE_FEATURES}
            record.update(label)
            record.update(
                timestamp_ms=int(row.timestamp_ms),
                episode_id=candidate.episode_id,
                symbol=symbol,
                beta_btc=hedge.btc,
                beta_eth=hedge.eth,
                sigma_h=sigma,
            )
            records.append(record)
    samples = pd.DataFrame(records)
    if samples.empty:
        raise SafeError("no eligible fully observable episodes; remain in cash")
    samples = samples.sort_values("timestamp_ms").reset_index(drop=True)
    samples.to_parquet(output / "episodes.parquet", index=False)
    store.audit(
        "dataset_built", {"samples": len(samples), "created_at": datetime.now(UTC).isoformat()}
    )
    return samples
