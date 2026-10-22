import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from test_confidence import fills
from test_simple import quote, row
from test_simple_live import setup_engine

from paperfrog.domain.types import SafeError, State, UnknownOrder
from paperfrog.simple import confidence
from paperfrog.simple.engine import Engine


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("profit", [False, True])
@pytest.mark.parametrize("fraction", ["0", ".5"])
async def test_terminal_partial_or_zero_close_keeps_remainder_protected(
    settings, store, filters, side, profit, fraction
):
    engine, sim, adapter, _, now = await setup_engine(
        settings, store, filters, simple_confidence_enabled=True
    )
    try:
        await engine.enter("SOLUSDT", row(now, direction=side), now)
        original = dict(engine.positions()["SOLUSDT"])
        initial = Decimal(original["quantity"])
        stop = dict(store.get("live_stops")["SOLUSDT"])
        sim.reduce_fraction = Decimal(fraction)
        await engine.close(
            "SOLUSDT", now, "profit" if profit else "risk exit",
            profit_target=confidence.SMALL if profit else None,
        )
        remaining = engine.positions()["SOLUSDT"]
        assert Decimal(remaining["quantity"]) == abs(sim.quantity)
        assert abs(sim.quantity) <= initial and sim.quantity * side > 0
        assert store.get("simple_closing") == {}
        assert store.get("state") == State.RUNNING
        assert not store.get("simple_closed")
        assert store.rows("entries")[0]["closed"] == 0
        assert confidence.state(store)["wins"] == 0
        assert store.get("live_stops")["SOLUSDT"] == stop
        await engine.require_journal()
        # Other monitoring remains available after a normal partial fill.
        now = engine.broker.now_ms()
        await engine.quote(quote(now_ms=now), now)
        await engine.monitor(now, {"SOLUSDT": row(now, direction=side)})
        sim.reduce_fraction = Decimal(1)
        await engine.close("SOLUSDT", now + 20, "final exit")
        assert not engine.positions() and sim.quantity == 0
        closed = store.get("simple_closed")[-1]
        assert closed["quantity"] == float(initial)
        assert len(closed["exit_fills"]) == (2 if fraction != "0" else 1)
        gross = sum(
            (float(f["price"]) - original["entry_price"]) * float(f["quantity"]) * side
            for f in closed["exit_fills"]
        )
        fees = original["entry_fee"] + sum(float(f["fee"]) for f in closed["exit_fills"])
        assert closed["gross_pnl"] == pytest.approx(gross)
        assert closed["pnl"] == pytest.approx(gross - fees + original["funding"])
        assert closed["fees"] == pytest.approx(fees)
        assert not store.get("live_stops")
        assert len(store.get("simple_closed")) == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize("crash", [True, False])
async def test_partial_close_recovers_after_crash_or_unknown_response_without_resubmission(
    settings, store, filters, monkeypatch, crash
):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        sim.reduce_fraction = Decimal(".5")
        if crash:
            def power_loss(*args, **kwargs):
                raise RuntimeError("power loss before strategy journal commit")
            monkeypatch.setattr(engine, "record_reduction", power_loss)
        else:
            sim.unknown_order = True
        with pytest.raises(RuntimeError if crash else UnknownOrder):
            await engine.close("SOLUSDT", now, "test exit")
        assert store.get("simple_closing")
        before = sum(method == "POST" for method, _ in sim.calls)
        restored = Engine(engine.s, store, filters, broker=engine.broker)
        await restored.restore()
        trade = restored.positions()["SOLUSDT"]
        assert Decimal(trade["quantity"]) == abs(sim.quantity)
        assert len(trade["partial_exit_fills"]) == 1
        await restored.restore()
        assert len(restored.positions()["SOLUSDT"]["partial_exit_fills"]) == 1
        assert sum(method == "POST" for method, _ in sim.calls) == before
        assert not store.get("simple_closing")
        assert not store.get("simple_closed")
        await restored.require_journal()
    finally:
        await adapter.close()


async def test_partial_close_then_hosted_stop_counts_whole_trade_once(settings, store, filters):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        initial = Decimal(engine.positions()["SOLUSDT"]["quantity"])
        sim.reduce_fraction = Decimal(".5")
        await engine.close("SOLUSDT", now, "partial risk exit")
        sim.fire_stop()
        await engine.quote(quote(now_ms=now), now)
        assert not engine.positions()
        closed = store.get("simple_closed")[-1]
        assert closed["quantity"] == float(initial)
        assert len(closed["exit_exchange_ids"]) == 2
        assert store.rows("entries")[0]["closed"] == 1
        assert await engine.coordinator.reconcile()
        assert len(store.get("simple_closed")) == 1
    finally:
        await adapter.close()


