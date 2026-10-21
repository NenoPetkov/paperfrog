from unittest.mock import AsyncMock

import pandas as pd
import pytest
from test_controls import update
from test_minute_entries import BOUNDARY, minute_row, minute_settings
from test_simple import engine_fixture, row

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
    [(5, 8, "current"), (6, 8, "opposite"), (7, 7, "current"), (8, 8, "opposite")],
)
def test_threshold_requires_eight_and_strictly_over_seventy(store, losses, total, expected):
    rotation.initialize(store, "base", 20)
    for i in range(total):
        outcome(store, i, -1 if i < losses else 0)
    assert rotation.selection(store)["active"] == expected
    before = rotation.selection(store)
    rotation.initialize(store, "base", 30)
    assert rotation.selection(store) == before


def test_switch_resets_sample_and_excludes_old_open_trades(store):
    rotation.initialize(store, "base", 0)
    for i in range(8):
        outcome(store, i)
    assert rotation.selection(store)["active"] == "opposite"
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
    assert rotation.selection(store)["active"] == "opposite"
    rotation.configure(store, "current", 200)
    for i in range(20, 28):
        outcome(store, i)
    assert rotation.selection(store)["active"] == "current"
    rotation.configure(store, "auto", 300)
    assert rotation.selection(store)["active"] == "opposite"
    assert store.get("manual_lock") is True
    assert store.get("risk_lock") == {"reason": "daily_loss"}
    assert store.get("simple_positions") == {"SOLUSDT": {"stop_price": 99}}
    with pytest.raises(SafeError):
        rotation.configure(store, "live", 400)


@pytest.mark.parametrize("legacy", [None, "current", "dip_peak"])
def test_new_controller_ignores_history_and_migrates_once(store, legacy):
    store.put("simple_closed", history(8))
    if legacy:
        store.put(
            rotation.KEY,
            {"active": legacy, "epoch": 5, "auto": False, "outcomes": [{"id": "old", "pnl": -1}]},
        )
    rotation.initialize(store, "base", 200)
    state = rotation.selection(store)
    assert state["active"] == "current" and state["auto"]
    assert state["outcomes"] == []
    assert state["epoch"] == (6 if legacy else 0)
    outcome(store, 100, strategy_epoch=-1)
    assert rotation.selection(store)["outcomes"] == []
    outcome(store, 101)
    rotation.initialize(store, "base", 300)
    assert len(rotation.selection(store)["outcomes"]) == 1


async def test_opposite_watchlist_uses_normal_ranking(settings, store):
    from test_minute_entries import slow_context

    service = SimpleService(minute_settings(settings, simple_selected_count=1))
    service.store.engine.dispose()
    service.store = store
    rotation.initialize(store, "base", 0)
    rotation.configure(store, "opposite", 1)
    service.context_rows = {"BTCUSDT": slow_context(), "SOLUSDT": slow_context()}
    service.now_ms = lambda: BOUNDARY + 1000
    store.put("simple_pool", ["SOLUSDT"])
    service.entry_frame = AsyncMock(return_value=pd.DataFrame([minute_row()]))
    try:
        await service.refresh_minutes(BOUNDARY)
        assert service.selected == ["SOLUSDT"]
    finally:
        await service.exchange.close()


