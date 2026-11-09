"""Two-venue execution, crash recovery and shared-budget regression tests."""

from decimal import Decimal

import pytest
from sqlalchemy import text
from test_simple_live import live_settings

from paperfrog.domain.types import (
    EntryVeto,
    Fill,
    Filters,
    Order,
    Quote,
    SafeError,
    State,
    UnknownOrder,
)
from paperfrog.settings import Settings
from paperfrog.simple.carry import DAY, Carry, permission_allowed, positions, require_order

D = Decimal


class Venue:
    def __init__(self, s, now):
        self.s, self.now = s, now
        self.filters = {
            symbol: Filters(D(".001"), D(".01"), D(".001"), D(1000), D(5))
            for symbol in ("BTCUSDT", "ETHUSDT")
        }
        self.orders, self.spot_balances, self.future_positions = {}, {}, {}
        self.posts = []
        self.buy_fraction = D(1)
        self.base_fee = D(0)
        self.unknown = False
        self.permission = True
        self.income_rows = []
        self.future_fills = {}

    async def positions(self):
        return {k: v for k, v in self.future_positions.items() if v}

    async def verify_risk_settings(self, symbols):
        pass

    async def request(self, method, path, params=None, **kw):
        p = params or {}
        if path == "/api/v3/account":
            return {
                "canTrade": True,
                "permissions": ["TRD_GRP_033"],
                "balances": [
                    {"asset": "USDT", "free": "10000", "locked": "0"},
                    *(
                        {"asset": k[:-4], "free": str(v), "locked": "0"}
                        for k, v in self.spot_balances.items()
                    ),
                ],
            }
        if path == "/sapi/v1/account/apiRestrictions":
            return {"enableSpotAndMarginTrading": self.permission}
        if path == "/api/v3/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": s,
                        "status": "TRADING",
                        "isSpotTradingAllowed": True,
                        "permissionSets": [["TRD_GRP_033"]],
                        "orderTypes": ["LIMIT"],
                        "filters": [
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": ".001",
                                "minQty": ".001",
                                "maxQty": "1000",
                            },
                            {"filterType": "PRICE_FILTER", "tickSize": ".01"},
                            {"filterType": "NOTIONAL", "minNotional": "5"},
                        ],
                    }
                    for s in self.filters
                ]
            }
        if path == "/api/v3/openOrders":
            return []
        if path == "/api/v3/ticker/bookTicker":
            return {"bidPrice": "99.99", "askPrice": "100.01", "bidQty": "10000", "askQty": "10000"}
        if path == "/fapi/v1/fundingRate":
            return [
                {"symbol": p["symbol"], "fundingTime": t, "fundingRate": ".0001"}
                for t in range(p["startTime"], p["endTime"] + 1, 8 * 3_600_000)
            ]
        if path == "/fapi/v1/klines":
            day = self.now // DAY * DAY
            return [[day - DAY, 100, 101, 99, 100, 1, day - 1, 100_000_000]]
        if path == "/fapi/v1/symbolConfig":
            return [{"symbol": p["symbol"], "leverage": 2}]
        if path == "/fapi/v1/income":
            return self.income_rows
        if path == "/fapi/v1/userTrades":
            fill = next(f for f in self.future_fills.values() if f.exchange_id == str(p["orderId"]))
            return [
                {
                    "symbol": fill.symbol,
                    "id": fill.exchange_id,
                    "orderId": fill.exchange_id,
                    "side": "BUY" if fill.side == 1 else "SELL",
                    "qty": str(fill.quantity),
                    "commissionAsset": "USDT",
                    "commission": str(fill.fee),
                    "realizedPnl": "0",
                }
            ]
        if path == "/api/v3/order":
            if method == "GET":
                return self.orders[p["origClientOrderId"]]
            self.posts.append(dict(p))
            quantity = self.filters[p["symbol"]].round_quantity(
                D(p["quantity"]) * (self.buy_fraction if p["side"] == "BUY" else 1)
            )
            base_fee = self.base_fee if p["side"] == "BUY" else D(0)
            self.spot_balances[p["symbol"]] = (
                self.spot_balances.get(p["symbol"], D(0))
                + quantity * (1 if p["side"] == "BUY" else -1)
                - base_fee
            )
            r = {
                **p,
                "clientOrderId": p["newClientOrderId"],
                "orderId": len(self.orders) + 1,
                "origQty": p["quantity"],
                "executedQty": str(quantity),
                "cummulativeQuoteQty": str(quantity * D(100)),
                "status": "FILLED" if quantity == D(p["quantity"]) else "EXPIRED",
            }
            self.orders[p["newClientOrderId"]] = r
            if self.unknown:
                self.unknown = False
                raise UnknownOrder("simulated timeout after accepted spot order")
            return r
        if path == "/api/v3/myTrades":
            r = next(r for r in self.orders.values() if r["orderId"] == p["orderId"])
            return [
                {
                    "id": r["orderId"],
                    "orderId": r["orderId"],
                    "symbol": r["symbol"],
                    "qty": r["executedQty"],
                    "quoteQty": r["cummulativeQuoteQty"],
                    "isBuyer": r["side"] == "BUY",
                    "commissionAsset": r["symbol"][:-4] if self.base_fee else "USDT",
                    "commission": str(self.base_fee if r["side"] == "BUY" else D(0)),
                }
            ]
        raise AssertionError((method, path))


