"""Synthetic mechanics fixture only. Never usable as readiness evidence."""

import numpy as np
import pandas as pd


def replay_panel() -> pd.DataFrame:
    rng = np.random.default_rng(71)
    n = 1000
    btc_returns = rng.normal(0, 0.001, n)
    eth_returns = rng.normal(0, 0.001, n)
    alt_returns = 0.5 * btc_returns + 0.4 * eth_returns + rng.normal(0, 0.00001, n)
    prices = {
        "BTCUSDT": 50000 * np.exp(np.cumsum(btc_returns)),
        "ETHUSDT": 2000 * np.exp(np.cumsum(eth_returns)),
        "SOLUSDT": 100 * np.exp(np.cumsum(alt_returns)),
    }
    rows = []
    for i in range(n):
        for symbol in prices:
            price = prices[symbol][i]
            episode = symbol == "SOLUSDT" and 970 <= i < 980
            basis = 0.01 if i < 974 else 0.009 if i < 975 else 0.008 if i < 978 else 0.004
            rows.append(
                {
                    "timestamp_ms": (i + 1) * 900000,
                    "symbol": symbol,
                    "bid": price - 0.01,
                    "ask": price + 0.01,
                    "bid_size": 100000.0,
                    "ask_size": 100000.0,
                    "mark": price,
                    "perp_mid": price,
                    "basis": basis if episode else 0.0001,
                    "basis_z_30d": 3.0 if episode else 0.2,
                    "funding_rate": 0.001,
                    "funding_z": 3.0 if episode else 0.0,
                    "funding_persistence": 0.8,
                    "oi_change_30m": -0.01 if i >= 974 else 0.02,
                    "oi_z_4h": 2.0 if episode else 0.0,
                    "volume_ratio_z": 2.0,
                    "perp_imbalance": -0.2 if i >= 974 else 0.3,
                    "spread": 0.0002,
                    "liquid": True,
                    "universe_asof_ms": (i // 672) * 672 * 900000,
                    "universe_expires_ms": ((i // 672) + 1) * 672 * 900000 + 1,
                    "in_universe": symbol == "SOLUSDT",
                    "funding_settlement": False,
                }
            )
    return pd.DataFrame(rows)
