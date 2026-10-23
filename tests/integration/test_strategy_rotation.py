from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_controls import update
from test_eight_minute import BOUNDARY, market
from test_market_trend import breadth, day, guarded
from test_simple import engine_fixture, row
from test_simple_live import setup_engine

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.simple import rotation
from paperfrog.simple.engine import Engine
from paperfrog.simple.service import SimpleService
from paperfrog.storage.database import Store


@pytest.mark.parametrize(
    "active,auto", [("current", True), ("opposite", True), ("opposite", False)]
)
def test_migration_preserves_selection_positions_and_locks_and_disables_auto(store, active, auto):
    store.put(
        rotation.KEY,
        {
            "active": active,
            "epoch": 5,
            "auto": auto,
            "policy_version": 2,
            "outcomes": [{"id": "old", "pnl": -1}],
        },
    )
    store.put("manual_lock", True)
    store.put("risk_lock", {"reason": "daily_loss"})
    store.put("simple_positions", {"SOLUSDT": {"stop_price": 99}})
    rotation.initialize(store, "base", 100)
    state = rotation.selection(store)
    assert state["active"] == active and state["auto"] is False
    assert state["epoch"] == (6 if auto else 5)
    assert state["outcomes"] == ([] if auto else [{"id": "old", "pnl": -1}])
    rotation.initialize(store, "base", 200)
    assert rotation.selection(store) == state
    assert len(store.get("simple_strategy_history", [])) == int(auto)
    assert store.get("manual_lock") is True
    assert store.get("risk_lock") == {"reason": "daily_loss"}
    assert store.get("simple_positions") == {"SOLUSDT": {"stop_price": 99}}


@pytest.mark.parametrize("choice", ["current", "opposite"])
def test_losses_never_switch_manually_selected_strategy(store, choice):
    rotation.initialize(store, "base", 0)
    rotation.configure(store, choice, 1)
    initial = rotation.selection(store)
    for i in range(30):
        rotation.closed(store, {"id": str(i), "strategy_epoch": initial["epoch"], "strategy_choice": choice}, -1, i)
    assert rotation.selection(store)["active"] == choice
    assert rotation.selection(store)["auto"] is False
    for invalid in ("auto", "live", "unknown"):
        with pytest.raises(SafeError, match="/strategy opposite"):
            rotation.configure(store, invalid, 100)
    assert "disabled" in rotation.describe(store, "UTC")


