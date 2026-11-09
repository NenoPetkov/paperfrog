"""Fixed BTC/ETH spot/perpetual carry, sharing LIVE authorization and risk state.

Spot intents have their own journal: the futures coordinator must never query a
spot order on the futures endpoint. An uncertain mutation is queried, never replayed.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from paperfrog.domain.types import EntryVeto, Fill, Filters, Order, SafeError, State
from paperfrog.execution.coordinator import Coordinator, client_id
from paperfrog.risk.engine import Health, Proposal, RiskEngine
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

if TYPE_CHECKING:
    from .live_broker import LiveBroker

SYMBOLS = ("BTCUSDT", "ETHUSDT")
DAY = 86_400_000
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (ArithmeticError, ValueError, TypeError):
        raise SafeError("invalid carry exchange number") from None


def positions(store: Store) -> dict[str, Decimal]:
    return {
        symbol: -number(c["future_quantity"])
        for symbol, c in store.get("carry_cycles", {}).items()
        if number(c.get("future_quantity", 0)) > 0
    }


def require_order(s: Settings, store: Store, order: Order, now_ms: int) -> None:
    """Exact, fresh, journal-bound permission for the second hedge leg only."""
    c = store.get("carry_cycles", {}).get(order.symbol, {})
    if not (
        s.simple_carry_enabled
        and s.trading_mode == "live"
        and order.symbol in SYMBOLS
        and order.side == -1
        and not order.reduce_only
        and c.get("phase") == "HEDGE"
        and c.get("future_order") == order.model_dump(mode="json")
        and c.get("config_hash") == s.config_hash
        and 0 <= now_ms - c.get("permission_ms", 0) < 5000
        and order.quantity <= number(c.get("spot_quantity", 0))
        and store.get("state") == State.RUNNING
        and store.get("reconciled") is True
        and not store.get("risk_lock")
        and not store.get("manual_lock")
        and not store.get("local_flatten_request")
    ):
        raise EntryVeto("carry hedge permission missing, changed or expired")
    with store.engine.connect() as conn:
        reserved = conn.execute(
            text("SELECT risk,gross FROM entries WHERE id=:i AND symbol=:s AND closed=0"),
            {"i": c.get("id"), "s": order.symbol},
        ).first()
    notional = order.quantity * order.limit_price
    if (
        reserved is None
        or number(reserved.risk) + Decimal("0.00000001") < notional * Decimal("1.03")
        or number(reserved.gross) + Decimal("0.00000001") < 2 * notional
    ):
        raise EntryVeto("carry shared risk reservation missing or insufficient")


def permission_allowed(account: dict[str, Any], symbol: dict[str, Any]) -> bool:
    groups = set(account.get("permissions", []))
    sets = symbol.get("permissionSets")
    if not isinstance(sets, list) or not sets:
        return False
    return all(isinstance(row, list) and bool(groups.intersection(row)) for row in sets)


class Carry:
    def __init__(self, s: Settings, store: Store, broker: LiveBroker):
        if not s.simple_carry_enabled or s.trading_mode != "live":
            raise SafeError("carry requires explicitly enabled LIVE execution")
        if broker.s != s or broker.store is not store or broker.exchange.s != s:
            raise SafeError("carry account, settings or ledger mismatch")
        self.s, self.store, self.broker = s, store, broker
        self.exchange = broker.exchange
        self.risk = RiskEngine(s, store)
        self.coordinator = Coordinator(broker, store, self.risk, self.exchange.filters)
        self.filters: dict[str, Filters] = {}
        self.info: dict[str, Any] = {}
        self.account: dict[str, Any] = {}
        self.depth: dict[str, tuple[Decimal, Decimal]] = {}
        self.last_entry_check = 0
        with store.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS carry_spot_orders "
                    "(id TEXT PRIMARY KEY, intent TEXT NOT NULL, result TEXT)"
                )
            )

    def now(self) -> int:
        return self.broker.now_ms()

    def cycles(self) -> dict[str, Any]:
        return dict(self.store.get("carry_cycles", {}))

    def save(self, symbol: str, cycle: dict[str, Any]) -> None:
        cycles = self.cycles()
        cycles[symbol] = cycle
        self.store.put("carry_cycles", cycles)

    async def preflight(self) -> None:
        self.account = dict(
            await self.exchange.request("GET", "/api/v3/account", signed=True, spot=True)
        )
        permissions = await self.exchange.request(
            "GET", "/sapi/v1/account/apiRestrictions", signed=True, spot=True
        )
        if (
            self.account.get("canTrade") is not True
            or permissions.get("enableSpotAndMarginTrading") is not True
        ):
            raise SafeError("carry requires spot trading permission on the existing API key")
        info = await self.exchange.request(
            "GET", "/api/v3/exchangeInfo", {"symbols": '["BTCUSDT","ETHUSDT"]'}, spot=True
        )
        self.info = {row["symbol"]: row for row in info["symbols"]}
        for symbol in SYMBOLS:
            row = self.info.get(symbol, {})
            if not (
                row.get("status") == "TRADING"
                and row.get("isSpotTradingAllowed") is True
                and permission_allowed(self.account, row)
                and "LIMIT" in row.get("orderTypes", [])
            ):
                raise SafeError("carry BTC/ETH USDT spot trading is unavailable for this account")
            filters = {f["filterType"]: f for f in row["filters"]}
            lot, tick = filters["LOT_SIZE"], filters["PRICE_FILTER"]
            minimum = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
            self.filters[symbol] = Filters(
                number(lot["stepSize"]),
                number(tick["tickSize"]),
                number(lot["minQty"]),
                number(lot["maxQty"]),
                number(minimum["minNotional"]),
            )
            if (
                min(
                    self.filters[symbol].step,
                    self.filters[symbol].tick,
                    self.filters[symbol].min_quantity,
                    self.filters[symbol].max_quantity,
                    self.filters[symbol].min_notional,
                )
                <= 0
            ):
                raise SafeError("carry spot filters invalid")
        if set(self.store.get("simple_positions", {})).intersection(SYMBOLS):
            raise SafeError("carry symbols already belong to another strategy")
        balances = {
            r["asset"]: number(r["free"]) + number(r["locked"]) for r in self.account["balances"]
        }
        for symbol, c in self.cycles().items():
            # A sell may have completed between its durable intent and the
            # response. Resolve that original order before comparing inventory.
            if c.get("spot_close"):
                continue
            if number(c.get("spot_quantity", 0)) > balances.get(symbol[:-4], Decimal(0)):
                raise SafeError("carry spot inventory mismatch; account remains paused")
        for symbol in SYMBOLS:
            if await self.exchange.request(
                "GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True, spot=True
            ):
                raise SafeError("carry spot orders remain open; reconcile before trading")

    async def books(
        self, symbol: str, *, entry: bool = False
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        started = self.now()
        spot = await self.exchange.request(
            "GET", "/api/v3/ticker/bookTicker", {"symbol": symbol}, spot=True
        )
        quote = await self.broker.refresh_quote(symbol)
        sb, sa = number(spot["bidPrice"]), number(spot["askPrice"])
        if entry:
            spot_depth = number(spot["askQty"])
            if spot_depth <= 0 or quote.bid_size <= 0:
                raise SafeError("carry entry liquidity unavailable")
            self.depth[symbol] = (spot_depth, quote.bid_size)
        if not (
            0 < sb <= sa
            and 0 <= self.now() - started < 5000
            and 0 <= self.now() - quote.timestamp_ms < 5000
            and (
                not entry
                or (
                    (sa - sb) / sb <= number(self.s.max_spread)
                    and (quote.ask - quote.bid) / quote.mid <= number(self.s.max_spread)
                )
            )
        ):
            raise SafeError("carry executable quotes unavailable, stale or too wide")
        return sb, sa, quote.bid, quote.ask

    async def spot_order(self, order: Order) -> dict[str, Any]:
        """Persist before POST; after any interruption only query the original ID."""
        with self.store.transaction() as conn:
            row = conn.execute(
                text("SELECT intent,result FROM carry_spot_orders WHERE id=:i"),
                {"i": order.client_id},
            ).first()
            if row:
                if Order.model_validate_json(row.intent) != order:
                    raise SafeError("carry spot order identity collision")
                if row.result:
                    return dict(json.loads(row.result))
                query = True
            else:
                self.filters[order.symbol].validate(order)
                if not order.reduce_only:
                    self.broker.require_authorization()
                    cycle = self.store.get("carry_cycles", {}, conn).get(order.symbol, {})
                    if (
                        self.store.get("state", None, conn) != State.RUNNING
                        or cycle.get("phase") != "SPOT"
                        or cycle.get("config_hash") != self.s.config_hash
                        or cycle.get("spot_order") != order.model_dump(mode="json")
                        or self.store.get("risk_lock", None, conn)
                        or self.store.get("manual_lock", False, conn)
                        or self.store.get("local_flatten_request", None, conn)
                        or not self.store.get("reconciled", False, conn)
                    ):
                        raise EntryVeto("carry spot entry gate closed")
                conn.execute(
                    text("INSERT INTO carry_spot_orders(id,intent) VALUES (:i,:o)"),
                    {"i": order.client_id, "o": order.model_dump_json()},
                )
                query = False
        if query:
            result = await self.exchange.request(
                "GET",
                "/api/v3/order",
                {"symbol": order.symbol, "origClientOrderId": order.client_id},
                signed=True,
                spot=True,
            )
        else:
            result = await self.exchange.request(
                "POST",
                "/api/v3/order",
                {
                    "symbol": order.symbol,
                    "side": "BUY" if order.side == 1 else "SELL",
                    "type": "LIMIT",
                    "timeInForce": "IOC",
                    "quantity": str(order.quantity),
                    "price": str(order.limit_price),
                    "newClientOrderId": order.client_id,
                    "newOrderRespType": "FULL",
                },
                signed=True,
                spot=True,
            )
        if not (
            result.get("symbol") == order.symbol
            and result.get("clientOrderId") == order.client_id
            and result.get("side") == ("BUY" if order.side == 1 else "SELL")
            and result.get("type") == "LIMIT"
            and result.get("timeInForce") == "IOC"
            and number(result.get("origQty")) == order.quantity
            and number(result.get("price")) == order.limit_price
            and result.get("status") in TERMINAL
        ):
            raise SafeError("carry spot terminal order identity not verified")
        quantity, quote = number(result["executedQty"]), number(result["cummulativeQuoteQty"])
        if not (0 <= quantity <= order.quantity and quote >= 0 and (quantity == 0) == (quote == 0)):
            raise SafeError("carry spot execution quantities invalid")
        trades = []
        if quantity:
            trades = await self.exchange.request(
                "GET",
                "/api/v3/myTrades",
                {"symbol": order.symbol, "orderId": result["orderId"], "limit": 1000},
                signed=True,
                spot=True,
            )
            if (
                len(trades) >= 1000
                or not trades
                or any(
                    str(t["orderId"]) != str(result["orderId"])
                    or t["symbol"] != order.symbol
                    or t.get("isBuyer") is not (order.side == 1)
                    for t in trades
                )
                or len({str(t["id"]) for t in trades}) != len(trades)
                or sum((number(t["qty"]) for t in trades), Decimal(0)) != quantity
                or sum((number(t["quoteQty"]) for t in trades), Decimal(0)) != quote
            ):
                raise SafeError("carry spot fills or commissions are incomplete")
        base_fee = Decimal(0)
        quote_fee = Decimal(0)
        third_fee = False
        for trade in trades:
            fee = number(trade["commission"])
            if fee < 0:
                raise SafeError("carry commission invalid")
            if trade["commissionAsset"] == order.symbol[:-4]:
                base_fee += fee
            elif trade["commissionAsset"] == "USDT":
                quote_fee += fee
            elif fee:
                third_fee = True
        # Third-asset fees are charged conservatively at 1% of turnover; never
        # pretend their absence improves returns or the available risk budget.
        cost = quote_fee + (quote * Decimal("0.01") if third_fee else Decimal(0))
        normalized = {
            "quantity": str(quantity),
            "inventory_delta": str(quantity * order.side - base_fee),
            "cash_delta": str(-quote * order.side - cost),
            "status": result["status"],
            "exchange_id": str(result["orderId"]),
            "commissions": [
                {"asset": t["commissionAsset"], "amount": t["commission"]} for t in trades
            ],
        }
        with self.store.transaction() as conn:
            conn.execute(
                text("UPDATE carry_spot_orders SET result=:r WHERE id=:i"),
                {"r": json.dumps(normalized), "i": order.client_id},
            )
            self.store.audit(
                "carry_spot_terminal",
                {"symbol": order.symbol, "side": order.side, **normalized},
                conn,
            )
        return normalized

    async def eligible(self, symbol: str, now: int) -> None:
        rates = await self.exchange.request(
            "GET",
            "/fapi/v1/fundingRate",
            {
                "symbol": symbol,
                "startTime": now - 30 * DAY,
                "endTime": now - 3_600_000,
                "limit": 1000,
            },
        )
        times = [int(r["fundingTime"]) for r in rates]
        if (
            not times
            or len(times) >= 1000
            or times != sorted(set(times))
            or times[0] > now - 30 * DAY + 8 * 3_600_000
            or times[-1] < now - 9 * 3_600_000
            or any(not now - 30 * DAY <= t <= now - 3_600_000 for t in times)
            or any(b - a != 8 * 3_600_000 for a, b in zip(times, times[1:], strict=False))
            or any(r.get("symbol") != symbol for r in rates)
            or sum((number(r["fundingRate"]) for r in rates), Decimal(0)) <= 0
        ):
            raise SafeError("carry requires complete positive prior thirty-day settled funding")
        candles = await self.exchange.request(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": 3}
        )
        boundary = now // DAY * DAY
        completed = [
            r for r in candles if int(r[0]) == boundary - DAY and int(r[6]) == boundary - 1
        ]
        if len(completed) != 1 or number(completed[0][7]) < number(self.s.min_daily_quote_volume):
            raise SafeError("carry prior-day futures liquidity unavailable")

    async def open(self, symbol: str) -> None:
        now = self.now()
        if symbol in self.cycles() or symbol in self.s.denylist:
            return
        await self.eligible(symbol, now)
        sb, sa, fb, fa = await self.books(symbol, entry=True)
        basis = (fb + fa) / (sb + sa) - 1
        if not Decimal("-0.001") <= basis <= Decimal("0.005"):
            raise SafeError("carry basis outside fixed entry interval")
        self.broker.require_authorization()
        await self.broker.refresh()
        equity = self.broker.equity()
        # Reserve the entire spot notional plus a 3% contingency as open risk,
        # rather than assuming perfect hedging during outages or legging.
        cap = min(
            Decimal("0.18"),
            number(self.s.risk_per_trade)
            * number(self.store.get("risk_multiplier", 1))
            / Decimal("1.03"),
            number(self.s.max_target_notional),
            number(self.s.simple_position_margin_cap) * 2,
        )
        sf, ff = self.filters[symbol], self.exchange.filters[symbol]
        # Decimal LCM supports different spot/perpetual lot sizes.
        places = max(-int(sf.step.as_tuple().exponent), -int(ff.step.as_tuple().exponent), 0)
        scale = 10**places
        step = Decimal(math.lcm(int(sf.step * scale), int(ff.step * scale))) / scale
        price = sf.round_price(sa * Decimal("1.001"))
        liquidity = min(self.depth[symbol])
        quantity = (
            min(equity * cap / max(price, fa), liquidity * number(self.s.max_participation)) / step
        ).to_integral_value(rounding=ROUND_DOWN) * step
        spot = Order(
            client_id=client_id(uuid.uuid4().hex, "carry-spot"),
            symbol=symbol,
            side=1,
            quantity=quantity,
            limit_price=price,
        )
        sf.validate(spot)
        future_price = ff.round_price(fb * Decimal("0.999"))
        ff.validate(spot.model_copy(update={"limit_price": future_price}))
        free = {r["asset"]: number(r["free"]) for r in self.account["balances"]}
        if free.get("USDT", Decimal(0)) < quantity * price * Decimal("1.01"):
            raise SafeError("carry requires separately available spot USDT; no automatic transfers")
        if self.broker.available < quantity * fa * Decimal("1.5"):
            raise SafeError("carry requires 150% futures collateral reserve")
        # Only lower leverage on a flat symbol; never increase it or change modes.
        configs = await self.exchange.request(
            "GET", "/fapi/v1/symbolConfig", {"symbol": symbol}, signed=True
        )
        matching = [r for r in configs if r.get("symbol") == symbol]
        if len(matching) != 1 or int(matching[0]["leverage"]) < 2:
            raise SafeError("carry requires existing leverage of at least 2 before reduction")
        if (await self.exchange.positions()).get(symbol):
            raise SafeError("carry entry symbol is not flat")
        if int(matching[0]["leverage"]) > 2:
            await self.exchange.request(
                "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": 2}, signed=True
            )
        await self.exchange.verify_risk_settings((symbol,))
        trade = "carry-" + uuid.uuid4().hex
        c = {
            "id": trade,
            "created_ms": now,
            "phase": "PREPARE",
            "config_hash": self.s.config_hash,
            "spot_order": spot.model_dump(mode="json"),
            "spot_quantity": "0",
            "spot_cash": "0",
            "future_quantity": "0",
            "initial_notional": str(quantity * price),
            "attempt": 0,
        }
        self.save(symbol, c)
        target = quantity * max(price, fa)
        try:
            self.risk.reserve(
                Proposal(
                    trade,
                    symbol,
                    float(equity),
                    float(target),
                    float(2 * target),
                    float(target * Decimal("1.03")),
                    0.0,
                    0.03,
                    0.5,
                    float(quantity / liquidity),
                ),
                Health(True, True, True, (self.now() - now) / 1000, 0),
                datetime.fromtimestamp(now / 1000, UTC),
            )
        except SafeError:
            cycles = self.cycles()
            cycles.pop(symbol, None)
            self.store.put("carry_cycles", cycles)
            raise
        c["phase"] = "SPOT"
        self.save(symbol, c)
        await self.valuation()
        await self.advance(symbol)

    async def advance(self, symbol: str, *, flatten: bool = False) -> None:
        c = self.cycles()[symbol]
        if c["phase"] == "CLOSED":
            return
        if c["phase"] == "PREPARE":
            # A crash between risk reservation and phase persistence cannot
            # authorize a new request. Retire this cycle without submitting.
            with self.store.engine.connect() as conn:
                reserved = conn.execute(
                    text("SELECT id FROM entries WHERE id=:i"), {"i": c["id"]}
                ).scalar()
            if reserved:
                self.risk.closed(c["id"], 0, datetime.fromtimestamp(self.now() / 1000, UTC))
            c["phase"] = "CLOSED"
            self.save(symbol, c)
            return
        if c["phase"] == "SPOT":
            order = Order.model_validate(c["spot_order"])
            with self.store.engine.connect() as conn:
                intended = conn.execute(
                    text("SELECT id FROM carry_spot_orders WHERE id=:i"), {"i": order.client_id}
                ).scalar()
            if not intended and (
                flatten
                or self.store.get("manual_lock")
                or self.store.get("risk_lock")
                or self.store.get("local_flatten_request")
                or self.store.get("state") != State.RUNNING
            ):
                c["phase"] = "CLOSE_SPOT"
                self.save(symbol, c)
        if c["phase"] == "SPOT":
            order = Order.model_validate(c["spot_order"])
            result = await self.spot_order(order)
            c.update(
                spot_quantity=result["inventory_delta"],
                spot_cash=result["cash_delta"],
                phase="HEDGE",
            )
            if number(c["spot_quantity"]) < 0:
                raise SafeError("carry spot acquisition inventory invalid")
            if number(c["spot_quantity"]) != order.quantity:
                c["phase"] = "CLOSE_SPOT"
            self.save(symbol, c)
            await self.valuation()
        if c["phase"] == "HEDGE":
            with self.store.engine.connect() as conn:
                submitted = conn.execute(
                    text("SELECT id FROM orders WHERE id=:i"),
                    {"i": c.get("future_order", {}).get("client_id", "")},
                ).scalar()
            if not submitted:
                try:
                    self.broker.require_authorization()
                except SafeError:
                    c["phase"] = "CLOSE_SPOT"
                    self.save(symbol, c)
        if c["phase"] == "HEDGE":
            if not c.get("future_order"):
                if (
                    flatten
                    or self.store.get("risk_lock")
                    or self.store.get("manual_lock")
                    or self.store.get("state") != State.RUNNING
                ):
                    c["phase"] = "CLOSE_SPOT"
                    self.save(symbol, c)
                else:
                    _, _, fb, _ = await self.books(symbol, entry=True)
                    order = Order(
                        client_id=client_id(c["id"], "future"),
                        symbol=symbol,
                        side=-1,
                        quantity=number(c["spot_quantity"]),
                        limit_price=self.exchange.filters[symbol].round_price(
                            fb * Decimal("0.999")
                        ),
                    )
                    c.update(future_order=order.model_dump(mode="json"), permission_ms=self.now())
                    self.save(symbol, c)
            if c["phase"] == "HEDGE":
                order = Order.model_validate(c["future_order"])
                try:
                    fill = await self.coordinator.submit_once(order)
                    self.verify_fill(order, fill)
                    c["future_quantity"] = str(fill.quantity)
                    c["future_entry"] = str(fill.price)
                    c.setdefault("future_fills", {})[fill.exchange_id] = fill.model_dump(
                        mode="json"
                    )
                    c["abort"] = fill.quantity != order.quantity
                    c["phase"] = "PROTECT" if fill.quantity else "CLOSE_FUTURE"
                except EntryVeto:
                    c["phase"] = "CLOSE_SPOT"
                self.save(symbol, c)
        if c["phase"] == "PROTECT":
            # Persisted stop intent is handled by LiveBroker; it never recreates
            # an ambiguous stop. Until verified, no further entries are allowed.
            stop = self.exchange.filters[symbol].round_price(
                number(c["future_entry"]) * Decimal("1.10")
            )
            triggered = None
            if symbol in self.store.get("live_stops", {}):
                triggered = await self.broker.protection_fill(symbol)
            if triggered is None:
                await self.broker.protect(symbol, -1, stop, client_id(c["id"], "stop"))
                await self.broker.verify_liquidation(0.03)
            await self.account_fills(symbol, c)
            c["phase"] = "CLOSE_FUTURE" if c.get("abort") and triggered is None else "OPEN"
            self.save(symbol, c)
        if c["phase"] == "OPEN":
            protective = await self.broker.protection_fill(symbol)
            if protective is not None:
                if protective.quantity != number(c["future_quantity"]):
                    raise SafeError("carry protective fill quantity mismatch")
                with self.store.transaction() as conn:
                    key = "carry-stop:" + protective.exchange_id + ":" + symbol
                    existing = conn.execute(
                        text("INSERT OR IGNORE INTO events VALUES (:k,:p)"), {"k": key, "p": "{}"}
                    ).rowcount
                    if existing:
                        expected = self.store.get("expected_positions", {}, conn)
                        expected[symbol] = str(
                            number(expected.get(symbol, 0)) + protective.quantity
                        )
                        self.store.put("expected_positions", expected, conn)
                    c.update(future_quantity="0", phase="CLOSE_SPOT")
                    c.setdefault("future_fills", {})[protective.exchange_id] = (
                        protective.model_dump(mode="json")
                    )
                    cycles = self.cycles()
                    cycles[symbol] = c
                    self.store.put("carry_cycles", cycles, conn)
            else:
                sb, _, _, fa = await self.books(symbol)
                pnl = (
                    number(c["spot_cash"])
                    + number(c["spot_quantity"]) * sb
                    + number(c["future_quantity"]) * (number(c["future_entry"]) - fa)
                )
                # Include actual funding and commissions, including negative funding.
                pnl += await self.income(symbol, c)
                pnl += await self.account_fills(symbol, c)
                if (
                    flatten
                    or self.store.get("risk_lock")
                    or self.store.get("local_flatten_request")
                    or self.now() >= c["created_ms"] + 360 * DAY
                    or pnl <= -number(c["initial_notional"]) * Decimal("0.03")
                ):
                    c["phase"] = "CLOSE_FUTURE"
                    self.save(symbol, c)
        if c["phase"] == "CLOSE_FUTURE":
            remaining = number(c["future_quantity"])
            if remaining:
                closing = c.get("future_close")
                if not closing:
                    _, _, _, fa = await self.books(symbol)
                    closing = Order(
                        client_id=client_id(c["id"], f"fc-{c['attempt']}"),
                        symbol=symbol,
                        side=1,
                        quantity=remaining,
                        limit_price=self.exchange.filters[symbol].round_price(fa * Decimal("1.01")),
                        reduce_only=True,
                    ).model_dump(mode="json")
                    c["future_close"] = closing
                    self.save(symbol, c)
                fill = await self.coordinator.submit_once(Order.model_validate(closing))
                self.verify_fill(Order.model_validate(closing), fill)
                if fill.quantity > remaining:
                    raise SafeError("carry futures reduction exceeds inventory")
                c["future_quantity"] = str(remaining - fill.quantity)
                c.setdefault("future_fills", {})[fill.exchange_id] = fill.model_dump(mode="json")
                c.pop("future_close", None)
                c["attempt"] += 1
                self.save(symbol, c)
                if number(c["future_quantity"]):
                    raise SafeError(
                        "carry futures reduction incomplete; retry remaining quantity only"
                    )
            if (await self.exchange.positions()).get(symbol):
                raise SafeError("carry futures close not flat on exchange")
            await self.broker.cancel_protection(symbol)
            c["phase"] = "CLOSE_SPOT"
            self.save(symbol, c)
        if c["phase"] == "CLOSE_SPOT":
            if (await self.exchange.positions()).get(symbol):
                raise SafeError("carry cannot sell spot while its futures exposure remains")
            sf = self.filters[symbol]
            remaining = number(c["spot_quantity"])
            if remaining:
                closing = c.get("spot_close")
                if not closing:
                    sb, _, _, _ = await self.books(symbol)
                    quantity = sf.round_quantity(remaining)
                    # Known dust is retained, valued at zero and blocks reentry.
                    if quantity < sf.min_quantity or quantity * sb < sf.min_notional:
                        c["dust"] = str(remaining)
                        c["spot_quantity"] = "0"
                        self.save(symbol, c)
                    else:
                        closing = Order(
                            client_id=client_id(c["id"], f"sc-{c['attempt']}"),
                            symbol=symbol,
                            side=-1,
                            quantity=quantity,
                            limit_price=sf.round_price(sb * Decimal("0.99")),
                            reduce_only=True,
                        ).model_dump(mode="json")
                        c["spot_close"] = closing
                        self.save(symbol, c)
                if closing:
                    result = await self.spot_order(Order.model_validate(closing))
                    c["spot_quantity"] = str(remaining + number(result["inventory_delta"]))
                    c["spot_cash"] = str(number(c["spot_cash"]) + number(result["cash_delta"]))
                    if number(c["spot_quantity"]) < 0:
                        raise SafeError("carry spot sale exceeded owned inventory")
                    c.pop("spot_close", None)
                    c["attempt"] += 1
                    self.save(symbol, c)
                    if number(c["spot_quantity"]):
                        raise SafeError(
                            "carry spot reduction incomplete; reconcile remaining inventory"
                        )
            c["phase"] = "SETTLE"
            self.save(symbol, c)
        if c["phase"] == "SETTLE":
            pnl = (
                number(c["spot_cash"])
                + await self.income(symbol, c)
                + await self.account_fills(symbol, c)
            )
            self.risk.closed(c["id"], float(pnl), datetime.fromtimestamp(self.now() / 1000, UTC))
            c.update(phase="CLOSED", net_pnl=str(pnl), closed_ms=self.now())
            self.save(symbol, c)
            self.store.audit(
                "carry_closed", {"symbol": symbol, "net_pnl": str(pnl), "dust": c.get("dust", "0")}
            )

    @staticmethod
    def verify_fill(order: Order, fill: Fill) -> None:
        if (
            fill.client_id != order.client_id
            or fill.symbol != order.symbol
            or fill.side != order.side
            or not 0 <= fill.quantity <= order.quantity
            or (fill.quantity > 0 and fill.price <= 0)
        ):
            raise SafeError("carry futures fill does not match original order")

    async def account_fills(self, symbol: str, c: dict[str, Any]) -> Decimal:
        """Archive actual futures fill fees and realized PnL while still queryable."""
        accounted = dict(c.get("future_accounting", {}))
        for key, value in c.get("future_fills", {}).items():
            fill = Fill.model_validate(value)
            if not fill.quantity or key in accounted:
                continue
            rows = await self.exchange.request(
                "GET",
                "/fapi/v1/userTrades",
                {"symbol": symbol, "orderId": key, "limit": 1000},
                signed=True,
            )
            if (
                not rows
                or len(rows) >= 1000
                or len({str(r["id"]) for r in rows}) != len(rows)
                or any(
                    r.get("symbol") != symbol
                    or str(r["orderId"]) != key
                    or r.get("side") != ("BUY" if fill.side == 1 else "SELL")
                    or r.get("commissionAsset") not in {"USDT", "USDC", "BNFCR"}
                    or number(r["commission"]) < 0
                    for r in rows
                )
                or sum((number(r["qty"]) for r in rows), Decimal(0)) != fill.quantity
            ):
                raise SafeError("carry futures fill accounting incomplete")
            accounted[key] = str(
                sum((number(r["realizedPnl"]) - number(r["commission"]) for r in rows), Decimal(0))
            )
        c["future_accounting"] = accounted
        self.save(symbol, c)
        return sum((number(v) for v in accounted.values()), Decimal(0))

    async def income(self, symbol: str, c: dict[str, Any]) -> Decimal:
        end = self.now()
        # Binance's income history is finite. Archive continuously and refuse
        # to invent settlements after an unobserved absence longer than a week.
        start = max(c["created_ms"], c.get("income_checked_ms", c["created_ms"]) - DAY)
        if end - start > 7 * DAY:
            raise SafeError("carry settlement history gap requires local accounting review")
        saved = dict(c.get("income_rows", {}))
        page = 1
        while True:
            rows = await self.exchange.request(
                "GET",
                "/fapi/v1/income",
                {"symbol": symbol, "startTime": start, "endTime": end, "limit": 1000, "page": page},
                signed=True,
            )
            for row in rows:
                if (
                    row.get("symbol") != symbol
                    or row.get("asset") not in {"USDT", "USDC", "BNFCR"}
                    or not start <= int(row["time"]) <= end
                ):
                    raise SafeError("carry funding and fee ledger incomplete or unsupported")
                if row.get("incomeType") in {"FUNDING_FEE", "COMMISSION", "REALIZED_PNL"}:
                    key = str(row["tranId"]) + ":" + row["incomeType"]
                    value = str(number(row["income"]))
                    if key in saved and saved[key] != value:
                        raise SafeError("carry historical settlement changed")
                    saved[key] = value
            if len(rows) < 1000:
                break
            page += 1
            if page > 20:
                raise SafeError("carry settlement pagination exceeds bounded history")
        c.update(income_rows=saved, income_checked_ms=end)
        self.save(symbol, c)
        return sum(
            (number(value) for key, value in saved.items() if key.endswith(":FUNDING_FEE")),
            Decimal(0),
        )

    async def valuation(self) -> None:
        adjustment = Decimal(0)
        for symbol, c in self.cycles().items():
            adjustment += number(c.get("spot_cash", 0))
            if number(c.get("spot_quantity", 0)):
                sb, _, _, _ = await self.books(symbol)
                adjustment += number(c["spot_quantity"]) * sb
        self.store.put("carry_valuation", {"spot_pnl": str(adjustment), "timestamp_ms": self.now()})

    async def tick(self, *, entries: bool = True, flatten: bool = False) -> None:
        try:
            await self.preflight()
            for symbol in self.cycles():
                await self.advance(symbol, flatten=flatten)
            await self.valuation()
            if (
                entries
                and self.store.get("state") == State.RUNNING
                and not self.store.get("manual_lock")
                and not self.store.get("risk_lock")
                and not self.store.get("local_flatten_request")
                and not self.store.get("profit_entry_lock")
                and self.now() - self.last_entry_check >= 3_600_000
            ):
                self.last_entry_check = self.now()
                for symbol in SYMBOLS:
                    if symbol not in self.cycles():
                        try:
                            await self.open(symbol)
                        except (SafeError, ValueError):
                            if (
                                symbol in self.cycles()
                                and self.cycles()[symbol]["phase"] != "CLOSED"
                            ):
                                raise
                            self.store.audit(
                                "carry_entry_blocked",
                                {
                                    "symbol": symbol,
                                    "reason": "funding, basis, funds, filters or shared risk gates",
                                },
                            )
                await self.valuation()
            self.store.put(
                "carry_status",
                {
                    "enabled": True,
                    "mode": "live",
                    "state": "ready",
                    "checked_ms": self.now(),
                    "cycles": {k: v["phase"] for k, v in self.cycles().items()},
                },
            )
        except (SafeError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            reason = (
                str(exc) if isinstance(exc, SafeError) else "carry exchange data validation failed"
            )
            self.store.put(
                "carry_status",
                {
                    "enabled": True,
                    "mode": "live",
                    "state": "blocked",
                    "reason": reason,
                    "checked_ms": self.now(),
                },
            )
            if any(c["phase"] != "CLOSED" for c in self.cycles().values()):
                self.store.put("reconciled", False)
                self.store.transition(State.ERROR_SAFE, reason)
                raise SafeError(reason) from None
            # An unavailable spot account blocks carry alone, before any mutation.
            await self.valuation()
