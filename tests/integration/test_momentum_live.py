import json
import time
from dataclasses import replace
from decimal import Decimal

import pytest
from test_abc_momentum import row, script
from test_simple_live import live_settings, setup_engine

from paperfrog.domain.types import EntryVeto, Order, Quote, SafeError
from paperfrog.settings import Settings
from paperfrog.simple.authorization import arm_simple, verify_simple_arm
from paperfrog.simple.momentum import classify, require_order
from paperfrog.simple.strategy import RULES, require_execution


def observation(**changes):
    return row(
        close=100,
        open=99.9,
        fast=99.8,
        slow=99.5,
        previous_fast=99.7,
        previous_close=99.8,
        previous_flow=0.2,
        trend_atr=1.2,
        previous_trend_atr=1.1,
        volume_ratio=2,
        day_return_4h=0.03,
        day_return_1h=0.02,
        day_slope_4h=0.02,
        day_mean_distance_4h=0.01,
        day_context_ms=0,
        **changes,
    )


def live(settings, **changes):
    return live_settings(
        settings, simple_entry_profile="momentum_selective", simple_entry_timeframe="5m", **changes
    )


def test_authorized_momentum_still_requires_exact_account_config_and_source_arm(settings):
    s = live(settings)
    require_execution(s)
    with pytest.raises(SafeError, match="arm missing"):
        verify_simple_arm(s)
    arm_simple(s, "ARM LIVE TRADING")
    assert verify_simple_arm(s)
    changed = s.model_copy(update={"simple_max_hold_hours": 8})
    with pytest.raises(SafeError, match="authorization changed"):
        verify_simple_arm(changed)
    assert Settings(_env_file=None).trading_mode == "paper"


@pytest.mark.parametrize(
    "profile,changes,expected",
    [
        ("momentum_selective", {}, 1),
        ("momentum_selective", {"path_efficiency_1h": 0.4}, 0),
        ("momentum_selective", {"day_return_4h": -0.01}, 0),
        ("momentum_selective", {"day_context_ms": -900000}, 0),
        ("momentum_breakout", {"prior_high": 99.9}, 1),
        ("momentum_breakout", {"prior_high": 101}, 0),
        ("momentum_pullback", {"previous_close": 99.6}, 1),
        ("momentum_pullback", {"previous_close": 99.9}, 0),
    ],
)
def test_completed_own_coin_setups(profile, changes, expected):
    obs = {**observation(), **changes}
    assert classify(obs, 0.00085, replace(RULES, version=f"abc-{profile}-v1"))[1] == expected


@pytest.mark.parametrize(
    "damage", [None, "unbound", "stale", "changed", "contrary", "wide", "cost", "context"]
)
def test_dispatch_independently_rechecks_full_momentum_permission(settings, store, damage):
    s = live(settings, max_spread=0.0003)
    now = 300001
    obs = observation()
    order = Order(
        client_id="pf-momentum",
        symbol="SOLUSDT",
        side=1,
        quantity=Decimal(1),
        limit_price=Decimal("100.02"),
    )
    quote = Quote(
        "SOLUSDT",
        Decimal("99.99"),
        Decimal("100.01"),
        Decimal(100),
        Decimal(100),
        Decimal(100),
        now,
    )
    if damage == "contrary":
        obs["day_return_4h"] = -0.01
    if damage == "cost":
        obs["atr_fraction"] = 0.001
    if damage == "context":
        obs["day_context_ms"] = -900000
    store.put("simple_minute:SOLUSDT", obs)
    store.put(
        "simple_minute_order:" + order.client_id,
        {"order": order.model_dump(mode="json"), "config_hash": s.config_hash, "row": obs},
    )
    if damage == "unbound":
        order = order.model_copy(update={"quantity": Decimal(2)})
    if damage == "stale":
        now = 600000
    if damage == "changed":
        store.put("simple_minute:SOLUSDT", {})
    if damage == "wide":
        quote = Quote(
            "SOLUSDT", Decimal(99), Decimal(101), Decimal(100), Decimal(100), Decimal(100), now
        )
    if damage is None:
        require_order(s, store, order, now, quote)
    else:
        with pytest.raises(EntryVeto):
            require_order(s, store, order, now, quote)
    require_order(s, store, order.model_copy(update={"reduce_only": True}), now, None)


