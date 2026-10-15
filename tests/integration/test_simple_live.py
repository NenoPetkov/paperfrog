import time
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from paperfrog.domain.types import Quote, SafeError, State, UnknownOrder
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.settings import Settings
from paperfrog.simple.authorization import arm_simple, directory, verify_simple_arm
from paperfrog.simple.data import scan
from paperfrog.simple.engine import Engine
from paperfrog.simple.live_broker import LiveBroker
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS, require_execution


def live_settings(settings, **changes):
    values = {
        **settings.public(),
        "trading_mode": "live",
        "binance_env": "live",
        "live_trading_enabled": True,
        "i_understand_live_risk": "YES",
        "simple_live_research_waiver": True,
        "futures_account_mode": "credits",
        "margin_type": "CROSSED",
        "binance_api_key": "mock-account-key",
        "binance_api_secret": "mock-signing-secret",
        **changes,
    }
    return Settings(_env_file=None, **values)


class ExchangeSimulation:
    def __init__(self):
        self.calls = []
        self.quantity = Decimal(0)
        self.orders = {}
        self.stops = {}
        self.leverage = 2
        self.multi = True
        self.dual = False
        self.equity = "500"
        self.available = None
        self.partial = False
        self.unknown_order = False
        self.reject_stop = False
        self.unknown_stop = False
        self.income = []

    def order_result(self, key, side, qty, price, status="FILLED"):
        result = {
            "clientOrderId": key,
            "symbol": "SOLUSDT",
            "side": side,
            "executedQty": str(qty),
            "avgPrice": str(price),
            "status": status,
            "orderId": len(self.orders) + 100,
        }
        self.orders[key] = result
        self.quantity += qty * (1 if side == "BUY" else -1)
        return result

    def fire_stop(self):
        key, stop = next(iter(self.stops.items()))
        result = self.order_result(key + "-actual", "SELL", self.quantity, Decimal("98"))
        stop.update(algoStatus="FINISHED", actualOrderId=str(result["orderId"]))

    def handle(self, request):
        path, method, params = request.url.path, request.method, request.url.params
        self.calls.append((method, path))
        result = None
        if path.endswith("/account"):
            result = {
                "totalMarginBalance": self.equity,
                "availableBalance": self.available if self.available is not None else self.equity,
                "assets": [
                    {"asset": "BNFCR", "availableBalance": self.equity},
                    {"asset": "USDC", "walletBalance": "500"},
                ],
            }
        elif path.endswith("/accountConfig"):
            result = {"canTrade": True}
        elif path.endswith("/positionSide/dual"):
            result = {"dualSidePosition": self.dual}
        elif path.endswith("/multiAssetsMargin"):
            result = {"multiAssetsMargin": self.multi}
        elif path.endswith("/symbolConfig"):
            result = [{"symbol": "SOLUSDT", "marginType": "CROSSED", "leverage": self.leverage}]
        elif path.endswith("/leverage"):
            self.leverage = int(params["leverage"])
            result = {"symbol": "SOLUSDT", "leverage": self.leverage}
        elif path.endswith("/leverageBracket"):
            result = [
                {
                    "brackets": [
                        {
                            "notionalFloor": 0,
                            "notionalCap": 1000000,
                            "initialLeverage": 20,
                            "maintMarginRatio": 0.005,
                        }
                    ]
                }
            ]
        elif path.endswith("/positionRisk"):
            result = [
                {
                    "symbol": "SOLUSDT",
                    "positionAmt": str(self.quantity),
                    "positionSide": "BOTH",
                    "markPrice": "100",
                    "liquidationPrice": "0",
                }
            ]
        elif path.endswith("/openOrders"):
            result = []
        elif path.endswith("/openAlgoOrders"):
            result = [s for s in self.stops.values() if s["algoStatus"] == "NEW"]
        elif path.endswith("/income"):
            result = self.income
        elif path.endswith("/order") and method == "POST":
            qty = Decimal(params["quantity"])
            if params["reduceOnly"] == "true":
                qty = min(qty, abs(self.quantity))
            elif self.partial:
                qty = (qty / 2 // Decimal(".001")) * Decimal(".001")
            result = self.order_result(
                params["newClientOrderId"],
                params["side"],
                qty,
                Decimal(params["price"]),
                "EXPIRED" if self.partial and params["reduceOnly"] == "false" else "FILLED",
            )
            if self.unknown_order:
                self.unknown_order = False
                return httpx.Response(503, json={"code": -1000})
        elif path.endswith("/order") and method == "GET":
            result = self.orders.get(params.get("origClientOrderId"))
            if "orderId" in params:
                result = next(
                    (r for r in self.orders.values() if str(r["orderId"]) == params["orderId"]),
                    None,
                )
        elif path.endswith("/algoOrder") and method == "POST":
            if self.reject_stop:
                return httpx.Response(400, json={"code": -2021})
            key = params["clientAlgoId"]
            result = {
                "clientAlgoId": key,
                "algoId": len(self.stops) + 1,
                "symbol": params["symbol"],
                "side": params["side"],
                "positionSide": "BOTH",
                "orderType": "STOP_MARKET",
                "closePosition": True,
                "workingType": "MARK_PRICE",
                "triggerPrice": params["triggerPrice"],
                "algoStatus": "NEW",
                "actualOrderId": "",
            }
            self.stops[key] = result
            if self.unknown_stop:
                return httpx.Response(503, json={"code": -1000})
        elif path.endswith("/algoOrder") and method == "GET":
            result = self.stops.get(params["clientAlgoId"])
        elif path.endswith("/algoOrder") and method == "DELETE":
            for stop in self.stops.values():
                if str(stop["algoId"]) == params["algoId"]:
                    stop["algoStatus"] = "CANCELED"
            result = {"code": 200}
        if result is None:
            return httpx.Response(400, json={"code": -2013})
        return httpx.Response(200, json=result)


async def setup_engine(settings, store, filters, **changes):
    s = live_settings(settings, **changes)
    arm_simple(s, "ARM LIVE TRADING")
    simulation = ExchangeSimulation()
    adapter = BinanceAdapter(s, httpx.AsyncClient(transport=httpx.MockTransport(simulation.handle)))
    adapter.filters = filters
    broker = LiveBroker(s, store, adapter)
    await broker.preflight()
    engine = Engine(s, store, filters, broker=broker)
    await engine.restore()
    now = int(time.time() * 1000)
    store.put("simple_selected", ["SOLUSDT"])
    await engine.quote(
        Quote(
            "SOLUSDT",
            Decimal("99.95"),
            Decimal("100.05"),
            Decimal(100000),
            Decimal(100000),
            Decimal(100),
            now,
        ),
        now,
    )
    await engine.resume(now)
    row = {
        "timestamp_ms": now // BAR_MS * BAR_MS,
        "close": 100,
        "trend_atr": 2,
        "prior_high": 99,
        "prior_low": 98,
        "atr": 0.8,
        "atr_fraction": 0.008,
        "flow": 0.4,
        "daily_volume": 100000000,
        "score": 10,
    }
    return engine, simulation, adapter, row, now


def test_waiver_preserves_paper_default_and_environment_authorization(settings):
    assert settings.trading_mode == "paper" and not settings.simple_live_research_waiver
    with pytest.raises(SafeError, match="no live approval"):
        require_execution(live_settings(settings, simple_live_research_waiver=False))
    with pytest.raises(ValidationError):
        live_settings(settings, live_trading_enabled=False)
    service = SimpleService(live_settings(settings))
    assert service.directory == directory(service.s)
    assert service.directory != settings.state_dir / "simple"
    service.store.engine.dispose()


def test_local_waiver_is_bound_to_account_config_and_strategy(settings):
    s = live_settings(settings)
    with pytest.raises(SafeError, match="arm missing"):
        verify_simple_arm(s)
    arm_simple(s, "ARM LIVE TRADING")
    assert verify_simple_arm(s)
    path = directory(s) / "live-arm.json"
    assert path.stat().st_mode & 0o077 == 0
    content = path.read_text()
    assert "mock-account-key" not in content and "mock-signing-secret" not in content
    assert '"profitability": "unproven"' in content
    for changes in (
        {"binance_api_key": "different"},
        {"simple_entry_mode": "reverse"},
        {"simple_entry_profile": "active"},
    ):
        with pytest.raises(SafeError, match="authorization changed"):
            verify_simple_arm(live_settings(settings, **changes))
    path.unlink()
    with pytest.raises(SafeError):
        verify_simple_arm(s)


async def test_bnfcr_aggregate_does_not_double_count_collateral(settings, store, filters):
    engine, sim, adapter, _, _ = await setup_engine(settings, store, filters)
    assert engine.broker.equity() == 500
    assert all(method == "GET" for method, _ in sim.calls)
    sim.equity = "NaN"
    with pytest.raises(SafeError, match="invalid exchange account"):
        await engine.broker.refresh()
    engine.broker.account_at_ms = 0
    with pytest.raises(SafeError, match="stale"):
        engine.broker.equity()
    await adapter.close()


@pytest.mark.parametrize("available", ["500", "100"])
async def test_margin_sizing_places_larger_entry_within_eighty_percent(
    settings, store, filters, available
):
    engine, sim, adapter, row, now = await setup_engine(
        settings,
        store,
        filters,
        simple_sizing_mode="margin",
        risk_per_trade=0.04,
        max_open_risk=0.05,
        max_target_notional=1.6,
        max_gross_notional=2.0,
        max_net_beta=2.0,
    )
    sim.available = available
    await engine.broker.refresh()
    now = engine.broker.now_ms()
    await engine.enter("SOLUSDT", row, now)
    trade = engine.positions()["SOLUSDT"]
    notional = Decimal(trade["quantity"]) * Decimal(str(trade["entry_price"]))
    ceiling = min(Decimal("800"), Decimal(available) / Decimal("0.5005"))
    assert ceiling - Decimal("1") < notional <= ceiling
    assert notional / 2 <= Decimal(sim.equity) * Decimal("0.8")
    assert len(sim.stops) == 1
    assert store.rows("entries")[0]["risk"] <= 500 * 0.04
    assert engine.risk.profit_guard_scale == 1
    await adapter.close()


async def test_margin_broker_rechecks_remaining_budget_before_submission(settings, store, filters):
    from paperfrog.domain.types import Order

    engine, sim, adapter, _, _ = await setup_engine(
        settings, store, filters, simple_sizing_mode="margin"
    )
    order = Order(
        client_id="pf-margin-budget-test",
        symbol="SOLUSDT",
        side=1,
        quantity=Decimal("5"),
        limit_price=Decimal("100"),
    )
    sim.quantity = Decimal("6")
    with pytest.raises(SafeError, match="total margin cap"):
        await engine.broker.submit(order)
    assert not any(method == "POST" for method, _ in sim.calls)
    sim.quantity = Decimal(0)
    with pytest.raises(SafeError, match="position margin cap"):
        await engine.broker.submit(order.model_copy(update={"quantity": Decimal("9")}))
    assert not any(method == "POST" for method, _ in sim.calls)
    sim.available = "100"
    with pytest.raises(SafeError, match="estimated entry fee"):
        await engine.broker.submit(order.model_copy(update={"quantity": Decimal("2")}))
    assert not any(method == "POST" for method, _ in sim.calls)
    await adapter.close()


@pytest.mark.parametrize("attribute", ["multi", "dual"])
async def test_wrong_exchange_mode_still_blocks(settings, store, filters, attribute):
    engine, sim, adapter, _, _ = await setup_engine(settings, store, filters)
    setattr(sim, attribute, attribute == "dual")
    with pytest.raises(SafeError, match="configured asset mode"):
        await engine.broker.preflight()
    await adapter.close()


@pytest.mark.parametrize("profile", ["standard", "active"])
async def test_live_entry_is_bounded_protected_and_not_duplicated(
    settings, store, filters, profile
):
    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, simple_entry_profile=profile
    )
    if profile == "active":
        row.update(trend_atr=0.45, flow=0.04)
    await engine.enter("SOLUSDT", row, now)
    await engine.enter("SOLUSDT", row, now)
    assert sim.quantity > 0
    assert len(store.rows("entries")) == 1
    assert store.rows("entries")[0]["risk"] <= 500 * settings.risk_per_trade
    assert store.rows("entries")[0]["gross"] <= 500 * settings.max_target_notional
    assert len(sim.stops) == 1
    assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
    await adapter.close()