async def test_repeated_partial_fills_retry_only_remaining_quantity(settings, store, filters):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        initial = Decimal(engine.positions()["SOLUSDT"]["quantity"])
        sim.reduce_fraction = Decimal(".5")
        for i in range(3):
            before = abs(sim.quantity)
            await engine.close("SOLUSDT", now + i, "risk exit")
            intent = json.loads(store.rows("orders", 1)[0]["intent"])
            assert Decimal(intent["quantity"]) == before
            assert Decimal(engine.positions()["SOLUSDT"]["quantity"]) == abs(sim.quantity)
            assert not store.get("simple_closed")
            assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        assert len(engine.positions()["SOLUSDT"]["partial_exit_fills"]) == 3
        sim.reduce_fraction = Decimal(1)
        await engine.close("SOLUSDT", now + 10, "final exit")
        closed = store.get("simple_closed")[-1]
        assert closed["quantity"] == float(initial)
        assert len(closed["exit_exchange_ids"]) == 4
        assert len(store.get("simple_closed")) == 1
        assert await engine.coordinator.reconcile()
    finally:
        await adapter.close()


@pytest.mark.parametrize("side", [1, -1])
def test_profit_boundary_includes_realized_partial_fees_and_full_entry_cost(settings, side):
    trade = {
        "side": side, "entry_price": 100, "quantity": "1", "initial_quantity": "2",
        "entry_fee": .1, "funding": -.02,
        "partial_exit_fills": [{"fill": {"price": str(100 + side), "quantity": "1", "fee": ".05"}}],
    }
    limit = confidence.limit_price(settings, trade, confidence.SMALL, Decimal(".01"))
    gross, fees = confidence.partial_accounting(trade)
    pnl = (float(limit) - 100) * side + float(gross - fees) - float(limit) * settings.fee_rate - .12
    assert pnl / 200 >= confidence.SMALL - 1e-12
    q = quote(price=100 + side * .2)
    executable = float(q.bid if side > 0 else q.ask) * (1 - side * settings.slippage_bps / 10000)
    expected = ((executable - 100) * side + float(gross - fees) - executable * settings.fee_rate - .12) / 200
    assert confidence.net_return(settings, trade, q) == pytest.approx(expected)


@pytest.mark.parametrize("missing", [False, True])
async def test_live_confidence_verifies_every_partial_exit_before_counting(settings, store, missing):
    closed = {
        "trade_id": "partial", "confidence_version": 1, "entry_price": 100., "quantity": 2.,
        "entry_exchange_id": "10", "exit_exchange_id": "12", "exit_exchange_ids": ["11", "12"],
        "direction": 1, "symbol": "SOLUSDT", "entry_ms": 1000, "timestamp_ms": 2000,
    }
    store.put("simple_closed", [closed])
    with store.transaction() as conn:
        confidence.enqueue(store, closed, conn)
    exchange = AsyncMock()
    exchange.request.side_effect = [
        [fills("10", "BUY", fee=".1")],
        [] if missing else [fills("11", "SELL", qty=".7", gross=".5", fee=".04")],
        [fills("12", "SELL", qty="1.3", gross=".3", fee=".06")],
        [],
    ]
    await confidence.reconcile(settings, store, exchange, 100000)
    if missing:
        assert confidence.state(store)["pending"] == ["partial"]
        assert confidence.state(store)["wins"] == 0
    else:
        assert confidence.state(store)["wins"] == 1
        assert store.get("simple_closed")[0]["verified_net_pnl"] == pytest.approx(.6)
        await confidence.reconcile(settings, store, exchange, 200000)
        assert confidence.state(store)["wins"] == 1


async def test_wrong_close_fill_identity_keeps_intent_and_journal_blocked(settings, store, filters, monkeypatch):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        original_submit = engine.coordinator.submit_once
        async def wrong_fill(order):
            fill = await original_submit(order)
            return fill.model_copy(update={"side": -fill.side})
        monkeypatch.setattr(engine.coordinator, "submit_once", wrong_fill)
        with pytest.raises(SafeError, match="close fill does not match"):
            await engine.close("SOLUSDT", now, "test exit")
        assert store.get("simple_closing")
        assert not store.get("simple_closed")
        with pytest.raises(SafeError, match="journal incomplete"):
            await engine.require_journal()
        assert json.loads(store.rows("orders", 1)[0]["intent"])["reduce_only"]
    finally:
        await adapter.close()
