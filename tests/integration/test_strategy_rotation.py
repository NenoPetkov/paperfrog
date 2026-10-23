from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_controls import update
from test_eight_minute import BOUNDARY, market
from test_market_trend import breadth, day, guarded
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
def test_auto_repeated_two_loss_switches_match_requested_sequence(store, initial):
    rotation.initialize(store, "base", 0)
    if initial == "opposite":
        rotation.configure(store, "opposite", 1)
        rotation.configure(store, "auto", 2)
    sequence = [1, 1, -1, -1, -1, -1, 1, -1, 1, -1, -1]
    expected = ["current", "current", "current", "opposite", "opposite", "current",
                "current", "current", "current", "current", "opposite"]
    streaks = [0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0]
    if initial == "opposite":
        expected = ["opposite" if active == "current" else "current" for active in expected]
    for i, (pnl, active, streak) in enumerate(zip(sequence, expected, streaks, strict=True)):
        settle_result(store, str(i), pnl, i)
        state = rotation.selection(store)
        assert state["active"] == active and state["loss_streak"] == streak
        assert state["auto"] is True
    events = [event for event in store.get("simple_strategy_history") if event["auto"]]
    other = "opposite" if initial == "current" else "current"
    assert [(e["from"], e["to"]) for e in events] == [
        (initial, other), (other, initial), (initial, other)
    ]
    assert all(e["consecutive_losses"] == 2 and e["auto"] for e in events)


@pytest.mark.parametrize("reset_pnl", [1.0, 0.0])
def test_win_or_breakeven_breaks_consecutive_losses(store, reset_pnl):
    rotation.initialize(store, "base", 0)
    for i, pnl in enumerate([-1, reset_pnl, -1]):
        settle_result(store, str(i), pnl)
    assert rotation.selection(store)["active"] == "current"
    assert rotation.selection(store)["loss_streak"] == 1
    assert not store.get("simple_strategy_history")


def test_auto_streak_survives_restart_and_duplicate_outcomes_do_not_count(store):
    rotation.initialize(store, "base", 0)
    first = settle_result(store, "one", -1)
    reopened = Store(Path(store.engine.url.database))
    try:
        rotation.initialize(reopened, "base", 200)
        rotation.closed(reopened, first, -1, 201)
        assert rotation.selection(reopened)["loss_streak"] == 1
        settle_result(reopened, "two", -1, 202)
        assert rotation.selection(reopened)["active"] == "opposite"
        rotation.closed(reopened, first, -1, 203)
        assert rotation.selection(reopened)["loss_streak"] == 0
        settle_result(reopened, "three", -1, 204)
        rotation.initialize(reopened, "base", 205)
        assert rotation.selection(reopened)["loss_streak"] == 1
        settle_result(reopened, "four", -1, 206)
        assert rotation.selection(reopened)["active"] == "current"
    finally:
        reopened.engine.dispose()


def test_old_stint_closures_cannot_count_again_when_strategy_cycles_back(store):
    rotation.initialize(store, "base", 0)
    old_trade = {"id": "still-open", "strategy_epoch": 0, "strategy_choice": "current"}
    for i in range(4):
        settle_result(store, str(i), -1)
    assert rotation.selection(store)["active"] == "current"
    assert rotation.selection(store)["epoch"] == 2
    rotation.closed(store, old_trade, -1, 200)
    assert rotation.selection(store)["loss_streak"] == 0


def test_results_are_deduplicated_beyond_the_recent_sample(store):
    rotation.configure(store, "current", 0)
    first = settle_result(store, "first", -1)
    for i in range(10):
        settle_result(store, str(i), 1)
    rotation.closed(store, first, -1, 200)
    assert rotation.selection(store)["loss_streak"] == 0
    rotation.configure(store, "auto", 201)
    rotation.closed(store, first, -1, 202)
    assert rotation.selection(store)["loss_streak"] == 0


def test_switch_and_dedup_marker_rollback_atomically(store):
    rotation.initialize(store, "base", 0)
    settle_result(store, "first", -1)
    trade = {"id": "second", "strategy_epoch": 0, "strategy_choice": "current"}
    with pytest.raises(RuntimeError), store.transaction() as conn:
        rotation.closed(store, trade, -1, 200, conn)
        raise RuntimeError("simulated failure before closure commit")
    assert rotation.selection(store)["active"] == "current"
    assert rotation.selection(store)["loss_streak"] == 1
    assert store.get(rotation.RESULT_PREFIX + "second") is None
    assert not store.get("simple_strategy_history")
    rotation.closed(store, trade, -1, 201)
    assert rotation.selection(store)["active"] == "opposite"