async def test_active_live_rules_still_reject_unapproved_experiments(settings, store, filters):
    from dataclasses import replace

    engine, _, adapter, _, _ = await setup_engine(
        settings, store, filters, simple_entry_profile="active"
    )
    with pytest.raises(SafeError, match="current default rules"):
        Engine(
            engine.s,
            store,
            filters,
            replace(engine.rules, minimum_flow=0),
            broker=engine.broker,
        )
    await adapter.close()


async def test_restart_settles_confirmed_hosted_stop(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)
    sim.fire_stop()
    restored = Engine(engine.s, store, filters, broker=engine.broker)
    await restored.restore()
    assert not restored.positions() and await restored.coordinator.reconcile()
    assert store.rows("entries")[0]["closed"] == 1
    assert len(store.get("simple_closed")) == 1
    await restored.restore()
    assert len(store.get("simple_closed")) == 1
    await adapter.close()


async def test_unknown_entry_response_queries_without_resubmission(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    sim.unknown_order = True
    with pytest.raises(UnknownOrder):
        await engine.enter("SOLUSDT", row, now)
    assert store.get("state") == State.ERROR_SAFE
    assert await engine.coordinator.reconcile()
    assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
    await adapter.close()


@pytest.mark.parametrize("source", ["cumQuote", "query", "unavailable"])
async def test_zero_execution_price_uses_confirmed_data_without_resubmission(
    settings, store, filters, source
):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)

    def handle(request):
        response = sim.handle(request)
        if request.url.path.endswith("/order"):
            result = response.json()
            if request.method == "POST" or source == "unavailable":
                if source == "cumQuote":
                    result["cumQuote"] = str(
                        Decimal(result["executedQty"]) * Decimal(result["avgPrice"])
                    )
                result["avgPrice"] = "0"
                return httpx.Response(200, json=result)
        return response

    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    if source == "unavailable":
        with pytest.raises(UnknownOrder, match="execution price unavailable"):
            await engine.enter("SOLUSDT", row, now)
        assert store.get("state") == State.ERROR_SAFE
        assert not store.get("reconciled")
        assert store.get("simple_pending")
        assert not engine.positions()
        assert not sim.stops
        assert len(store.unresolved_orders()) == 1
        assert not store.rows("fills")
        assert not await engine.coordinator.reconcile()
    else:
        await engine.enter("SOLUSDT", row, now)
        assert engine.positions()["SOLUSDT"]["entry_price"] > 0
        assert len(sim.stops) == 1
        assert Decimal(next(iter(sim.stops.values()))["triggerPrice"]) > 0
        assert not store.unresolved_orders()
    assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
    if source == "query":
        assert sum(method == "GET" and path.endswith("/order") for method, path in sim.calls) == 1
    await adapter.close()


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-1", "invalid", None])
async def test_invalid_execution_price_fails_with_safe_error(settings, price):
    adapter = BinanceAdapter(settings)
    with pytest.raises(UnknownOrder, match="invalid execution pricing"):
        adapter._fill({"executedQty": "1", "avgPrice": price})
    await adapter.close()


