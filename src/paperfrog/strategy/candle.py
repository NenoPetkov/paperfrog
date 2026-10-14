from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings


@dataclass(frozen=True)
class CandleRules:
    version: str = "candle-unwind-v1"
    lookback_bars: int = 20 * 96
    entry_z: float = 2.5
    minimum_entry_z: float = 2.0
    exit_z: float = 0.5
    oi_expansion: float = 0.02
    stop_fraction: float = 0.02
    max_hold_bars: int = 16
    assumed_half_spread_bps: float = 5.0

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


RULES = CandleRules()


def require_paper(s: Settings) -> None:
    if s.trading_mode != "paper" or s.live_trading_enabled:
        raise SafeError("candle experiment is PAPER-only and cannot authorize exchange trading")


def candle_features(frame: pd.DataFrame, rules: CandleRules = RULES) -> pd.DataFrame:
    """Closed candle indicators; every rolling window is backward-looking."""
    required = {
        "timestamp_ms",
        "close",
        "btc_close",
        "eth_close",
        "open_interest",
        "quote_volume",
        "spot_quote_volume",
        "taker_buy_quote",
    }
    if not required <= set(frame):
        raise SafeError("candle experiment input schema incomplete")
    x = frame.copy().sort_values("timestamp_ms").reset_index(drop=True)
    if x.timestamp_ms.duplicated().any() or (x.timestamp_ms.diff().dropna() != 900000).any():
        raise SafeError("candle experiment requires continuous closed 15-minute candles")
    prices = x[["close", "btc_close", "eth_close"]].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise SafeError("invalid candle prices")
    x["relative_log_price"] = (
        np.log(x.close) - 0.5 * np.log(x.btc_close) - 0.5 * np.log(x.eth_close)
    )
    rolling = x.relative_log_price.rolling(rules.lookback_bars, min_periods=rules.lookback_bars)
    x["relative_z"] = (x.relative_log_price - rolling.mean()) / rolling.std().replace(0, np.nan)
    x["oi_prior_expansion"] = x.open_interest.shift(2) / x.open_interest.shift(18) - 1
    x["oi_unwind"] = x.open_interest / x.open_interest.shift(2) - 1
    x["flow"] = 2 * x.taker_buy_quote / x.quote_volume.replace(0, np.nan) - 1
    z = x.relative_z
    side = np.sign(z)
    shrinking = (z.abs() < z.shift(1).abs()) & (z.shift(1).abs() < z.shift(2).abs())
    same_side = (side == np.sign(z.shift(1))) & (side == np.sign(z.shift(2)))
    flow_opposite = (x.flow * side < 0) & (x.flow.shift(1) * side < 0)
    x["crisis"] = (x.btc_close.pct_change().abs() >= 0.04) | (
        x.btc_close.pct_change(4).abs() >= 0.07
    )
    finite = np.isfinite(
        x[["relative_z", "oi_prior_expansion", "oi_unwind", "flow"]].to_numpy()
    ).all(axis=1)
    eligible = (
        finite
        & shrinking
        & same_side
        & flow_opposite
        & (z.shift(2).abs() >= rules.entry_z)
        & (z.abs() >= rules.minimum_entry_z)
        & (x.oi_prior_expansion >= rules.oi_expansion)
        & (x.oi_unwind < 0)
        & (x.quote_volume >= 10000)
        & (x.spot_quote_volume >= 10000)
        & ~x.crisis
    )
    x["direction"] = np.where(eligible, -side, 0).astype(int)
    return x


def load_candles(s: Settings, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    require_paper(s)
    if symbol not in s.symbols or symbol in {"BTCUSDT", "ETHUSDT"}:
        raise SafeError("choose a configured target distinct from BTC and ETH")
    if start_ms >= end_ms or start_ms % 900000 or end_ms % 900000:
        raise SafeError("experiment range requires aligned closed 15-minute boundaries")
    lake = Lake(s.data_dir)
    binding = s.data_dir / "market-environment.json"
    if (
        not binding.exists()
        or json.loads(binding.read_text()).get("environment") != "https://fapi.binance.com"
    ):
        raise SafeError("candle experiment requires a production public-data lake")
    frames = {}
    for asset in (symbol, "BTCUSDT", "ETHUSDT"):
        raw = lake.read("perp_15m", asset)
        if raw.empty:
            raise SafeError("futures candle history missing")
        raw = raw[raw.timestamp_ms.between(start_ms + 900000, end_ms)].copy()
        required = {
            "timestamp_ms",
            "open_ms",
            "open",
            "high",
            "low",
            "close",
            "quote_volume",
            "taker_buy_quote",
        }
        if not required <= set(raw) or len(raw) != (end_ms - start_ms) // 900000:
            raise SafeError("requested candle history is incomplete")
        if (raw.timestamp_ms.diff().dropna() != 900000).any() or not (
            raw.open_ms == raw.timestamp_ms - 900000
        ).all():
            raise SafeError("candle chronology or availability invalid")
        values = raw[["open", "high", "low", "close", "quote_volume"]].to_numpy(dtype=float)
        if (
            not np.isfinite(values).all()
            or (values <= 0).any()
            or (raw.high < raw[["open", "close", "low"]].max(axis=1)).any()
            or (raw.low > raw[["open", "close", "high"]].min(axis=1)).any()
        ):
            raise SafeError("invalid OHLC or volume observations")
        frames[asset] = raw
    x = frames[symbol].copy()
    for asset, prefix in (("BTCUSDT", "btc"), ("ETHUSDT", "eth")):
        columns = ["timestamp_ms", "open", "high", "low", "close", "quote_volume"]
        x = x.merge(
            frames[asset][columns].rename(
                columns={c: prefix + "_" + c for c in columns if c != "timestamp_ms"}
            ),
            on="timestamp_ms",
            validate="one_to_one",
        )
    spot = lake.read("spot_15m", symbol)
    if spot.empty:
        raise SafeError("target spot volume history missing")
    x = x.merge(
        spot[["timestamp_ms", "quote_volume"]].rename(
            columns={"quote_volume": "spot_quote_volume"}
        ),
        on="timestamp_ms",
        how="left",
        validate="one_to_one",
    )
    oi = lake.read("open_interest", symbol)
    if oi.empty:
        raise SafeError("target open-interest history missing")
    oi = oi[np.isfinite(oi.open_interest) & (oi.open_interest > 0)]
    x = pd.merge_asof(
        x.sort_values("timestamp_ms"),
        oi[["timestamp_ms", "open_interest"]].sort_values("timestamp_ms"),
        on="timestamp_ms",
        direction="backward",
        tolerance=900000,
    )
    for asset, prefix in ((symbol, "target"), ("BTCUSDT", "btc"), ("ETHUSDT", "eth")):
        funding = lake.read("funding", asset)
        if funding.empty or not {"timestamp_ms", "funding_rate", "mark"} <= set(funding):
            raise SafeError("actual funding settlements missing")
        if (
            not np.isfinite(funding[["funding_rate", "mark"]].to_numpy()).all()
            or (funding.mark <= 0).any()
        ):
            raise SafeError("invalid funding settlements")
        funding = funding.copy()
        funding["open_ms"] = funding.timestamp_ms // 900000 * 900000
        funding["unit_cost"] = funding.funding_rate * funding.mark
        for name, selected in (
            ("at_open", funding.timestamp_ms == funding.open_ms),
            ("after_open", funding.timestamp_ms > funding.open_ms),
        ):
            costs = funding[selected].groupby("open_ms").unit_cost.sum()
            x[f"{prefix}_funding_{name}"] = x.open_ms.map(costs).fillna(0.0)
    return candle_features(x)
