import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings
from paperfrog.simple import rotation
from paperfrog.simple.engine import Engine
from paperfrog.simple.momentum import classify
from paperfrog.simple.strategy import RULES, choose, execution_rules, exit_reason, require_execution


def script(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[2] / f"scripts/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(side=1, **changes):
    return {
        "entry_timeframe": "5m",
        "timestamp_ms": 300000,
        "open": 100,
        "close": 100 + side,
        "fast": 100,
        "slow": 100 - side,
        "fast_change": side,
        "trend_atr": side,
        "previous_trend_atr": side,
        "minute_atr": 1,
        "atr": 0.5,
        "atr_fraction": 0.005,
        "flow": side * 0.3,
        "return_5m": side * 0.01,
        "return_15m": side * 0.02,
        "return_25m": side * 0.02,
        "return_1h": side * 0.03,
        "path_efficiency_1h": 0.6,
        "path_efficiency_15m": 0.6,
        "momentum_trend_started_ms": 0,
        "momentum_trend_age_bars": 1,
        "rsi_14": 50,
        "prior_high": 105,
        "prior_low": 95,
        "daily_volume": 20_000_000,
        "score": 1,
        **changes,
    }


def settings(**changes):
    return Settings(
        _env_file=None,
        simple_entry_profile="momentum",
        simple_entry_timeframe="5m",
        simple_btc_reference_enabled=False,
        simple_market_trend_enabled=False,
        **changes,
    )


@pytest.mark.parametrize("side,label", [(1, "A"), (-1, "C")])
def test_abc_uses_coin_momentum_without_bitcoin_or_ether(side, label):
    pattern, direction, score = classify(row(side), 0.0012, RULES)
    assert (pattern, direction) == (label, side)
    assert score > 0


@pytest.mark.parametrize(
    "damage",
    [
        {"return_1h": -0.01},
        {"return_15m": -0.01},
        {"previous_trend_atr": -1},
        {"path_efficiency_1h": 0.2},
        {"path_efficiency_1h": float("nan")},
        {"flow": -0.1},
        {"atr_fraction": 0.0001},
        {"return_1h": None},
    ],
)
def test_ambiguous_invalid_or_expensive_pattern_waits(damage):
    assert classify(row(**damage), 0.0012, RULES) == ("B", 0, 0.0)


def test_top_ten_ranks_momentum_after_liquidity_filter():
    from paperfrog.simple.learning_replay import SYMBOLS

    s = settings(simple_selected_count=10)
    rows = {
        SYMBOLS[i]: row(return_1h=0.01 + i / 100, daily_volume=20_000_000) for i in range(12)
    }
    rows[SYMBOLS[0]]["daily_volume"] = 1_000_000_000
    rows["ILLIQUIDUSDT"] = row(return_1h=0.9, daily_volume=10)
    rows["BTCUSDT"] = row(return_1h=1)
    assert choose(rows, s, replace(execution_rules(s), version="abc-momentum-v1")) == [SYMBOLS[i] for i in range(11, 1, -1)]


@pytest.mark.parametrize(
    "changes",
    [
        {"trading_mode": "live"},
        {"simple_ai_enabled": True},
        {"simple_confidence_enabled": True},
    ],
)
def test_momentum_preserves_live_environment_gates_and_refuses_adaptive_exits(changes):
    with pytest.raises(ValidationError):
        settings(**changes)


def test_execution_boundary_requires_local_research_waiver_for_momentum():
    s = settings().model_copy(update={"trading_mode": "live", "live_trading_enabled": True})
    with pytest.raises(SafeError, match="waiver"):
        require_execution(s)


def test_confirmed_exit_never_delays_stop_or_time_limit():
    p = {
        "side": 1,
        "stop_price": 95,
        "opened_ms": 0,
        "momentum_exit_version": 1,
        "max_hold_hours": 4,
    }
    assert exit_reason(p, 100, 300000, row(trend_atr=-1)) is None
    assert "two completed" in exit_reason(p, 100, 600000, row(trend_atr=-1, previous_trend_atr=-1))
    assert "stop" in exit_reason(p, 94, 300000, row())
    assert "holding limit" in exit_reason(p, 100, 4 * 3600000, row())
    old = {k: v for k, v in p.items() if k != "momentum_exit_version"}
    assert "recent trend reversed" in exit_reason(old, 100, 300000, row(trend_atr=-1))


def test_engine_uses_abc_and_refuses_auto_mixing(store, filters):
    s = settings()
    engine = Engine(s, store, filters)
    assert engine.entry_signal(row(), 300000, 0.001)[0] == 1
    rotation.configure(store, "auto-active", 300000)
    assert engine.entry_signal(row(), 300000, 0.001)[0] == 0


def test_isolated_paper_runner_ignores_production_credentials_and_risk(monkeypatch):
    for key, value in {
        "TRADING_MODE": "live",
        "LIVE_TRADING_ENABLED": "true",
        "BINANCE_API_KEY": "test-only-never-a-real-key",
        "TELEGRAM_BOT_TOKEN": "test-only-never-a-real-token",
        "RISK_PER_TRADE": "0.04",
        "STATE_DIR": "/wrong",
    }.items():
        monkeypatch.setenv(key, value)
    s = script("run_momentum_paper").paper_settings(Path(__file__).resolve().parents[2])
    assert s.trading_mode == "paper" and not s.live_trading_enabled
    assert not s.binance_api_key.get_secret_value()
    assert not s.telegram_bot_token.get_secret_value()
    assert s.risk_per_trade == 0.002
    assert s.state_dir.name == "state-momentum-paper"
    assert s.health_port == 8082


def test_strict_activation_preserves_positions_losses_and_risk_locks(tmp_path):
    from paperfrog.storage.database import Store

    s = Settings(_env_file=None, state_dir=tmp_path / "state")
    ledger = Store(s.state_dir / "simple/paperfrog.sqlite")
    try:
        rotation.configure(ledger, "auto-active", 300000)
        saved = {
            "simple_positions": {"SOLUSDT": {"stop_price": 95}},
            "risk_lock": {"reason": "loss limit"},
            "manual_lock": True,
            "simple_closed": [{"pnl": -1}],
            "consecutive_losses": 3,
            "simple_activity_budget": {"attempts": [300000]},
        }
        for key, value in saved.items():
            ledger.put(key, value)
        script("server_ops").strict_auto(tmp_path, s)
        state = rotation.selection(ledger)
        assert state["auto"] and state["activity_mode"] == "strict"
        assert ledger.get("simple_learning") == {}
        assert {key: ledger.get(key) for key in saved} == saved
    finally:
        ledger.engine.dispose()


def test_portfolio_stops_on_entry_bar_before_later_profit():
    study = script("study_holding_momentum")
    tape = {
        now: {"open": 100, "high": 101, "low": 99.9, "close": 100}
        for now in range(0, 2 * study.HOUR, study.FIVE)
    }
    tape[0].update(low=95, high=110, close=109)
    features = {"X": {0: row(close=100, momentum_side=1, momentum_score=1)}}
    result = study.portfolio({"X": tape}, features, 0, 2 * study.HOUR, 1, "confirmed")
    assert result["trades"] == 1 and result["net"] < 0
    assert result["exit_reasons"] == {"stop": 1}
    assert result["median_hold_minutes"] == 5
    assert result["return"] == pytest.approx(result["net"] / 10000)


def test_portfolio_never_opens_before_signal_or_across_validation_boundary():
    study = script("study_holding_momentum")
    tape = {
        now: {"open": 100, "high": 101, "low": 99.9, "close": 100}
        for now in range(0, 2 * study.HOUR, study.FIVE)
    }
    # A signal in the last hour cannot finish before the split: no label leakage.
    features = {"X": {study.HOUR: row(momentum_side=1, momentum_score=1)}}
    result = study.portfolio({"X": tape}, features, 0, 2 * study.HOUR, 1, "confirmed")
    assert result["trades"] == 0 and result["net"] == 0
