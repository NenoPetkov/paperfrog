from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from test_execution import order
from test_simple_live import setup_engine

from paperfrog.domain.types import OrderNotFound, SafeError, State, UnknownOrder
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.settings import Settings
from paperfrog.simple.authorization import directory
from paperfrog.simple.service import SimpleService


@pytest.mark.parametrize("lookup_misses", [0, 1, 2, 3])
async def test_order_lookup_visibility_delay_never_repeats_submission(filters, lookup_misses):
    calls = []
    result = {
        "clientOrderId": "pf-first", "symbol": "SOLUSDT", "side": "BUY",
        "executedQty": "1", "avgPrice": "100", "status": "FILLED", "orderId": 101,
    }
    misses = lookup_misses

    def handle(request):
        nonlocal misses
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={**result, "avgPrice": "0"})
        assert request.url.params["origClientOrderId"] == "pf-first"
        if misses:
            misses -= 1
            return httpx.Response(400, json={"code": -2013})
        return httpx.Response(200, json=result)

    s = Settings(_env_file=None, trading_mode="testnet", binance_api_key="key", binance_api_secret="secret")
    adapter = BinanceAdapter(s, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    adapter.filters = filters
    adapter.entry_authorized = True
    try:
        if lookup_misses == 3:
            with pytest.raises(OrderNotFound):
                await adapter.submit(order())
        else:
            fill = await adapter.submit(order())
            assert fill.quantity == 1 and fill.price == 100
        assert sum(method == "POST" for method, _ in calls) == 1
        assert sum(method == "GET" for method, _ in calls) == min(3, lookup_misses + 1)
    finally:
        await adapter.close()


async def test_resolved_unknown_close_recovers_and_runtime_resumes_without_operator(
    settings, store, filters, monkeypatch
):
    import paperfrog.simple.service as service_module

    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        sim.unknown_order = True
        with pytest.raises(UnknownOrder):
            await engine.close("SOLUSDT", now, "profit exit")
        assert store.get("state") == State.ERROR_SAFE
        assert store.get("simple_closing") and sim.quantity == 0
        before = sum(method == "POST" for method, _ in sim.calls)
        service = SimpleService(engine.s)
        service.store.engine.dispose()
        await service.exchange.close()
        service.store, service.engine, service.exchange = store, engine, adapter
        service.rows, service.selected, service.crisis = {"SOLUSDT": {**obs, "flow": 0}}, ["SOLUSDT"], False
        service.auto_start = False

        async def fetch_quote(symbol):
            return replace(engine.broker.quotes[symbol], timestamp_ms=service.now_ms())

        async def sleep(seconds):
            service.stop.set()

        monkeypatch.setattr(service, "fetch_quote", fetch_quote)
        monkeypatch.setattr(service_module.asyncio, "sleep", sleep)
        await service.monitor()
        assert store.get("state") == State.RUNNING
        assert not store.get("simple_closing") and not store.unresolved_orders()
        assert not engine.positions() and not store.get("live_stops")
        assert sum(method == "POST" for method, _ in sim.calls) == before
        assert len(store.get("simple_closed")) == 1
        assert sum(r["kind"] == "simple_execution_recovered" for r in store.rows("audit")) == 1
        assert not any(r["kind"] == "operator_risk_reset" for r in store.rows("audit"))
    finally:
        await adapter.close()


async def test_unknown_result_keeps_gate_closed_until_original_order_resolves(settings, store, filters):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        sim.unknown_order = True
        with pytest.raises(UnknownOrder):
            await engine.close("SOLUSDT", now, "test exit")
        before = sum(method == "POST" for method, _ in sim.calls)
        sim.order_lookup_misses = 3
        with pytest.raises(SafeError, match="unresolved startup execution"):
            await engine.recover_execution()
        assert store.get("state") == State.ERROR_SAFE
        assert store.get("simple_closing") and store.unresolved_orders()
        calls = list(sim.calls)
        assert await engine.recover_execution() is False
        assert sim.calls == calls  # Backoff avoids hammering the exchange.
        store.put("simple_execution_recovery_retry_ms", 0)
        assert await engine.recover_execution() is True
        assert store.get("state") == State.DEGRADED
        assert not store.get("simple_closing") and not store.unresolved_orders()
        assert sum(method == "POST" for method, _ in sim.calls) == before
    finally:
        await adapter.close()


@pytest.mark.parametrize("lock", ["manual", "risk", "none"])
async def test_execution_recovery_preserves_financial_anchors_losses_and_locks(
    settings, store, filters, lock
):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        store.put("consecutive_losses", 2)
        store.put("loss_cooldown_step", 3)
        store.put("cooldown:OTHERUSDT", now / 1000 + 100)
        if lock == "manual":
            store.put("manual_lock", True)
        elif lock == "risk":
            store.put("risk_lock", {"state": State.HARD_RISK_LOCK, "until": None, "reason": "high_water"})
        keys = ["anchors", "risk_lock", "manual_lock", "consecutive_losses", "loss_cooldown_step", "cooldown:OTHERUSDT"]
        before = {key: store.get(key) for key in keys}
        posts = sum(method == "POST" for method, _ in sim.calls)
        store.transition(State.ERROR_SAFE, "temporary execution confirmation failed")
        assert await engine.recover_execution() is True
        assert {key: store.get(key) for key in keys} == before
        assert store.get("state") == {"manual": State.PAUSED, "risk": State.HARD_RISK_LOCK, "none": State.DEGRADED}[lock]
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        assert Decimal(engine.positions()["SOLUSDT"]["quantity"]) == sim.quantity
        assert sum(method == "POST" for method, _ in sim.calls) == posts
    finally:
        await adapter.close()


@pytest.mark.parametrize("fault", ["missing_stop", "external_stop", "quantity_mismatch", "disarmed"])
async def test_unverified_protection_account_or_authorization_cannot_auto_resume(
    settings, store, filters, fault
):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        if fault == "missing_stop":
            sim.stops.clear()
        elif fault == "external_stop":
            sim.stops["external"] = {"clientAlgoId": "external", "algoStatus": "NEW"}
        elif fault == "quantity_mismatch":
            sim.quantity /= 2
        else:
            (directory(engine.s) / "live-arm.json").unlink()
        store.transition(State.ERROR_SAFE, "execution verification needed")
        before = sum(method == "POST" for method, _ in sim.calls)
        with pytest.raises(SafeError):
            await engine.recover_execution()
        assert store.get("state") == State.ERROR_SAFE
        assert sum(method == "POST" for method, _ in sim.calls) == before
        assert not store.get("simple_closed")
    finally:
        await adapter.close()


@pytest.mark.parametrize("fault", ["critical_task", "emergency_close"])
async def test_recovery_does_not_override_supervisor_or_emergency_close(settings, store, filters, fault):
    engine, sim, adapter, _, _ = await setup_engine(settings, store, filters)
    try:
        store.transition(State.ERROR_SAFE, "trade monitoring stopped (RuntimeError); restart required" if fault == "critical_task" else "execution fault")
        if fault == "emergency_close":
            store.put("local_flatten_request", "operation")
        before = list(sim.calls)
        assert await engine.recover_execution() is False
        assert sim.calls == before
        assert store.get("state") == State.ERROR_SAFE
    finally:
        await adapter.close()


async def test_already_reconciled_close_does_not_leave_stale_execution_latch(settings, store, filters):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", obs, now)
        await engine.close("SOLUSDT", now, "confirmed close")
        assert not store.get("simple_closing") and not store.unresolved_orders()
        assert not engine.positions() and sim.quantity == 0
        store.transition(State.ERROR_SAFE, "order not found; absence does not authorize resubmission")
        before = sum(method == "POST" for method, _ in sim.calls)
        closed = store.get("simple_closed")
        assert await engine.recover_execution() is True
        assert store.get("state") == State.DEGRADED
        assert store.get("simple_closed") == closed
        assert sum(method == "POST" for method, _ in sim.calls) == before
        assert await engine.recover_execution() is False
    finally:
        await adapter.close()
