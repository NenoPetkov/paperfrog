from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from paperfrog.domain.types import (
    Broker,
    Fill,
    Filters,
    Order,
    Quote,
    SafeError,
    State,
    UnknownOrder,
)
from paperfrog.risk.engine import Health, Proposal, RiskEngine
from paperfrog.storage.database import Store


def client_id(trade_id: str, role: str) -> str:
    return "pf-" + hashlib.sha256(f"{trade_id}:{role}".encode()).hexdigest()[:30]


class Coordinator:
    def __init__(self, broker: Broker, store: Store, risk: RiskEngine, filters: dict[str, Filters]):
        self.broker, self.store, self.risk, self.filters = broker, store, risk, filters
        self._mutex = asyncio.Lock()

    async def submit_once(self, order: Order) -> Fill:
        with self.store.transaction() as conn:
            row = (
                conn.execute(text("SELECT * FROM orders WHERE id=:i"), {"i": order.client_id})
                .mappings()
                .first()
            )
            if row:
                if json.loads(row["intent"]) != order.model_dump(mode="json"):
                    raise SafeError("client ID collision with different intent")
                if row["result"]:
                    return Fill.model_validate_json(row["result"])
                query_only = True
            else:
                if not order.reduce_only and (
                    self.store.get("state", State.PAUSED, conn) != State.RUNNING
                    or self.store.get("risk_lock", None, conn)
                    or self.store.get("manual_lock", False, conn)
                    or not self.store.get("reconciled", False, conn)
                ):
                    raise SafeError("execution entry gate closed")
                query_only = False
                conn.execute(
                    text("INSERT INTO orders(id,symbol,intent,state) VALUES (:i,:s,:v,:st)"),
                    {
                        "i": order.client_id,
                        "s": order.symbol,
                        "v": order.model_dump_json(),
                        "st": "INTENDED",
                    },
                )
                self.store.audit("order_intended", order.model_dump(mode="json"), conn)
        try:
            fill = (
                await self.broker.query(order.symbol, order.client_id)
                if query_only
                else await self.broker.submit(order)
            )
        except SafeError as failure:
            with self.store.transaction() as conn:
                conn.execute(
                    text("UPDATE orders SET state=:s WHERE id=:i"),
                    {"s": "UNKNOWN", "i": order.client_id},
                )
                self.store.transition(State.ERROR_SAFE, str(failure), conn)
                self.store.put("reconciled", False, conn)
                self.store.audit("unknown_order", {"client_id": order.client_id}, conn)
            raise
        if fill.status not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:
            self.store.transition(State.ERROR_SAFE, "order not terminal; reconciliation required")
            raise UnknownOrder("order not terminal; reconcile before continuing")
        with self.store.transaction() as conn:
            conn.execute(
                text("UPDATE orders SET result=:r,state=:s WHERE id=:i"),
                {"r": fill.model_dump_json(), "s": fill.status, "i": order.client_id},
            )
            inserted = conn.execute(
                text("INSERT OR IGNORE INTO fills VALUES (:i,:p)"),
                {"i": fill.symbol + ":" + fill.exchange_id, "p": fill.model_dump_json()},
            ).rowcount
            if inserted:
                positions = self.store.get("expected_positions", {}, conn)
                positions[fill.symbol] = str(
                    Decimal(positions.get(fill.symbol, "0")) + fill.quantity * fill.side
                )
                self.store.put("expected_positions", positions, conn)
                self.store.audit("order_terminal", fill.model_dump(mode="json"), conn)
        return fill

    async def enter(
        self,
        proposal: Proposal,
        orders: list[Order],
        health: Health,
        now: datetime,
        stop_fraction: float,
    ) -> list[Fill]:
        return await self._enter(proposal, orders, health, now, stop_fraction)

    async def enter_single_paper(
        self,
        proposal: Proposal,
        order: Order,
        health: Health,
        now: datetime,
        stop_fraction: float,
    ) -> list[Fill]:
        """Isolated directional research. Exchange brokers cannot use this entry point."""
        from paperfrog.paper.broker import PaperBroker

        if self.risk.s.trading_mode != "paper" or not isinstance(self.broker, PaperBroker):
            raise SafeError("single-leg experiment is PAPER-only")
        return await self._enter(proposal, [order], health, now, stop_fraction, single_paper=True)

    async def enter_single_exchange(
        self,
        proposal: Proposal,
        order: Order,
        health: Health,
        now: datetime,
        stop_fraction: float,
    ) -> list[Fill]:
        from paperfrog.simple.live_broker import LiveBroker
        from paperfrog.simple.strategy import require_execution

        require_execution(self.risk.s)
        if self.risk.s.trading_mode == "paper" or not isinstance(self.broker, LiveBroker):
            raise SafeError("single exchange execution requires the authorized live broker")
        self.broker.require_authorization()
        return await self._enter(
            proposal, [order], health, now, stop_fraction, single_exchange=True
        )

    async def _enter(
        self,
        proposal: Proposal,
        orders: list[Order],
        health: Health,
        now: datetime,
        stop_fraction: float,
        single_paper: bool = False,
        single_exchange: bool = False,
    ) -> list[Fill]:
        async with self._mutex:
            if single_paper:
                from paperfrog.paper.broker import PaperBroker

                if self.risk.s.trading_mode != "paper" or not isinstance(self.broker, PaperBroker):
                    raise SafeError("single-leg experiment is PAPER-only")
            if single_exchange:
                from paperfrog.simple.live_broker import LiveBroker

                if not isinstance(self.broker, LiveBroker):
                    raise SafeError("exchange broker mismatch")
                self.broker.require_authorization()
            if (len(orders) != 1 if single_paper or single_exchange else len(orders) < 2) or any(
                o.reduce_only for o in orders
            ):
                raise SafeError("paired entry requires target and hedge")
            if (
                len({o.symbol for o in orders}) != len(orders)
                or orders[0].symbol != proposal.symbol
            ):
                raise SafeError("invalid paired leg identities")
            gross = sum(float(o.quantity * o.limit_price) for o in orders)
            target = float(orders[0].quantity * orders[0].limit_price)
            if (
                gross > proposal.gross + 1e-8
                or target > proposal.target + 1e-8
                or stop_fraction != proposal.stop_fraction
            ):
                raise SafeError("orders exceed approved proposal")
            for o in orders:
                self.filters[o.symbol].validate(o)
            self.risk.reserve(proposal, health, now)
            self.store.put(
                "trade:" + proposal.trade_id,
                {
                    "orders": [o.model_dump(mode="json") for o in orders],
                    "fills": [],
                    "opened_at": now.isoformat(),
                    "stop_fraction": stop_fraction,
                    "risk": proposal.risk,
                    "partial_taken": False,
                    "execution_complete": False,
                },
            )
            filled: list[Fill] = []
            try:
                # Sequential with hosted protection after each fill bounds naked time.
                async with asyncio.timeout(8):
                    for index, order in enumerate(orders):
                        f = await self.submit_once(order)
                        filled.append(f)
                        journal = self.store.get("trade:" + proposal.trade_id)
                        journal["fills"] = [x.model_dump(mode="json") for x in filled]
                        self.store.put("trade:" + proposal.trade_id, journal)
                        if f.quantity:
                            trigger = self.filters[f.symbol].round_price(
                                f.price * (1 - Decimal(str(stop_fraction)) * f.side)
                            )
                            await self.broker.protect(
                                f.symbol,
                                f.side,
                                trigger,
                                client_id(proposal.trade_id, f"stop-{index}"),
                            )
                        if f.quantity != order.quantity:
                            raise SafeError("partial pair; unwind confirmed fills")
                    if single_exchange:
                        from paperfrog.simple.live_broker import LiveBroker

                        if isinstance(self.broker, LiveBroker):
                            await self.broker.verify_liquidation(stop_fraction)
                self.store.put(
                    "trade:" + proposal.trade_id,
                    {
                        "orders": [o.model_dump(mode="json") for o in orders],
                        "fills": [f.model_dump(mode="json") for f in filled],
                        "opened_at": now.isoformat(),
                        "stop_fraction": stop_fraction,
                        "risk": proposal.risk,
                        "partial_taken": False,
                        "execution_complete": True,
                    },
                )
                return filled
            except (SafeError, TimeoutError) as failure:
                if isinstance(failure, TimeoutError):
                    self.store.put("reconciled", False)
                # Unknown orders require a fresh position reconciliation before any repair.
                self.store.transition(
                    State.ERROR_SAFE,
                    str(failure)
                    if isinstance(failure, SafeError)
                    else "entry protection deadline exceeded",
                )
                if self.store.get("reconciled", False):
                    for f in reversed(filled):
                        if f.quantity:
                            price = self.filters[f.symbol].round_price(
                                f.price * (Decimal(".98") if f.side > 0 else Decimal("1.02"))
                            )
                            await self.submit_once(
                                Order(
                                    client_id=client_id(proposal.trade_id, "unwind-" + f.symbol),
                                    symbol=f.symbol,
                                    side=-f.side,
                                    quantity=f.quantity,
                                    limit_price=price,
                                    reduce_only=True,
                                )
                            )
                    actual = await self.broker.positions()
                    if await self.reconcile() and not any(actual.get(f.symbol) for f in filled):
                        entry_fees = sum(float(f.fee) for f in filled)
                        unwind_rows = [
                            r
                            for r in self.store.rows("orders", 100000)
                            if r["id"]
                            in {client_id(proposal.trade_id, "unwind-" + f.symbol) for f in filled}
                            and r["result"]
                        ]
                        pnl = -entry_fees
                        for row in unwind_rows:
                            close_fill = Fill.model_validate_json(row["result"])
                            initial = next(f for f in filled if f.symbol == close_fill.symbol)
                            pnl += float(
                                (close_fill.price - initial.price)
                                * close_fill.quantity
                                * initial.side
                                - close_fill.fee
                            )
                        self.risk.closed(proposal.trade_id, pnl, now)
                        if single_exchange:
                            for f in filled:
                                await self.broker.cancel_protection(f.symbol)
                            self.store.audit(
                                "simple_entry_aborted",
                                {
                                    "symbol": proposal.symbol,
                                    "reason": str(failure)
                                    if isinstance(failure, SafeError)
                                    else "entry protection deadline exceeded",
                                    "flat_verified": True,
                                },
                            )
                            if not self.store.get("risk_lock") and not self.store.get(
                                "manual_lock"
                            ):
                                self.store.transition(
                                    State.DEGRADED,
                                    "failed entry unwound; flat account verified; checking fresh data before retry",
                                )
                raise

    async def reconcile(self) -> bool:
        actual = await self.broker.positions()
        expected = {
            k: Decimal(v) for k, v in self.store.get("expected_positions", {}).items() if Decimal(v)
        }
        consistent = True
        unresolved = self.store.unresolved_orders()
        for row in unresolved:
            try:
                await self.submit_once(Order.model_validate_json(row["intent"]))
            except SafeError:
                consistent = False
        if unresolved:
            actual = await self.broker.positions()
            expected = {
                k: Decimal(v)
                for k, v in self.store.get("expected_positions", {}).items()
                if Decimal(v)
            }
        consistent = consistent and actual == expected
        self.store.put("reconciled", consistent)
        if not consistent:
            self.store.transition(
                State.ERROR_SAFE, "exchange positions or orders do not match the journal"
            )
        self.store.audit(
            "reconciliation",
            {"consistent": consistent, "actual": {k: str(v) for k, v in actual.items()}},
        )
        return consistent

    async def flatten(self, quotes: dict[str, Quote], operation: str) -> bool:
        async with self._mutex:
            self.store.transition(State.EMERGENCY_FLATTENING, "operator requested emergency close")
            self.store.put("manual_lock", True)
            await self.broker.cancel_entries()
            # Resolve all ambiguous submissions first. Unknown external positions may be reduced
            # after an authoritative position snapshot, but never adopted for new entries.
            unresolved = self.store.unresolved_orders()
            for row in unresolved:
                await self.submit_once(Order.model_validate_json(row["intent"]))
            positions = await self.broker.positions()
            for symbol, qty in positions.items():
                q = quotes.get(symbol)
                if q is None:
                    raise SafeError("no reliable quote for emergency reduction")
                side = -1 if qty > 0 else 1
                limit = self.filters[symbol].round_price(
                    q.bid * Decimal(".98") if side < 0 else q.ask * Decimal("1.02")
                )
                await self.submit_once(
                    Order(
                        client_id=client_id(operation, symbol),
                        symbol=symbol,
                        side=side,
                        quantity=abs(qty),
                        limit_price=limit,
                        reduce_only=True,
                    )
                )
            closed = not await self.broker.positions()
            if closed:
                for symbol in positions:
                    await self.broker.cancel_protection(symbol)
                self.store.put("expected_positions", {})
                self.store.put("reconciled", True)
            self.store.transition(
                State.SOFT_RISK_LOCK if closed else State.ERROR_SAFE,
                "emergency close verified; manual pause active"
                if closed
                else "emergency close incomplete",
            )
            self.store.audit("flatten_result", {"closed": closed, "operation": operation})
            return closed

    async def reduce(self, quotes: dict[str, Quote], fraction: float, operation: str) -> None:
        if not 0 < fraction < 1:
            raise SafeError("partial reduction must be between zero and one")
        async with self._mutex:
            await self.broker.cancel_entries()
            positions = await self.broker.positions()
            for symbol, qty in positions.items():
                q = quotes[symbol]
                quantity = self.filters[symbol].round_quantity(abs(qty) * Decimal(str(fraction)))
                if quantity == 0:
                    continue
                price = self.filters[symbol].round_price(
                    q.bid * Decimal(".98") if qty > 0 else q.ask * Decimal("1.02")
                )
                await self.submit_once(
                    Order(
                        client_id=client_id(operation, symbol),
                        symbol=symbol,
                        side=-1 if qty > 0 else 1,
                        quantity=quantity,
                        limit_price=price,
                        reduce_only=True,
                    )
                )
            await self.reconcile()
