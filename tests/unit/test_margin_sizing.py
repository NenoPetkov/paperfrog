from dataclasses import replace

import pytest
from pydantic import ValidationError

from paperfrog.domain.types import SafeError
from paperfrog.risk.engine import Proposal, RiskEngine
from paperfrog.settings import Settings


def margin_settings(settings, **changes):
    return Settings(
        _env_file=None,
        **{
            **settings.public(),
            "simple_sizing_mode": "margin",
            "risk_per_trade": 0.04,
            "max_open_risk": 0.05,
            "max_target_notional": 1.6,
            "max_gross_notional": 2.0,
            "max_net_beta": 2.0,
            **changes,
        },
    )


def test_larger_limits_require_explicit_opt_in(settings):
    with pytest.raises(ValidationError, match="explicit simple margin sizing"):
        margin_settings(settings, simple_sizing_mode="risk")
    with pytest.raises(ValidationError, match="simple profile"):
        margin_settings(settings, strategy_profile="legacy")
    with pytest.raises(ValidationError):
        margin_settings(settings, simple_position_margin_cap=0.81)
    with pytest.raises(ValidationError):
        margin_settings(settings, simple_total_margin_cap=1.01)
    assert settings.risk_per_trade == 0.002
    assert settings.trading_mode == "paper"


def test_margin_reservations_share_total_budget(settings, store, health, now):
    s = margin_settings(settings)
    engine = RiskEngine(s, store)
    first = Proposal("first", "SOLUSDT", 500, 800, 800, 16, 1.6, 0.02, 0.5, 0.001)
    engine.reserve(first, health, now)
    too_large = replace(
        first, trade_id="second", symbol="ETHUSDT", target=201, gross=201, risk=4.02, net_beta=0.402
    )
    with pytest.raises(SafeError, match="gross exposure cap"):
        engine.reserve(too_large, health, now)
    engine.reserve(replace(too_large, target=200, gross=200, risk=4, net_beta=0.4), health, now)
    assert sum(r["gross"] for r in store.rows("entries")) / 2 == 500


def test_margin_mode_keeps_loss_locks(settings, store, health, now):
    engine = RiskEngine(margin_settings(settings), store)
    store.put("manual_lock", True)
    with pytest.raises(SafeError, match="risk lock active"):
        engine.reserve(
            Proposal("first", "SOLUSDT", 500, 800, 800, 16, 1.6, 0.02, 0.5, 0.001), health, now
        )
