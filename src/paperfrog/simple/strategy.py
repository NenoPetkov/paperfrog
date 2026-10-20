from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

import numpy as np
import pandas as pd

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings, SimpleEntryMode

from .daily import daily_atr
from .rotation import features as rotation_features

BAR_MS = 900_000
WARMUP = 192
VERSION = "simple-trend-v1"


def require_paper(s: Settings) -> None:
    if s.trading_mode != "paper" or s.live_trading_enabled:
        raise SafeError("simple strategy has no live approval; use PAPER mode")


def require_execution(s: Settings) -> None:
    if s.trading_mode == "paper":
        require_paper(s)
        return
    if s.strategy_profile != "simple" or not s.simple_live_research_waiver:
        raise SafeError(
            "simple strategy has no live approval; explicit local research waiver missing"
        )
    if s.trading_mode == "live" and not (
        s.binance_env == "live" and s.live_trading_enabled and s.i_understand_live_risk == "YES"
    ):
        raise SafeError("local live environment authorization missing")
    if s.trading_mode == "testnet" and (s.binance_env != "testnet" or s.live_trading_enabled):
        raise SafeError("testnet environment mismatch")
    if not s.binance_api_key.get_secret_value() or not s.binance_api_secret.get_secret_value():
        raise SafeError("exchange credentials missing")


@dataclass(frozen=True)
class Rules:
    version: str = VERSION
    fast_ema: int = 16
    slow_ema: int = 48
    breakout_bars: int = 12
    atr_bars: int = 16
    min_atr_fraction: float = 0.001
    max_atr_fraction: float = 0.0125
    minimum_trend_atr: float = 0.5
    minimum_flow: float = 0.05
    stop_atr: float = 2.0
    trailing_atr: float = 2.0
    maximum_hold_bars: int = 16
    assumed_half_spread_bps: float = 5.0
    minimum_stop_fraction: float = 0.006
    maximum_stop_fraction: float = 0.02
    entry_style: Literal["breakout", "trend_follow"] = "breakout"
    allowed_side: Literal["both", "long", "short"] = "both"
    warmup_bars: int = WARMUP
    atr_timeframe_bars: Literal[1, 96] = 1

    def __post_init__(self) -> None:
        if not (
            WARMUP <= self.warmup_bars <= 3072
            and 1 <= self.fast_ema < self.slow_ema <= self.warmup_bars
            and 1 <= self.breakout_bars < self.warmup_bars
            and 1 <= self.atr_bars < self.warmup_bars
            and 1 <= self.maximum_hold_bars <= 2688
            and self.atr_timeframe_bars in {1, 96}
            and self.atr_bars * self.atr_timeframe_bars <= self.warmup_bars
        ):
            raise SafeError("invalid research rule windows")
        values = (
            self.min_atr_fraction,
            self.max_atr_fraction,
            self.minimum_trend_atr,
            self.minimum_flow,
            self.stop_atr,
            self.trailing_atr,
            self.assumed_half_spread_bps,
            self.minimum_stop_fraction,
            self.maximum_stop_fraction,
        )
        if not all(math.isfinite(v) for v in values) or not (
            0
            < self.min_atr_fraction
            <= self.max_atr_fraction
            <= (0.0125 if self.atr_timeframe_bars == 1 else 0.10)
            and self.minimum_trend_atr > 0
            and 0 <= self.minimum_flow <= 1
            and self.stop_atr > 0
            and self.trailing_atr > 0
            and self.assumed_half_spread_bps >= 5
            and 0.006 <= self.minimum_stop_fraction <= self.maximum_stop_fraction <= 0.10
            and self.entry_style in {"breakout", "trend_follow"}
            and self.allowed_side in {"both", "long", "short"}
        ):
            raise SafeError("invalid research rule thresholds")

    def public(self) -> dict[str, Any]:
        return asdict(self)


RULES = Rules()
ACTIVE_RULES = replace(
    RULES,
    version="simple-trend-active-v1",
    breakout_bars=8,
    minimum_trend_atr=0.4,
    minimum_flow=0.03,
)


def execution_rules(s: Settings) -> Rules:
    rules = RULES
    if s.simple_entry_profile == "active":
        if s.simple_entry_mode != "trend":
            raise SafeError("active entry profile requires trend mode")
        rules = ACTIVE_RULES
    return replace(rules, maximum_hold_bars=s.simple_max_hold_hours * 4)


def strategy_name(mode: SimpleEntryMode, rules: Rules = RULES) -> str:
    if mode == "trend":
        return rules.version
    if mode == "reverse":
        return "simple-breakout-reverse-v1"
    if mode == "failed_breakout":
        return "simple-failed-breakout-v1"
    raise SafeError("unknown simple strategy mode")


