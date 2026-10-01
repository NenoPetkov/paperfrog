from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def metrics(equity: pd.DataFrame, trades: pd.DataFrame) -> dict[str, Any]:
    if equity.empty:
        return {"trades": 0, "validation": "no_data", "live_permitted": False}
    series = equity.set_index(pd.to_datetime(equity.timestamp_ms, unit="ms", utc=True)).equity
    daily = series.resample("1D").last().pct_change().dropna()
    dd = series / series.cummax() - 1
    downside = daily[daily < 0]
    pnl = trades.pnl if not trades.empty else pd.Series(dtype=float)
    gains, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    result = {
        "net_return": float(series.iloc[-1] / series.iloc[0] - 1),
        "trades": len(trades),
        "sharpe": float(np.sqrt(365) * daily.mean() / daily.std())
        if len(daily) > 2 and daily.std() > 0
        else None,
        "sortino": float(np.sqrt(365) * daily.mean() / downside.std())
        if len(downside) > 2 and downside.std() > 0
        else None,
        "profit_factor": float(gains / losses) if losses else None,
        "hit_rate": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "expectancy": float(pnl.mean()) if len(pnl) else 0.0,
        "max_drawdown": float(-dd.min()),
        "average_win": float(pnl[pnl > 0].mean()) if gains else 0.0,
        "average_loss": float(pnl[pnl < 0].mean()) if losses else 0.0,
        "underwater_observations": int((dd < 0).sum()),
        "historical_llm_used": False,
        "validation": "research_only",
        "live_permitted": False,
    }
    if not trades.empty:
        result["pnl_by_symbol"] = trades.groupby("symbol").pnl.sum().to_dict()
        result["pnl_by_month"] = (
            trades.groupby(
                pd.to_datetime(trades.timestamp_ms, unit="ms", utc=True).dt.strftime("%Y-%m")
            )
            .pnl.sum()
            .to_dict()
        )
        result["fees"] = float(trades.fees.sum())
        result["funding"] = float(trades.funding.sum())
        result["turnover"] = float(trades.turnover.sum())
    return result


def bootstrap(trades: pd.DataFrame, seed: int = 42, samples: int = 1000) -> dict[str, float]:
    if trades.empty:
        return {"p05_pnl": 0.0, "median_pnl": 0.0, "p95_pnl": 0.0}
    rng = np.random.default_rng(seed)
    sums = rng.choice(trades.pnl.to_numpy(), size=(samples, len(trades)), replace=True).sum(axis=1)
    return dict(
        zip(
            ["p05_pnl", "median_pnl", "p95_pnl"],
            map(float, np.quantile(sums, [0.05, 0.5, 0.95])),
            strict=True,
        )
    )
