from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from paperfrog.backtest.candle import CandleStress, replay_candles
from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings
from paperfrog.strategy.candle import RULES, candle_features, load_candles


def sample_frame(n=2600):
    rng = np.random.default_rng(42)
    btc = rng.normal(0, 0.001, n)
    eth = rng.normal(0, 0.001, n)
    target = 0.4 * btc + 0.3 * eth + rng.normal(0, 0.0001, n)
    frame = pd.DataFrame({"timestamp_ms": (np.arange(n) + 1) * 900000})
    for prefix, returns in (("", target), ("btc_", btc), ("eth_", eth)):
        prices = 100 * np.exp(np.cumsum(returns))
        frame[prefix + "close"] = prices
        frame[prefix + "open"] = np.r_[100, prices[:-1]]
        frame[prefix + "high"] = np.maximum(frame[prefix + "open"], prices) * 1.0002
        frame[prefix + "low"] = np.minimum(frame[prefix + "open"], prices) * 0.9998
        frame[prefix + "quote_volume"] = 1_000_000
    frame["open_ms"] = frame.timestamp_ms - 900000
    frame["spot_quote_volume"] = 1_000_000
    frame["taker_buy_quote"] = 400_000
    frame["open_interest"] = 10000 + np.arange(n)
    for p in ("target", "btc", "eth"):
        frame[p + "_funding_at_open"] = 0.0
        frame[p + "_funding_after_open"] = 0.0
    return frame


def test_candle_features_are_prefix_invariant():
    source = sample_frame()
    prefix = candle_features(source.iloc[:2200])
    source.loc[2200:, ["close", "btc_close", "eth_close", "open_interest"]] *= 100
    assert_frame_equal(prefix, candle_features(source).iloc[:2200])


def test_missing_oi_blocks_candidates():
    source = sample_frame()
    source["open_interest"] = np.nan
    assert not candle_features(source).direction.any()


@pytest.mark.parametrize("mode", ["testnet", "live"])
def test_experiment_rejects_nonpaper_before_creating_output(tmp_path, mode):
    s = Settings(_env_file=None)
    s = s.model_copy(update={"trading_mode": mode})
    path = tmp_path / "forbidden"
    with pytest.raises(SafeError, match="PAPER-only"):
        replay_candles(pd.DataFrame(), s, "SOLUSDT", 0, 900000, path)
    assert not path.exists()


def test_cash_comparison_is_flat(settings, tmp_path):
    frame = candle_features(sample_frame(1100))
    result = replay_candles(
        frame, settings, "SOLUSDT", 0, 1100 * 900000, tmp_path / "cash", cash_only=True
    )
    assert result["net_return"] == 0 and result["trades"] == 0
    assert result["live_permitted"] is False


def replay_fixture():
    frame = candle_features(sample_frame(1100))
    # The execution tests inject one candidate independently of the signal formula.
    frame["direction"] = 0
    frame["relative_z"] = 2.0
    frame.loc[1000, "direction"] = 1
    return frame


def test_execution_is_later_bar_and_respects_risk_budget(settings, tmp_path):
    frame = replay_fixture()
    path = tmp_path / "trade"
    result = replay_candles(
        frame, settings, "SOLUSDT", 0, 1100 * 900000, path, rules=replace(RULES, max_hold_bars=1)
    )
    trades = pd.read_parquet(path / "trades.parquet")
    assert result["trades"] == 1
    assert trades.iloc[0].signal_ms == frame.iloc[1000].timestamp_ms
    assert trades.iloc[0].entry_ms == frame.iloc[1001].open_ms
    assert trades.iloc[0].planned_risk <= settings.paper_equity * settings.risk_per_trade + 1e-8
    assert trades.iloc[0].fees > 0
    assert not result["live_permitted"]
    assert not (settings.state_dir / "paperfrog.sqlite").exists()


def test_extra_delay_does_not_fill_at_signal_close(settings, tmp_path):
    frame = replay_fixture()
    path = tmp_path / "delay"
    replay_candles(
        frame,
        settings,
        "SOLUSDT",
        0,
        1100 * 900000,
        path,
        CandleStress("delay", 1, 2),
        rules=replace(RULES, max_hold_bars=1),
    )
    trade = pd.read_parquet(path / "trades.parquet").iloc[0]
    assert trade.entry_ms == frame.iloc[1002].open_ms


def test_entry_boundary_funding_is_not_credited(settings, tmp_path):
    frame = replay_fixture()
    frame.loc[1001, "target_funding_at_open"] = -100.0
    path = tmp_path / "funding"
    replay_candles(
        frame, settings, "SOLUSDT", 0, 1100 * 900000, path, rules=replace(RULES, max_hold_bars=1)
    )
    assert pd.read_parquet(path / "trades.parquet").iloc[0].funding == 0


def test_intrabar_stops_use_adverse_prices_and_cash_accounting(settings, tmp_path):
    frame = replay_fixture()
    frame.loc[1001, "low"] = frame.loc[1001, "open"] * 0.9
    path = tmp_path / "stop"
    result = replay_candles(frame, settings, "SOLUSDT", 0, 1100 * 900000, path)
    trade = pd.read_parquet(path / "trades.parquet").iloc[0]
    assert trade.reason == "conservative_intrabar_stop" and trade.pnl < 0
    assert result["ending_equity"] == pytest.approx(settings.paper_equity + trade.pnl)
    equity = pd.read_parquet(path / "equity.parquet")
    assert result["net_return"] == pytest.approx(equity.iloc[-1].equity / equity.iloc[0].equity - 1)


def test_oi_join_cannot_use_a_future_observation(settings):
    lake = Lake(settings.data_dir)
    lake.bind_environment("https://fapi.binance.com")
    frame = sample_frame(4)
    for symbol, prefix in (("SOLUSDT", ""), ("BTCUSDT", "btc_"), ("ETHUSDT", "eth_")):
        columns = [
            "timestamp_ms",
            "open_ms",
            prefix + "open",
            prefix + "high",
            prefix + "low",
            prefix + "close",
            prefix + "quote_volume",
        ]
        raw = frame[columns].rename(
            columns={c: c.removeprefix(prefix) for c in columns if prefix and c.startswith(prefix)}
        )
        raw["taker_buy_quote"] = 400000
        lake.append("perp_15m", symbol, raw, "fixture")
        lake.append(
            "funding",
            symbol,
            pd.DataFrame({"timestamp_ms": [0], "funding_rate": [0.0001], "mark": [100]}),
            "fixture",
        )
    lake.append("spot_15m", "SOLUSDT", frame[["timestamp_ms", "quote_volume"]], "fixture")
    lake.append(
        "open_interest",
        "SOLUSDT",
        pd.DataFrame({"timestamp_ms": [900001, 1800000], "open_interest": [100, 101]}),
        "fixture",
    )
    result = load_candles(settings, "SOLUSDT", 0, 3600000)
    assert pd.isna(result.iloc[0].open_interest)
    assert result.iloc[1].open_interest == 101
