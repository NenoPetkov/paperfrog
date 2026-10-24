from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_simple import engine_fixture, quote, row

from paperfrog.domain.types import SafeError
from paperfrog.simple import confidence
from paperfrog.simple.engine import Engine
from paperfrog.storage.database import Store


def record(i):
    return {
        "trade_id": str(i),
        "confidence_version": 1,
        "entry_price": 100.0,
        "quantity": 2.0,
        "entry_exchange_id": "10",
        "exit_exchange_id": "11",
        "direction": 1,
        "symbol": "SOLUSDT",
        "entry_ms": 1000,
        "timestamp_ms": 2000,
    }


def outcome(store, i, pnl):
    with store.transaction() as conn:
        confidence.enqueue(store, record(i), conn)
        confidence.settle(store, record(i), pnl, conn)


def test_three_wins_promote_loss_and_breakeven_reset_restart_and_dedup(store):
    for i in range(2):
        outcome(store, i, 1)
        assert confidence.target(store) == confidence.SMALL
    outcome(store, 2, 1)
    assert confidence.target(store) == confidence.LARGE
    with store.transaction() as conn:
        confidence.settle(store, record(2), 1, conn)
    assert confidence.state(store)["wins"] == 3
    reopened = Store(Path(store.engine.url.database))
    assert confidence.target(reopened) == confidence.LARGE
    reopened.engine.dispose()
    outcome(store, 3, -0.1)
    assert confidence.state(store)["streak"] == 0
    assert confidence.target(store) == confidence.SMALL
    for i in range(4, 7):
        outcome(store, i, 1)
    outcome(store, 7, 0)
    assert confidence.state(store)["breakevens"] == 1
    assert confidence.state(store)["streak"] == 0


def test_pending_result_blocks_promotion_and_out_of_order_settlement(store):
    for i in range(3):
        outcome(store, i, 1)
    with store.transaction() as conn:
        confidence.enqueue(store, record(3), conn)
        confidence.enqueue(store, record(4), conn)
    assert confidence.target(store) == confidence.SMALL
    with pytest.raises(SafeError, match="closure order"), store.transaction() as conn:
        confidence.settle(store, record(4), 1, conn)
    with pytest.raises(SafeError), store.transaction() as conn:
        confidence.settle(store, record(3), float("nan"), conn)
    assert confidence.state(store)["pending"] == ["3", "4"]


def test_accounting_transaction_rolls_back(store):
    with pytest.raises(RuntimeError), store.transaction() as conn:
        confidence.enqueue(store, record(0), conn)
        confidence.settle(store, record(0), 1, conn)
        raise RuntimeError()
    assert confidence.state(store)["wins"] == 0
    outcome(store, 0, 1)
    assert confidence.state(store)["wins"] == 1


@pytest.mark.parametrize("side", [1, -1])
def test_executable_profit_includes_fees_funding_spread_and_slippage(settings, side):
    trade = {"side": side, "entry_price": 100, "quantity": "2", "entry_fee": 0.1, "funding": -0.1}
    assert confidence.net_return(settings, trade, quote(price=100 + side * 0.1)) < 0
    limit = confidence.limit_price(settings, trade, confidence.SMALL, Decimal(".01"))
    pnl = (float(limit) - 100) * 2 * side - float(limit) * 2 * settings.fee_rate - 0.2
    assert pnl / 200 >= confidence.SMALL - 1e-12


