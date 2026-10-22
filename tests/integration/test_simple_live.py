import json
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
from paperfrog.telegram.control import Controls


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
        self.reduce_fraction = Decimal(1)
        self.unknown_order = False
        self.order_lookup_misses = 0
        self.reject_stop = False
        self.unknown_stop = False
        self.stop_lookup_misses = 0
        self.income = []
        self.bid = "99.95"
        self.ask = "100.05"
        self.bid_size = "100000"
        self.ask_size = "100000"
        self.market_time = None
        self.market_unavailable = False

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
        if path.endswith(("/bookTicker", "/premiumIndex")):
            if self.market_unavailable:
                return httpx.Response(503, json={"code": -1000})
            now = int(time.time() * 1000) if self.market_time is None else self.market_time
            result = (
                {
                    "bidPrice": self.bid,
                    "askPrice": self.ask,
                    "bidQty": self.bid_size,
                    "askQty": self.ask_size,
                    "time": now,
                }
                if path.endswith("/bookTicker")
                else {"markPrice": str((Decimal(self.bid) + Decimal(self.ask)) / 2), "time": now}
            )
        elif path.endswith("/account"):
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
                qty = (qty * self.reduce_fraction // Decimal(".001")) * Decimal(".001")
            elif self.partial:
                qty = (qty / 2 // Decimal(".001")) * Decimal(".001")
            result = self.order_result(
                params["newClientOrderId"],
                params["side"],
                qty,
                Decimal(params["price"]),
                "EXPIRED"
                if (self.partial and params["reduceOnly"] == "false")
                or (params["reduceOnly"] == "true" and self.reduce_fraction < 1)
                else "FILLED",
            )
            if self.unknown_order:
                self.unknown_order = False
                return httpx.Response(503, json={"code": -1000})
        elif path.endswith("/order") and method == "GET":
            if self.order_lookup_misses:
                self.order_lookup_misses -= 1
                return httpx.Response(400, json={"code": -2013})
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
            if self.stop_lookup_misses:
                self.stop_lookup_misses -= 1
                return httpx.Response(400, json={"code": -2013})
            result = self.stops.get(params.get("clientAlgoId"))
            if "algoId" in params:
                result = next(
                    (s for s in self.stops.values() if str(s["algoId"]) == params["algoId"]), None
                )
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
        {"simple_btc_direction_policy": "neutral_allowed"},
        {"simple_sizing_mode": "equity_range", "simple_position_margin_cap": 0.5},
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


def equity_range_options(**changes):
    return {
        "simple_sizing_mode": "equity_range",
        "simple_position_margin_cap": 0.5,
        "risk_per_trade": 0.04,
        "max_open_risk": 0.05,
        "max_target_notional": 1.6,
        "max_gross_notional": 2.0,
        "max_net_beta": 2.0,
        **changes,
    }


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("available", ["500", "150", "20"])
async def test_equity_range_live_sizes_without_top_quote_participation_cap(
    settings, store, filters, side, available
):
    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, **equity_range_options()
    )
    sim.available = available
    sim.bid_size = sim.ask_size = "10"
    if side < 0:
        row.update(trend_atr=-2, flow=-0.4, prior_low=101)
    try:
        await engine.enter("SOLUSDT", row, now)
        trade = engine.positions()["SOLUSDT"]
        margin = Decimal(trade["quantity"]) * Decimal(str(trade["entry_price"])) / 2
        assert trade["side"] == side and margin <= 250
        if available == "20":
            assert Decimal("19.75") < margin < 20
        else:
            assert margin >= 50
            expected = min(250, Decimal(available) / Decimal("1.001"))
            assert expected - 1 < margin <= expected
        assert Decimal(trade["quantity"]) / 10 > Decimal(".01")
        allocation = next(
            json.loads(item["payload"])
            for item in store.rows("audit")
            if item["kind"] == "simple_equity_allocation"
        )
        assert allocation["remaining_balance"] == (available == "20")
        assert "liquidity" not in allocation["caps"]
        assert store.rows("entries")[0]["risk"] <= 20
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        # Exits are independent of sizing and remain available after disarming.
        (directory(engine.s) / "live-arm.json").unlink()
        await engine.close("SOLUSDT", engine.broker.now_ms(), "sizing does not restrict reductions")
        assert not sim.quantity and await engine.coordinator.reconcile()
    finally:
        await adapter.close()


async def test_equity_range_does_not_make_a_subminimum_trade_when_risk_is_tight(
    settings, store, filters
):
    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, **equity_range_options(risk_per_trade=0.001)
    )
    try:
        await engine.enter("SOLUSDT", row, now)
        assert not sim.quantity and not store.rows("orders") and not store.rows("entries")
        assert any(
            "10% minimum" in item["payload"]
            for item in store.rows("audit")
            if item["kind"] == "simple_entry_skipped"
        )
    finally:
        await adapter.close()


