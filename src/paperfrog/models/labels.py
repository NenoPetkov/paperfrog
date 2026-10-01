from __future__ import annotations

import numpy as np
import pandas as pd

from paperfrog.domain.types import SafeError


def label_episode(
    future: pd.DataFrame,
    sigma_h: float,
    initial_basis: float,
    crowded_side: int,
    beta_btc: float,
    beta_eth: float,
    initial_prices: tuple[float, float, float],
    fee_rate: float = 0.0005,
    slippage: float = 0.0004,
    spread: float = 0.0002,
) -> dict[str, float | int]:
    """Future path is used ONLY for labels, never feature construction."""
    if len(future) < 16 or initial_basis == 0 or sigma_h < 0:
        raise SafeError("complete four-hour label path required")
    path = future.iloc[:16]
    direction = -crowded_side
    alt, btc, eth = initial_prices
    gross = direction * (
        (path.perp_mid / alt - 1)
        - beta_btc * (path.btc_mid / btc - 1)
        - beta_eth * (path.eth_mid / eth - 1)
    )
    fees = 2 * fee_rate * (1 + beta_btc + beta_eth)
    funding = (
        path.funding_pnl.cumsum() if "funding_pnl" in path else pd.Series(0.0, index=path.index)
    )
    costs = (slippage + spread) * (1 + beta_btc + beta_eth)
    net = gross + funding - fees - costs
    upper, lower = np.clip(0.75 * sigma_h, 0.0035, 0.020), np.clip(0.60 * sigma_h, 0.0025, 0.015)
    exit_index, success = len(path) - 1, 0
    for i, value in enumerate(net):
        # Adverse wins a same-bar ambiguity; minute/tick paths are preferable.
        if value <= -lower or bool(path.iloc[i].get("invalidated", False)):
            exit_index = i
            break
        if value >= upper:
            exit_index = i
            success = int(
                abs(path.iloc[i].basis) <= 0.65 * abs(initial_basis)
                and bool(path.iloc[: i + 1].liquid.all())
            )
            break
    return {
        "success": success,
        "net_return": float(np.clip(net.iloc[exit_index], -0.1, 0.1)),
        "gross_return": float(gross.iloc[exit_index]),
        "funding": float(funding.iloc[exit_index]),
        "fees": float(fees),
        "slippage_spread": float(costs),
        "mfe": float(net.iloc[: exit_index + 1].max()),
        "mae": float(net.iloc[: exit_index + 1].min()),
        "basis_contraction": float(1 - abs(path.iloc[exit_index].basis / initial_basis)),
        "label_end_ms": int(path.iloc[exit_index].timestamp_ms),
    }