def indicators(raw: pd.DataFrame, rules: Rules = RULES) -> pd.DataFrame:
    """All information is available at the close timestamp, including breakout bounds."""
    required = {"timestamp_ms", "open", "high", "low", "close", "quote_volume", "taker_buy_quote"}
    if not required <= set(raw):
        raise SafeError("simple candle schema incomplete")
    x = raw.sort_values("timestamp_ms").reset_index(drop=True).copy()
    if x.timestamp_ms.duplicated().any() or (x.timestamp_ms.diff().dropna() != BAR_MS).any():
        raise SafeError("simple candles must be continuous and unique")
    values = x[["open", "high", "low", "close", "quote_volume", "taker_buy_quote"]].to_numpy(
        dtype=float
    )
    if not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4:] < 0).any():
        raise SafeError("invalid simple candle observations")
    if (
        (x.high < x[["open", "close", "low"]].max(axis=1))
        | (x.low > x[["open", "close", "high"]].min(axis=1))
        | (x.taker_buy_quote > x.quote_volume)
    ).any():
        raise SafeError("invalid simple OHLC or flow bounds")
    previous = x.close.shift(1)
    tr = pd.concat(
        [x.high - x.low, (x.high - previous).abs(), (x.low - previous).abs()], axis=1
    ).max(axis=1)
    x["atr"] = (
        tr.rolling(rules.atr_bars).mean()
        if rules.atr_timeframe_bars == 1
        else daily_atr(x, rules.atr_bars)
    )
    x["atr_fraction"] = x.atr / x.close
    for name, span in (("fast", rules.fast_ema), ("slow", rules.slow_ema)):
        weights = (1 - 2 / (span + 1)) ** np.arange(rules.warmup_bars - 1, -1, -1)
        weights = weights / weights.sum()
        x[name] = x.close.rolling(rules.warmup_bars).apply(
            lambda prices, w=weights: float(np.dot(prices, w)), raw=True
        )
    x["trend_atr"] = (x.fast - x.slow) / x.atr.replace(0, np.nan)
    x["prior_high"] = x.high.shift(1).rolling(rules.breakout_bars).max()
    x["prior_low"] = x.low.shift(1).rolling(rules.breakout_bars).min()
    x["daily_volume"] = x.quote_volume.rolling(96).sum()
    x["flow"] = 2 * x.taker_buy_quote / x.quote_volume.replace(0, np.nan) - 1
    # Stable volatility and directional persistence rank opportunities; no future return is used.
    x["score"] = x.trend_atr.abs().clip(upper=5) * np.sqrt(x.daily_volume / 10_000_000)
    # A failed breakout needs an earlier completed breakout and a later rejection.
    previous_up = (
        (x.trend_atr.shift(1) >= rules.minimum_trend_atr)
        & (x.close.shift(1) > x.prior_high.shift(1))
        & (x.flow.shift(1) >= rules.minimum_flow)
        & x.atr_fraction.shift(1).between(rules.min_atr_fraction, rules.max_atr_fraction)
    )
    previous_down = (
        (x.trend_atr.shift(1) <= -rules.minimum_trend_atr)
        & (x.close.shift(1) < x.prior_low.shift(1))
        & (x.flow.shift(1) <= -rules.minimum_flow)
        & x.atr_fraction.shift(1).between(rules.min_atr_fraction, rules.max_atr_fraction)
    )
    x["failed_up_breakout"] = (
        previous_up
        & (x.close < x.prior_high.shift(1))
        & (x.close < x.open)
        & (x.flow <= -rules.minimum_flow)
    )
    x["failed_down_breakout"] = (
        previous_down
        & (x.close > x.prior_low.shift(1))
        & (x.close > x.open)
        & (x.flow >= rules.minimum_flow)
    )
    x["rejection_level"] = np.where(
        previous_up, x.prior_high.shift(1), np.where(previous_down, x.prior_low.shift(1), np.nan)
    )
    return rotation_features(x)


def choose(rows: dict[str, dict[str, Any]], s: Settings, rules: Rules = RULES) -> list[str]:
    candidates = []
    for symbol, row in rows.items():
        if symbol == "BTCUSDT":
            continue
        values = [row.get(k, math.nan) for k in ("score", "atr_fraction", "daily_volume")]
        if not all(math.isfinite(float(v)) for v in values):
            continue
        if symbol in s.denylist or row["daily_volume"] < s.min_daily_quote_volume:
            continue
        if not rules.min_atr_fraction <= row["atr_fraction"] <= rules.max_atr_fraction:
            continue
        if s.simple_btc_reference_enabled and row.get("btc_pair_valid") is not True:
            continue
        candidates.append(symbol)
    # First bound the pool using trailing liquidity, then rank tradeability.
    pool = sorted(candidates, key=lambda k: (-float(rows[k]["daily_volume"]), k))[
        : s.simple_scan_limit
    ]
    return sorted(
        pool,
        key=lambda k: (
            -float(rows[k]["score"])
            * (
                float(rows[k].get("btc_reference_score", 0))
                if s.simple_btc_reference_enabled
                else 1
            ),
            k,
        ),
    )[: s.simple_selected_count]


