from unittest.mock import AsyncMock

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from test_controls import update
from test_minute_entries import BOUNDARY, minute_row, minute_settings
from test_simple import engine_fixture, row, source

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.simple import rotation
from paperfrog.simple.engine import Engine
from paperfrog.simple.service import SimpleService
from paperfrog.telegram.control import Controls
from paperfrog.telegram.events import EventNotifications


def history(losses, total=8):
    return [
        {"trade_id": str(i), "timestamp_ms": i, "pnl": -1 if i < losses else 0}
        for i in range(total)
    ]


def outcome(store, i, pnl=-1, **overrides):
    state = rotation.selection(store)
    rotation.closed(
        store,
        {
            "id": str(i),
            "strategy_choice": state["active"],
            "strategy_epoch": state["epoch"],
            **overrides,
        },
        pnl,
        100 + i,
    )


@pytest.mark.parametrize(
    "losses,total,expected",
    [(5, 8, "current"), (6, 8, "dip_peak"), (7, 7, "current"), (8, 8, "dip_peak")],
)
def test_threshold_requires_eight_and_strictly_over_seventy(store, losses, total, expected):
    store.put("simple_closed", history(losses, total))
    rotation.initialize(store, "base", 20)
    assert rotation.selection(store)["active"] == expected
    before = rotation.selection(store)
    rotation.initialize(store, "base", 30)
    assert rotation.selection(store) == before


def test_switch_resets_sample_and_excludes_old_open_trades(store):
    rotation.initialize(store, "base", 0)
    for i in range(8):
        outcome(store, i)
    assert rotation.selection(store)["active"] == "dip_peak"
    for i in range(8, 16):
        outcome(store, i, strategy_choice="current", strategy_epoch=0)
    assert rotation.selection(store)["outcomes"] == []
    for i in range(16, 24):
        outcome(store, i)
    assert rotation.selection(store)["active"] == "current"
    assert len(store.get("simple_strategy_history")) == 2
    outcome(store, 99)
    outcome(store, 99)
    assert len(rotation.selection(store)["outcomes"]) == 1


def test_rolling_window_manual_mode_and_existing_locks(store):
    store.put("manual_lock", True)
    store.put("risk_lock", {"reason": "daily_loss"})
    store.put("simple_positions", {"SOLUSDT": {"stop_price": 99}})
    rotation.initialize(store, "base", 0)
    for i in range(8):
        outcome(store, i, -1 if i < 5 else 1)
    assert rotation.selection(store)["active"] == "current"
    for i in range(8, 14):
        outcome(store, i)
    assert rotation.selection(store)["active"] == "dip_peak"
    rotation.configure(store, "current", 200)
    for i in range(20, 28):
        outcome(store, i)
    assert rotation.selection(store)["active"] == "current"
    rotation.configure(store, "auto", 300)
    assert rotation.selection(store)["active"] == "dip_peak"
    assert store.get("manual_lock") is True
    assert store.get("risk_lock") == {"reason": "daily_loss"}
    assert store.get("simple_positions") == {"SOLUSDT": {"stop_price": 99}}
    with pytest.raises(SafeError):
        rotation.configure(store, "live", 400)


def test_bootstrap_orders_closes_and_ignores_other_strategies_and_duplicates(store):
    trades = history(6)
    trades += [
        trades[0],
        {
            "trade_id": "other",
            "timestamp_ms": 100,
            "pnl": 1,
            "entry_context": {"strategy": "other"},
        },
    ]
    store.put("simple_closed", list(reversed(trades)))
    rotation.initialize(store, "base", 200)
    assert rotation.selection(store)["active"] == "dip_peak"


def dip_row(side=1):
    return {
        "close": 102 if side == 1 else 108,
        "open": 101 if side == 1 else 109,
        "dip_floor": 100,
        "peak_ceiling": 110,
        "dip_recent_low": 100,
        "peak_recent_high": 110,
        "atr": 1,
    }


@pytest.mark.parametrize("side", [1, -1])
def test_dip_peak_requires_recovery_near_extreme(side):
    obs = dip_row(side)
    assert rotation.dip_peak_gate(obs, side) is None
    assert rotation.dip_peak_gate({**obs, "close": 107 if side == 1 else 103}, side)
    assert rotation.dip_peak_gate({**obs, "open": obs["close"]}, side)
    assert rotation.dip_peak_gate({**obs, "atr": float("nan")}, side)
    assert rotation.dip_peak_gate({**obs, "dip_recent_low": 104, "peak_recent_high": 106}, side)
    assert rotation.dip_peak_gate({**obs, "close": 99 if side == 1 else 111}, side)


