from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from paperfrog.domain.types import SafeError, State
from paperfrog.risk.engine import Proposal, RiskEngine, crisis_score
from paperfrog.settings import Settings
from paperfrog.storage.database import Store


def proposal(i="trade", symbol="SOLUSDT", **kwargs):
    return replace(Proposal(i, symbol, 10000, 500, 1000, 13, 0, 0.01, 0.3, 0.001), **kwargs)


@pytest.mark.parametrize("cap", [-1, 17, 100])
def test_cap_validation(cap):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_strategy_entries_per_day=cap)


@pytest.mark.parametrize(
    "field,value",
    [
        ("risk_per_trade", 0.003),
        ("max_open_risk", 0.006),
        ("max_target_notional", 0.1),
        ("max_gross_notional", 0.6),
        ("exchange_leverage", 3),
        ("margin_type", "CROSSED"),
    ],
)
def test_config_cannot_raise_hard_limits(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("state", [s for s in State if s != State.RUNNING])
def test_states_block_entries(settings, store, health, now, state):
    store.put("state", state)
    with pytest.raises(SafeError):
        RiskEngine(settings, store).reserve(proposal(), health, now)


@pytest.mark.parametrize(
    "changes",
    [
        {"market_age": 61},
        {"account_age": 61},
        {"reconciled": False},
        {"model_valid": False},
        {"filters_loaded": False},
        {"crisis_score": 1},
        {"event_allowed": False},
    ],
)
def test_health_blocks(settings, store, health, now, changes):
    with pytest.raises(SafeError):
        RiskEngine(settings, store).reserve(proposal(), replace(health, **changes), now)


@pytest.mark.parametrize(
    "changes",
    [
        {"target": 801},
        {"gross": 5001},
        {"risk": 21},
        {"net_beta": 0.101},
        {"liquidation_distance": 0.24},
        {"participation": 0.011},
        {"gross": float("nan")},
    ],
)
def test_proposal_caps(settings, store, health, now, changes):
    with pytest.raises(SafeError):
        RiskEngine(settings, store).reserve(proposal(**changes), health, now)


def test_daily_count_includes_aborted_entries(settings, store, health, now):
    risk = RiskEngine(settings, store)
    for i in range(12):
        risk.reserve(proposal(str(i), f"ALT{i}USDT"), health, now)
        risk.closed(str(i), 1, now)
    with pytest.raises(SafeError, match="frequency"):
        risk.reserve(proposal("13", "NEWUSDT"), health, now)


@pytest.mark.parametrize(
    "equity,expected",
    [(9875, State.DAILY_RISK_LOCK), (9650, State.WEEKLY_RISK_LOCK), (9400, State.HARD_RISK_LOCK)],
)
def test_exact_loss_boundaries(settings, store, now, equity, expected):
    risk = RiskEngine(settings, store)
    risk.anchors(10000, now)
    risk.anchors(equity, now)
    assert store.get("risk_lock")["state"] == expected


def test_profit_giveback_and_restart(settings, store, now):
    risk = RiskEngine(settings, store)
    risk.anchors(10000, now)
    risk.anchors(10100, now)
    assert store.get("risk_multiplier") == 0.5
    reopened = Store(Path(store.engine.url.database))
    RiskEngine(settings, reopened).anchors(10065, now)
    assert reopened.get("risk_lock")["reason"] == "profit_giveback"
    assert reopened.get("anchors")["day_equity"] == 10000


def test_profit_entry_lock(settings, store, now):
    risk = RiskEngine(settings, store)
    risk.anchors(10000, now)
    risk.anchors(10125, now)
    assert store.get("profit_entry_lock")


def test_cooldown_and_three_losses(settings, store, health, now):
    risk = RiskEngine(settings, store)
    risk.reserve(proposal("a"), health, now)
    risk.closed("a", -2, now)
    with pytest.raises(SafeError, match="cooldown"):
        risk.reserve(proposal("b"), health, now + timedelta(minutes=179))
    for i in range(2):
        risk.reserve(proposal(str(i), f"OTHER{i}USDT"), health, now)
        risk.closed(str(i), -1, now)
    assert store.get("risk_lock")["until"] == now.timestamp() + 43200


def test_crisis_exact_boundaries():
    assert (
        crisis_score(
            btc_5m=0.025,
            sigma_5m=0.001,
            btc_15m=0.04,
            sigma_15m=0.001,
            btc_1h=0.07,
            spread_ratio=1,
            dispersion_z=0,
            mark_deviation=0,
        )
        == 3
    )


def test_crisis_cooldown_and_single_reduction(store):
    from paperfrog.risk.crisis import CrisisMonitor, Observations

    monitor = CrisisMonitor(store)
    action = monitor.observe(1000, Observations(btc_5m=0.03, btc_15m=0.05))
    assert action.state == "CRISIS" and action.reduce_fraction == 0.5
    assert monitor.observe(1010, Observations(btc_5m=0.03, btc_15m=0.05)).reduce_fraction == 0
    assert monitor.observe(1100, Observations()).state == "CAUTION"
    assert monitor.observe(3000, Observations()).state == "NORMAL"
    assert monitor.observe(3001, Observations(), emergency=True).reduce_fraction == 1