class Broker:
    def __init__(self, s, store):
        self.s, self.store = s, store
        self.clock = 1_790_000_000_000
        self.exchange = Venue(s, self.clock)
        self.available = D(10000)
        self.quotes = {}
        self.future_orders = self.exchange.future_fills
        self.stops = {}
        self.future_fraction = D(1)
        self.unknown = False

    def require_authorization(self):
        pass

    def now_ms(self):
        return self.clock

    async def refresh(self):
        pass

    def equity(self):
        return D(10000)

    async def refresh_quote(self, symbol):
        q = Quote(symbol, D(100), D("100.02"), D(1000), D(1000), D(100), self.clock)
        self.quotes[symbol] = q
        return q

    async def submit(self, order):
        if not order.reduce_only:
            require_order(self.s, self.store, order, self.clock)
        q = order.quantity * (1 if order.reduce_only else self.future_fraction)
        fill = Fill(
            client_id=order.client_id,
            symbol=order.symbol,
            side=order.side,
            quantity=q,
            price=D(100),
            fee=q * D(".05"),
            status="FILLED" if q == order.quantity else "EXPIRED",
            exchange_id=order.client_id,
        )
        self.exchange.future_positions[order.symbol] = (
            self.exchange.future_positions.get(order.symbol, D(0)) + q * order.side
        )
        self.future_orders[order.client_id] = fill
        if self.unknown:
            self.unknown = False
            raise UnknownOrder("simulated timeout after accepted futures order")
        return fill

    async def query(self, symbol, key):
        return self.future_orders[key]

    async def positions(self):
        return await self.exchange.positions()

    async def protect(self, symbol, side, trigger, key):
        self.stops[symbol] = {"key": key, "quantity": self.exchange.future_positions[symbol]}

    async def verify_liquidation(self, fraction):
        pass

    async def protection_fill(self, symbol):
        return None

    async def cancel_protection(self, symbol):
        self.stops.pop(symbol, None)


@pytest.fixture
def carry(settings, store):
    s = live_settings(
        settings,
        **{
            "trading_mode": "live",
            "simple_carry_enabled": True,
            "simple_entry_profile": "momentum",
            "simple_entry_timeframe": "5m",
            "simple_btc_reference_enabled": False,
            "simple_market_trend_enabled": False,
            "simple_sizing_mode": "margin",
            "simple_position_margin_cap": 0.1,
            "max_target_notional": 1.6,
            "max_gross_notional": 2.0,
            "max_net_beta": 2.0,
            "risk_per_trade": 0.04,
            "max_open_risk": 0.05,
            "max_concurrent_strategy_trades": 4,
        },
    )
    store.put("state", State.RUNNING)
    store.put("reconciled", True)
    b = Broker(s, store)
    return Carry(s, store, b)


def test_carry_is_local_live_opt_in_and_default_remains_paper():
    assert Settings(_env_file=None).trading_mode == "paper"
    assert not Settings(_env_file=None).simple_carry_enabled
    with pytest.raises(ValueError, match="LIVE momentum"):
        Settings(_env_file=None, simple_carry_enabled=True)
    assert permission_allowed(
        {"permissions": ["TRD_GRP_033"]}, {"permissionSets": [["SPOT", "TRD_GRP_033"]]}
    )
    assert not permission_allowed(
        {"permissions": ["TRD_GRP_033"]}, {"permissionSets": [["SPOT"], ["TRD_GRP_033"]]}
    )
    assert not permission_allowed({"permissions": ["SPOT"]}, {})


async def test_two_leg_live_cycle_has_matched_inventory_and_hosted_protection(carry):
    await carry.tick()
    c = carry.cycles()["BTCUSDT"]
    assert c["phase"] == "OPEN"
    assert D(c["spot_quantity"]) == D(c["future_quantity"])
    assert carry.exchange.future_positions["BTCUSDT"] == -D(c["spot_quantity"])
    assert carry.broker.stops["BTCUSDT"]
    assert await carry.coordinator.reconcile()
    assert positions(carry.store) == await carry.broker.positions()
    await carry.tick(entries=False, flatten=True)
    assert carry.cycles()["BTCUSDT"]["phase"] == "CLOSED"
    assert not await carry.exchange.positions()
    assert carry.exchange.spot_balances["BTCUSDT"] == 0
    assert not carry.broker.stops
    assert carry.store.rows("entries")[0]["closed"] == 1