@pytest.mark.parametrize("quantity", ["0.5", "5.1"])
async def test_equity_range_gateway_rejects_bypass_of_floor_and_ceiling(
    settings, store, filters, quantity
):
    from paperfrog.domain.types import EntryVeto, Order

    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, **equity_range_options()
    )
    try:
        order = Order(
            client_id="pf-equity-bypass",
            symbol="SOLUSDT",
            side=1,
            quantity=Decimal(quantity),
            limit_price=Decimal("100"),
        )
        with pytest.raises(EntryVeto):
            await engine.broker.submit(order)
        assert not sim.quantity and not any(
            method == "POST" and path.endswith("/order") for method, path in sim.calls
        )
    finally:
        await adapter.close()


async def test_equity_range_rechecks_cash_before_submission_and_releases_local_veto(
    settings, store, filters
):
    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, **equity_range_options()
    )
    original = engine.broker.submit

    async def changed_funds(order):
        sim.available = "20"
        return await original(order)

    engine.broker.submit = changed_funds
    try:
        with pytest.raises(SafeError, match="available margin"):
            await engine.enter("SOLUSDT", row, now)
        assert not sim.quantity and not store.rows("entries")
        assert not store.unresolved_orders() and not store.get("simple_pending")
        assert store.rows("orders")[0]["state"] == "REJECTED"
        assert store.get("state") == State.RUNNING and store.get("reconciled") is True
    finally:
        await adapter.close()


async def test_equity_range_paper_positions_fit_the_remaining_open_risk_budget(
    settings, store, filters
):
    from dataclasses import replace

    from test_simple import engine_fixture, quote, row

    s = Settings(_env_file=None, **{**settings.public(), **equity_range_options()})
    engine = await engine_fixture(s, store, filters)
    now = 200 * BAR_MS
    store.put("simple_selected", ["SOLUSDT", "ETHUSDT"])
    for symbol in ("SOLUSDT", "ETHUSDT"):
        await engine.quote(replace(quote(symbol), bid=Decimal("99.9"), ask=Decimal("100.1")), now)
    obs = {**row(), "atr": 1.2, "atr_fraction": 0.012}
    await engine.enter("SOLUSDT", obs, now)
    await engine.enter("ETHUSDT", obs, now)
    entries = store.rows("entries")
    assert len(engine.positions()) == 2
    allocations = [
        json.loads(item["payload"])
        for item in store.rows("audit")
        if item["kind"] == "simple_equity_allocation"
    ]
    assert (
        sum(item["risk"] for item in entries) <= allocations[0]["equity"] * s.max_open_risk + 1e-8
    )
    assert allocations[0]["limiting_cap"] == "open_risk"
    assert all(0.1 <= item["margin_fraction"] <= 0.5 for item in allocations)


@pytest.mark.parametrize("failure", ["partial", "unknown", "stop"])
async def test_equity_range_preserves_partial_unwind_and_uncertain_order_recovery(
    settings, store, filters, failure
):
    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, **equity_range_options()
    )
    if failure == "partial":
        sim.partial = True
    elif failure == "unknown":
        sim.unknown_order = True
    else:
        sim.reject_stop = True
    try:
        with pytest.raises(SafeError):
            await engine.enter("SOLUSDT", row, now)
        if failure == "unknown":
            assert store.unresolved_orders()
            assert await engine.coordinator.reconcile()
            assert (
                sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
            )
        else:
            assert not sim.quantity and not engine.positions()
            assert (
                sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 2
            )
    finally:
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


