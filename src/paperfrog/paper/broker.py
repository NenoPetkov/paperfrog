from __future__ import annotations

import asyncio
import hashlib
import random
from decimal import Decimal

from paperfrog.domain.types import Fill, Filters, Order, Quote, SafeError, UnknownOrder
from paperfrog.storage.database import Store


class PaperBroker:
    def __init__(
        self,
        store: Store,
        filters: dict[str, Filters],
        equity: Decimal = Decimal("10000"),
        seed: int = 42,
        fee: Decimal = Decimal(".0005"),
        slippage_bps: float = 2,
        latency_ms: int = 0,
        rejection_rate: float = 0,
    ):
        self.store, self.filters = store, filters
        self.rng = random.Random(seed)
        self.fee, self.slippage_bps, self.latency_ms = fee, slippage_bps, latency_ms
        self.rejection_rate = rejection_rate
        self.quotes: dict[str, Quote] = {}
        self.connected = True
        self.clock_ms = 0
        if self.store.get("paper_account") is None:
            self.store.put(
                "paper_account",
                {
                    "cash": str(equity),
                    "positions": {},
                    "average": {},
                    "stops": {},
                    "orders": {},
                    "funding": {},
                },
            )

    async def positions(self) -> dict[str, Decimal]:
        return {
            k: Decimal(v) for k, v in self.store.paper_summary()["positions"].items() if Decimal(v)
        }

    def equity(self) -> Decimal:
        a = self.store.paper_summary()
        value = Decimal(a["cash"])
        for symbol, quantity in a["positions"].items():
            if Decimal(quantity):
                if symbol not in self.quotes:
                    raise SafeError("mark missing for paper equity")
                value += Decimal(quantity) * (
                    self.quotes[symbol].mark - Decimal(a["average"][symbol])
                )
        return value

    async def quote(self, q: Quote) -> list[Fill]:
        if q.symbol in self.quotes and q.timestamp_ms < self.quotes[q.symbol].timestamp_ms:
            raise SafeError("out-of-order paper quote")
        self.clock_ms = max(self.clock_ms, q.timestamp_ms)
        self.quotes[q.symbol] = q
        triggered = []
        a = self.store.paper_summary()
        for key, stop in list(a["stops"].items()):
            if stop["symbol"] != q.symbol:
                continue
            quantity = Decimal(a["positions"].get(q.symbol, "0"))
            if not quantity:
                continue
            if (stop["side"] > 0 and q.mark <= Decimal(stop["trigger"])) or (
                stop["side"] < 0 and q.mark >= Decimal(stop["trigger"])
            ):
                # Hosted protection operates even when the client connection is down.
                order = Order(
                    client_id="pf-"
                    + hashlib.sha256(f"{key}:{q.timestamp_ms}".encode()).hexdigest()[:30],
                    symbol=q.symbol,
                    side=-1 if quantity > 0 else 1,
                    quantity=abs(quantity),
                    limit_price=self.filters[q.symbol].round_price(
                        q.bid * Decimal(".8") if quantity > 0 else q.ask * Decimal("1.2")
                    ),
                    reduce_only=True,
                )
                triggered.append(self._execute(order, hosted=True))
        return triggered

    async def submit(self, order: Order) -> Fill:
        if not self.connected:
            raise UnknownOrder("paper connection unavailable; query after reconnect")
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        return self._execute(order)

    def _execute(self, order: Order, hosted: bool = False) -> Fill:
        self.filters[order.symbol].validate(order)
        with self.store.transaction() as conn:
            a = self.store.get("paper_account", conn=conn)
            if order.client_id in a["orders"]:
                return Fill.model_validate(a["orders"][order.client_id])
            q = self.quotes.get(order.symbol)
            if q is None or self.clock_ms - q.timestamp_ms > 60000:
                raise SafeError("paper quote missing or stale")
            old = Decimal(a["positions"].get(order.symbol, "0"))
            requested = order.quantity
            if order.reduce_only:
                if old * order.side >= 0:
                    raise SafeError("reduce-only order would increase exposure")
                requested = min(requested, abs(old))
            visible = q.ask_size if order.side > 0 else q.bid_size
            quantity = self.filters[order.symbol].round_quantity(min(requested, visible))
            participation = float(requested / max(visible, Decimal(".00000001")))
            impact = Decimal(str(self.slippage_bps / 10000 * (1 + participation)))
            price = (q.ask * (1 + impact)) if order.side > 0 else (q.bid * (1 - impact))
            executable = (
                price <= order.limit_price if order.side > 0 else price >= order.limit_price
            )
            if not executable or (not hosted and self.rng.random() < self.rejection_rate):
                quantity = Decimal(0)
            fee = quantity * price * self.fee
            fill = Fill(
                client_id=order.client_id,
                symbol=order.symbol,
                side=order.side,
                quantity=quantity,
                price=price if quantity else Decimal(0),
                fee=fee,
                status="FILLED" if quantity == order.quantity else "EXPIRED",
                exchange_id=order.client_id,
            )
            average = Decimal(a["average"].get(order.symbol, "0"))
            cash = Decimal(a["cash"]) - fee
            if old * order.side < 0:
                closed = min(abs(old), quantity)
                cash += closed * (price - average) * (1 if old > 0 else -1)
            new = old + quantity * order.side
            if old * order.side >= 0 and new:
                average = (abs(old) * average + quantity * price) / abs(new)
            elif new * old < 0:
                average = price
            a["cash"], a["positions"][order.symbol], a["average"][order.symbol] = (
                str(cash),
                str(new),
                str(average),
            )
            a["orders"][order.client_id] = fill.model_dump(mode="json")
            if not new:
                a["stops"] = {k: s for k, s in a["stops"].items() if s["symbol"] != order.symbol}
            self.store.put("paper_account", a, conn)
            self.store.audit("paper_fill", fill.model_dump(mode="json"), conn)
            return fill

    async def query(self, symbol: str, client_id: str) -> Fill:
        if not self.connected:
            raise SafeError("paper disconnected")
        value = self.store.get("paper_account")["orders"].get(client_id)
        if not value or value["symbol"] != symbol:
            raise SafeError("paper order not found")
        return Fill.model_validate(value)

    async def cancel_entries(self) -> None:
        # IOC orders are terminal by the time submit returns; none rest on the book.
        self.store.audit("cancel_entries", {"broker": "paper", "resting_entries": 0})

    async def protect(self, symbol: str, side: int, trigger: Decimal, key: str) -> None:
        with self.store.transaction() as conn:
            a = self.store.get("paper_account", conn=conn)
            quantity = Decimal(a["positions"].get(symbol, "0"))
            if quantity * side <= 0 or trigger <= 0:
                raise SafeError("invalid protective stop")
            a["stops"][key] = {"symbol": symbol, "side": side, "trigger": str(trigger)}
            self.store.put("paper_account", a, conn)
            self.store.audit("stop_placed", {"symbol": symbol, "key": key}, conn)

    def funding(self, symbol: str, timestamp: int, rate: Decimal, mark: Decimal) -> Decimal:
        key = f"{symbol}:{timestamp}"
        with self.store.transaction() as conn:
            a = self.store.get("paper_account", conn=conn)
            if key in a["funding"]:
                return Decimal(0)
            pnl = -Decimal(a["positions"].get(symbol, "0")) * mark * rate
            a["cash"] = str(Decimal(a["cash"]) + pnl)
            a["funding"][key] = str(pnl)
            self.store.put("paper_account", a, conn)
            self.store.audit(
                "funding", {"symbol": symbol, "timestamp": timestamp, "pnl": str(pnl)}, conn
            )
            return pnl

    async def cancel_protection(self, symbol: str) -> None:
        with self.store.transaction() as conn:
            account = self.store.get("paper_account", conn=conn)
            if Decimal(account["positions"].get(symbol, "0")):
                raise SafeError("cannot remove protection from an open position")
            account["stops"] = {k: v for k, v in account["stops"].items() if v["symbol"] != symbol}
            self.store.put("paper_account", account, conn)
