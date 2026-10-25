from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_controls import update
from test_eight_minute import BOUNDARY
from test_market_trend import breadth, day, guarded
from test_opposite_confirmation import confirmed_row
from test_simple import engine_fixture, quote, row
from test_simple_live import setup_engine

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.simple import rotation
from paperfrog.simple.engine import Engine
from paperfrog.simple.service import SimpleService
from paperfrog.storage.database import Store


def settle_result(store, trade_id, pnl, now_ms=100):
    state = rotation.selection(store)
    trade = {"id": trade_id, "strategy_epoch": state["epoch"], "strategy_choice": state["active"]}
    rotation.closed(store, trade, pnl, now_ms)
    return trade


@pytest.mark.parametrize("initial", ["current", "opposite"])
def test_losses_never_switch_selected_strategy_even_across_restart(store, initial):
    rotation.configure(store, initial, 0)
    epoch = rotation.selection(store)["epoch"]
    for i, pnl in enumerate([1, 1, -1, -1, -1, -1, 1, -1, -1]):
        settle_result(store, str(i), pnl, i)
        assert rotation.selection(store)["active"] == initial
        assert rotation.selection(store)["auto"] is False
    reopened = Store(Path(store.engine.url.database))
    try:
        rotation.initialize(reopened, "base", 200)
        settle_result(reopened, "later-loss", -1, 201)
        assert rotation.selection(reopened)["active"] == initial
        assert rotation.selection(reopened)["epoch"] == epoch
        assert not any(e["auto"] for e in reopened.get("simple_strategy_history", []))
    finally:
        reopened.engine.dispose()


@pytest.mark.parametrize("active", ["current", "opposite"])
def test_upgrade_disables_auto_preserving_selection_positions_and_financial_locks(store, active):
    store.put(rotation.KEY, {"policy_version": 5, "active": active, "auto": True,
                            "epoch": 7, "loss_streak": 1, "outcomes": [{"id": "old", "pnl": -1}]})
    preserved = {"manual_lock": True, "risk_lock": {"reason": "three_losses"},
                 "consecutive_losses": 3, "anchors": {"equity": 500},
                 "simple_positions": {"SOLUSDT": {"side": -1, "stop_price": 101}}}
    for key, value in preserved.items():
        store.put(key, value)
    rotation.initialize(store, "base", 100)
    state = rotation.selection(store)
    assert state["active"] == active and state["epoch"] == 7 and state["auto"] is False
    assert state["outcomes"] == [] and state["loss_streak"] == 0
    assert store.get(rotation.RESULT_PREFIX + "old") is not None
    rotation.initialize(store, "base", 200)
    assert rotation.selection(store) == state
    assert {key: store.get(key) for key in preserved} == preserved


def test_manual_switch_dedup_and_transaction_rollback(store):
    rotation.configure(store, "current", 0)
    first = settle_result(store, "first", -1)
    for i in range(10):
        settle_result(store, str(i), 1)
    rotation.closed(store, first, -1, 100)
    assert rotation.selection(store)["loss_streak"] == 0
    with pytest.raises(RuntimeError), store.transaction() as conn:
        rotation.closed(store, {"id": "rollback", "strategy_choice": "current", "strategy_epoch": 0}, -1, 101, conn)
        raise RuntimeError("rollback")
    assert store.get(rotation.RESULT_PREFIX + "rollback") is None
    rotation.configure(store, "opposite", 102)
    rotation.configure(store, "current", 103)
    assert rotation.selection(store)["epoch"] == 2
    rotation.closed(store, first, -1, 104)
    assert rotation.selection(store)["loss_streak"] == 0
    for choice in ("unknown", "live"):
        with pytest.raises(SafeError, match="/strategy opposite"):
            rotation.configure(store, choice, 105)
    assert "/strategy auto" in rotation.describe(store, "UTC")