async def test_zero_quantity_expiry_does_not_require_execution_price(settings):
    adapter = BinanceAdapter(settings)
    fill = adapter._fill(
        {
            "clientOrderId": "unfilled",
            "symbol": "SOLUSDT",
            "side": "BUY",
            "executedQty": "0",
            "avgPrice": "0",
            "status": "EXPIRED",
            "orderId": 1,
        }
    )
    assert fill.quantity == 0 and fill.price == 0 and fill.fee == 0
    await adapter.close()


@pytest.mark.parametrize("failure", ["partial", "reject_stop", "unknown_stop"])
async def test_partial_entry_and_protection_failure_unwind(settings, store, filters, failure):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    setattr(sim, failure, True)
    with pytest.raises(SafeError):
        await engine.enter("SOLUSDT", row, now)
    assert sim.quantity == 0
    assert store.rows("entries")[0]["closed"] == 1
    assert not store.get("simple_pending")
    assert not [stop for stop in sim.stops.values() if stop["algoStatus"] == "NEW"]
    await adapter.close()


async def test_tightening_keeps_hosted_initial_protection(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)
    stop = next(iter(sim.stops.values())).copy()
    await engine.broker.protect("SOLUSDT", 1, Decimal("101"), stop["clientAlgoId"])
    assert len(sim.stops) == 1 and next(iter(sim.stops.values())) == stop
    assert not [call for call in sim.calls if call[0] == "DELETE"]
    await adapter.close()


