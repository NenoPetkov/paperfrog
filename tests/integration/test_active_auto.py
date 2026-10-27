import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from test_historical_learning import NOW, evidence, market, seed
from test_market_trend import breadth, day
from test_simple_live import equity_range_options, setup_engine

from paperfrog.domain.types import SafeError
from paperfrog.risk import activity
from paperfrog.risk.engine import Proposal, RiskEngine
from paperfrog.risk.sizing import require_equity_margin_allocation
from paperfrog.settings import Settings
from paperfrog.simple import learning, rotation
from paperfrog.simple.authorization import strategy_hash
from paperfrog.simple.strategy import BAR_MS
from paperfrog.storage.database import Store


def active_settings(settings):
    return Settings(_env_file=None, **{**settings.public(), "simple_market_trend_enabled": True})


def select_probe(store, s, boundary=NOW):
    rotation.configure(store, "auto-active", boundary - learning.DWELL_MS - BAR_MS)
    seed(store, s, [], boundary - BAR_MS)
    first = learning.review(store, s, market(s, boundary - BAR_MS), boundary - BAR_MS)
    assert not first["allowed"]
    return learning.review(store, s, market(s, boundary), boundary)


def test_active_mode_requires_confirmation_preserves_locks_and_survives_restart(settings, store):
    s = active_settings(settings)
    locks = {"manual_lock": True, "risk_lock": {"reason": "daily_loss"}, "anchors": {"equity": 500}}
    for key, value in locks.items():
        store.put(key, value)
    result = select_probe(store, s)
    assert result["allowed"] and result["entry_policy"] == "probe"
    assert result["ranking"] == [] and "unvalidated" in result["reason"]
    assert rotation.selection(store)["active"] == "current"
    assert rotation.timeframe(store, s) == "15m" and rotation.hold_hours(store, s) == 1
    assert {key: store.get(key) for key in locks} == locks
    reopened = Store(Path(store.engine.url.database))
    try:
        rotation.initialize(reopened, "base", NOW)
        assert learning.is_probe(reopened)
        assert learning.veto(reopened, s, NOW + 1) is None
        assert learning.veto(reopened, s, NOW + BAR_MS)
    finally:
        reopened.engine.dispose()


@pytest.mark.parametrize("damage", ["missing", "stale", "failed", "future", "ai", "disabled"])
def test_active_mode_never_bypasses_missing_research_or_market_protection(settings, store, damage):
    s = active_settings(settings)
    rotation.configure(store, "auto-active", NOW - 2 * learning.DWELL_MS)
    seed(store, s, [])
    m = market(s)
    if damage == "mixed":
        m["regime"] = "MIXED"
    elif damage == "fading":
        m["median_return_1h"] = .001
    elif damage == "missing":
        store.put("simple_learning_research", {})
    elif damage in {"stale", "future", "failed"}:
        r = store.get("simple_learning_research")
        if damage == "failed":
            r["errors"] = [{"symbol": "SOLUSDT"}]
        else:
            r["asof_ms"] = NOW - 5 * activity.HOUR_MS if damage == "stale" else NOW + BAR_MS * 3
        store.put("simple_learning_research", r)
    else:
        s = s.model_copy(update={"simple_ai_enabled": damage == "ai", "simple_market_trend_enabled": damage != "disabled"})
    for at in (NOW, NOW + BAR_MS):
        result = learning.review(store, s, {**m, "timestamp_ms": at}, at)
        assert not result["allowed"] and result["candidate"] is None
        assert learning.veto(store, s, at)


def test_strict_mode_still_waits_and_switching_does_not_reset_probe_budget(settings, store):
    s = active_settings(settings)
    assert select_probe(store, s)["allowed"]
    store.put(activity.KEY, [{"id": "attempt", "ts": NOW}])
    rotation.configure(store, "auto", NOW + 1)
    assert learning.veto(store, s, NOW + 1)
    result = learning.review(store, s, market(s), NOW + 1)
    assert not result["allowed"] and result["candidate"] is None
    rotation.configure(store, "current", NOW + 2)
    assert not learning.is_probe(store)
    rotation.configure(store, "auto-active", NOW + 3)
    assert store.get(activity.KEY) == [{"id": "attempt", "ts": NOW}]


def test_validated_variant_takes_priority_over_probe(settings, store):
    s = active_settings(settings)
    rotation.configure(store, "auto-active", NOW - learning.DWELL_MS)
    seed(store, s, evidence(strategy="current", timeframe="15m", hold=4))
    result = learning.review(store, s, market(s), NOW)
    assert result["allowed"] and result["entry_policy"] == "validated"
    assert not learning.is_probe(store)


def test_probe_requires_new_confirmation_when_market_direction_changes(settings, store):
    s = active_settings(settings)
    assert select_probe(store, s)["allowed"]
    bear = market(s, NOW + BAR_MS, regime="BEAR", median_return_4h=-.02,
                  median_return_1h=-.008, bull_index=25)
    result = learning.review(store, s, bear, NOW + BAR_MS)
    assert result["confirmations"] == 1 and not result["allowed"]
    result = learning.review(store, s, {**bear, "timestamp_ms": NOW + 2 * BAR_MS}, NOW + 2 * BAR_MS)
    assert result["allowed"] and result["entry_policy"] == "probe"


def probe_proposal(identifier="probe", symbol="SOLUSDT"):
    return Proposal(identifier, symbol, 10000, 200, 200, 5, .02, .01, .3, .001,
                    activity_probe=True)


