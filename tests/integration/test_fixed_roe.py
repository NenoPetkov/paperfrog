"""Fixed margin-return brackets, original-order recovery and safe local activation."""

import time
from decimal import Decimal

import pytest
from test_early_momentum import early
from test_simple_live import setup_engine

from paperfrog.domain.types import Quote, SafeError, State
from paperfrog.settings import Settings
from paperfrog.simple.fixed_roe import PRICE_FRACTION, reason, trigger
from paperfrog.simple.selection import pool_hash
from paperfrog.simple.strategy import RULES, exit_reason, tighten_stop

D = Decimal
BAR = 300000


@pytest.fixture(autouse=True)
def stable_closed_bar_clock(monkeypatch):
    # These tests exercise bracket lifecycle, not candle-boundary expiry.
    # A real clock crossing a five-minute boundary must still veto dispatch.
    fixed_ms = int(time.time() * 1000) // BAR * BAR + 60000
    monkeypatch.setattr(time, "time", lambda: fixed_ms / 1000)


def position(side=1):
    entry = D("100")
    return {
        "side": side,
        "entry_price": 100,
        "stop_price": float(trigger(entry, side, D(".01"), profit=False)),
        "take_profit_price": float(trigger(entry, side, D(".01"), profit=True)),
        "exit_policy": "fixed_roe",
        "entry_leverage": 2,
        "roe_target": 0.05,
        "opened_ms": 0,
        "max_hold_hours": 4,
        "momentum_exit_version": 1,
        "initial_stop_distance": 2.5,
    }


def options():
    return {
        "simple_exit_policy": "fixed_roe",
        "simple_entry_profile": "momentum",
        "simple_entry_timeframe": "5m",
        "simple_selected_count": 1,
        "simple_sizing_mode": "margin",
        "simple_position_margin_cap": 0.1,
    }


async def opened(settings, store, filters, side=1, monkeypatch=None):
    engine, sim, adapter, _, now = await setup_engine(settings, store, filters, **options())
    boundary = now // BAR * BAR
    obs = early(
        side, open=100 - side * 0.2, close=100, fast=100 - side * 0.5, slow=100 - side * 1.5
    )
    obs.update(timestamp_ms=boundary, momentum_trend_started_ms=boundary - 2 * BAR)
    store.put("simple_pool", ["SOLUSDT"])
    store.put("simple_minute:SOLUSDT", obs)
    store.put(
        "simple_candidate_ranking",
        {
            "asof_ms": boundary,
            "config_hash": engine.s.config_hash,
            "pool_hash": pool_hash(["SOLUSDT"]),
            "selected": ["SOLUSDT"],
        },
    )
    if monkeypatch is not None:
        original = engine.broker.query_stop

        async def timeout_take(key, **kw):
            if kw.get("mapping") == "live_takes":
                raise TimeoutError()
            return await original(key, **kw)

        monkeypatch.setattr(engine.broker, "query_stop", timeout_take)
    try:
        await engine.enter("SOLUSDT", obs, now)
    except BaseException:
        if monkeypatch is None:
            await adapter.close()
        raise
    return engine, sim, adapter, obs, now


@pytest.mark.parametrize("side", [-1, 1])
def test_fixed_exit_waits_through_trend_reversal_time_limit_and_small_moves(side):
    p = position(side)
    for now in (1000, 4 * 3600000, 30 * 86400000):
        for mark in (97.51, 99, 100, 101, 102.49):
            assert (
                exit_reason(p, mark, now, {"trend_atr": -side, "previous_trend_atr": -side}, RULES)
                is None
            )
            assert tighten_stop(p, mark, 1, RULES) == p["stop_price"]
    assert "-5%" in exit_reason(p, p["stop_price"], 1000, None, RULES)
    assert "+5%" in exit_reason(p, p["take_profit_price"], 1000, None, RULES)


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("entry,tick", [("100.13", ".01"), (".00437", ".00001"), ("683.17", ".1")])
def test_tick_rounding_never_triggers_inside_five_percent_margin_return(side, entry, tick):
    e, t = D(entry), D(tick)
    stop = trigger(e, side, t, profit=False)
    take = trigger(e, side, t, profit=True)
    assert side * (stop / e - 1) * 2 <= D("-.05")
    assert side * (take / e - 1) * 2 >= D(".05")
    assert stop % t == 0 and take % t == 0
    assert PRICE_FRACTION == D(".025")