async def test_shared_risk_budget_prevents_second_hedge_and_preserves_locks(carry):
    await carry.tick()
    assert len(carry.exchange.posts) == 1
    assert len(await carry.exchange.positions()) == 1
    saved = {
        "risk_lock": {"state": State.HARD_RISK_LOCK, "reason": "existing"},
        "manual_lock": True,
        "anchors": {"high_water": 12000, "day_equity": 11000},
    }
    for key, value in saved.items():
        carry.store.put(key, value)
    await carry.tick(entries=False, flatten=True)
    assert {k: carry.store.get(k) for k in saved} == saved
    assert not await carry.exchange.positions()


@pytest.mark.parametrize("venue", ["spot", "future"])
async def test_crash_after_acceptance_queries_original_order_without_resubmission(carry, venue):
    target = carry.exchange if venue == "spot" else carry.broker
    target.unknown = True
    with pytest.raises(UnknownOrder):
        await carry.preflight()
        await carry.open("BTCUSDT")
    assert len(carry.exchange.posts) == 1
    carry.store.put("state", State.ERROR_SAFE)
    await carry.advance("BTCUSDT", flatten=True)
    assert len([r for r in carry.exchange.posts if r["side"] == "BUY"]) == 1
    assert len([r for r in carry.broker.future_orders.values() if r.side == -1]) <= 1
    assert not await carry.exchange.positions()
    assert carry.exchange.spot_balances["BTCUSDT"] == 0


@pytest.mark.parametrize("fraction,fee", [(D(".5"), D(0)), (D(1), D(".001"))])
async def test_partial_spot_fill_or_base_commission_unwinds_without_futures(carry, fraction, fee):
    carry.exchange.buy_fraction, carry.exchange.base_fee = fraction, fee
    await carry.preflight()
    await carry.open("BTCUSDT")
    assert carry.cycles()["BTCUSDT"]["phase"] == "CLOSED"
    assert not carry.broker.future_orders
    assert carry.exchange.spot_balances["BTCUSDT"] == 0


async def test_partial_futures_fill_is_protected_then_both_legs_are_closed(carry):
    carry.broker.future_fraction = D(".5")
    await carry.preflight()
    await carry.open("BTCUSDT")
    assert carry.cycles()["BTCUSDT"]["phase"] == "CLOSED"
    assert not await carry.exchange.positions()
    assert carry.exchange.spot_balances["BTCUSDT"] == 0


async def test_missing_spot_permission_blocks_only_unstarted_carry(carry):
    carry.exchange.permission = False
    await carry.tick()
    assert carry.store.get("carry_status")["state"] == "blocked"
    assert carry.store.get("state") == State.RUNNING
    assert not carry.exchange.posts


async def test_inventory_removed_outside_bot_blocks_new_entries(carry):
    await carry.tick()
    carry.exchange.spot_balances["BTCUSDT"] = D(0)
    with pytest.raises(SafeError, match="inventory mismatch"):
        await carry.tick()
    assert carry.store.get("state") == State.ERROR_SAFE
    assert carry.store.get("reconciled") is False


async def test_stale_changed_or_unbound_hedge_permission_is_rejected(carry):
    await carry.tick()
    c = carry.cycles()["BTCUSDT"]
    order = Order.model_validate(c["future_order"])
    c["phase"] = "HEDGE"
    carry.save("BTCUSDT", c)
    require_order(carry.s, carry.store, order, carry.now())
    for changed, now in [
        (order.model_copy(update={"quantity": order.quantity * 2}), carry.now()),
        (order, carry.now() + 5000),
    ]:
        with pytest.raises(EntryVeto):
            require_order(carry.s, carry.store, changed, now)
    c["config_hash"] = "changed"
    carry.save("BTCUSDT", c)
    with pytest.raises(EntryVeto):
        require_order(carry.s, carry.store, order, carry.now())


async def test_no_new_buy_after_flatten_interrupts_prepared_spot_phase(carry):
    await carry.tick()
    c = carry.cycles()["BTCUSDT"]
    # Simulate interruption before the first spot request, with a reserved slot.
    carry.exchange.future_positions.clear()
    carry.exchange.spot_balances.clear()
    carry.broker.future_orders.clear()
    carry.exchange.posts.clear()
    with carry.store.engine.begin() as conn:
        conn.execute(text("DELETE FROM carry_spot_orders"))
    c.update(phase="SPOT", spot_quantity="0", future_quantity="0", spot_cash="0")
    carry.save("BTCUSDT", c)
    await carry.advance("BTCUSDT", flatten=True)
    assert not carry.exchange.posts
    assert carry.cycles()["BTCUSDT"]["phase"] == "CLOSED"


