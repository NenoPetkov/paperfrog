from decimal import Decimal

import httpx
import pytest

from paperfrog.domain.types import Order, Quote, SafeError, UnknownOrder
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.risk.engine import Proposal
from paperfrog.settings import Settings


def order(id="pf-first", symbol="SOLUSDT", side=1, quantity="1", price="101", reduce=False):
    return Order(
        client_id=id,
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        reduce_only=reduce,
    )


async def test_fill_idempotence(coordinator, broker, store):
    first = await coordinator.submit_once(order())
    second = await coordinator.submit_once(order())
    assert first == second
    assert (await broker.positions())["SOLUSDT"] == 1
    assert len(store.rows("fills")) == 1


async def test_ioc_partial_fill(broker):
    await broker.quote(
        Quote(
            "SOLUSDT",
            Decimal("99"),
            Decimal("100"),
            Decimal(".3"),
            Decimal(".4"),
            Decimal("100"),
            1800000,
        )
    )
    fill = await broker.submit(order())
    assert fill.quantity == Decimal(".4") and fill.status == "EXPIRED"


async def test_reduce_only_cannot_flip(broker):
    await broker.submit(order())
    fill = await broker.submit(order("pf-exit", side=-1, quantity="10", price="90", reduce=True))
    assert fill.quantity == 1
    assert not await broker.positions()
    with pytest.raises(SafeError, match="increase"):
        await broker.submit(order("pf-badexit", side=-1, price="90", reduce=True))


async def test_stop_works_during_client_outage(broker):
    await broker.submit(order())
    await broker.protect("SOLUSDT", 1, Decimal("98"), "pf-stop")
    broker.connected = False
    fills = await broker.quote(
        Quote(
            "SOLUSDT",
            Decimal("96"),
            Decimal("97"),
            Decimal("100"),
            Decimal("100"),
            Decimal("97"),
            1801000,
        )
    )
    assert fills and not await broker.positions()


async def test_funding_idempotent(broker):
    await broker.submit(order())
    assert broker.funding("SOLUSDT", 1800000, Decimal(".001"), Decimal("100")) == Decimal("-.1")
    assert broker.funding("SOLUSDT", 1800000, Decimal(".001"), Decimal("100")) == 0


async def test_emergency_flatten(coordinator, broker, store):
    await coordinator.submit_once(order())
    assert await coordinator.flatten(broker.quotes, "emergency-1")
    assert not await broker.positions()
    import json

    reductions = [json.loads(r["intent"]) for r in store.rows("orders") if r["id"] != "pf-first"]
    assert reductions and all(r["reduce_only"] for r in reductions)


async def test_restart_unknown_positions_blocks(coordinator, broker, store):
    await broker.submit(order())
    assert not await coordinator.reconcile()
    assert store.get("state") == "ERROR_SAFE"


async def test_partial_pair_unwinds(coordinator, broker, health, now, store):
    await broker.quote(
        Quote(
            "SOLUSDT",
            Decimal("99.99"),
            Decimal("100.01"),
            Decimal("100"),
            Decimal(".3"),
            Decimal("100"),
            1800000,
        )
    )
    p = Proposal("paired", "SOLUSDT", 10000, 101, 202, 3, 0, 0.01, 0.3, 0.001)
    with pytest.raises(SafeError, match="partial"):
        await coordinator.enter(
            p, [order(), order("pf-hedge", "ETHUSDT", -1, ".05", "2000")], health, now, 0.01
        )
    assert not await broker.positions()
    assert store.rows("entries")[0]["closed"] == 1


async def test_unknown_response_never_resubmits(coordinator, broker, store):
    original = broker.submit
    calls = 0

    async def uncertain(o):
        nonlocal calls
        calls += 1
        await original(o)
        raise UnknownOrder("unknown execution")

    broker.submit = uncertain
    with pytest.raises(UnknownOrder):
        await coordinator.submit_once(order())
    await coordinator.submit_once(order())
    assert calls == 1
    assert await coordinator.reconcile()


async def test_binance_503_no_retry(filters):
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        assert "signature=" in str(request.url)
        return httpx.Response(503, json={"msg": "Unknown error"})

    s = Settings(
        _env_file=None,
        trading_mode="testnet",
        binance_api_key="private-key",
        binance_api_secret="secret-value",
    )
    adapter = BinanceAdapter(s, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    adapter.filters = filters
    adapter.entry_authorized = True
    with pytest.raises(UnknownOrder) as exc:
        await adapter.submit(order())
    assert count == 1 and "secret-value" not in str(exc.value)


async def test_mark_stop_uses_algo_endpoint():
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"algoId": 1})

    s = Settings(
        _env_file=None, trading_mode="testnet", binance_api_key="key", binance_api_secret="secret"
    )
    a = BinanceAdapter(s, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    await a.protect("SOLUSDT", 1, Decimal("90"), "pf-stop")
    params = calls[0].url.params
    assert calls[0].url.path == "/fapi/v1/algoOrder"
    assert params["workingType"] == "MARK_PRICE" and params["closePosition"] == "true"
    assert "quantity" not in params and "reduceOnly" not in params


async def test_paper_adapter_cannot_submit():
    a = BinanceAdapter(Settings(_env_file=None))
    with pytest.raises(SafeError, match="paper"):
        await a.submit(order())
    await a.close()


async def test_reductions_work_while_hard_locked(coordinator, broker, store):
    await coordinator.submit_once(order())
    store.put("state", "HARD_RISK_LOCK")
    store.put("risk_lock", {"state": "HARD_RISK_LOCK", "until": None, "reason": "test"})
    with pytest.raises(SafeError, match="entry gate"):
        await coordinator.submit_once(order("pf-entry-after-lock"))
    await coordinator.submit_once(order("pf-reduce-after-lock", side=-1, price="90", reduce=True))
    assert not await broker.positions()


async def test_stop_partial_continues_on_later_quote(broker):
    await broker.submit(order())
    await broker.protect("SOLUSDT", 1, Decimal("98"), "pf-stop-partial")
    quote = Quote(
        "SOLUSDT",
        Decimal("96"),
        Decimal("97"),
        Decimal(".4"),
        Decimal(".4"),
        Decimal("97"),
        1801000,
    )
    await broker.quote(quote)
    assert (await broker.positions())["SOLUSDT"] == Decimal(".6")
    await broker.quote(
        Quote(
            "SOLUSDT",
            Decimal("96"),
            Decimal("97"),
            Decimal("100"),
            Decimal("100"),
            Decimal("97"),
            1802000,
        )
    )
    assert not await broker.positions()