@pytest.mark.parametrize("side", [1, -1])
async def test_entry_refreshes_price_liquidity_and_margin_after_setup(
    settings, store, filters, side
):
    engine, sim, adapter, row, now = await setup_engine(
        settings, store, filters, simple_sizing_mode="margin"
    )
    if side < 0:
        row.update(trend_atr=-2, flow=-0.4, prior_low=101)
    original = adapter.prepare_simple_symbol

    async def prepare(symbol):
        await original(symbol)
        sim.bid, sim.ask = "149.95", "150.05"
        sim.bid_size = sim.ask_size = "100"
        sim.available = "15"

    adapter.prepare_simple_symbol = prepare
    try:
        await engine.enter("SOLUSDT", row, now)
        trade = engine.positions()["SOLUSDT"]
        fresh = engine.broker.quotes["SOLUSDT"]
        price = filters["SOLUSDT"].round_price(
            fresh.ask * Decimal("1.001") if side > 0 else fresh.bid * Decimal(".999")
        )
        expected = filters["SOLUSDT"].round_quantity(Decimal("15") / Decimal("0.5005") / price)
        assert trade["side"] == side and Decimal(str(trade["entry_price"])) == price
        assert Decimal(trade["quantity"]) == expected
        assert fresh.bid_size == fresh.ask_size == Decimal("100")
        book_index = sim.calls.index(("GET", "/fapi/v1/ticker/bookTicker"))
        assert any(path.endswith("/symbolConfig") for _, path in sim.calls[:book_index])
        assert sim.calls.index(("POST", "/fapi/v1/order")) > book_index
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        assert store.rows("entries")[0]["risk"] <= 500 * engine.s.risk_per_trade
        assert any(r["kind"] == "simple_entry_refreshed" for r in store.rows("audit"))
    finally:
        await adapter.close()


@pytest.mark.parametrize("cached_problem", ["wide", "missing", "stale"])
async def test_cached_quote_does_not_discard_a_still_valid_candle_setup(
    settings, store, filters, monkeypatch, cached_problem
):
    from dataclasses import replace

    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    cached = engine.broker.quotes["SOLUSDT"]
    if cached_problem == "wide":
        engine.broker.quotes["SOLUSDT"] = replace(cached, bid=Decimal("99"), ask=Decimal("101"))
    elif cached_problem == "missing":
        engine.broker.quotes.clear()
    else:
        engine.broker.quotes["SOLUSDT"] = replace(cached, timestamp_ms=0)
    # No shorter signal lifetime: this setup remains eligible 14 minutes into its candle.
    current_ms = row["timestamp_ms"] + 14 * 60000
    monkeypatch.setattr(time, "time", lambda: current_ms / 1000)
    try:
        await engine.enter("SOLUSDT", row, current_ms)
        assert sim.quantity > 0 and len(store.rows("entries")) == 1
        assert engine.broker.quotes["SOLUSDT"].timestamp_ms == current_ms
    finally:
        await adapter.close()


async def test_current_spread_veto_can_recover_without_consuming_the_signal(
    settings, store, filters
):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    sim.bid, sim.ask = "99", "101"
    try:
        with pytest.raises(SafeError, match="spread veto"):
            await engine.enter("SOLUSDT", row, now)
        assert not sim.quantity and not store.rows("orders") and not store.rows("entries")
        sim.bid, sim.ask = "99.95", "100.05"
        await engine.enter("SOLUSDT", row, engine.broker.now_ms())
        assert sim.quantity > 0 and len(store.rows("entries")) == 1
        assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
    finally:
        await adapter.close()


