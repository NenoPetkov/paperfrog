import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_simple_live import telegram_resume_fixture

from paperfrog.domain.types import SafeError, State
from paperfrog.simple.authorization import directory


@pytest.mark.parametrize("cached", ["missing", "stale_after_recovery"])
async def test_resume_refreshes_market_quotes_after_recovery(settings, store, filters, cached):
    service, sim, adapter, controls, update = await telegram_resume_fixture(
        settings, store, filters
    )
    engine = service.engine
    restore = engine.restore

    async def recovery():
        await restore()
        if cached == "missing":
            engine.broker.quotes.clear()
        else:
            engine.broker.quotes["SOLUSDT"] = replace(
                engine.broker.quotes["SOLUSDT"],
                timestamp_ms=engine.broker.now_ms()
                - int(service.s.market_stale_seconds * 1000)
                - 1,
            )

    engine.restore = recovery
    try:
        response = await controls.handle(update)
        assert "Trading is running" in response[1]
        assert any(path.endswith("/bookTicker") for _, path in sim.calls)
        assert not any(method == "POST" and path.endswith("/order") for method, path in sim.calls)
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "blocker", ["empty_scan", "expired_scan", "crisis", "stale_quote", "unavailable_quote"]
)
async def test_refuse_force_overrides_resume_market_checks_only(settings, store, filters, blocker):
    service, sim, adapter, controls, update = await telegram_resume_fixture(
        settings, store, filters
    )
    update["message"]["text"] = "/refuse_force"
    arm_path = directory(service.s) / "live-arm.json"
    original_arm = arm_path.read_bytes()
    if blocker == "empty_scan":
        service.selected = []
        store.put("simple_selected", [])
        service.engine.broker.quotes.clear()
    elif blocker == "expired_scan":
        service.rows["SOLUSDT"]["timestamp_ms"] = service.now_ms() - service.s.simple_entry_bar_ms
    elif blocker == "crisis":
        service.crisis = True
    elif blocker == "stale_quote":
        sim.market_time = service.now_ms() - int(service.s.market_stale_seconds * 1000) - 1
    elif blocker == "unavailable_quote":
        sim.market_unavailable = True
    try:
        response = await controls.handle(update)
        assert "checks overridden" in response[1]
        assert store.get("state") == State.RUNNING
        assert not store.get("manual_lock") and not store.get("risk_lock")
        assert arm_path.read_bytes() == original_arm
        resets = [
            json.loads(r["payload"])
            for r in store.rows("audit")
            if r["kind"] == "operator_risk_reset"
        ]
        assert resets[-1]["resume_market_checks_overridden"] is True
        if blocker in {"empty_scan", "stale_quote", "unavailable_quote"}:
            with pytest.raises(SafeError, match="market quote"):
                service.engine.risk.require_health(service.engine.health(service.now_ms()))
        if blocker in {"expired_scan", "stale_quote", "unavailable_quote"}:
            with pytest.raises(SafeError):
                await service.engine.enter("SOLUSDT", service.rows["SOLUSDT"], service.now_ms())
        assert not any(method == "POST" and path.endswith("/order") for method, path in sim.calls)
        # An update replay must not reset baselines a second time.
        assert await controls.handle(update) is None
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "failure", ["arm", "mismatch", "pending_close", "invalid_equity", "missing_engine"]
)
async def test_refuse_force_retains_execution_gates(settings, store, filters, failure):
    service, sim, adapter, controls, update = await telegram_resume_fixture(
        settings, store, filters
    )
    update["message"]["text"] = "/refuse_force"
    if failure == "arm":
        (directory(service.s) / "live-arm.json").unlink()
    elif failure == "mismatch":
        sim.quantity = Decimal("1")
    elif failure == "pending_close":
        store.put("local_flatten_request", "test-close")
    elif failure == "invalid_equity":
        sim.equity = "0"
    elif failure == "missing_engine":
        service.engine = None
    before = {
        k: store.get(k) for k in ("anchors", "risk_lock", "manual_lock", "consecutive_losses")
    }
    try:
        response = await controls.handle(update)
        assert response[1].startswith("Refused:")
        assert {k: store.get(k) for k in before} == before
        assert not any(row["kind"] == "operator_risk_reset" for row in store.rows("audit"))
        assert not any(method == "POST" and path.endswith("/order") for method, path in sim.calls)
    finally:
        await adapter.close()


async def test_normal_resume_rechecks_candle_after_recovery(settings, store, filters):
    service, sim, adapter, controls, update = await telegram_resume_fixture(
        settings, store, filters
    )
    original = service.engine.restore

    async def recovery():
        await original()
        service.rows["SOLUSDT"]["timestamp_ms"] = service.now_ms() - service.s.simple_entry_bar_ms

    service.engine.restore = recovery
    before = {k: store.get(k) for k in ("anchors", "risk_lock", "manual_lock")}
    try:
        response = await controls.handle(update)
        assert "closed-candle scan" in response[1]
        assert {k: store.get(k) for k in before} == before
    finally:
        await adapter.close()


async def test_health_requires_a_quote_for_every_selected_symbol(settings, store, filters):
    service, sim, adapter, controls, update = await telegram_resume_fixture(
        settings, store, filters
    )
    store.put("simple_selected", ["SOLUSDT", "ETHUSDT"])
    try:
        with pytest.raises(SafeError, match="market quote"):
            service.engine.risk.require_health(service.engine.health(service.now_ms()))
    finally:
        await adapter.close()