@pytest.mark.parametrize("side", [1, -1])
def test_recovery_can_reach_middle_of_range_without_chasing_breakout(side):
    obs = {**dip_row(side), "close": 106 if side == 1 else 104, "open": 105}
    assert rotation.dip_peak_gate(obs, side) is None
    assert rotation.dip_peak_gate({**obs, "close": 106.51 if side == 1 else 103.49}, side)


@pytest.mark.parametrize("side", [1, -1])
def test_smaller_confirmed_recovery_and_wider_extreme_tolerance(side):
    obs = {
        **dip_row(side),
        "close": 101 if side == 1 else 109,
        "open": 100.9 if side == 1 else 109.1,
        "dip_recent_low": 100.75,
        "peak_recent_high": 109.25,
    }
    assert rotation.dip_peak_gate(obs, side) is None
    assert rotation.dip_peak_gate({**obs, "close": 100.99 if side == 1 else 109.01}, side)


@pytest.mark.parametrize("damage", [None, "invalid", float("inf"), float("nan")])
def test_malformed_extremes_return_a_blocker(damage):
    assert rotation.dip_peak_gate({**dip_row(), "dip_floor": damage}, 1)


def test_extremes_are_causal_and_exclude_entry_candle():
    frame = source()
    before = rotation.features(frame.iloc[:250])
    frame.loc[250:, ["close", "high", "low"]] *= 100
    assert_frame_equal(before, rotation.features(frame).iloc[:250])
    assert before.iloc[-1].dip_floor == frame.low.iloc[225:249].min()
    assert before.iloc[-1].dip_recent_low == frame.low.iloc[237:249].min()
    assert before.iloc[-1].peak_recent_high == frame.high.iloc[237:249].max()


async def test_dip_watchlist_filters_before_ranking_and_retains_exit_rows(settings, store):
    from test_minute_entries import slow_context

    s = minute_settings(settings, simple_selected_count=1)
    service = SimpleService(s)
    service.store.engine.dispose()
    service.store = store
    rotation.initialize(store, "base", 0)
    rotation.configure(store, "dip_peak", 1)
    service.context_rows = {"BTCUSDT": slow_context(), "SOLUSDT": slow_context(), "ETHUSDT": slow_context()}
    service.now_ms = lambda: BOUNDARY + 1000
    store.put("simple_pool", ["SOLUSDT", "ETHUSDT"])
    recovery = {**minute_row(), "dip_floor": 94, "peak_ceiling": 104, "dip_recent_low": 94, "peak_recent_high": 104, "score": 1}
    breakout = {**recovery, "dip_floor": 90, "peak_ceiling": 99, "score": 100}
    data = {"SOLUSDT": pd.DataFrame([recovery]), "ETHUSDT": pd.DataFrame([breakout])}
    service.entry_frame = AsyncMock(side_effect=lambda symbol, *args: data[symbol])
    try:
        await service.refresh_minutes(BOUNDARY)
        assert service.selected == ["SOLUSDT"]
        assert set(service.rows) == {"SOLUSDT", "ETHUSDT"}
        assert store.get("simple_minute:ETHUSDT") == breakout
        audit = store.rows("audit", 1)[0]["payload"]
        assert "strategy_blocks" in audit and "ETHUSDT" in audit
        rotation.configure(store, "current", BOUNDARY + 1001)
        await service.refresh_minutes(BOUNDARY)
        assert service.selected == ["ETHUSDT"]
    finally:
        await service.exchange.close()


async def test_engine_keeps_origin_when_position_closes_after_manual_switch(
    settings, store, filters
):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    await engine.enter("SOLUSDT", row(), 200 * 900000)
    trade = engine.positions()["SOLUSDT"].copy()
    rotation.configure(store, "dip_peak", 200 * 900000 + 1)
    assert engine.positions()["SOLUSDT"] == trade
    await engine.close("SOLUSDT", 200 * 900000, "test")
    assert rotation.selection(store)["outcomes"] == []
    closed = store.get("simple_closed")[-1]
    assert closed["strategy_choice"] == "current" and closed["strategy_epoch"] == 0


