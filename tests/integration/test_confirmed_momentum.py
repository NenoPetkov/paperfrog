"""Tighter permissions must agree at selection and execution; exits stay intact."""

import time
from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_abc_momentum import settings as momentum_settings
from test_early_momentum import BOUNDARY, early

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings
from paperfrog.simple.momentum import classify, timing_veto
from paperfrog.simple.strategy import EARLY_MOMENTUM_RULES, RULES, execution_rules

CONFIRMED = replace(EARLY_MOMENTUM_RULES, momentum_entry_policy="confirmed_short")


def confirmed_row(**changes):
    return early(-1, **{
        "return_15m": -0.04,
        "day_context_ms": BOUNDARY,
        "day_return_4h": -0.03,
        "day_slope_4h": -0.02,
        **changes,
    })


def test_confirmed_short_is_opt_in_and_does_not_change_risk_or_brackets():
    assert Settings(_env_file=None).trading_mode == "paper"
    assert Settings(_env_file=None).simple_momentum_entry_policy == "baseline"
    before = momentum_settings()
    after = momentum_settings(simple_momentum_entry_policy="confirmed_short")
    key = "simple_momentum_entry_policy"
    assert {k: v for k, v in before.public().items() if k != key} == {
        k: v for k, v in after.public().items() if k != key
    }
    assert before.config_hash != after.config_hash
    old, new = execution_rules(before).public(), execution_rules(after).public()
    assert {k: v for k, v in old.items() if k != "momentum_entry_policy"} == {
        k: v for k, v in new.items() if k != "momentum_entry_policy"
    }


def test_policy_requires_base_momentum_and_cannot_be_attached_to_legacy_rules():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, simple_momentum_entry_policy="confirmed_short")
    with pytest.raises(SafeError):
        replace(RULES, momentum_entry_policy="confirmed_short")


def test_strong_aligned_short_passes_and_longs_are_rejected():
    assert classify(confirmed_row(), 0.00085, CONFIRMED)[1] == -1
    assert classify(early(1), 0.00085, EARLY_MOMENTUM_RULES)[1] == 1
    assert classify(early(1), 0.00085, CONFIRMED)[1] == 0


@pytest.mark.parametrize("damage", [
    {"day_context_ms": BOUNDARY - 900000},
    {"day_context_ms": BOUNDARY + 900000},
    {"day_context_ms": BOUNDARY + 0.1},
    {"day_context_ms": float("nan")},
    {"day_return_4h": 0},
    {"day_return_4h": 0.01},
    {"day_slope_4h": 0.01},
    {"day_slope_4h": None},
    {"return_1h": 0.01},
    {"path_efficiency_1h": 0.29},
    {"path_efficiency_1h": 1.01},
    {"path_efficiency_15m": 0.59},
    {"return_15m": -0.005},
    {"momentum_trend_age_bars": 13, "momentum_trend_started_ms": BOUNDARY - 13*300000},
    {"rsi_14": 29},
])
def test_weak_exhausted_old_or_unconfirmed_short_is_not_authorized(damage):
    row = confirmed_row(**damage)
    assert classify(row, 0.00085, CONFIRMED) == ("B", 0, 0.0)
    assert timing_veto(row, CONFIRMED)


def test_no_missing_longer_context_can_authorize_confirmed_entries():
    row = confirmed_row()
    for key in ("day_context_ms", "day_return_4h", "day_slope_4h", "return_1h", "path_efficiency_1h"):
        damaged = {k: v for k, v in row.items() if k != key}
        assert classify(damaged, 0.00085, CONFIRMED)[1] == 0


@pytest.mark.parametrize("side", [-1, 1])
async def test_live_filter_upgrade_keeps_existing_long_and_short_brackets(
    settings, store, filters, monkeypatch, side
):
    from test_fixed_roe import opened

    from paperfrog.simple.authorization import arm_simple
    from paperfrog.simple.engine import Engine

    fixed = int(time.time()) // 300 * 300 + 60
    monkeypatch.setattr(time, "time", lambda: fixed)
    engine, sim, adapter, _, _ = await opened(settings, store, filters, side)
    try:
        positions = engine.positions()
        stops, takes = store.get("live_stops"), store.get("live_takes")
        orders = {k: dict(v) for k, v in sim.stops.items()}
        calls = len(sim.calls)
        # The deployed schema before this change did not include this field.
        old_rules = store.get("simple_rules")
        old_rules.pop("momentum_entry_policy")
        store.put("simple_rules", old_rules)
        target = engine.s.model_copy(update={"simple_momentum_entry_policy": "confirmed_short"})
        engine.broker.s = target
        arm_simple(target, "ARM LIVE TRADING")
        restored = Engine(target, store, filters, rules=execution_rules(target), broker=engine.broker)
        assert restored.positions() == positions
        assert store.get("live_stops") == stops and store.get("live_takes") == takes
        assert sim.stops == orders and len(sim.calls) == calls
        assert store.get("simple_rules")["momentum_entry_policy"] == "confirmed_short"
    finally:
        await adapter.close()
