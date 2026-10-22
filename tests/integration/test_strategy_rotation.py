from decimal import Decimal

import pytest
from test_simple import engine_fixture, row

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.simple import rotation
from paperfrog.simple.engine import Engine
from paperfrog.simple.service import SimpleService


@pytest.mark.parametrize(
    "active,auto", [("current", True), ("opposite", True), ("opposite", False)]
)
def test_migration_locks_current_once_preserving_positions_and_locks(store, active, auto):
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
    assert state["active"] == "current" and state["auto"] is False
    assert state["epoch"] == 6 and state["outcomes"] == []
    rotation.initialize(store, "base", 200)
    assert rotation.selection(store) == state
    assert len(store.get("simple_strategy_history")) == 1
    assert store.get("manual_lock") is True
    assert store.get("risk_lock") == {"reason": "daily_loss"}
    assert store.get("simple_positions") == {"SOLUSDT": {"stop_price": 99}}


def test_losses_never_switch_and_commands_cannot_enable_opposite(store):
    rotation.initialize(store, "base", 0)
    for i in range(30):
        rotation.closed(store, {"id": str(i), "strategy_epoch": 0}, -1, i)
    assert rotation.selection(store)["active"] == "current"
    assert rotation.selection(store)["auto"] is False
    for choice in ("opposite", "auto", "live"):
        with pytest.raises(SafeError, match="locked to current"):
            rotation.configure(store, choice, 100)
    rotation.configure(store, "current", 101)
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
    rotation.require_order(store, order.model_copy(update={"reduce_only": True}))


async def test_service_exposes_locked_strategy_and_confidence(settings):
    service = SimpleService(settings.model_copy(update={"simple_confidence_enabled": True}))
    try:
        rotation.initialize(service.store, "base", 0)
        service.engine = object()
        response = await service.strategy_control("current")
        assert "current" in response and "disabled" in response and "0.15%" in response
        with pytest.raises(SafeError):
            await service.strategy_control("auto")
    finally:
        await service.exchange.close()
