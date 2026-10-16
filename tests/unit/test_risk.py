import json
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


@pytest.mark.parametrize("minutes", [0, -1, 721])
def test_loss_cooldown_cannot_be_disabled_or_invalid(minutes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, soft_loss_cooldown_minutes=minutes)


def test_five_minute_loss_lock_expires_but_preserves_loss_history(settings, store, health, now):
    s = Settings(_env_file=None, **{**settings.public(), "soft_loss_cooldown_minutes": 5})
    risk = RiskEngine(s, store)
    for i in range(3):
        risk.reserve(proposal(str(i), f"ALT{i}USDT"), health, now)
        risk.closed(str(i), -1, now)
    assert store.get("risk_lock")["until"] == now.timestamp() + 300
    with pytest.raises(SafeError, match="cannot be overridden"):
        risk.resume(health, now + timedelta(seconds=299))
    risk.resume(health, now + timedelta(minutes=5))
    assert store.get("state") == State.RUNNING
    assert store.get("consecutive_losses") == 3
    later = now + timedelta(minutes=6)
    risk.reserve(proposal("fourth", "OTHERUSDT"), health, later)
    risk.closed("fourth", -1, later)
    assert store.get("risk_lock")["until"] == later.timestamp() + 300
    assert store.get("consecutive_losses") == 4


def test_shorter_cooldown_does_not_shorten_an_existing_lock(settings, store, health, now):
    s = Settings(_env_file=None, **{**settings.public(), "soft_loss_cooldown_minutes": 5})
    lock = {
        "state": State.SOFT_RISK_LOCK,
        "until": now.timestamp() + 43200,
        "reason": "three_losses",
    }
    store.put("risk_lock", lock)
    with pytest.raises(SafeError, match="cannot be overridden"):
        RiskEngine(s, store).resume(health, now + timedelta(minutes=5))
    assert store.get("risk_lock") == lock


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


def account_settings(settings):
    return Settings(
        _env_file=None,
        **{
            **settings.public(),
            "daily_loss_limit": 0.125,
            "weekly_loss_limit": 0.35,
            "high_water_drawdown": 0.60,
            "profit_guard_scale": 10,
        },
    )


@pytest.mark.parametrize(
    "equity,expected",
    [
        (9875, None),
        (9650, None),
        (9400, None),
        (8750.01, None),
        (8750, State.DAILY_RISK_LOCK),
        (6500, State.WEEKLY_RISK_LOCK),
        (4000, State.HARD_RISK_LOCK),
    ],
)
def test_tenfold_account_loss_boundaries(settings, store, now, equity, expected):
    risk = RiskEngine(account_settings(settings), store)
    risk.anchors(10000, now)
    risk.anchors(equity, now)
    lock = store.get("risk_lock")
    assert (lock["state"] if lock else None) == expected


def test_tenfold_profit_protection_and_exact_giveback(settings, store, now):
    risk = RiskEngine(account_settings(settings), store)
    risk.anchors(10000, now)
    risk.anchors(10125, now)
    assert not store.get("profit_entry_lock")
    assert store.get("risk_multiplier") == 1
    risk.anchors(10750, now)
    assert store.get("risk_multiplier") == 0.5
    risk.anchors(10400.01, now)
    assert store.get("risk_lock") is None
    risk.anchors(10400, now)
    assert store.get("risk_lock")["reason"] == "profit_giveback"


def test_tenfold_profit_entry_boundary(settings, store, now):
    risk = RiskEngine(account_settings(settings), store)
    risk.anchors(10000, now)
    risk.anchors(11249.99, now)
    assert not store.get("profit_entry_lock")
    risk.anchors(11250, now)
    assert store.get("profit_entry_lock")


@pytest.mark.parametrize(
    "field,value",
    [
        ("daily_loss_limit", 0.126),
        ("weekly_loss_limit", 0.351),
        ("high_water_drawdown", 0.601),
        ("profit_guard_scale", 10.01),
        ("profit_guard_scale", 0),
        ("daily_loss_limit", float("nan")),
        ("profit_guard_scale", float("inf")),
    ],
)
def test_account_limits_remain_bounded(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("state", list(State))
def test_operator_resume_resets_all_financial_locks_without_losing_history(
    settings, store, health, now, state
):
    risk = RiskEngine(settings, store)
    risk.reserve(proposal(), health, now)
    risk.closed("trade", -1, now)
    risk.anchors(10000, now)
    risk.anchors(9400, now)
    lock = {"state": state, "until": None, "reason": "operator_review"}
    store.put("risk_lock", lock)
    store.put("state", state)
    store.put("manual_lock", True)
    store.put("profit_entry_lock", True)
    store.put("risk_multiplier", 0.5)
    store.put("consecutive_losses", 3)
    entries = store.rows("entries")
    anchors = store.get("anchors")
    risk.resume(health, now, override_locks=True, equity=9300)
    assert store.get("state") == State.RUNNING
    assert store.get("risk_lock") is None
    assert not store.get("manual_lock")
    assert not store.get("profit_entry_lock")
    assert store.get("risk_multiplier") == 1
    assert store.get("consecutive_losses") == 0
    assert store.get("cooldown:SOLUSDT") == 0
    assert store.rows("entries") == entries
    # The next tick at the acknowledged equity must not immediately re-lock.
    risk.anchors(9300, now)
    assert store.get("risk_lock") is None
    for key in ("equity", "day_equity", "week_equity", "peak", "high_water"):
        assert store.get("anchors")[key] == 9300
    reset = next(row for row in store.rows("audit") if row["kind"] == "operator_risk_reset")
    previous = json.loads(reset["payload"])
    assert previous["previous_anchors"] == anchors
    assert previous["previous_lock"] == lock
    assert previous["previous_consecutive_losses"] == 3
    assert previous["previous_cooldowns"]["cooldown:SOLUSDT"] > now.timestamp()
    risk.reserve(proposal("next", equity=9300), health, now)


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
def test_operator_reset_fails_atomically_on_unhealthy_inputs(settings, store, health, now, changes):
    risk = RiskEngine(settings, store)
    risk.anchors(10000, now)
    risk.anchors(9400, now)
    before = {key: store.get(key) for key in ("risk_lock", "anchors", "state")}
    with pytest.raises(SafeError):
        risk.resume(replace(health, **changes), now, override_locks=True, equity=9400)
    assert {key: store.get(key) for key in before} == before
    assert not any(row["kind"] == "operator_risk_reset" for row in store.rows("audit"))


@pytest.mark.parametrize("equity", [None, 0, -1, float("nan"), float("inf")])
def test_operator_reset_requires_valid_fresh_equity(settings, store, health, now, equity):
    store.put("manual_lock", True)
    with pytest.raises(SafeError, match="fresh positive equity"):
        RiskEngine(settings, store).resume(health, now, override_locks=True, equity=equity)
    assert store.get("manual_lock")


def test_operator_resume_does_not_arm_live(settings, store, health, now):
    s = settings.model_copy(update={"trading_mode": "live"})
    store.put("manual_lock", True)
    with pytest.raises(SafeError, match="local live arm missing"):
        RiskEngine(s, store).resume(health, now, override_locks=True, equity=10000)
    assert store.get("manual_lock")


def test_operator_resume_cannot_cancel_pending_emergency_close(settings, store, health, now):
    store.put("local_flatten_request", "local-emergency")
    store.put("manual_lock", True)
    with pytest.raises(SafeError, match="emergency close pending"):
        RiskEngine(settings, store).resume(health, now, override_locks=True, equity=10000)
    assert store.get("local_flatten_request") == "local-emergency"
    assert store.get("manual_lock")


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
