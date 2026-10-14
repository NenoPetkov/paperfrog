"""Daily volatility available only after a complete UTC day's candles have closed."""

from __future__ import annotations

import numpy as np
import pandas as pd


def daily_atr(raw: pd.DataFrame, days: int = 14) -> pd.Series:
    available = pd.DatetimeIndex(pd.to_datetime(raw.timestamp_ms, unit="ms", utc=True))
    x = raw.copy()
    # A candle closing at midnight belongs to the day that just ended.
    x.index = available - pd.Timedelta(1, "ms")
    daily = x.resample("1D").agg(
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        count=("timestamp_ms", "size"),
    )
    previous = daily.close.shift(1)
    tr = pd.concat(
        [
            daily.high - daily.low,
            (daily.high - previous).abs(),
            (daily.low - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr = tr.where(daily["count"] == 96, np.nan)
    volatility = tr.rolling(days).mean()
    volatility.index += pd.Timedelta(1, "D")
    values = volatility.reindex(available, method="ffill").to_numpy()
    return pd.Series(values, index=raw.index, name="atr")
