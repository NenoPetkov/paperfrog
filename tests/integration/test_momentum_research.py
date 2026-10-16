from dataclasses import replace
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from pydantic import ValidationError

from paperfrog.domain.types import Quote, SafeError, State
from paperfrog.simple.backtest import replay
from paperfrog.simple.daily import daily_atr
from paperfrog.simple.engine import Engine
from paperfrog.simple.research import candidates, monthly_performance
from paperfrog.simple.research_budget import OfflineResearchSettings, offline_budget
from paperfrog.simple.strategy import BAR_MS, RULES, exit_reason, indicators
from paperfrog.storage.database import Store


def history(n=300):
    close = 100 * np.exp(np.arange(n) * 0.0025)
    opened = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "timestamp_ms": (np.arange(n) + 1) * BAR_MS,
            "open": opened,
            "high": close * 1.002,
            "low": opened * 0.998,
            "close": close,
            "quote_volume": 100_000_000.0,
            "taker_buy_quote": 70_000_000.0,
        }
    )


def test_custom_indicators_remain_causal_and_defaults_unchanged():
    rule = candidates()[0]
    raw = history()
    earlier = indicators(raw.iloc[:250], rule)
    raw.loc[250:, ["open", "high", "low", "close"]] *= 100
    assert_frame_equal(earlier, indicators(raw, rule).iloc[:250])
    assert RULES.maximum_hold_bars == 16
    assert RULES.breakout_bars == 12
    position = {"side": 1, "opened_ms": 0, "stop_price": 90}
    assert exit_reason(position, 100, 16 * BAR_MS, None, rule) is None
    assert "holding" in exit_reason(position, 100, rule.maximum_hold_bars * BAR_MS, None, rule)


@pytest.mark.parametrize(
    "changes",
    [
        {"slow_ema": 193},
        {"breakout_bars": 192},
        {"maximum_hold_bars": 2689},
        {"minimum_flow": float("nan")},
        {"assumed_half_spread_bps": 0},
        {"max_atr_fraction": 0.02},
    ],
)
def test_invalid_or_optimistic_research_rules_are_rejected(changes):
    with pytest.raises(SafeError, match="research rule"):
        replace(RULES, **changes)


async def test_wider_stop_reduces_size_without_increasing_planned_cash_risk(
    settings, store, filters
):
    rule = candidates("sustained_trend")[0]
    engine = Engine(settings, store, filters, rule)
    await engine.restore()
    now = 200 * BAR_MS
    store.put("simple_selected", ["SOLUSDT"])
    await engine.quote(
        Quote(
            "SOLUSDT",
            Decimal("99.95"),
            Decimal("100.05"),
            Decimal(100000),
            Decimal(100000),
            Decimal(100),
            now,
        ),
        now,
    )
    await engine.resume(now)
    await engine.enter(
        "SOLUSDT",
        {
            "timestamp_ms": now,
            "close": 100.0,
            "trend_atr": 3.0,
            "prior_high": 99.0,
            "prior_low": 98.0,
            "atr": 0.8,
            "atr_fraction": 0.008,
            "flow": 0.4,
        },
        now,
    )
    trade = engine.positions()["SOLUSDT"]
    reservation = store.rows("entries")[0]
    assert trade["initial_stop_distance"] / trade["entry_price"] == pytest.approx(0.048)
    assert reservation["gross"] < 0.5 * settings.paper_equity * settings.max_target_notional
    assert reservation["risk"] <= settings.paper_equity * settings.risk_per_trade
    assert engine.s == settings


async def test_memory_research_matches_disk_and_archives_same_risk(settings, tmp_path):
    rule = candidates()[0]
    # BTC remains the market reference; the synthetic alt is the tradable leg.
    frames = {symbol: indicators(history(), rule) for symbol in ("BTCUSDT", "SOLUSDT")}
    funding = {
        "BTCUSDT": pd.DataFrame(
            {
                "timestamp_ms": [0],
                "funding_rate": [0.0],
                "mark": [100.0],
            }
        )
    }
    funding["SOLUSDT"] = funding["BTCUSDT"].copy()
    results = []
    for name, memory in (("disk", False), ("memory", True)):
        output = tmp_path / name
        results.append(
            await replay(
                frames,
                funding,
                settings,
                192 * BAR_MS,
                300 * BAR_MS,
                output,
                rules=rule,
                memory_ledger=memory,
            )
        )
        ledger = Store(output / "paper-risk.sqlite")
        entries = ledger.rows("entries", 10000)
        equity = pd.read_parquet(output / "equity.parquet").set_index("timestamp_ms").equity
        assert entries
        assert all(e["risk"] <= equity.loc[e["ts"]] * settings.risk_per_trade for e in entries)
        assert all(
            e["gross"] <= equity.loc[e["ts"]] * settings.max_target_notional for e in entries
        )
        assert ledger.get("simple_positions") == {}
        assert ledger.get("simple_rules")["version"] == rule.version
        ledger.engine.dispose()
    assert results[0] == results[1]
    assert_frame_equal(
        pd.read_parquet(tmp_path / "disk" / "equity.parquet"),
        pd.read_parquet(tmp_path / "memory" / "equity.parquet"),
    )