async def test_disarming_blocks_new_entries_but_allows_reduction(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)
    (directory(engine.s) / "live-arm.json").unlink()
    with pytest.raises(SafeError):
        engine.broker.require_authorization()
    await engine.close("SOLUSDT", now, "operator disarmed")
    assert sim.quantity == 0 and not engine.positions()
    await adapter.close()


async def test_live_funding_is_authoritative_and_deduplicated(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)
    sim.income = [{"tranId": 1, "time": now + 1000, "asset": "BNFCR", "income": "-0.03"}]
    await engine.broker.refresh_funding(now + 60000)
    await engine.broker.refresh_funding(now + 120000)
    assert engine.positions()["SOLUSDT"]["funding"] == pytest.approx(-0.03)
    assert engine.broker.equity() == 500
    await adapter.close()


async def test_completed_entry_journal_recovers_without_another_order(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)
    trade = engine.positions()["SOLUSDT"]
    store.put(
        "simple_pending",
        {"SOLUSDT": {"id": trade["id"], "side": 1, "opened_ms": now, "stop_fraction": 0.016}},
    )
    store.put("simple_positions", {})
    restored = Engine(engine.s, store, filters, broker=engine.broker)
    await restored.restore()
    assert restored.positions()["SOLUSDT"]["quantity"] == trade["quantity"]
    assert not store.get("simple_pending")
    assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
    assert len(sim.stops) == 1
    await adapter.close()


