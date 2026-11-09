"""All 100 candidates are considered before selecting one executable setup."""

import time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text
from test_early_momentum import early
from test_fixed_roe import options
from test_simple_live import live_settings

from paperfrog.domain.types import EntryVeto, Filters, Order, Quote
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.simple.data import scan
from paperfrog.simple.momentum import require_order
from paperfrog.simple.selection import pool_hash, select_best

D = Decimal
BAR = 300000
BOUNDARY = 270000000
NOW = BOUNDARY + 1000


def setup(settings, store, monkeypatch, damage=None):
    monkeypatch.setattr(time, "time", lambda: NOW / 1000)
    s = live_settings(settings, **options())
    pool = [f"C{i:03}USDT" for i in range(100)]
    store.put("simple_pool", pool)
    rows = {
        symbol: early(1, open=99.8, close=100, fast=99.5, slow=98.5, return_15m=0.003 + i * 0.00001)
        for i, symbol in enumerate(pool)
    }
    rows = {
        symbol: {**r, "timestamp_ms": BOUNDARY, "momentum_trend_started_ms": BOUNDARY - 600000}
        for symbol, r in rows.items()
    }
    f = Filters(D(".001"), D(".01"), D(".001"), D(1000000), D(5))
    e = SimpleNamespace(s=s, offset_ms=0, filters={symbol: f for symbol in pool})

    async def request(method, path, p):
        symbol = p["symbol"]
        broken = symbol == pool[-1]
        if path.endswith("premiumIndex"):
            return {
                "markPrice": "100",
                "time": NOW - 61000 if broken and damage == "stale" else NOW,
            }
        if broken and damage == "spread":
            return {
                "bidPrice": "99",
                "askPrice": "101",
                "bidQty": "10000",
                "askQty": "10000",
                "time": NOW,
            }
        return {
            "bidPrice": "99.99",
            "askPrice": "100.01",
            "bidQty": "10000",
            "askQty": ".01" if broken and damage == "depth" else "10000",
            "time": NOW,
        }

    e.request = AsyncMock(side_effect=request)
    return s, pool, rows, e


async def test_candidate_number_one_hundred_can_win(settings, store, monkeypatch):
    s, pool, rows, e = setup(settings, store, monkeypatch)
    selected, report = await select_best(s, store, e, rows, BOUNDARY, lambda: NOW)
    assert selected == [pool[-1]]
    assert report["pool_size"] == 100 and report["quote_checked_count"] == 100
    assert len(report["ranked"]) == 100
    assert len(e.request.await_args_list) == 200
    assert report["selected"] == selected


@pytest.mark.parametrize("damage", ["spread", "depth", "stale", "held", "cooldown", "daily"])
async def test_untradeable_strongest_setup_does_not_hide_second_best(
    settings, store, monkeypatch, damage
):
    s, pool, rows, e = setup(settings, store, monkeypatch, damage)
    if damage == "held":
        store.put("simple_positions", {pool[-1]: {}})
    if damage == "cooldown":
        store.put("cooldown:" + pool[-1], NOW / 1000 + 100)
    if damage == "daily":
        day = datetime.fromtimestamp(NOW / 1000, UTC).date().isoformat()
        with store.engine.begin() as c:
            for i in range(s.max_entries_per_symbol_per_day):
                c.execute(
                    text(
                        "INSERT INTO entries(id,day,symbol,ts,closed,risk,gross) VALUES (:id,:d,:s,:t,1,1,1)"
                    ),
                    {"id": str(i), "d": day, "s": pool[-1], "t": NOW},
                )
    selected, report = await select_best(s, store, e, rows, BOUNDARY, lambda: NOW)
    assert selected == [pool[-2]]
    assert pool[-1] in report["rejected"]
    assert all(r["symbol"] != pool[-1] for r in report["ranked"])


async def test_missing_or_old_candles_do_not_receive_a_live_quote_permission(
    settings, store, monkeypatch
):
    s, pool, rows, e = setup(settings, store, monkeypatch)
    for row in rows.values():
        row["momentum_trend_age_bars"] = -1
    selected, report = await select_best(s, store, e, rows, BOUNDARY, lambda: NOW)
    assert selected == [] and len(report["rejected"]) == 100
    assert e.request.await_count == 0