async def test_negative_funding_is_included_and_settlements_are_not_counted_twice(carry):
    await carry.tick()
    c = carry.cycles()["BTCUSDT"]
    carry.exchange.income_rows = [
        {
            "symbol": "BTCUSDT",
            "asset": "USDT",
            "incomeType": "FUNDING_FEE",
            "tranId": 1,
            "time": carry.now(),
            "income": "-20",
        }
    ]
    assert await carry.income("BTCUSDT", c) == -20
    assert await carry.income("BTCUSDT", c) == -20
    carry.broker.clock += 8 * DAY
    with pytest.raises(SafeError, match="history gap"):
        await carry.income("BTCUSDT", c)


async def test_unknown_spot_exit_recovers_after_inventory_has_already_been_sold(carry):
    await carry.tick()
    carry.exchange.unknown = True
    with pytest.raises(SafeError):
        await carry.tick(entries=False, flatten=True)
    assert carry.exchange.spot_balances["BTCUSDT"] == 0
    await carry.tick(entries=False, flatten=True)
    assert carry.cycles()["BTCUSDT"]["phase"] == "CLOSED"
    assert len([p for p in carry.exchange.posts if p["side"] == "SELL"]) == 1


async def test_disarmed_strategy_can_still_close_both_legs(carry, monkeypatch):
    await carry.tick()

    def disarmed():
        raise SafeError("local arm removed")

    monkeypatch.setattr(carry.broker, "require_authorization", disarmed)
    await carry.tick(entries=False, flatten=True)
    assert carry.cycles()["BTCUSDT"]["phase"] == "CLOSED"
    assert not await carry.exchange.positions()


async def test_known_carry_positions_are_accepted_by_momentum_journal_check(carry):
    from paperfrog.simple.engine import Engine
    from paperfrog.simple.live_broker import LiveBroker

    broker = LiveBroker(carry.s, carry.store, carry.exchange)

    async def account_positions():
        return await carry.exchange.positions()

    broker.positions = account_positions
    engine = Engine(carry.s, carry.store, carry.exchange.filters, broker=broker)
    await carry.tick()
    await engine.require_journal()
    carry.exchange.future_positions["SOLUSDT"] = D(1)
    with pytest.raises(SafeError, match="journal incomplete"):
        await engine.require_journal()


async def test_unknown_futures_exit_is_queried_without_second_reduction(carry):
    await carry.tick()
    carry.broker.unknown = True
    with pytest.raises(SafeError):
        await carry.tick(entries=False, flatten=True)
    assert not await carry.exchange.positions()
    await carry.tick(entries=False, flatten=True)
    assert carry.cycles()["BTCUSDT"]["phase"] == "CLOSED"
    assert len([f for f in carry.broker.future_orders.values() if f.side == 1]) == 1
    assert await carry.coordinator.reconcile()


async def test_hosted_stop_after_downtime_closes_spot_and_reconciles_once(carry, monkeypatch):
    await carry.tick()
    cycle = carry.cycles()["BTCUSDT"]
    quantity = D(cycle["future_quantity"])
    stopped = Fill(
        client_id="pf-hosted-stop",
        symbol="BTCUSDT",
        side=1,
        quantity=quantity,
        price=D(110),
        fee=D(".1"),
        status="FILLED",
        exchange_id="hosted-123",
    )
    carry.broker.future_orders["stop"] = stopped
    carry.exchange.future_positions["BTCUSDT"] = D(0)

    async def protection(symbol):
        return stopped

    monkeypatch.setattr(carry.broker, "protection_fill", protection)
    await carry.tick(entries=False)
    assert carry.cycles()["BTCUSDT"]["phase"] == "CLOSED"
    assert carry.exchange.spot_balances["BTCUSDT"] == 0
    assert await carry.coordinator.reconcile()
    await carry.tick(entries=False)
    assert await carry.coordinator.reconcile()
    with carry.store.engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM events WHERE key LIKE 'carry-stop:%'")).scalar()
            == 1
        )
    assert len([p for p in carry.exchange.posts if p["side"] == "SELL"]) == 1


async def test_gateway_requires_reserved_shared_risk_even_with_exact_hedge_intent(carry):
    await carry.tick()
    cycle = carry.cycles()["BTCUSDT"]
    cycle["phase"] = "HEDGE"
    carry.save("BTCUSDT", cycle)
    with carry.store.engine.begin() as conn:
        conn.execute(text("UPDATE entries SET closed=1 WHERE id=:i"), {"i": cycle["id"]})
    with pytest.raises(EntryVeto, match="risk reservation"):
        require_order(
            carry.s, carry.store, Order.model_validate(cycle["future_order"]), carry.now()
        )