@pytest.mark.parametrize(
    "damage", [{"entry_leverage": 4}, {"roe_target": 0.1}, {"take_profit_price": None}, {"side": 0}]
)
def test_invalid_position_evidence_fails_closed(damage):
    with pytest.raises(SafeError):
        reason({**position(), **damage}, 100)


def test_default_remains_trend_and_fixed_policy_requires_full_selection():
    s = Settings(_env_file=None)
    assert s.trading_mode == "paper" and s.simple_exit_policy == "trend"
    with pytest.raises(ValueError):
        Settings(_env_file=None, simple_exit_policy="fixed_roe")


@pytest.mark.parametrize("side", [-1, 1])
async def test_live_entry_has_both_hosted_brackets_and_sized_loss_budget(
    settings, store, filters, side, monkeypatch
):
    engine, sim, adapter, obs, now = await opened(settings, store, filters, side)
    try:
        t = engine.positions()["SOLUSDT"]
        assert (
            t["exit_policy"] == "fixed_roe" and t["roe_target"] == 0.05 and t["entry_leverage"] == 2
        )
        assert {v["orderType"] for v in sim.stops.values()} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
        assert all(
            v["closePosition"] and v["workingType"] == "MARK_PRICE" for v in sim.stops.values()
        )
        assert all(v["positionSide"] == "BOTH" for v in sim.stops.values())
        assert store.get("live_stops")["SOLUSDT"]["trigger"] == str(D(str(t["stop_price"]))) or D(
            store.get("live_stops")["SOLUSDT"]["trigger"]
        ) == D(str(t["stop_price"]))
        risk = store.rows("entries")[0]["risk"]
        assert risk <= float(engine.broker.equity()) * engine.s.risk_per_trade
        # Five hours, reversed candles and profit below threshold do not close it.
        later = now + 5 * 3600000
        q = Quote("SOLUSDT", D("99.99"), D("100.01"), D(100000), D(100000), D(100), later)
        engine.broker.quotes["SOLUSDT"] = q
        monkeypatch.setattr(engine.broker, "now_ms", lambda: later)
        await engine.broker.refresh()
        await engine.monitor(later, {}, awaiting_initial_scan=False)
        assert engine.positions() and sim.quantity
        # The portfolio emergency lock still has authority to reduce exposure.
        store.put("risk_lock", {"state": "HARD_RISK_LOCK", "reason": "high_water"})
        await engine.monitor(later, {}, awaiting_initial_scan=False)
        assert not engine.positions() and sim.quantity == 0
        assert not store.get("live_takes") and not store.get("live_stops")
    finally:
        await adapter.close()