async def test_open_positions_reject_rule_changes(settings, store, filters):
    engine = Engine(settings, store, filters)
    await engine.restore()
    now = 200 * BAR_MS
    store.put("simple_selected", ["SOLUSDT"])
    await engine.quote(
        Quote(
            "SOLUSDT",
            Decimal("99.95"),
            Decimal("100.05"),
            Decimal(100000),
            Decimal(100000),
            Decimal(100),
            now,
        ),
        now,
    )
    await engine.resume(now)
    await engine.enter(
        "SOLUSDT",
        {
            "timestamp_ms": now,
            "close": 100.0,
            "trend_atr": 2.0,
            "prior_high": 99.0,
            "prior_low": 98.0,
            "atr": 0.8,
            "atr_fraction": 0.008,
            "flow": 0.4,
        },
        now,
    )
    assert engine.positions()
    with pytest.raises(SafeError, match="changing strategy rules"):
        Engine(settings, store, filters, candidates()[0])
    assert store.get("simple_rules")["version"] == RULES.version
    legacy = store.get("simple_rules")
    for key in (
        "minimum_stop_fraction",
        "maximum_stop_fraction",
        "entry_style",
        "allowed_side",
        "warmup_bars",
        "atr_timeframe_bars",
    ):
        legacy.pop(key)
    store.put("simple_rules", legacy)
    restarted = Engine(settings, store, filters)
    await restarted.restore()
    assert restarted.positions() == engine.positions()
    assert store.get("state") == State.PAUSED


def test_weekly_rules_need_full_warmup_and_cannot_see_future():
    rule = candidates("weekly_trend")[0]
    raw = history(3300)
    earlier = indicators(raw.iloc[:3200], rule)
    assert earlier.fast.iloc[: rule.warmup_bars - 1].isna().all()
    assert earlier.fast.iloc[-1] > 0
    raw.loc[3200:, ["open", "high", "low", "close"]] *= 100
    assert_frame_equal(earlier, indicators(raw, rule).iloc[:3200])


def test_daily_volatility_ignores_unfinished_day_and_future_candles():
    raw = history(1700)
    earlier = daily_atr(raw.iloc[:1500])
    raw.loc[1500:, ["open", "high", "low", "close"]] *= 100
    pd.testing.assert_series_equal(earlier, daily_atr(raw).iloc[:1500])
    assert earlier.iloc[: 14 * 96 - 1].isna().all()
    assert earlier.iloc[14 * 96 - 1] > 0
    assert earlier.iloc[14 * 96 - 1] == earlier.iloc[14 * 96 + 50]


@pytest.mark.parametrize("mode", ["testnet", "live"])
def test_research_candidates_cannot_enable_exchange_execution(settings, store, filters, mode):
    s = settings.model_copy(update={"trading_mode": mode})
    original_state = store.get("state", State.PAUSED)
    with pytest.raises(SafeError, match="no live approval"):
        Engine(s, store, filters, candidates()[0])
    assert store.get("state", State.PAUSED) == original_state


def test_monthly_account_returns_compound_and_flag_partial_months():
    timestamps = pd.to_datetime(
        [
            "2026-01-15",
            "2026-02-01",
            "2026-03-01",
            "2026-03-15",
        ],
        utc=True,
    )
    equity = pd.DataFrame(
        {
            "timestamp_ms": timestamps.as_unit("ms").astype("int64"),
            "equity": [10000.0, 11000.0, 9900.0, 10890.0],
        }
    )
    months, summary = monthly_performance(equity)
    assert months.month.tolist() == ["2026-01", "2026-02", "2026-03"]
    assert months.complete_month.tolist() == [False, True, False]
    assert months.net_return.tolist() == pytest.approx([0.1, -0.1, 0.1])
    assert (1 + months.net_return).prod() == pytest.approx(10890 / 10000)
    assert summary["complete_months"] == 1
    assert summary["worst_complete_month"] == pytest.approx(-0.1)
    assert summary["complete_months_at_least_20pct"] == 0
    assert not summary["monthly_target_supported"]


def test_monthly_returns_reject_duplicate_or_invalid_observations():
    for values in ([10000.0, float("nan")], [10000.0, 0.0]):
        with pytest.raises(SafeError, match="finite chronological equity"):
            monthly_performance(pd.DataFrame({"timestamp_ms": [1, 2], "equity": values}))
    with pytest.raises(SafeError, match="finite chronological equity"):
        monthly_performance(pd.DataFrame({"timestamp_ms": [1, 1], "equity": [10000, 11000]}))


def test_offline_higher_risk_profile_is_validated_without_changing_default_limits(settings):
    research = offline_budget(settings)
    assert research.trading_mode == "paper"
    assert not research.live_trading_enabled
    assert research.risk_per_trade == 0.02
    assert research.high_water_drawdown == 0.20
    assert settings.risk_per_trade == 0.002
    assert settings.high_water_drawdown == 0.06
    with pytest.raises(ValidationError):
        type(settings)(_env_file=None, risk_per_trade=0.02)


@pytest.mark.parametrize(
    "changes",
    [
        {"trading_mode": "live"},
        {"trading_mode": "testnet"},
        {"live_trading_enabled": True},
        {"risk_per_trade": 0.021},
        {"high_water_drawdown": 0.21},
        {"max_open_risk": 0.01},
    ],
)
def test_offline_budget_cannot_enable_live_or_exceed_declared_limits(changes):
    with pytest.raises(ValidationError):
        OfflineResearchSettings(_env_file=None, **changes)


def test_offline_budget_ignores_environment_flags_and_credentials(settings, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("RISK_PER_TRADE", "0.5")
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-key-never-load-in-offline-research")
    research = offline_budget(settings)
    assert research.trading_mode == "paper"
    assert research.risk_per_trade == 0.02
    assert research.binance_api_key.get_secret_value() == ""
    assert research.telegram_bot_token.get_secret_value() == ""