async def test_quote_refresh_failure_does_not_consume_or_submit_the_signal(
    settings, store, filters
):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    sim.market_unavailable = True
    try:
        with pytest.raises(SafeError):
            await engine.enter("SOLUSDT", row, now)
        assert not sim.quantity and not store.rows("orders") and not store.rows("entries")
        assert store.get("state") == State.RUNNING and store.get("reconciled") is True
        sim.market_unavailable = False
        await engine.enter("SOLUSDT", row, engine.broker.now_ms())
        assert sim.quantity > 0 and len(store.rows("entries")) == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize("age_ms", [61000, -5000])
async def test_refreshed_market_data_preserves_existing_freshness_limits(
    settings, store, filters, age_ms
):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    sim.market_time = now - age_ms
    try:
        with pytest.raises(SafeError, match="stale futures mark data"):
            await engine.enter("SOLUSDT", row, now)
        assert not sim.quantity and not store.rows("orders") and not store.rows("entries")
        assert store.get("state") == State.RUNNING
    finally:
        await adapter.close()


async def test_quote_refresh_is_only_for_eligible_unconsumed_entry_signals(
    settings, store, filters
):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", {**row, "flow": 0}, now)
        assert not any(path.endswith("/bookTicker") for _, path in sim.calls)
        await engine.enter("SOLUSDT", row, now)
        await engine.enter("SOLUSDT", row, engine.broker.now_ms())
        assert sum(path.endswith("/bookTicker") for _, path in sim.calls) == 1
        assert len(store.rows("entries")) == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize("age_ms", [61000, -5000])
async def test_exit_keeps_quote_age_limits_after_entry_refresh(settings, store, filters, age_ms):
    from dataclasses import replace

    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", row, now)
        quote = engine.broker.quotes["SOLUSDT"]
        engine.broker.quotes["SOLUSDT"] = replace(
            quote, timestamp_ms=engine.broker.now_ms() - age_ms
        )
        with pytest.raises(SafeError, match="fresh quote required for reduction"):
            await engine.close("SOLUSDT", now - 1000, "age checks still apply")
        assert sim.quantity > 0
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        assert len(store.rows("orders")) == 1
    finally:
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


@pytest.mark.parametrize(
    "condition", ["waiting", "timeout", "risk", "stop", "hold", "confirmed_crisis"]
)
async def test_initial_scan_wait_keeps_protection_without_fabricating_a_btc_crisis(
    settings, store, filters, monkeypatch, condition
):
    import paperfrog.simple.service as service_module

    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)
    await engine.restore()
    service = SimpleService(engine.s)
    service.store.engine.dispose()
    await service.exchange.close()
    service.store, service.engine, service.exchange = store, engine, adapter
    # A real restart begins with an empty in-memory scan and the default crisis flag.
    assert not service.rows and service.crisis and not service.scan_complete
    if condition == "timeout":
        service.initial_scan_deadline = time.monotonic() - 1
    elif condition == "risk":
        store.put(
            "risk_lock", {"state": State.DAILY_RISK_LOCK, "until": None, "reason": "daily_loss"}
        )
    elif condition == "stop":
        sim.bid, sim.ask = "97.95", "98.05"
    elif condition == "hold":
        positions = engine.positions()
        positions["SOLUSDT"]["opened_ms"] = now - engine.rules.maximum_hold_bars * BAR_MS - 1000
        store.put("simple_positions", positions)
    elif condition == "confirmed_crisis":
        service.rows, service.scan_complete = {"SOLUSDT": row}, True

    async def sleep(seconds):
        service.stop.set()

    monkeypatch.setattr(service_module.asyncio, "sleep", sleep)
    try:
        await service.monitor()
        if condition == "waiting":
            assert sim.quantity > 0 and engine.positions()
            assert store.get("state") == State.PAUSED
            assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
            assert len(store.rows("orders")) == 1
        else:
            assert not sim.quantity and not engine.positions()
            closed = store.get("simple_closed")[-1]
            expected = {
                "timeout": "closed-candle monitoring data unavailable",
                "risk": "portfolio loss/risk limit reached",
                "stop": "protective stop or volatility trailing stop reached",
                "hold": "4-hour holding limit reached",
                "confirmed_crisis": "BTC market crisis detected",
            }
            assert closed["reason"] == expected[condition]
    finally:
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