async def test_engine_records_net_loss_and_bootstrap_after_restart(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    await engine.enter("SOLUSDT", row(), 200 * 900000)
    await engine.close("SOLUSDT", 200 * 900000, "flat price with fees")
    assert rotation.selection(store)["outcomes"][0]["pnl"] < 0
    Engine(settings, store, filters)
    rotation.initialize(store, engine.strategy, 500)
    assert len(rotation.selection(store)["outcomes"]) == 1


def test_order_permissions_remain_exact_and_stale_epochs_fail_closed(store):
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
    permission = {
        "order": order.model_dump(mode="json"),
        "active": "current",
        "epoch": 0,
        "decision_side": 1,
    }
    store.put("simple_strategy_order:" + order.client_id, permission)
    rotation.require_order(store, order)
    store.put("simple_strategy_order:" + order.client_id, {**permission, "epoch": -1})
    with pytest.raises(EntryVeto):
        rotation.require_order(store, order)
    store.put("simple_strategy_order:" + order.client_id, permission)
    rotation.configure(store, "opposite", 100)
    with pytest.raises(EntryVeto):
        rotation.require_order(store, order)
    rotation.configure(store, "current", 101)
    with pytest.raises(EntryVeto):
        rotation.require_order(store, order)
    rotation.require_order(store, order.model_copy(update={"reduce_only": True}))


async def test_service_exposes_strategy_choices_and_confidence(settings):
    service = SimpleService(settings.model_copy(update={"simple_confidence_enabled": True}))
    try:
        rotation.initialize(service.store, "base", 0)
        service.engine = object()
        response = await service.strategy_control("current")
        assert "current" in response and "disabled" in response and "0.30%" in response
        assert "/strategy current" in response and "/strategy opposite" in response
        response = await service.strategy_control("opposite")
        assert "Strategy: opposite" in response
        assert "LONG → SHORT" in response and "SHORT → LONG" in response
        with pytest.raises(SafeError):
            await service.strategy_control("auto")
    finally:
        await service.exchange.close()


@pytest.mark.parametrize("decision", [1, -1])
async def test_opposite_reverses_paper_entries_and_retains_original_exit_thesis(
    settings, store, filters, decision
):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    rotation.configure(store, "opposite", 1)
    now = 200 * 900000
    obs = row(direction=decision)
    await engine.enter("SOLUSDT", obs, now)
    trade = engine.positions()["SOLUSDT"]
    assert trade["side"] == -decision
    assert trade["decision_side"] == decision
    assert trade["strategy_choice"] == "opposite"
    assert trade["entry_context"]["decision_side"] == decision
    await engine.monitor(now, {"SOLUSDT": obs})
    assert engine.positions()  # The original trend does not immediately close its inverse.
    rotation.configure(store, "current", now + 1)
    assert engine.positions()["SOLUSDT"]["side"] == -decision
    await engine.close("SOLUSDT", now + 2, "test close after switching strategy")
    assert not engine.positions() and not await engine.broker.positions()
    assert rotation.selection(store)["outcomes"] == []


@pytest.mark.parametrize("decision", [1, -1])
async def test_opposite_dispatches_reversed_live_order_and_matching_hosted_stop(
    settings, store, filters, decision
):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        rotation.initialize(store, engine.strategy, now)
        rotation.configure(store, "opposite", now + 1)
        if decision < 0:
            obs.update(trend_atr=-2, flow=-0.4, prior_low=101)
        await engine.enter("SOLUSDT", obs, now)
        trade = engine.positions()["SOLUSDT"]
        actual = -decision
        assert trade["side"] == actual and trade["decision_side"] == decision
        assert sim.quantity * actual > 0
        assert next(iter(sim.orders.values()))["side"] == ("BUY" if actual > 0 else "SELL")
        assert store.get("live_stops")["SOLUSDT"]["side"] == actual
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
    finally:
        await adapter.close()


async def test_opposite_keeps_no_signal_as_no_trade(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    rotation.configure(store, "opposite", 1)
    obs = row()
    obs["flow"] = 0
    await engine.enter("SOLUSDT", obs, 200 * 900000)
    assert not engine.positions() and not store.rows("orders")


@pytest.mark.parametrize("decision", [1, -1])
async def test_opposite_requires_original_decision_to_pass_market_gate(settings, store, filters, decision):
    s = guarded(settings)
    engine = await engine_fixture(s, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    rotation.configure(store, "opposite", 1)
    obs = {**market(side=decision).iloc[-1].to_dict(), **day(side=decision)}
    breadth(store, s, side=decision)
    assert engine.entry_signal(obs, BOUNDARY + 1000, 0)[0] == -decision
    breadth(store, s, side=-decision)
    assert engine.entry_signal(obs, BOUNDARY + 1000, 0)[0] == 0


def test_manual_selection_survives_reopened_database(store):
    rotation.configure(store, "opposite", 100)
    state = rotation.selection(store)
    reopened = Store(Path(store.engine.url.database))
    try:
        rotation.initialize(reopened, "base", 200)
        assert rotation.selection(reopened) == state
    finally:
        reopened.engine.dispose()


async def test_strategy_commands_are_authenticated_durable_and_leave_risk_state_untouched(settings):
    s = settings.model_copy(update={"telegram_allowed_user_ids": (1,), "telegram_allowed_chat_ids": (2,)})
    service = SimpleService(s)
    try:
        rotation.initialize(service.store, "base", 0)
        service.engine = object()
        service.controls.resume = AsyncMock()
        preserved = {
            "manual_lock": True,
            "risk_lock": {"reason": "daily_loss"},
            "anchors": {"equity": 500},
            "simple_positions": {"SOLUSDT": {"side": 1, "stop_price": 99}},
        }
        for key, value in preserved.items():
            service.store.put(key, value)
        request = update("/strategy opposite")
        assert await service.controls.handle(update("/strategy opposite", uid=99)) is None
        assert rotation.selection(service.store)["active"] == "current"
        response = await service.controls.handle(request)
        assert "Strategy: opposite" in response[1]
        state = rotation.selection(service.store)
        assert await service.controls.handle(request) is None
        await service.controls.handle(update("/strategy opposite"))
        rotation.initialize(service.store, "base", 100)
        assert rotation.selection(service.store) == state
        assert len(service.store.get("simple_strategy_history")) == 1
        assert {key: service.store.get(key) for key in preserved} == preserved
        service.controls.resume.assert_not_awaited()
        response = await service.controls.handle(update("/strategy"))
        assert "/strategy current" in response[1] and "/strategy opposite" in response[1]
        response = await service.controls.handle(update("/strategy current"))
        assert "Strategy: current" in response[1]
    finally:
        await service.exchange.close()