def test_auto_mode_enabling_and_repeated_commands_preserve_live_streak(store):
    rotation.configure(store, "opposite", 0)
    settle_result(store, "manual", -1)
    rotation.configure(store, "auto", 10)
    assert rotation.selection(store)["active"] == "opposite"
    assert rotation.selection(store)["loss_streak"] == 0
    settle_result(store, "auto-one", -1)
    state = rotation.selection(store)
    rotation.configure(store, "auto", 20)
    rotation.initialize(store, "base", 21)
    assert rotation.selection(store) == state
    settle_result(store, "auto-two", -1)
    assert rotation.selection(store)["active"] == "current"


def test_automatic_switch_preserves_positions_financial_state_and_locks(store):
    rotation.initialize(store, "base", 0)
    preserved = {
        "manual_lock": True, "risk_lock": {"state": "SOFT_RISK_LOCK", "reason": "three_losses"},
        "anchors": {"equity": 500}, "consecutive_losses": 3,
        "cooldown:SOLUSDT": 2000, "simple_positions": {"SOLUSDT": {"side": 1, "stop_price": 99}},
    }
    for key, value in preserved.items():
        store.put(key, value)
    settle_result(store, "one", -1)
    settle_result(store, "two", -1)
    assert rotation.selection(store)["active"] == "opposite"
    assert {key: store.get(key) for key in preserved} == preserved


@pytest.mark.parametrize(
    "active,auto", [("current", True), ("opposite", True), ("opposite", False)]
)
def test_migration_enables_auto_without_changing_strategy_or_seeding_old_losses(store, active, auto):
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
    assert state["active"] == active and state["auto"] is True
    assert state["epoch"] == 5
    assert state["outcomes"] == [] and state["loss_streak"] == 0
    rotation.initialize(store, "base", 200)
    assert rotation.selection(store) == state
    assert not store.get("simple_strategy_history")
    assert store.get(rotation.RESULT_PREFIX + "old") is not None
    rotation.closed(store, {"id": "old", "strategy_epoch": 5, "strategy_choice": active}, -1, 201)
    assert rotation.selection(store)["loss_streak"] == 0
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
    for invalid in ("live", "unknown"):
        with pytest.raises(SafeError, match="/strategy opposite"):
            rotation.configure(store, invalid, 100)
    assert "Automatic switching paused" in rotation.describe(store, "UTC")


async def test_engine_records_net_loss_and_bootstrap_after_restart(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    rotation.initialize(store, engine.strategy, 0)
    await engine.enter("SOLUSDT", row(), 200 * 900000)
    await engine.close("SOLUSDT", 200 * 900000, "flat price with fees")
    assert rotation.selection(store)["outcomes"][0]["pnl"] < 0
    Engine(settings, store, filters)
    rotation.initialize(store, engine.strategy, 500)
    assert len(rotation.selection(store)["outcomes"]) == 1


async def test_two_complete_net_losses_switch_the_next_executed_entry(settings, store, filters):
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
    assert rotation.selection(store)["active"] == "opposite"
    assert rotation.selection(store)["loss_streak"] == 0
    assert store.get("consecutive_losses") == 2  # Financial breaker history is preserved.
    now = 200 * 900000 + 8 * 3600000
    await engine.quote(quote(now_ms=now), now)
    await engine.enter("SOLUSDT", row(now), now)
    assert engine.positions()["SOLUSDT"]["side"] == -1
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
        assert rotation.selection(store)["active"] == "opposite"
        assert rotation.selection(store)["loss_streak"] == 0
        await engine.restore()
        assert len(store.get("simple_strategy_history")) == 1
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


def test_automatic_switch_invalidates_entry_permission_but_preserves_exits(store):
    rotation.initialize(store, "base", 0)
    order = Order(client_id="pf-auto", symbol="SOLUSDT", side=1,
                  quantity=Decimal(1), limit_price=Decimal(100))
    store.put("simple_strategy_order:" + order.client_id, {
        "order": order.model_dump(mode="json"), "active": "current", "epoch": 0, "decision_side": 1,
    })
    rotation.require_order(store, order)
    settle_result(store, "first", -1)
    settle_result(store, "second", -1)
    with pytest.raises(EntryVeto, match="strategy selection changed"):
        rotation.require_order(store, order)
    rotation.require_order(store, order.model_copy(update={"reduce_only": True}))


async def test_service_exposes_strategy_choices_and_confidence(settings):
    service = SimpleService(settings.model_copy(update={"simple_confidence_enabled": True}))
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
        assert "Strategy: opposite · AUTO" in response
        assert "Consecutive losses: 0/2" in response
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
        assert await service.controls.handle(update("/strategy auto", uid=99)) is None
        assert rotation.selection(service.store)["auto"] is False
        response = await service.controls.handle(update("/strategy auto"))
        assert "Strategy: current · AUTO" in response[1]
        assert {key: service.store.get(key) for key in preserved} == preserved
    finally:
        await service.exchange.close()