def entry(
    row: dict[str, Any],
    one_way_cost: float,
    mode: SimpleEntryMode = "trend",
    rules: Rules = RULES,
) -> tuple[int, str]:
    strategy_name(mode, rules)
    keys = ("trend_atr", "flow", "atr_fraction", "close", "prior_high", "prior_low")
    if not all(math.isfinite(float(row.get(k, math.nan))) for k in keys):
        return 0, "insufficient closed-candle history"
    if not rules.min_atr_fraction <= row["atr_fraction"] <= rules.max_atr_fraction:
        return 0, "volatility outside the permitted range"
    if (
        min(rules.maximum_stop_fraction, rules.stop_atr * row["atr_fraction"])
        <= 2 * one_way_cost + 0.0035
    ):
        return 0, "price range too small relative to assumed trading costs"
    if mode == "reverse":
        direction, reason = entry(row, one_way_cost, rules=rules)
        if not direction:
            return 0, reason
        side = "SHORT" if direction > 0 else "LONG"
        return (
            -direction,
            f"{side}: reversal experiment takes the opposite side of the original breakout; reversion is unproven",
        )
    if mode == "failed_breakout":
        if not math.isfinite(float(row.get("rejection_level", math.nan))):
            return 0, "waiting for a completed breakout followed by rejection"
        if row.get("failed_up_breakout") is True:
            return (
                -1,
                "SHORT: earlier upward breakout failed, price returned below its level and selling flow confirms rejection",
            )
        if row.get("failed_down_breakout") is True:
            return (
                1,
                "LONG: earlier downward breakout failed, price returned above its level and buying flow confirms rejection",
            )
        return 0, "waiting for a completed breakout followed by rejection"
    if rules.entry_style == "trend_follow":
        if not math.isfinite(float(row.get("fast", math.nan))):
            return 0, "insufficient trend-following history"
        up = row["close"] > row["fast"]
        down = row["close"] < row["fast"]
        trigger = "price on the trend side of its fast average"
    else:
        up = row["close"] > row["prior_high"]
        down = row["close"] < row["prior_low"]
        trigger = "breakout through the prior range"
    if (
        row["trend_atr"] >= rules.minimum_trend_atr
        and up
        and row["flow"] >= rules.minimum_flow
        and rules.allowed_side != "short"
    ):
        return 1, f"LONG: upward trend, {trigger}, buying flow confirms"
    if (
        row["trend_atr"] <= -rules.minimum_trend_atr
        and down
        and row["flow"] <= -rules.minimum_flow
        and rules.allowed_side != "long"
    ):
        return (
            -1,
            f"SHORT: downward trend, {trigger}, selling flow confirms",
        )
    return 0, "waiting for a trend breakout confirmed by trading flow"


def exit_reason(
    position: dict[str, Any],
    mark: float,
    now_ms: int,
    row: dict[str, Any] | None,
    rules: Rules = RULES,
) -> str | None:
    side = int(position["side"])
    if side * (mark - float(position["stop_price"])) <= 0:
        return "protective stop or volatility trailing stop reached"
    if now_ms - int(position["opened_ms"]) >= rules.maximum_hold_bars * BAR_MS:
        return f"{rules.maximum_hold_bars / 4:g}-hour holding limit reached"
    if row is not None and math.isfinite(float(row.get("trend_atr", math.nan))):
        mode = position.get("entry_mode", "trend")
        if mode == "failed_breakout":
            if side * (float(row["close"]) - float(position["rejection_level"])) <= 0:
                return "failed-breakout thesis invalidated: price reclaimed the breakout level"
        elif (side if mode == "trend" else -side) * float(row["trend_atr"]) < 0:
            return (
                "recent trend reversed against the position"
                if mode == "trend"
                else "original breakout trend ended; reversal experiment exit"
            )
    return None


def tighten_stop(position: dict[str, Any], mark: float, atr: float, rules: Rules = RULES) -> float:
    """Never loosen a stop. Activate trailing after the initial planned stop distance is earned."""
    old = float(position["stop_price"])
    initial = float(position["initial_stop_distance"])
    side = int(position["side"])
    if (
        not math.isfinite(atr)
        or atr <= 0
        or side * (mark - float(position["entry_price"])) < initial
    ):
        return old
    candidate = mark - side * rules.trailing_atr * atr
    return max(old, candidate) if side > 0 else min(old, candidate)