def close_reservation(store, identifier):
    with store.transaction() as conn:
        conn.execute(text("UPDATE entries SET closed=1 WHERE id=:id"), {"id": identifier})


def test_probe_reservation_is_atomic_and_rate_limit_survives_restart(settings, store, health, now):
    risk = RiskEngine(settings, store)
    risk.reserve(probe_proposal(), health, now)
    assert len(store.get(activity.KEY)) == 1
    with pytest.raises(SafeError, match="empty portfolio"):
        risk.reserve(probe_proposal("other", "ETHUSDT"), health, now)
    with pytest.raises(SafeError, match="probe must close"):
        risk.reserve(replace(probe_proposal("normal", "ETHUSDT"), activity_probe=False), health, now)
    close_reservation(store, "probe")
    reopened = Store(Path(store.engine.url.database))
    try:
        risk = RiskEngine(settings, reopened)
        with pytest.raises(SafeError, match="one attempt per hour"):
            risk.reserve(probe_proposal("early"), health, now + timedelta(minutes=59))
        for i in range(1, 6):
            risk.reserve(probe_proposal(str(i), f"ALT{i}USDT"), health, now + timedelta(hours=i))
            close_reservation(reopened, str(i))
        with pytest.raises(SafeError, match="six attempts"):
            risk.reserve(probe_proposal("seventh", "ALT6USDT"), health, now + timedelta(hours=6))
        risk.reserve(probe_proposal("next-day", "ALT7USDT"), health, now + timedelta(hours=24))
        assert len(reopened.get(activity.KEY)) == 6
    finally:
        reopened.engine.dispose()


def test_probe_budget_is_not_consumed_when_risk_lock_rejects(settings, store, health, now):
    store.put("manual_lock", True)
    with pytest.raises(SafeError, match="risk lock"):
        RiskEngine(settings, store).reserve(probe_proposal(), health, now)
    assert store.get(activity.KEY) is None
    assert store.rows("entries") == []


def test_probe_has_lower_risk_cap_and_rejects_future_budget(settings, store, health, now):
    with pytest.raises(SafeError, match="per trade risk"):
        RiskEngine(settings, store).reserve(replace(probe_proposal(), risk=15), health, now)
    store.put(activity.KEY, [{"id": "future", "ts": int(now.timestamp() * 1000) + 1}])
    with pytest.raises(SafeError, match="valid clock"):
        RiskEngine(settings, store).reserve(probe_proposal(), health, now)


@pytest.mark.parametrize("scale", [0, -1, 1.01, float("nan")])
def test_allocation_scaling_cannot_increase_size(settings, scale):
    s = Settings(_env_file=None, **{**settings.public(), **equity_range_options()})
    with pytest.raises(SafeError, match="allocation scale"):
        require_equity_margin_allocation(s, 500, 100, 500, 1, scale)


@pytest.mark.parametrize("available", ["500", "20"])
async def test_live_probe_half_size_stop_and_exit_despite_expired_permission(
    settings, store, filters, monkeypatch, available
):
    fixed_now = NOW + 2_000
    monkeypatch.setattr(time, "time", lambda: fixed_now / 1000)
    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, **equity_range_options(simple_market_trend_enabled=True)
    )
    try:
        sim.available = available
        row.update(day(NOW), path_efficiency_1h=.8, direction_changes_1h=0,
                   previous_trend_atr=1., previous_flow=.4)
        breadth(store, engine.s, NOW)
        assert select_probe(store, engine.s)["allowed"]
        assert engine.entry_signal(row, now, .001)[0] == 1
        await engine.enter("SOLUSDT", row, now)
        trade = engine.positions()["SOLUSDT"]
        assert trade["activity_probe"] and trade["max_hold_hours"] == 1
        assert 0 < float(trade["quantity"]) * 100 <= min(250, float(available))
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        assert len(store.get(activity.KEY)) == 1
        store.put(learning.KEY, {})
        assert engine.entry_signal(row, now, .001)[0] == 0
        await engine.close("SOLUSDT", now + 1, "protective close despite expired probe")
        assert not engine.positions() and sim.quantity == 0
        assert activity.blocker(store, now + 1) == "active auto probe cooldown: one attempt per hour"
    finally:
        await adapter.close()


async def test_probe_mode_change_at_dispatch_rejects_without_order(settings, store, filters, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: (NOW + 2_000) / 1000)
    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, **equity_range_options(simple_market_trend_enabled=True)
    )
    try:
        row.update(day(NOW), path_efficiency_1h=.8, direction_changes_1h=0,
                   previous_trend_atr=1., previous_flow=.4)
        breadth(store, engine.s, NOW)
        select_probe(store, engine.s)
        original = engine.broker.submit

        async def change_mode(order):
            rotation.configure(store, "auto", now)
            return await original(order)

        engine.broker.submit = change_mode
        with pytest.raises(SafeError):
            await engine.enter("SOLUSDT", row, now)
        assert sim.quantity == 0 and not sim.orders
        assert len(store.get(activity.KEY)) == 1
        assert not [r for r in store.rows("entries") if not r["closed"]]
    finally:
        await adapter.close()


def test_activity_limits_are_part_of_live_authorization(settings, monkeypatch):
    before = strategy_hash(settings)
    original = Path.read_bytes

    def changed(path):
        data = original(path)
        return data + b"\n# changed probe limits\n" if path.name == "activity.py" else data

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert strategy_hash(settings) != before