async def test_full_scan_really_has_one_hundred_and_excludes_reserved_btc_eth(
    settings, monkeypatch
):
    monkeypatch.setattr(time, "time", lambda: NOW / 1000)
    s = live_settings(settings, **options(), simple_carry_enabled=True)
    pool = [f"C{i:03}USDT" for i in range(120)] + ["BTCUSDT", "ETHUSDT"]
    metadata = [
        {
            "symbol": symbol,
            "baseAsset": symbol[:-4],
            "quoteAsset": "USDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "underlyingType": "COIN",
            "onboardDate": NOW - 4 * 86400000,
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": ".001",
                    "minQty": ".001",
                    "maxQty": "1000000",
                },
                {"filterType": "PRICE_FILTER", "tickSize": ".01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
        for symbol in pool
    ]

    def handle(request):
        return httpx.Response(
            200,
            json={"symbols": metadata}
            if request.url.path.endswith("exchangeInfo")
            else [
                {"symbol": symbol, "quoteVolume": str(50000000 - i * 10000), "closeTime": NOW}
                for i, symbol in enumerate(pool)
            ],
        )

    e = BinanceAdapter(s, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        result = await scan(e, s)
        assert len(result["symbols"]) == 100
        assert set(result["symbols"]) == set(pool[:100])
        assert "BTCUSDT" not in result["symbols"] and "ETHUSDT" not in result["symbols"]
    finally:
        await e.close()


@pytest.mark.parametrize("damage", [None, "selection", "pool", "config", "expired"])
def test_dispatch_requires_current_pool_and_single_winner_binding(
    settings, store, monkeypatch, damage
):
    s, pool, rows, e = setup(settings, store, monkeypatch)
    symbol = pool[-1]
    order = Order(
        client_id="pf-universe", symbol=symbol, side=1, quantity=D(1), limit_price=D("100.02")
    )
    store.put("simple_minute:" + symbol, rows[symbol])
    store.put(
        "simple_minute_order:" + order.client_id,
        {"order": order.model_dump(mode="json"), "row": rows[symbol], "config_hash": s.config_hash},
    )
    report = {
        "selected": [symbol],
        "pool_hash": pool_hash(pool),
        "config_hash": s.config_hash,
        "asof_ms": BOUNDARY,
    }
    if damage == "selection":
        report["selected"] = [pool[0]]
    if damage == "pool":
        store.put("simple_pool", pool[:-1])
    if damage == "config":
        report["config_hash"] = "changed"
    if damage == "expired":
        report["asof_ms"] = BOUNDARY - BAR
    store.put("simple_candidate_ranking", report)
    q = Quote(symbol, D("99.99"), D("100.01"), D(10000), D(10000), D(100), NOW)
    if damage:
        with pytest.raises(EntryVeto):
            require_order(s, store, order, NOW, q)
    else:
        require_order(s, store, order, NOW, q)
    require_order(s, store, order.model_copy(update={"reduce_only": True}), NOW, None)


async def test_service_publishes_one_winner_after_assessing_full_pool(settings, store, monkeypatch):
    import pandas as pd
    from test_minute_entries import slow_context

    from paperfrog.simple.service import SimpleService

    s, pool, rows, e = setup(settings, store, monkeypatch)
    service = SimpleService(s)
    service.store.put("simple_pool", pool)
    service.context_rows = {symbol: slow_context(BOUNDARY) for symbol in [*pool, "BTCUSDT"]}
    service.exchange.request = e.request
    service.exchange.filters = e.filters
    service.now_ms = lambda: NOW

    async def completed(symbol, timeframe, boundary, context):
        return pd.DataFrame([rows[symbol]])

    service.entry_frame = AsyncMock(side_effect=completed)
    try:
        assert await service.refresh_minutes(BOUNDARY)
        assert service.selected == [pool[-1]]
        report = service.store.get("simple_candidate_ranking")
        assert report["pool_size"] == 100 and len(report["ranked"]) == 100
        assert service.entry_frame.await_count == 100
        assert service.store.get("simple_selected") == [pool[-1]]
        assert service.advisor is None
    finally:
        await service.exchange.close()