async def test_new_stop_lookup_delay_keeps_entry_and_never_resubmits(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    sim.stop_lookup_misses = 2
    await engine.enter("SOLUSDT", row, now)
    assert sim.quantity > 0 and engine.positions()["SOLUSDT"]
    assert store.get("state") == State.RUNNING
    assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
    assert store.get("live_stops")["SOLUSDT"]["algo_id"] == "1"
    assert sum(method == "POST" and path.endswith("/algoOrder") for method, path in sim.calls) == 1
    assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
    await adapter.close()


async def test_missing_stop_unwinds_and_allows_verified_recovery(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    sim.stop_lookup_misses = 3
    with pytest.raises(SafeError, match="protection cannot be confirmed"):
        await engine.enter("SOLUSDT", row, now)
    assert sim.quantity == 0 and not engine.positions()
    assert not store.unresolved_orders() and not store.get("simple_pending")
    assert store.get("state") == State.DEGRADED
    assert await engine.coordinator.reconcile()
    await engine.resume(engine.broker.now_ms())
    assert store.get("state") == State.RUNNING
    # Recovery does not submit a replacement entry for the same signal.
    await engine.enter("SOLUSDT", row, engine.broker.now_ms())
    assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 2
    await adapter.close()


async def test_confirmed_unwind_does_not_clear_loss_cooldown(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    store.put("consecutive_losses", 2)
    sim.stop_lookup_misses = 3
    with pytest.raises(SafeError):
        await engine.enter("SOLUSDT", row, now)
    assert sim.quantity == 0
    assert store.get("state") == State.SOFT_RISK_LOCK
    assert store.get("risk_lock")["reason"] == "three_losses"
    with pytest.raises(SafeError, match="cannot be overridden"):
        await engine.resume(engine.broker.now_ms())
    await adapter.close()


async def test_health_uses_clock_after_account_refresh(settings, store, filters):
    engine, _, adapter, _, now = await setup_engine(settings, store, filters)
    await engine.broker.refresh()
    # A newly refreshed snapshot must not appear to be from the future merely
    # because the caller's old iteration timestamp predates that refresh.
    assert engine.health(now - 500).account_age >= 0
    engine.broker.account_at_ms -= engine.s.account_stale_seconds * 1000 + 1000
    with pytest.raises(SafeError, match="stale or invalid"):
        engine.risk.require_health(engine.health(now))
    await adapter.close()


async def test_under_minimum_entry_is_skipped_before_exchange_mutations(settings, store, filters):
    from dataclasses import replace

    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    engine.filters["SOLUSDT"] = replace(engine.filters["SOLUSDT"], min_notional=Decimal("1000"))
    await engine.enter("SOLUSDT", row, now)
    assert not sim.quantity and not store.rows("entries")
    assert all(method == "GET" for method, _ in sim.calls)
    assert store.get("state") == State.RUNNING
    assert any(r["kind"] == "simple_entry_skipped" for r in store.rows("audit"))
    await adapter.close()


async def test_runtime_resumes_confirmed_failed_entry_without_manual_command(
    settings, store, filters, monkeypatch
):
    import paperfrog.simple.service as service_module

    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    sim.reject_stop = True
    with pytest.raises(SafeError):
        await engine.enter("SOLUSDT", row, now)
    assert store.get("state") == State.DEGRADED
    service = SimpleService(engine.s)
    service.store.engine.dispose()
    await service.exchange.close()
    service.store, service.engine, service.exchange = store, engine, adapter
    service.rows, service.selected, service.crisis = {"SOLUSDT": row}, ["SOLUSDT"], False
    service.auto_start = False

    async def fetch_quote(symbol):
        from dataclasses import replace

        return replace(engine.broker.quotes[symbol], timestamp_ms=service.now_ms())

    async def sleep(seconds):
        service.stop.set()

    monkeypatch.setattr(service, "fetch_quote", fetch_quote)
    monkeypatch.setattr(service_module.asyncio, "sleep", sleep)
    await service.monitor()
    assert store.get("state") == State.RUNNING
    assert not store.get("manual_lock") and not sim.quantity
    assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 2
    await adapter.close()


async def test_runtime_retries_transport_failure_and_only_announces_state_changes(
    settings, store, filters, monkeypatch
):
    import paperfrog.simple.service as service_module

    engine, sim, adapter, row, _ = await setup_engine(settings, store, filters)
    row["flow"] = 0
    service = SimpleService(engine.s)
    service.store.engine.dispose()
    await service.exchange.close()
    service.store, service.engine, service.exchange = store, engine, adapter
    service.rows, service.selected, service.crisis = {"SOLUSDT": row}, ["SOLUSDT"], False
    service.auto_start = False
    attempts = 0

    async def fetch_quote(symbol):
        nonlocal attempts
        from dataclasses import replace

        attempts += 1
        if attempts <= 2:
            raise SafeError("exchange transport unavailable")
        return replace(engine.broker.quotes[symbol], timestamp_ms=service.now_ms())

    async def sleep(seconds):
        if attempts >= 3:
            service.stop.set()

    monkeypatch.setattr(service, "fetch_quote", fetch_quote)
    monkeypatch.setattr(service_module.asyncio, "sleep", sleep)
    await service.monitor()
    assert store.get("state") == State.RUNNING
    assert not sim.quantity
    import json

    transitions = [
        json.loads(r["payload"]) for r in store.rows("audit") if r["kind"] == "state_transition"
    ]
    assert sum(r["to"] == State.DEGRADED for r in transitions) == 1
    await adapter.close()


async def test_restart_keeps_cooldown_visible_and_preserves_expiry(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    await engine.enter("SOLUSDT", row, now)
    store.put("consecutive_losses", 2)
    sim.fire_stop()
    restored = Engine(engine.s, store, filters, broker=engine.broker)
    await restored.restore()
    lock = store.get("risk_lock")
    assert lock["reason"] == "three_losses"
    assert store.get("state") == State.SOFT_RISK_LOCK
    await restored.restore()
    assert store.get("risk_lock") == lock
    assert store.get("state") == State.SOFT_RISK_LOCK
    await adapter.close()


async def telegram_resume_fixture(settings, store, filters):
    engine, sim, adapter, row, now = await setup_engine(
        settings,
        store,
        filters,
        telegram_allowed_user_ids=(1,),
        telegram_allowed_chat_ids=(2,),
    )
    service = SimpleService(engine.s)
    await service.exchange.close()
    service.store.engine.dispose()
    service.store, service.engine, service.exchange = store, engine, adapter
    service.rows, service.selected, service.crisis = {"SOLUSDT": row}, ["SOLUSDT"], False
    controls = Controls(
        engine.s,
        store,
        service.resume,
        service.flatten,
        service.status,
        force_resume=service.refuse_force,
    )
    update = {
        "update_id": time.time_ns(),
        "message": {
            "from": {"id": 1},
            "chat": {"id": 2, "type": "private"},
            "date": time.time(),
            "text": "/resume",
        },
    }
    lock = {
        "state": State.DAILY_RISK_LOCK,
        "until": now / 1000 + 86400,
        "reason": "profit_giveback",
    }
    store.put("risk_lock", lock)
    store.put("state", lock["state"])
    store.put("manual_lock", True)
    store.put("profit_entry_lock", True)
    store.put("consecutive_losses", 3)
    store.put("cooldown:SOLUSDT", now / 1000 + 10800)
    return service, sim, adapter, controls, update


@pytest.mark.parametrize(
    "state,reason",
    [
        (State.SOFT_RISK_LOCK, "three_losses"),
        (State.DAILY_RISK_LOCK, "daily_loss"),
        (State.DAILY_RISK_LOCK, "profit_giveback"),
        (State.WEEKLY_RISK_LOCK, "weekly_local_review"),
        (State.HARD_RISK_LOCK, "high_water"),
        (State.ERROR_SAFE, "previous execution failure"),
        (State.DEGRADED, "previous transport failure"),
        (State.PAUSED, "manual pause"),
    ],
)
async def test_telegram_resume_resets_financial_locks_using_fresh_exchange_equity(
    settings, store, filters, state, reason
):
    service, sim, adapter, controls, update = await telegram_resume_fixture(
        settings, store, filters
    )
    store.put("risk_lock", {"state": state, "until": None, "reason": reason})
    store.put("state", state)
    sim.equity = "475"
    arm_path = directory(service.s) / "live-arm.json"
    arm_record = arm_path.read_bytes()
    response = await controls.handle(update)
    assert "Trading is running" in response[1]
    assert store.get("state") == State.RUNNING
    assert store.get("anchors")["day_equity"] == 475
    assert store.get("anchors")["high_water"] == 475
    assert not store.get("risk_lock") and not store.get("profit_entry_lock")
    assert not store.get("manual_lock")
    assert store.get("consecutive_losses") == 0
    assert store.get("cooldown:SOLUSDT") == 0
    assert arm_path.read_bytes() == arm_record
    # Telegram's durable update deduplication must not rebase equity twice.
    sim.equity = "470"
    assert await controls.handle(update) is None
    assert store.get("anchors")["day_equity"] == 475
    await service.engine.monitor(service.now_ms(), service.rows)
    assert store.get("state") == State.RUNNING
    assert not any(method == "POST" and path.endswith("/order") for method, path in sim.calls)
    await adapter.close()


@pytest.mark.parametrize(
    "failure", ["arm", "mismatch", "stale", "crisis", "future_candle", "pending_close"]
)
async def test_telegram_resume_keeps_financial_locks_when_execution_is_unhealthy(
    settings, store, filters, failure
):
    service, sim, adapter, controls, update = await telegram_resume_fixture(
        settings, store, filters
    )
    if failure == "arm":
        (directory(service.s) / "live-arm.json").unlink()
    elif failure == "mismatch":
        sim.quantity = Decimal("1")
    elif failure == "stale":
        service.engine.broker.quotes.clear()
        # A cold cache is now refreshed. Truly stale exchange data must still fail.
        sim.market_time = service.now_ms() - int(service.s.market_stale_seconds * 1000) - 1
    elif failure == "crisis":
        service.crisis = True
    elif failure == "future_candle":
        service.rows["SOLUSDT"]["timestamp_ms"] = service.now_ms() + BAR_MS
    elif failure == "pending_close":
        store.put("local_flatten_request", "local-emergency")
    before = {
        key: store.get(key)
        for key in (
            "anchors",
            "risk_lock",
            "manual_lock",
            "profit_entry_lock",
            "consecutive_losses",
            "cooldown:SOLUSDT",
        )
    }
    response = await controls.handle(update)
    assert response[1].startswith("Refused:")
    assert {key: store.get(key) for key in before} == before
    assert store.get("state") != State.RUNNING
    assert not any(row["kind"] == "operator_risk_reset" for row in store.rows("audit"))
    if failure == "arm":
        assert not (directory(service.s) / "live-arm.json").exists()
    await adapter.close()


@pytest.mark.parametrize("failure", ["journal", "hosted_stop"])
async def test_operator_resume_verifies_execution_recovery_before_resetting_locks(
    settings, store, filters, failure
):
    engine, sim, adapter, row, now = await setup_engine(settings, store, filters)
    if failure == "journal":
        sim.unknown_order = True
        with pytest.raises(UnknownOrder):
            await engine.enter("SOLUSDT", row, now)
    else:
        await engine.enter("SOLUSDT", row, now)
        next(iter(sim.stops.values()))["algoStatus"] = "CANCELED"
    store.put("risk_lock", {"state": State.HARD_RISK_LOCK, "until": None, "reason": "high_water"})
    store.put("state", State.HARD_RISK_LOCK)
    store.put("manual_lock", True)
    before = {key: store.get(key) for key in ("anchors", "risk_lock", "manual_lock")}
    with pytest.raises(SafeError):
        await engine.resume(engine.broker.now_ms(), override_locks=True)
    assert {key: store.get(key) for key in before} == before
    assert not any(row["kind"] == "operator_risk_reset" for row in store.rows("audit"))
    assert store.get("state") != State.RUNNING
    await adapter.close()