@pytest.mark.parametrize("kind", ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
@pytest.mark.parametrize("side", [-1, 1])
async def test_either_hosted_order_closes_once_and_sibling_is_removed(
    settings, store, filters, kind, side
):
    engine, sim, adapter, _, now = await opened(settings, store, filters, side)
    try:
        key, bracket = next((k, v) for k, v in sim.stops.items() if v["orderType"] == kind)
        fill = sim.order_result(
            key + "-actual",
            "SELL" if side > 0 else "BUY",
            abs(sim.quantity),
            D(bracket["triggerPrice"]),
        )
        bracket.update(algoStatus="FINISHED", actualOrderId=str(fill["orderId"]))
        await engine.restore()
        assert not engine.positions() and sim.quantity == 0
        assert len(store.get("simple_closed")) == 1
        assert ("+5%" if kind == "TAKE_PROFIT_MARKET" else "-5%") in store.get("simple_closed")[0][
            "reason"
        ]
        assert not store.get("live_takes") and not store.get("live_stops")
        assert await engine.coordinator.reconcile()
        await engine.restore()
        assert len(store.get("simple_closed")) == 1
    finally:
        await adapter.close()


async def test_pending_take_profit_timeout_recovers_without_any_new_post(
    settings, store, filters, monkeypatch
):
    engine, sim, adapter, _, now = await setup_engine(settings, store, filters, **options())
    boundary = now // BAR * BAR
    obs = early(1, open=99.8, close=100, fast=99.5, slow=98.5)
    obs.update(timestamp_ms=boundary, momentum_trend_started_ms=boundary - 2 * BAR)
    store.put("simple_pool", ["SOLUSDT"])
    store.put("simple_minute:SOLUSDT", obs)
    store.put(
        "simple_candidate_ranking",
        {
            "asof_ms": boundary,
            "config_hash": engine.s.config_hash,
            "pool_hash": pool_hash(["SOLUSDT"]),
            "selected": ["SOLUSDT"],
        },
    )
    original = engine.broker.query_stop

    async def timeout(key, **kw):
        if kw.get("mapping") == "live_takes":
            raise TimeoutError()
        return await original(key, **kw)

    try:
        monkeypatch.setattr(engine.broker, "query_stop", timeout)
        with pytest.raises(TimeoutError):
            await engine.enter("SOLUSDT", obs, now)
        assert store.get("state") == State.ERROR_SAFE and sim.quantity > 0
        assert store.get("simple_pending") and store.get("live_takes")
        monkeypatch.setattr(engine.broker, "query_stop", original)
        count = sum(m == "POST" for m, _ in sim.calls)
        assert await engine.recover_execution()
        assert sum(m == "POST" for m, _ in sim.calls) == count
        assert engine.positions()["SOLUSDT"]["exit_policy"] == "fixed_roe"
    finally:
        await adapter.close()


async def test_missing_or_changed_take_profit_cannot_be_adopted(settings, store, filters):
    engine, sim, adapter, _, now = await opened(settings, store, filters)
    try:
        takes = store.get("live_takes")
        takes["SOLUSDT"]["trigger"] = "101"
        store.put("live_takes", takes)
        with pytest.raises(SafeError, match="identity changed|does not match"):
            await engine.restore()
        assert sim.quantity > 0
    finally:
        await adapter.close()


async def test_changing_new_entry_policy_never_widens_an_existing_trade(settings, store, filters):
    import copy

    from test_simple_live import live_settings

    from paperfrog.simple.authorization import arm_simple
    from paperfrog.simple.engine import Engine
    from paperfrog.simple.live_broker import LiveBroker

    old_options = {**options(), "simple_exit_policy": "trend", "simple_selected_count": 10}
    engine, sim, adapter, _, now = await setup_engine(settings, store, filters, **old_options)
    boundary = now // BAR * BAR
    obs = early(1, open=99.8, close=100, fast=99.5, slow=98.5)
    obs.update(timestamp_ms=boundary, momentum_trend_started_ms=boundary - 2 * BAR)
    store.put("simple_minute:SOLUSDT", obs)
    try:
        await engine.enter("SOLUSDT", obs, now)
        held = copy.deepcopy(engine.positions())
        stops = copy.deepcopy(sim.stops)
        updated = live_settings(settings, **options())
        adapter.s = updated
        arm_simple(updated, "ARM LIVE TRADING")
        broker = LiveBroker(updated, store, adapter)
        await broker.preflight()
        restored = Engine(updated, store, filters, broker=broker)
        await restored.restore()
        assert restored.positions() == held
        assert sim.stops == stops and len(sim.stops) == 1
        assert not store.get("live_takes")
        assert restored.positions()["SOLUSDT"]["exit_policy"] == "trend"
    finally:
        await adapter.close()