@pytest.mark.parametrize("complete", [False, True])
async def test_restart_does_not_adopt_zero_price_journal(settings, store, filters, complete):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)
    trade = engine.positions()["SOLUSDT"]
    store.put(
        "simple_pending",
        {"SOLUSDT": {"id": trade["id"], "side": 1, "opened_ms": now, "stop_fraction": 0.016}},
    )
    store.put("simple_positions", {})
    journal = store.get("trade:" + trade["id"])
    journal["execution_complete"] = complete
    journal["fills"][0]["price"] = "0"
    store.put("trade:" + trade["id"], journal)
    store.put("state", State.PAUSED)
    calls_before = list(sim.calls)
    restored = Engine(engine.s, store, filters, broker=engine.broker)
    with pytest.raises(SafeError, match="account remains paused"):
        await restored.restore()
    assert not restored.positions()
    assert store.get("simple_pending")
    assert store.get("state") == State.PAUSED
    assert sim.calls == calls_before
    await adapter.close()


async def test_confirmed_close_recovers_after_process_crash(settings, store, filters, monkeypatch):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)

    def crash(*args, **kwargs):
        raise RuntimeError("simulated power loss before close journal commit")

    monkeypatch.setattr(engine, "finish", crash)
    with pytest.raises(RuntimeError):
        await engine.close("SOLUSDT", now, "test exit")
    assert sim.quantity == 0 and store.get("simple_closing")
    restored = Engine(engine.s, store, filters, broker=engine.broker)
    await restored.restore()
    assert not restored.positions() and not store.get("simple_closing")
    assert await restored.coordinator.reconcile()
    assert len(store.get("simple_closed")) == 1
    assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 2
    await adapter.close()


async def test_unresolved_simple_journal_blocks_runtime_resume_and_monitor(
    settings, store, filters
):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    sim.unknown_order = True
    with pytest.raises(UnknownOrder):
        await engine.enter("SOLUSDT", row, now)
    with pytest.raises(SafeError, match="journal incomplete"):
        await engine.monitor(now, {"SOLUSDT": row})
    with pytest.raises(SafeError, match="journal incomplete"):
        await engine.resume(now)
    assert store.get("state") != State.RUNNING
    await adapter.close()


async def test_authorized_live_scanner_reads_public_market_data(settings, filters):
    s = live_settings(settings)
    called = []
    now = int(time.time() * 1000)

    def handle(request):
        called.append(request)
        assert request.method == "GET"
        assert "X-MBX-APIKEY" not in request.headers and "signature" not in request.url.params
        if request.url.path.endswith("/exchangeInfo"):
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "SOLUSDT",
                            "status": "TRADING",
                            "contractType": "PERPETUAL",
                            "quoteAsset": "USDT",
                            "baseAsset": "SOL",
                            "underlyingType": "COIN",
                            "onboardDate": now - 5 * 86400000,
                            "filters": [
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.001",
                                    "minQty": "0.001",
                                    "maxQty": "1000000",
                                },
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json=[{"symbol": "SOLUSDT", "quoteVolume": "100000000", "closeTime": now}]
        )

    adapter = BinanceAdapter(s, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    snapshot = await scan(adapter, s)
    assert snapshot["symbols"] == ["SOLUSDT"] and len(called) == 2
    await adapter.close()