async def test_engine_keeps_origin_when_position_closes_after_manual_switch(
    settings, store, filters
):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    await engine.enter("SOLUSDT", row(), 200 * 900000)
    trade = engine.positions()["SOLUSDT"].copy()
    rotation.configure(store, "opposite", 200 * 900000 + 1)
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
def test_opposite_reverses_qualifying_normal_decision(settings, store, filters, side):
    engine = Engine(minute_settings(settings), store, filters)
    rotation.initialize(store, engine.strategy, 0)
    obs = minute_row(side=side)
    assert engine.entry_signal(obs, BOUNDARY, 0.001)[0] == side
    rotation.configure(store, "opposite", 1)
    assert engine.entry_signal(obs, BOUNDARY, 0.001)[0] == -side
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
    rotation.configure(store, "opposite", 1)
    store.put(
        "simple_strategy_order:" + order.client_id,
        {
            "order": order.model_dump(mode="json"),
            "active": "opposite",
            "epoch": 1,
            "decision_side": -1,
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
    assert await controls.handle(update("/strategy opposite", uid=99)) is None
    assert (
        await controls.handle(update("/strategy opposite", forward_origin={"type": "user"})) is None
    )
    assert "Unknown" in (await controls.handle(update("/strategy opposite extra")))[1]
    callback.assert_not_awaited()
    request = update("/strategy opposite")
    assert (await controls.handle(request))[1] == "strategy"
    assert await controls.handle(request) is None
    callback.assert_awaited_once_with("opposite")
    await controls.handle(update("/strategy"))
    callback.assert_awaited_with(None)
    controls.resume.assert_not_awaited()


async def test_service_command_and_event_are_readable(settings):
    service = SimpleService(settings)
    try:
        rotation.initialize(service.store, "base", 0)
        service.engine = object()
        response = await service.strategy_control("opposite")
        assert "opposite" in response and "MANUAL" in response and "→" in response
        assert "AUTO" in await service.strategy_control("auto")
        event = service.store.get("simple_strategy_history")[-1]
        rendered = EventNotifications(settings, service.store, None).render(
            "simple_strategy_changed", event
        )
        assert "opposite" in rendered and "/strategy" in rendered
        with pytest.raises(SafeError):
            await service.strategy_control("arm")
    finally:
        await service.exchange.close()


@pytest.mark.parametrize("change_during_dispatch", [False, True])
@pytest.mark.parametrize("side", [1, -1])
async def test_live_opposite_five_minute_order_and_protection(
    settings, store, filters, monkeypatch, change_during_dispatch, side
):
    import time

    from test_adaptive import permit, plan
    from test_bitcoin_reference import context
    from test_market_trend import breadth, day
    from test_simple_live import setup_engine

    from paperfrog.simple.strategy import exit_reason

    fixed_time = time.time()
    monkeypatch.setattr(time, "time", lambda: fixed_time)
    engine, simulation, adapter, _, now = await setup_engine(
        settings,
        store,
        filters,
        simple_entry_timeframe="5m",
        simple_ai_review_minutes=5,
        max_concurrent_strategy_trades=4,
        simple_market_trend_enabled=True,
        simple_btc_reference_enabled=True,
        simple_ai_enabled=True,
    )
    rotation.initialize(store, engine.strategy, now)
    rotation.configure(store, "opposite", now)
    boundary = now // engine.s.simple_entry_bar_ms * engine.s.simple_entry_bar_ms
    obs = minute_row(boundary, side=side, timeframe="5m")
    obs.update(day(boundary, side))
    breadth(store, engine.s, boundary, side)
    obs.update(context(store, engine.s, boundary // 900000 * 900000, side, now))
    conditions = [
        {
            "conditions": [
                {
                    "feature": "flow",
                    "operator": "ge" if side > 0 else "le",
                    "threshold": side * 0.1,
                },
                {
                    "feature": "trend_atr",
                    "operator": "ge" if side > 0 else "le",
                    "threshold": side * 0.4,
                },
            ]
        }
    ]
    permit(
        engine.s,
        store,
        plan(
            boundary,
            long_rules=conditions if side > 0 else [],
            short_rules=conditions if side < 0 else [],
        ),
    )
    store.put("simple_minute:SOLUSDT", obs)
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
            assert simulation.quantity * side < 0
            trade = engine.positions()["SOLUSDT"]
            assert trade["strategy_choice"] == "opposite"
            assert trade["strategy_epoch"] == 1
            assert trade["side"] == -side and trade["decision_side"] == side
            assert simulation.stops
            assert trade["side"] * (trade["entry_price"] - trade["stop_price"]) > 0
            assert exit_reason(trade, trade["entry_price"], now, obs, engine.rules) is None
            assert exit_reason(trade, trade["stop_price"], now, obs, engine.rules)
            rotation.configure(store, "current", now + 1)
            assert exit_reason(trade, trade["entry_price"], now, obs, engine.rules) is None
            assert exit_reason(
                trade,
                trade["entry_price"],
                now,
                {**obs, "trend_atr": -obs["trend_atr"]},
                engine.rules,
            )
            await engine.close("SOLUSDT", now, "test")
            assert simulation.quantity == 0
            assert rotation.selection(store)["outcomes"] == []
    finally:
        await adapter.close()