@pytest.mark.parametrize("side", [1, -1])
async def test_monitor_runs_winners_past_target_then_exits_on_retracement(
    settings, store, filters, side
):
    s = settings.model_copy(update={"simple_confidence_enabled": True})
    engine = await engine_fixture(s, store, filters)
    now = 200 * 900000
    obs = row(direction=side)
    await engine.enter("SOLUSDT", obs, now)
    trade = engine.positions()["SOLUSDT"]
    assert trade["profit_target_fraction"] == confidence.SMALL
    await engine.quote(quote(price=100 + side * 0.6, now_ms=now + 5000), now + 5000)
    await engine.monitor(now + 5000, {"SOLUSDT": obs})
    assert engine.positions()  # The old target arms a runner instead of selling.
    first = engine.positions()["SOLUSDT"]["profit_runner"]
    await engine.quote(quote(price=100 + side * 1.2, now_ms=now + 10000), now + 10000)
    await engine.monitor(now + 10000, {"SOLUSDT": obs})
    assert engine.positions()
    high = engine.positions()["SOLUSDT"]["profit_runner"]
    assert high["floor_net_return"] > first["floor_net_return"]
    # Restore the engine to prove the earned floor survives a restart.
    restored = Engine(s, store, filters)
    await restored.restore()
    await restored.quote(quote(price=100 + side * .6, now_ms=now + 15000), now + 15000)
    await restored.monitor(now + 15000, {"SOLUSDT": obs})
    assert not restored.positions()
    closed = store.get("simple_closed")[-1]
    assert "profit runner retracement" in closed["reason"]
    assert closed["pnl"] > 0 and closed["profit_runner"]["peak_net_return"] > .006
    assert confidence.state(store)["wins"] == 1
    assert confidence.state(store)["pending"] == []