async def test_live_momentum_entry_and_hosted_stop_with_simulated_exchange(
    settings, store, filters, monkeypatch
):
    fixed = time.time()
    monkeypatch.setattr(time, "time", lambda: fixed)
    engine, sim, adapter, _, now = await setup_engine(
        settings,
        store,
        filters,
        simple_entry_profile="momentum_selective",
        simple_entry_timeframe="5m",
        simple_sizing_mode="margin",
        simple_position_margin_cap=0.1,
    )
    boundary = now // 300000 * 300000
    obs = {**observation(), "timestamp_ms": boundary, "day_context_ms": boundary // 900000 * 900000}
    store.put("simple_minute:SOLUSDT", obs)
    try:
        await engine.enter("SOLUSDT", obs, now)
        assert sim.quantity > 0 and sim.stops
        p = engine.positions()["SOLUSDT"]
        assert p["momentum_exit_version"] == 1
        assert p["entry_context"]["entry_profile"] == "momentum_selective"
    finally:
        await adapter.close()


def test_historical_funding_has_correct_direction_and_does_not_stress_credits():
    study = script("study_holding_momentum")
    tape = {
        now: {"open": 100, "high": 100, "low": 100, "close": 100}
        for now in range(0, 2 * study.HOUR, study.FIVE)
    }
    funding = {"X": [{"timestamp_ms": 1800000, "funding_rate": 0.001, "mark": 100}]}
    empty = {"X": []}
    for side in (1, -1):
        features = {"X": {0: {**observation(), "momentum_side": side, "momentum_score": 1}}}
        baseline = study.portfolio(
            {"X": tape}, features, 0, 2 * study.HOUR, 1, "confirmed", funding=empty, friction=0
        )
        actual = study.portfolio(
            {"X": tape}, features, 0, 2 * study.HOUR, 1, "confirmed", funding=funding, friction=0
        )
        stress = study.portfolio(
            {"X": tape},
            features,
            0,
            2 * study.HOUR,
            1,
            "confirmed",
            funding=funding,
            friction=0,
            stress=2,
        )
        stress_base = study.portfolio(
            {"X": tape},
            features,
            0,
            2 * study.HOUR,
            1,
            "confirmed",
            funding=empty,
            friction=0,
            stress=2,
        )
        assert actual["net"] - baseline["net"] == pytest.approx(-side * 2)
        assert stress["net"] - stress_base["net"] == pytest.approx(-4 if side > 0 else 2)


def test_sparse_replay_keeps_rsi_exits_and_matches_full_candidate_scan(monkeypatch):
    from pathlib import Path

    import numpy as np
    import pandas as pd

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from scripts.study_momentum_live import TimeRows

    study = script("study_holding_momentum")
    times = list(range(0, 2 * study.HOUR, study.FIVE))
    tape = {now: {"open": 100, "high": 100, "low": 100, "close": 100} for now in times}
    frame = pd.DataFrame(
        [
            {**observation(), "timestamp_ms": now, "rsi_14": 35 if i == 0 else 55}
            for i, now in enumerate(times)
        ]
    )
    rows = TimeRows(frame, "timestamp_ms")
    sides = np.zeros(len(times), dtype=int)
    sides[0] = 1
    rows.extra = {"momentum_side": sides, "momentum_score": sides.astype(float)}
    dense = study.portfolio(
        {"X": tape}, {"X": rows}, 0, 2 * study.HOUR, 1, "rsi", funding={"X": []}
    )
    sparse = study.portfolio(
        {"X": tape},
        {"X": rows},
        0,
        2 * study.HOUR,
        1,
        "rsi",
        funding={"X": []},
        schedule={0: [(1.0, "X", rows[0])]},
    )
    assert sparse == dense
    assert sparse["exit_reasons"] == {"RSI returned to neutral": 1}
    assert sparse["median_hold_minutes"] == 5


def test_hourly_research_context_cannot_see_future_prices(monkeypatch):
    from pathlib import Path

    import numpy as np
    import pandas as pd

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from scripts.study_alternative_entries import hourly

    study = script("study_holding_momentum")
    raw = pd.DataFrame(
        {
            "timestamp_ms": np.arange(study.FIVE, 72 * study.HOUR + 1, study.FIVE),
            "close": np.linspace(100, 105, 72 * 12),
        }
    )
    cutoff = 60 * study.HOUR
    before = hourly(raw)
    raw.loc[raw.timestamp_ms > cutoff, "close"] *= 20
    after = hourly(raw)
    pd.testing.assert_frame_equal(
        before[before.timestamp_ms <= cutoff], after[after.timestamp_ms <= cutoff]
    )
    partial = hourly(raw.iloc[:-1])
    assert partial.timestamp_ms.max() == 71 * study.HOUR


@pytest.mark.parametrize("with_carry", [False, True])
def test_live_activation_preserves_account_and_locks_and_refuses_open_position(
    settings, tmp_path, monkeypatch, with_carry
):
    from paperfrog.storage.database import Store

    monkeypatch.chdir(tmp_path)
    s = live(
        settings,
        state_dir=tmp_path / "state",
        simple_sizing_mode="equity_range",
        simple_position_margin_cap=0.2,
        simple_max_hold_hours=8,
    )
    # Private settings are mock test credentials, never real keys.
    values = {
        **s.public(),
        "binance_api_key": "mock-account-key",
        "binance_api_secret": "mock-signing-secret",
    }
    # Integer Literal defaults are not dotenv strings; production leaves this
    # invariant at its typed default rather than setting EXCHANGE_LEVERAGE.
    values.pop("exchange_leverage")
    env = tmp_path / ".env"
    env.write_text("\n".join(f"{key.upper()}={json.dumps(v)}" for key, v in values.items()) + "\n")
    ops = script("server_ops")
    context = tmp_path / "context.json"
    context.write_text(json.dumps(ops.context(s)))
    ledger = Store(s.state_dir / "simple-live/paperfrog.sqlite")
    saved = {
        "risk_lock": {"reason": "loss"},
        "manual_lock": True,
        "consecutive_losses": 3,
        "simple_closed": [{"pnl": -1}],
    }
    try:
        ledger.put("reconciled", True)
        for key, value in saved.items():
            ledger.put(key, value)
        ledger.put("simple_positions", {"SOLUSDT": {"stop_price": 99}})
        original = env.read_bytes()
        with pytest.raises(ops.DeploymentError, match="flat"):
            ops.activate_momentum(tmp_path, s, context, "momentum_breakout", 4, with_carry)
        assert env.read_bytes() == original
        ledger.put("simple_positions", {})
        ops.activate_momentum(tmp_path, s, context, "momentum_breakout", 4, with_carry)
        new = Settings()
        assert new.trading_mode == "live" and new.simple_entry_profile == "momentum_breakout"
        assert new.exchange_leverage == 2 and new.simple_position_margin_cap == 0.1
        assert new.simple_sizing_mode == "margin"
        assert new.simple_carry_enabled is with_carry
        assert new.max_strategy_entries_per_day <= s.max_strategy_entries_per_day
        assert (
            new.binance_api_key == s.binance_api_key
            and new.binance_api_secret == s.binance_api_secret
        )
        assert {k: ledger.get(k) for k in saved} == saved
        assert json.loads(context.read_text()) == ops.context(new)
        with pytest.raises(SafeError):
            verify_simple_arm(new)
    finally:
        ledger.engine.dispose()
