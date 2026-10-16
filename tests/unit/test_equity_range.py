from dataclasses import replace

import pytest
from pydantic import ValidationError

from paperfrog.domain.types import SafeError
from paperfrog.risk.engine import Proposal, RiskEngine
from paperfrog.risk.sizing import equity_margin_bounds, require_equity_margin_allocation
from paperfrog.settings import Settings


def range_settings(settings, **changes):
    return Settings(
        _env_file=None,
        **{
            **settings.public(),
            "simple_sizing_mode": "equity_range",
            "simple_position_margin_cap": 0.5,
            "risk_per_trade": 0.04,
            "max_open_risk": 0.05,
            "max_target_notional": 1.6,
            "max_gross_notional": 2.0,
            "max_net_beta": 2.0,
            **changes,
        },
    )


@pytest.mark.parametrize("cap", [0.09, 0.51, 0.8])
def test_equity_range_rejects_caps_outside_ten_to_fifty_percent(settings, cap):
    with pytest.raises(ValidationError, match="10% to 50%"):
        range_settings(settings, simple_position_margin_cap=cap)


def test_equity_range_is_explicit_and_displays_its_policy(settings):
    assert settings.trading_mode == "paper" and settings.simple_sizing_mode == "risk"
    s = range_settings(settings)
    parameters = s.simple_parameters()
    assert parameters["simple_position_margin_min"] == 0.1
    assert parameters["simple_position_margin_cap"] == 0.5
    assert parameters["simple_remaining_margin_fallback"] is True
    assert parameters["simple_liquidity_cap_enabled"] is False
    assert s.config_hash != settings.config_hash
    with pytest.raises(ValidationError, match="simple profile"):
        range_settings(settings, strategy_profile="legacy")


@pytest.mark.parametrize("notional", [100, 250, 500])
def test_normal_allocations_stay_between_ten_and_fifty_percent(settings, notional):
    bounds = require_equity_margin_allocation(range_settings(settings), 500, notional, 500, 1)
    assert not bounds.remaining_balance


@pytest.mark.parametrize("notional", [99.99, 500.01])
def test_ordinary_allocations_cannot_cross_either_boundary(settings, notional):
    with pytest.raises(SafeError):
        require_equity_margin_allocation(range_settings(settings), 500, notional, 500, 1)


def test_small_available_balance_is_used_after_fees_and_quantity_rounding(settings):
    s = range_settings(settings)
    bounds = equity_margin_bounds(s, 500, 20)
    assert bounds.remaining_balance and bounds.available_notional < 40
    assert require_equity_margin_allocation(s, 500, 39, 20, 1).remaining_balance
    with pytest.raises(SafeError, match="fully allocated"):
        require_equity_margin_allocation(s, 500, 30, 20, 1)
    with pytest.raises(SafeError, match="after entry fee"):
        require_equity_margin_allocation(s, 500, 40, 20, 1)


@pytest.mark.parametrize(
    "equity,available", [(0, 20), (500, -1), (float("nan"), 20), (500, float("inf"))]
)
def test_equity_range_fails_closed_on_invalid_account_inputs(settings, equity, available):
    with pytest.raises(SafeError):
        equity_margin_bounds(range_settings(settings), equity, available)


def range_proposal(**changes):
    return replace(
        Proposal("first", "SOLUSDT", 500, 500, 500, 12, 1, 0.02, 0.5, 0.5, 500, 1), **changes
    )


def test_range_reservation_removes_participation_cap_only_in_new_policy(
    settings, store, health, now
):
    risk = RiskEngine(range_settings(settings), store)
    risk.reserve(range_proposal(), health, now)
    assert store.rows("entries")[0]["gross"] == 500
    with pytest.raises(SafeError, match="liquidity"):
        RiskEngine(settings, store).reserve(
            Proposal("old", "ETHUSDT", 10000, 500, 1000, 13, 0, 0.01, 0.3, 0.5), health, now
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"target": 99, "gross": 99},
        {"target": 501, "gross": 501},
        {"available_margin": None},
        {"risk": 21},
    ],
)
def test_range_reservations_enforce_bounds_and_existing_risk(settings, store, health, now, changes):
    with pytest.raises(SafeError):
        RiskEngine(range_settings(settings), store).reserve(range_proposal(**changes), health, now)
    assert not store.rows("entries")


def test_range_low_balance_exception_does_not_override_locks(settings, store, health, now):
    risk = RiskEngine(range_settings(settings), store)
    store.put("manual_lock", True)
    with pytest.raises(SafeError, match="risk lock"):
        risk.reserve(
            range_proposal(target=39, gross=39, risk=1, net_beta=0.078, available_margin=20),
            health,
            now,
        )


def test_range_positions_share_the_existing_open_risk_budget(settings, store, health, now):
    risk = RiskEngine(range_settings(settings), store)
    risk.reserve(range_proposal(risk=13), health, now)
    with pytest.raises(SafeError, match="open risk"):
        risk.reserve(range_proposal(trade_id="second", symbol="ETHUSDT", risk=13), health, now)
    risk.reserve(range_proposal(trade_id="second", symbol="ETHUSDT", risk=12), health, now)
    assert sum(row["risk"] for row in store.rows("entries")) == 25