async def test_existing_position_gets_profit_runner_but_not_new_sample(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    now = 200 * 900000
    await engine.enter("SOLUSDT", row(), now)
    engine.s = settings.model_copy(update={"simple_confidence_enabled": True})
    await engine.quote(quote(price=100.6, now_ms=now + 5000), now + 5000)
    await engine.monitor(now + 5000, {"SOLUSDT": row()})
    assert engine.positions()["SOLUSDT"]["profit_runner"]
    await engine.quote(quote(price=100.3, now_ms=now + 10000), now + 10000)
    await engine.monitor(now + 10000, {"SOLUSDT": row()})
    assert not engine.positions()
    assert confidence.state(store)["wins"] == 0


async def test_unfilled_profit_order_retries_without_counting(settings, store, filters):
    engine = await engine_fixture(
        settings.model_copy(update={"simple_confidence_enabled": True}), store, filters
    )
    now = 200 * 900000
    await engine.enter("SOLUSDT", row(), now)
    await engine.close("SOLUSDT", now, "profit", profit_target=confidence.SMALL)
    assert engine.positions()
    assert store.get("simple_closing") == {}
    assert confidence.state(store)["wins"] == 0
    await engine.quote(quote(price=100.6, now_ms=now + 5000), now + 5000)
    await engine.monitor(now + 5000, {"SOLUSDT": row()})
    assert engine.positions()["SOLUSDT"]["profit_runner"]
    await engine.quote(quote(price=100.3, now_ms=now + 10000), now + 10000)
    await engine.monitor(now + 10000, {"SOLUSDT": row()})
    assert not engine.positions()


def fills(order, side, fee=".1", gross="0", qty="2", asset="BNFCR"):
    return {
        "time": 1000 if order == "10" else 2000,
        "id": order,
        "orderId": order,
        "symbol": "SOLUSDT",
        "side": side,
        "qty": qty,
        "commission": fee,
        "commissionAsset": asset,
        "realizedPnl": gross,
    }


@pytest.mark.parametrize("failure", [None, "missing", "currency", "api"])
async def test_live_verification_uses_actual_commissions_and_deduplicates(settings, store, failure):
    closed = {**record(0), "pnl": 10}  # Estimated win is actually a loss after exchange fees.
    store.put("simple_closed", [closed])
    with store.transaction() as conn:
        confidence.enqueue(store, closed, conn)
    entry = fills("10", "BUY")
    exit_fill = fills("11", "SELL", gross=".15")
    if failure == "currency":
        exit_fill["commissionAsset"] = "BNB"
    exchange = AsyncMock()
    exchange.request.side_effect = ([entry, entry], [] if failure == "missing" else [exit_fill], [])
    if failure == "api":
        exchange.request.side_effect = SafeError("unavailable")
    await confidence.reconcile(settings, store, exchange, 100000)
    if failure:
        assert confidence.state(store)["pending"] == ["0"]
        assert confidence.state(store)["wins"] == 0
    else:
        assert confidence.state(store)["losses"] == 1
        assert store.get("simple_closed")[0]["verified_net_pnl"] == pytest.approx(-0.05)
        await confidence.reconcile(settings, store, exchange, 200000)
        assert confidence.state(store)["losses"] == 1
        assert exchange.request.await_count == 3


async def test_promoted_target_is_not_raised_mid_trade_and_loss_lowers_it(settings, store, filters):
    engine = await engine_fixture(
        settings.model_copy(update={"simple_confidence_enabled": True}), store, filters
    )
    now = 200 * 900000
    for i in range(3):
        outcome(store, i, 1)
    await engine.enter("SOLUSDT", row(), now)
    assert engine.positions()["SOLUSDT"]["profit_target_fraction"] == confidence.LARGE
    outcome(store, 3, -1)
    await engine.monitor(now, {"SOLUSDT": row()})
    assert engine.positions()["SOLUSDT"]["profit_target_fraction"] == confidence.SMALL
    for i in range(4, 7):
        outcome(store, i, 1)
    await engine.monitor(now, {"SOLUSDT": row()})
    assert confidence.target(store) == confidence.LARGE
    assert engine.positions()["SOLUSDT"]["profit_target_fraction"] == confidence.SMALL


@pytest.mark.parametrize("side", [1, -1])
async def test_live_profit_exit_is_reduce_only_and_waits_for_actual_accounting(
    settings, store, filters, side
):
    from test_simple_live import setup_engine

    engine, simulation, adapter, _, now = await setup_engine(
        settings,
        store,
        filters,
        simple_confidence_enabled=True,
    )
    # This simulation normally fills at the submitted limit, even when a much
    # better touch is available. For a marketable runner exit, simulate matching
    # at the executable touch rather than the 2% emergency price boundary.
    original_result = simulation.order_result

    def execute_touch(key, direction, qty, price, status="FILLED"):
        if simulation.quantity and (direction == "SELL") == (simulation.quantity > 0):
            q = engine.broker.quotes["SOLUSDT"]
            price = q.bid if direction == "SELL" else q.ask
        return original_result(key, direction, qty, price, status)

    simulation.order_result = execute_touch
    try:
        await engine.enter("SOLUSDT", row(now, direction=side), now)
        assert simulation.quantity * side > 0
        await engine.quote(quote(price=100 + side * 0.8, now_ms=now), now)
        await engine.monitor(now, {"SOLUSDT": row(now, direction=side)})
        assert engine.positions()["SOLUSDT"]["profit_runner"]
        await engine.quote(quote(price=100 + side * .4, now_ms=now), now)
        await engine.monitor(now, {"SOLUSDT": row(now, direction=side)})
        assert not engine.positions()
        assert simulation.quantity == 0
        closed = store.get("simple_closed")[-1]
        assert closed["pnl"] > 0
        assert closed["entry_exchange_id"] and closed["exit_exchange_id"]
        assert confidence.state(store)["wins"] == 0
        assert confidence.state(store)["pending"] == [closed["trade_id"]]
        order = store.rows("orders", 1)[0]
        import json

        assert json.loads(order["intent"])["reduce_only"] is True
    finally:
        await adapter.close()


async def test_disabling_ai_prevents_both_service_and_daily_review_client(
    settings, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from pydantic import SecretStr

    from paperfrog.simple import daily_review
    from paperfrog.simple.service import SimpleService

    s = settings.model_copy(
        update={"simple_ai_enabled": False, "openai_api_key": SecretStr("unused")}
    )
    service = SimpleService(s)
    try:
        assert service.advisor is None
    finally:
        await service.exchange.close()
    monkeypatch.setattr(daily_review, "Settings", lambda: s)

    def forbidden_client(**kwargs):
        raise AssertionError("disabled OpenAI must not instantiate a client")

    monkeypatch.setattr(daily_review, "AsyncOpenAI", forbidden_client)
    reviewer = AsyncMock()
    reviewer.generate.return_value = {"review": None, "proposals": []}

    def build_reviewer(settings, root, state, client):
        assert client is None
        return reviewer

    monkeypatch.setattr(daily_review, "Reviewer", build_reviewer)
    await daily_review.run(
        SimpleNamespace(
            timezone="UTC",
            show_proposal=None,
            preview_day="2026-10-01",
            hour=10,
            first_delivery="1970-01-01",
        )
    )
    reviewer.generate.assert_awaited_once()


async def test_actual_fill_times_bound_funding_and_duplicate_income_counts_once(settings, store):
    closed = {**record(0), "entry_ms": 500, "timestamp_ms": 1800}
    store.put("simple_closed", [closed])
    with store.transaction() as conn:
        confidence.enqueue(store, closed, conn)
    funding = {
        "tranId": "fund",
        "time": 1900,
        "symbol": "SOLUSDT",
        "incomeType": "FUNDING_FEE",
        "asset": "BNFCR",
        "income": "-.3",
    }
    outside = {**funding, "tranId": "outside", "time": 600, "income": "-9"}
    exchange = AsyncMock()
    exchange.request.side_effect = [
        [fills("10", "BUY")],
        [fills("11", "SELL", gross=".4")],
        [funding, funding, outside],
    ]
    await confidence.reconcile(settings, store, exchange, 100000)
    assert confidence.state(store)["losses"] == 1
    saved = store.get("simple_closed")[0]
    assert saved["verified_net_pnl"] == pytest.approx(-0.1)
    assert saved["verified_funding"] == pytest.approx(-0.3)
    assert saved["verified_entry_ms"] == 1000 and saved["verified_exit_ms"] == 2000
    params = exchange.request.call_args.args[2]
    assert params["startTime"] == 1000 and params["endTime"] == 2000


def test_runner_floor_never_loosens_when_confidence_or_price_falls():
    trade = {}
    assert not confidence.update_runner(trade, .006, .006, 1)
    assert not confidence.update_runner(trade, .012, .006, 2)
    assert trade["profit_runner"]["floor_net_return"] == .006
    assert not confidence.update_runner(trade, .009, .003, 3)
    assert trade["profit_runner"]["floor_net_return"] == .006
    assert confidence.update_runner(trade, .005, .003, 4)
    assert trade["profit_runner"]["floor_net_return"] == .006


@pytest.mark.parametrize("side", [1, -1])
async def test_runner_gap_exits_even_below_earned_floor(settings, store, filters, side):
    engine = await engine_fixture(settings.model_copy(update={"simple_confidence_enabled": True}), store, filters)
    now = 200 * 900000
    await engine.enter("SOLUSDT", row(now, side), now)
    await engine.quote(quote(price=100 + side * .8, now_ms=now + 5000), now + 5000)
    await engine.monitor(now + 5000, {"SOLUSDT": row(now, side)})
    assert engine.positions()
    await engine.quote(quote(price=100 - side * .2, now_ms=now + 10000), now + 10000)
    await engine.monitor(now + 10000, {"SOLUSDT": row(now, side)})
    assert not engine.positions()  # An unreachable profit limit must not trap the exit.
    assert "profit runner retracement" in store.get("simple_closed")[-1]["reason"]


async def test_runner_partial_exit_preserves_floor_until_fully_closed(settings, store, filters):
    from test_simple_live import setup_engine

    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters, simple_confidence_enabled=True)
    try:
        await engine.enter("SOLUSDT", obs, now)
        await engine.quote(quote(price=100.8, now_ms=now), now)
        await engine.monitor(now, {"SOLUSDT": obs})
        floor = engine.positions()["SOLUSDT"]["profit_runner"]["floor_net_return"]
        sim.reduce_fraction = Decimal(".5")
        await engine.quote(quote(price=100.3, now_ms=now), now)
        await engine.monitor(now, {"SOLUSDT": obs})
        assert engine.positions()["SOLUSDT"]["profit_runner"]["floor_net_return"] == floor
        assert confidence.state(store)["pending"] == []
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        sim.reduce_fraction = Decimal(1)
        await engine.monitor(now + 1, {"SOLUSDT": obs})
        assert not engine.positions() and sim.quantity == 0
        assert len(confidence.state(store)["pending"]) == 1
    finally:
        await adapter.close()