async def test_engine_records_net_loss_and_bootstrap_after_restart(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    await engine.enter("SOLUSDT", row(), 200 * 900000)
    await engine.close("SOLUSDT", 200 * 900000, "flat price with fees")
    assert rotation.selection(store)["outcomes"][0]["pnl"] < 0
    Engine(settings, store, filters)
    rotation.initialize(store, engine.strategy, 500)
    assert len(rotation.selection(store)["outcomes"]) == 1


async def test_two_complete_net_losses_leave_next_entry_in_selected_strategy(settings, store, filters):
    s = settings.model_copy(update={"max_entries_per_symbol_per_day": 4})
    engine = await engine_fixture(s, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    for i in range(2):
        now = 200 * 900000 + i * 4 * 3600000
        await engine.quote(quote(now_ms=now), now)
        await engine.enter("SOLUSDT", row(now), now)
        assert engine.positions()["SOLUSDT"]["side"] == 1
        await engine.close("SOLUSDT", now + 1, "flat price; fees make this a net loss")
        assert store.get("simple_closed")[-1]["pnl"] < 0
    assert rotation.selection(store)["active"] == "current"
    assert rotation.selection(store)["loss_streak"] == 2
    assert store.get("consecutive_losses") == 2  # Financial breaker history is preserved.
    now = 200 * 900000 + 8 * 3600000
    await engine.quote(quote(now_ms=now), now)
    await engine.enter("SOLUSDT", row(now), now)
    assert engine.positions()["SOLUSDT"]["side"] == 1
    assert engine.positions()["SOLUSDT"]["decision_side"] == 1


async def test_partial_live_exits_count_only_when_the_whole_position_is_closed(settings, store, filters):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        rotation.initialize(store, engine.strategy, now)
        settle_result(store, "prior-loss", -1, now)
        await engine.enter("SOLUSDT", obs, now)
        sim.reduce_fraction = Decimal(".5")
        for _i in range(2):
            await engine.close("SOLUSDT", engine.broker.now_ms(), "partial exit")
            assert engine.positions()
            assert rotation.selection(store)["active"] == "current"
            assert rotation.selection(store)["loss_streak"] == 1
        sim.reduce_fraction = Decimal(1)
        await engine.close("SOLUSDT", engine.broker.now_ms(), "final exit")
        assert not engine.positions() and sim.quantity == 0
        assert rotation.selection(store)["active"] == "current"
        assert rotation.selection(store)["loss_streak"] == 2
        await engine.restore()
        assert not store.get("simple_strategy_history")
    finally:
        await adapter.close()


@pytest.mark.parametrize("pnl", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_outcomes_never_modify_strategy_or_streak(store, pnl):
    rotation.initialize(store, "base", 0)
    before = rotation.selection(store)
    with pytest.raises(SafeError, match="finite net result"):
        settle_result(store, "invalid", pnl)
    assert rotation.selection(store) == before
    assert store.get(rotation.RESULT_PREFIX + "invalid") is None


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


def test_manual_switch_invalidates_entry_permission_but_preserves_exits(store):
    rotation.initialize(store, "base", 0)
    order = Order(client_id="pf-auto", symbol="SOLUSDT", side=1,
                  quantity=Decimal(1), limit_price=Decimal(100))
    store.put("simple_strategy_order:" + order.client_id, {
        "order": order.model_dump(mode="json"), "active": "current", "epoch": 0, "decision_side": 1,
    })
    rotation.require_order(store, order)
    settle_result(store, "first", -1)
    settle_result(store, "second", -1)
    rotation.require_order(store, order)
    rotation.configure(store, "opposite", 100)
    with pytest.raises(EntryVeto, match="strategy selection changed"):
        rotation.require_order(store, order)
    rotation.require_order(store, order.model_copy(update={"reduce_only": True}))


async def test_service_exposes_strategy_choices_and_confidence(settings):
    service = SimpleService(settings.model_copy(update={"simple_confidence_enabled": True, "simple_market_trend_enabled": True}))
    try:
        rotation.initialize(service.store, "base", 0)
        service.engine = object()
        response = await service.strategy_control("current")
        assert "Strategy: current · MANUAL" in response and "0.30%" in response
        assert "/strategy current" in response and "/strategy opposite" in response
        response = await service.strategy_control("opposite")
        assert "Strategy: opposite" in response
        assert "LONG → SHORT" in response and "SHORT → LONG" in response
        response = await service.strategy_control("auto")
        assert "AUTO" in response and rotation.selection(service.store)["auto"]
    finally:
        await service.exchange.close()


@pytest.mark.parametrize("decision", [1, -1])
async def test_opposite_confirms_paper_entries_and_retains_recorded_exit_thesis(
    settings, store, filters, decision
):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    rotation.configure(store, "opposite", 1)
    now = 200 * 900000
    obs = confirmed_row(direction=decision)
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
        obs.update(confirmed_row(now, decision))
        # Real frames contain optional NaN features. Journal only the finite
        # confirmation evidence so an irrelevant indicator cannot break entry.
        obs.update(rejection_level=float("nan"), optional_indicator=float("nan"))
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
    s = guarded(settings, simple_entry_timeframe="15m", simple_ai_review_minutes=15)
    engine = await engine_fixture(s, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    rotation.configure(store, "opposite", 1)
    obs = {**confirmed_row(BOUNDARY, decision), **day(side=decision)}
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
    s = settings.model_copy(update={"telegram_allowed_user_ids": (1,), "telegram_allowed_chat_ids": (2,), "simple_market_trend_enabled": True})
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
        assert await service.controls.handle(update("/strategy auto", uid=99)) is None
        assert rotation.selection(service.store)["auto"] is False
        response = await service.controls.handle(update("/strategy auto"))
        assert "AUTO" in response[1]
        assert rotation.selection(service.store)["auto"] is True
        assert {key: service.store.get(key) for key in preserved} == preserved
    finally:
        await service.exchange.close()


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("damage", ["missing", "flow", "expired", "future", "level", "price"])
def test_opposite_exact_order_permission_revalidates_confirmation_and_live_price(store, side, damage):
    from paperfrog.simple.strategy import BAR_MS, RULES, opposite_entry

    rotation.configure(store, "opposite", 0)
    now = 200 * BAR_MS
    obs = confirmed_row(now, side)
    level = opposite_entry(obs, .0007)[2]
    order = Order(client_id="pf-confirm", symbol="SOLUSDT", side=-side,
                  quantity=Decimal(1), limit_price=Decimal(100))
    permission = {"order": order.model_dump(mode="json"), "active": "opposite",
                  "epoch": rotation.selection(store)["epoch"], "decision_side": side,
                  "opposite_level": level,
                  "opposite_evidence": {"row": obs, "one_way_cost": .0007, "rules": RULES.public()}}
    store.put("simple_strategy_order:" + order.client_id, permission)
    q = quote(now_ms=now)
    rotation.require_order(store, order, now, q)
    if damage == "missing":
        permission.pop("opposite_evidence")
    elif damage == "flow":
        obs["flow"] = side * .4
    elif damage == "expired":
        now += BAR_MS
    elif damage == "future":
        now -= 1
    elif damage == "level":
        permission["opposite_level"] = 0
    else:
        q = quote(price=100 + side, now_ms=now)
    store.put("simple_strategy_order:" + order.client_id, permission)
    with pytest.raises(EntryVeto, match="failed-breakout or reversal evidence"):
        rotation.require_order(store, order, now, q)
    rotation.require_order(store, order.model_copy(update={"reduce_only": True}), now, q)