async def test_engine_records_net_loss_and_bootstrap_after_restart(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    await engine.enter("SOLUSDT", row(), 200 * 900000)
    await engine.close("SOLUSDT", 200 * 900000, "flat price with fees")
    assert rotation.selection(store)["outcomes"][0]["pnl"] < 0
    Engine(settings, store, filters)
    rotation.initialize(store, engine.strategy, 500)
    assert len(rotation.selection(store)["outcomes"]) == 1


@pytest.mark.parametrize("side", [1, -1])
def test_new_strategy_is_additional_filter_and_keeps_existing_gates(settings, store, filters, side):
    s = minute_settings(settings)
    engine = Engine(s, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    obs = minute_row(side=side)
    assert engine.entry_signal(obs, BOUNDARY, 0.001)[0] == side
    rotation.configure(store, "dip_peak", 1)
    assert engine.entry_signal(obs, BOUNDARY, 0.001)[0] == 0
    obs.update(dip_floor=98, peak_ceiling=108, dip_recent_low=98, peak_recent_high=108)
    if side == -1:
        obs.update(dip_floor=92, peak_ceiling=102, dip_recent_low=92, peak_recent_high=102)
    assert engine.entry_signal(obs, BOUNDARY, 0.001)[0] == side
    obs["flow"] *= -1
    assert engine.entry_signal(obs, BOUNDARY, 0.001)[0] == 0


def test_dispatch_binds_exact_order_and_selection_and_preserves_exits(store):
    from decimal import Decimal

    rotation.initialize(store, "base", 0)
    order = Order(
        client_id="pf-rotation-test",
        symbol="SOLUSDT",
        side=1,
        quantity=Decimal(1),
        limit_price=Decimal(102),
    )
    with pytest.raises(EntryVeto):
        rotation.require_order(store, order)
    rotation.configure(store, "dip_peak", 1)
    store.put(
        "simple_strategy_order:" + order.client_id,
        {
            "order": order.model_dump(mode="json"),
            "active": "dip_peak",
            "epoch": 1,
            "row": dip_row(),
        },
    )
    rotation.require_order(store, order)
    rotation.configure(store, "current", 2)
    with pytest.raises(EntryVeto):
        rotation.require_order(store, order)
    rotation.require_order(store, order.model_copy(update={"reduce_only": True}))


async def test_telegram_strategy_authorization_syntax_and_deduplication(settings, store):
    s = settings.model_copy(
        update={"telegram_allowed_user_ids": (1,), "telegram_allowed_chat_ids": (2,)}
    )
    callback = AsyncMock(return_value="strategy")
    controls = Controls(s, store, AsyncMock(), AsyncMock(), AsyncMock(), strategy=callback)
    assert await controls.handle(update("/strategy dip_peak", uid=99)) is None
    assert (
        await controls.handle(update("/strategy dip_peak", forward_origin={"type": "user"})) is None
    )
    assert "Unknown" in (await controls.handle(update("/strategy dip_peak extra")))[1]
    callback.assert_not_awaited()
    request = update("/strategy dip_peak")
    assert (await controls.handle(request))[1] == "strategy"
    assert await controls.handle(request) is None
    callback.assert_awaited_once_with("dip_peak")
    await controls.handle(update("/strategy"))
    callback.assert_awaited_with(None)
    controls.resume.assert_not_awaited()


async def test_service_command_and_event_are_readable(settings):
    service = SimpleService(settings)
    try:
        rotation.initialize(service.store, "base", 0)
        service.engine = object()
        response = await service.strategy_control("dip_peak")
        assert "dip_peak" in response and "MANUAL" in response and "→" in response
        assert "AUTO" in await service.strategy_control("auto")
        event = service.store.get("simple_strategy_history")[-1]
        rendered = EventNotifications(settings, service.store, None).render(
            "simple_strategy_changed", event
        )
        assert "dip_peak" in rendered and "/strategy" in rendered
        with pytest.raises(SafeError):
            await service.strategy_control("arm")
    finally:
        await service.exchange.close()


@pytest.mark.parametrize("change_during_dispatch", [False, True])
@pytest.mark.parametrize("late_recovery", [False, True])
async def test_live_simulation_rechecks_strategy_before_exchange_order(
    settings, store, filters, monkeypatch, change_during_dispatch, late_recovery
):
    from test_simple_live import setup_engine

    engine, simulation, adapter, obs, now = await setup_engine(settings, store, filters)
    rotation.initialize(store, engine.strategy, now)
    rotation.configure(store, "dip_peak", now)
    obs.update(open=99, dip_floor=98, peak_ceiling=108, dip_recent_low=98, peak_recent_high=108)
    if late_recovery:
        obs.update(dip_floor=94, peak_ceiling=104, dip_recent_low=94, peak_recent_high=104)
    submit = engine.broker.submit

    async def dispatch(order):
        if change_during_dispatch and not order.reduce_only:
            rotation.configure(store, "current", now)
        return await submit(order)

    monkeypatch.setattr(engine.broker, "submit", dispatch)
    try:
        if change_during_dispatch:
            with pytest.raises(EntryVeto, match="strategy selection changed"):
                await engine.enter("SOLUSDT", obs, now)
            assert simulation.quantity == 0
        else:
            await engine.enter("SOLUSDT", obs, now)
            assert simulation.quantity > 0
            assert engine.positions()["SOLUSDT"]["strategy_choice"] == "dip_peak"
            assert engine.positions()["SOLUSDT"]["strategy_epoch"] == 1
    finally:
        await adapter.close()
