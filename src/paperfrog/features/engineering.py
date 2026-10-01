from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from paperfrog.domain.types import SafeError

# Explicit core schema; optional depth/liquidation never silently enter trained models.
CORE_FEATURES = [
    "basis",
    "basis_z_30d",
    "funding_rate",
    "funding_z",
    "funding_persistence",
    "oi_change_30m",
    "oi_z_4h",
    "volume_ratio_z",
    "perp_imbalance",
    "spot_imbalance",
    "return_15m",
    "return_1h",
    "volatility",
    "spread",
    "basis_slope_1h",
]
SCHEMA_HASH = hashlib.sha256(json.dumps(CORE_FEATURES).encode()).hexdigest()


def robust_z(series: pd.Series, window: int, minimum: int | None = None) -> pd.Series:
    minimum = minimum or window
    roll = series.rolling(window, min_periods=minimum)
    median = roll.median()
    mad = roll.apply(lambda x: np.median(np.abs(x - np.median(x))), raw=True)
    return (series - median) / (1.4826 * mad.replace(0, np.nan))


def funding_persistence(minute_basis: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for event in funding.itertuples():
        f = int(event.timestamp_ms)
        before = minute_basis.loc[
            minute_basis.timestamp_ms.between(f - 600000, f - 120000), "basis"
        ]
        after = minute_basis.loc[
            minute_basis.timestamp_ms.between(f + 300000, f + 1200000), "basis"
        ]
        if len(before) < 9 or len(after) < 16:
            continue
        b, a = float(before.median()), float(after.median())
        rows.append(
            {
                "timestamp_ms": f + 1200000,
                "settlement_ms": f,
                "funding_persistence": abs(a) / max(abs(b), 1e-10),
                "funding_efficiency": (abs(b) - abs(a)) / max(abs(event.funding_rate), 1e-10),
            }
        )
    return pd.DataFrame(
        rows, columns=["timestamp_ms", "settlement_ms", "funding_persistence", "funding_efficiency"]
    )


def build_features(frame: pd.DataFrame, persistence: pd.DataFrame | None = None) -> pd.DataFrame:
    required = {
        "timestamp_ms",
        "perp_mid",
        "spot_mid",
        "funding_rate",
        "open_interest",
        "perp_quote_volume",
        "spot_quote_volume",
        "perp_taker_buy_quote",
        "spot_taker_buy_quote",
        "bid",
        "ask",
    }
    if not required <= set(frame):
        raise SafeError("feature inputs missing: " + ",".join(sorted(required - set(frame))))
    x = frame.copy().sort_values("timestamp_ms").reset_index(drop=True)
    if x.timestamp_ms.duplicated().any() or (x.timestamp_ms.diff().dropna() != 900000).any():
        raise SafeError("features require continuous closed 15-minute bars")
    x["basis"] = x.perp_mid / x.spot_mid - 1
    for days in (7, 30, 90):
        x[f"basis_z_{days}d"] = robust_z(x.basis, days * 96)
    x["basis_percentile"] = x.basis.rolling(30 * 96).rank(pct=True)
    x["basis_distance"] = x.basis - x.basis.rolling(30 * 96).median()
    for label, n in (("15m", 1), ("30m", 2), ("1h", 4), ("4h", 16)):
        x["basis_slope_" + label] = x.basis.diff(n) / n
    x["basis_acceleration"] = x.basis.diff().diff()
    # Funding is a sparse settlement process. Normalize the event series separately
    # upstream when available; this repeated last-observed rate is declared in registry.
    x["funding_z"] = robust_z(x.funding_rate, 30 * 96)
    x["funding_percentile"] = x.funding_rate.rolling(30 * 96).rank(pct=True)
    x["sign_alignment"] = (x.basis * x.funding_rate > 0).astype(float)
    for label, n in (("15m", 1), ("30m", 2), ("1h", 4), ("4h", 16), ("12h", 48), ("24h", 96)):
        x["oi_change_" + label] = x.open_interest.pct_change(n, fill_method=None)
        x["oi_z_" + label] = robust_z(x["oi_change_" + label], 30 * 96)
        x["return_" + label] = x.perp_mid.pct_change(n, fill_method=None)
    x["oi_price_interaction"] = x.oi_change_1h * x.return_1h
    x["oi_per_spot_volume"] = x.open_interest / x.spot_quote_volume.replace(0, np.nan)
    x["oi_per_perp_volume"] = x.open_interest / x.perp_quote_volume.replace(0, np.nan)
    x["volume_ratio"] = x.perp_quote_volume / x.spot_quote_volume.replace(0, np.nan)
    x["volume_ratio_change"] = x.volume_ratio.diff()
    x["volume_ratio_z"] = robust_z(x.volume_ratio, 30 * 96)
    for market in ("perp", "spot"):
        x[market + "_imbalance"] = (
            2 * x[market + "_taker_buy_quote"] / x[market + "_quote_volume"].replace(0, np.nan) - 1
        )
        for n in (2, 4, 16):
            x[f"{market}_imbalance_{n}"] = x[market + "_imbalance"].rolling(n).mean()
    x["flow_divergence"] = x.perp_imbalance - x.spot_imbalance
    x["volatility"] = x.return_15m.rolling(96).std()
    x["downside_volatility"] = x.return_15m.clip(upper=0).rolling(96).std()
    x["volatility_of_volatility"] = x.volatility.rolling(96).std()
    x["abnormal_volume"] = robust_z(x.perp_quote_volume, 30 * 96)
    x["spread"] = (x.ask - x.bid) / ((x.ask + x.bid) / 2)
    x["spread_relative"] = x.spread / x.spread.rolling(30 * 96).median()
    x["amihud"] = x.return_15m.abs() / x.perp_quote_volume.replace(0, np.nan)
    x["liquid"] = (
        (x.spread <= 0.002) & (x.perp_quote_volume >= 10000) & (x.spot_quote_volume >= 10000)
    )
    if persistence is not None and not persistence.empty:
        x = pd.merge_asof(
            x,
            persistence.sort_values("timestamp_ms"),
            on="timestamp_ms",
            direction="backward",
            tolerance=12 * 3600000,
        )
    else:
        x["funding_persistence"] = np.nan
        x["funding_efficiency"] = np.nan
    return x.replace([np.inf, -np.inf], np.nan)


def align_inputs(
    perp: pd.DataFrame,
    spot: pd.DataFrame,
    oi: pd.DataFrame,
    funding: pd.DataFrame,
    snapshots: pd.DataFrame,
) -> pd.DataFrame:
    if any(f.empty for f in (perp, spot, oi, funding, snapshots)):
        raise SafeError(
            "honest features require OI, funding and historical quotes; collect missing coverage"
        )
    p = perp[["timestamp_ms", "close", "quote_volume", "taker_buy_quote"]].rename(
        columns={
            "close": "perp_mid",
            "quote_volume": "perp_quote_volume",
            "taker_buy_quote": "perp_taker_buy_quote",
        }
    )
    s = spot[["timestamp_ms", "close", "quote_volume", "taker_buy_quote"]].rename(
        columns={
            "close": "spot_mid",
            "quote_volume": "spot_quote_volume",
            "taker_buy_quote": "spot_taker_buy_quote",
        }
    )
    x = p.merge(s, on="timestamp_ms", validate="one_to_one")
    for f, cols, tolerance in (
        (oi, ["open_interest"], 900000),
        (funding, ["funding_rate"], 12 * 3600000),
        (snapshots, ["bid", "ask"], 60000),
    ):
        x = pd.merge_asof(
            x.sort_values("timestamp_ms"),
            f[["timestamp_ms", *cols]].sort_values("timestamp_ms"),
            on="timestamp_ms",
            direction="backward",
            tolerance=tolerance,
        )
    # Kline close is an explicit proxy; use actual point-in-time midpoint where stored.
    return x
