from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection

from paperfrog.domain.types import Fill, Filters, Order, Quote, SafeError, State
from paperfrog.execution.coordinator import Coordinator, client_id
from paperfrog.paper.broker import PaperBroker
from paperfrog.risk import activity
from paperfrog.risk.engine import Health, Proposal, RiskEngine
from paperfrog.risk.sizing import equity_margin_bounds, require_equity_margin_allocation
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from . import confidence, learning, rotation
from .adaptive import active_plan
from .adaptive import signal as adaptive_signal
from .authorization import verify_simple_arm
from .bitcoin import BTC
from .bitcoin import entry_gate as btc_entry_gate
from .journal import execution_cost, indicators, observe, reference
from .live_broker import LiveBroker
from .market_trend import entry_gate as market_entry_gate
from .minute import MINUTE_MS
from .minute import direction as minute_direction
from .minute import veto as minute_veto
from .strategy import (
    BAR_MS,
    RULES,
    Rules,
    entry,
    execution_rules,
    exit_reason,
    opposite_entry,
    require_execution,
    require_paper,
    strategy_name,
    tighten_stop,
)


class Engine:
    """Shared directional logic for replay, PAPER and authorized exchange execution."""

    def __init__(
        self,
        s: Settings,
        store: Store,
        filters: dict[str, Filters],
        rules: Rules | None = None,
        broker: LiveBroker | None = None,
    ):
        rules = execution_rules(s) if rules is None else rules
        if broker is None:
            require_paper(s)
        else:
            require_execution(s)
            if broker.s != s or rules != execution_rules(s):
                raise SafeError(
                    "live execution requires current default rules and matching account"
                )
        self.rules = rules
        self.strategy = strategy_name(s.simple_entry_mode, rules)
        if (
            store.get("simple_positions")
            and store.get("simple_entry_mode", "trend") != s.simple_entry_mode
        ):
            raise SafeError("close existing PAPER positions before changing strategy mode")
        public_rules = {
            **rules.public(),
            "version": self.strategy,
            "entry_mode": s.simple_entry_mode,
        }
        stored_rules = store.get("simple_rules", public_rules)
        # Older default PAPER journals predate research fields; restore their same rules.
        defaults = {
            key: RULES.public()[key]
            for key in (
                "minimum_stop_fraction",
                "maximum_stop_fraction",
                "entry_style",
                "allowed_side",
                "warmup_bars",
                "atr_timeframe_bars",
            )
        }
        if store.get("simple_positions") and {**defaults, **stored_rules} != public_rules:
            raise SafeError("close existing PAPER positions before changing strategy rules")
        self.s, self.store, self.filters = s, store, filters
        self.risk = RiskEngine(
            s,
            store,
            profit_guard_scale=(
                max(1.0, s.risk_per_trade / 0.002) if s.simple_sizing_mode == "risk" else 1.0
            ),
        )
        self.broker: PaperBroker | LiveBroker = (
            broker
            if broker is not None
            else PaperBroker(
                store,
                filters,
                Decimal(str(s.paper_equity)),
                s.paper_seed,
                Decimal(str(s.fee_rate)),
                s.slippage_bps,
                0,
            )
        )
        self.coordinator = Coordinator(self.broker, store, self.risk, filters)
        self.store.put("simple_rules", public_rules)
        self.store.put("simple_entry_mode", s.simple_entry_mode)
        self.store.put("simple_entry_profile", s.simple_entry_profile)

    @property
    def entry_timeframe(self) -> str:
        return rotation.timeframe(self.store, self.s)

    @property
    def entry_bar_ms(self) -> int:
        return {"1m": 60_000, "5m": 300_000, "8m": 480_000, "15m": BAR_MS}[self.entry_timeframe]

    def positions(self) -> dict[str, dict[str, Any]]:
        return dict(self.store.get("simple_positions", {}))

    def notice(self, key: str, message: str, conn: Connection | None = None) -> None:
        notices = self.store.get("simple_notices", {}, conn)
        notices[key] = message
        self.store.put("simple_notices", notices, conn)
        self.store.audit("simple_explanation", {"message": message}, conn)

    def health(self, now_ms: int) -> Health:
        if isinstance(self.broker, LiveBroker):
            # Network calls can refresh the account after the caller sampled its clock.
            # Compare both snapshots with the current exchange clock, never an old loop time.
            now_ms = self.broker.now_ms()
        symbols = set(self.positions()) | set(self.store.get("simple_selected", []))
        ages = [
            (now_ms - self.broker.quotes[symbol].timestamp_ms) / 1000
            if symbol in self.broker.quotes
            else float("inf")
            for symbol in symbols
        ]
        # model_valid means frozen strategy rules; exchange authorization is checked separately.
        return Health(
            bool(self.store.get("reconciled")),
            True,
            bool(self.filters),
            max(ages, default=float("inf")),
            (now_ms - self.broker.account_at_ms) / 1000
            if isinstance(self.broker, LiveBroker)
            else 0,
        )

    async def restore(self, *, recover_state: bool = True) -> None:
        if isinstance(self.broker, LiveBroker):
            # Reconcile uncertain IOC submissions without ever submitting them again.
            if self.store.unresolved_orders() and not await self.coordinator.reconcile():
                raise SafeError("unresolved startup execution; account remains paused")
            for symbol, intent in self.store.get("simple_pending", {}).items():
                if symbol in self.positions():
                    continue
                journal = self.store.get("trade:" + intent["id"], {})
                if journal.get("execution_complete") is not True:
                    journal = await self.recover_pending_entry(symbol, intent, journal)
                if (
                    journal.get("execution_complete") is not True
                    or len(journal.get("fills", [])) != 1
                ):
                    raise SafeError(
                        f"incomplete execution journal for {symbol}; verify original order "
                        "and hosted protection before recovery; account remains paused"
                    )
                fill = Fill.model_validate(journal["fills"][0])
                if fill.quantity <= 0 or fill.price <= 0:
                    raise SafeError("invalid journaled entry fill; account remains paused")
                self.record_entry(symbol, fill, intent)
            await self.recover_closings()
            if self.positions():
                await self.broker.refresh_funding(self.broker.now_ms())
            for symbol in list(self.positions()):
                stop_fill = await self.broker.stop_fill(symbol)
                if stop_fill is not None:
                    self.record_stop(stop_fill, self.broker.now_ms())
                    await self.broker.cancel_protection(symbol)
        positions = await self.broker.positions()
        journals = self.positions()
        if set(positions) != set(journals):
            raise SafeError("simple position journal mismatch; manual review required")
        for symbol, trade in journals.items():
            if positions[symbol] != Decimal(trade["quantity"]) * trade["side"]:
                raise SafeError("simple position quantity mismatch")
            await self.broker.protect(
                symbol,
                trade["side"],
                Decimal(str(trade["stop_price"])),
                client_id(trade["id"], "stop-0"),
            )
        if not await self.coordinator.reconcile():
            raise SafeError("simple startup reconciliation failed")
        if recover_state:
            lock = self.store.get("risk_lock")
            self.store.transition(
                lock["state"] if lock else State.PAUSED,
                lock["reason"]
                if lock
                else "startup reconciliation complete; waiting for fresh market data",
            )

    async def recover_pending_entry(
        self, symbol: str, intent: dict[str, Any], journal: dict[str, Any]
    ) -> dict[str, Any]:
        """Verify a filled entry and its original stop after a protection timeout.

        Only reads of original exchange identities are allowed here. An absent,
        partial or mismatched execution/protection never authorizes a new entry.
        """
        assert isinstance(self.broker, LiveBroker)
        if len(journal.get("orders", [])) != 1 or len(journal.get("fills", [])) > 1:
            raise SafeError("incomplete entry identities; account remains paused")
        order = Order.model_validate(journal["orders"][0])
        if (
            order.reduce_only
            or order.symbol != symbol
            or order.side != intent["side"]
            or order.client_id != client_id(intent["id"], symbol)
            or journal.get("entry_vetoed_before_submission")
        ):
            raise SafeError("pending entry identity mismatch; account remains paused")
        if journal.get("fills"):
            saved = Fill.model_validate(journal["fills"][0])
            if saved.quantity <= 0 or saved.price <= 0:
                raise SafeError("invalid journaled entry fill; account remains paused")
        fill = await self.broker.query(symbol, order.client_id)
        if (
            fill.client_id != order.client_id
            or fill.symbol != symbol
            or fill.side != order.side
            or fill.status != "FILLED"
            or fill.quantity != order.quantity
            or fill.quantity <= 0
            or fill.price <= 0
            or (
                journal.get("fills")
                and Fill.model_validate(journal["fills"][0]) != fill
            )
        ):
            raise SafeError("original entry fill is incomplete or changed; account remains paused")
        stop = self.store.get("live_stops", {}).get(symbol, {})
        trigger = self.filters[symbol].round_price(
            fill.price * (1 - Decimal(str(intent["stop_fraction"])) * fill.side)
        )
        if (
            stop.get("key") != client_id(intent["id"], "stop-0")
            or stop.get("side") != fill.side
            or Decimal(stop.get("trigger", "0")) != trigger
        ):
            raise SafeError("original entry protection missing or changed; account remains paused")
        stop_fill = await self.broker.protection_fill(symbol)
        actual = (await self.broker.positions()).get(symbol, Decimal(0))
        if stop_fill is None:
            if actual != fill.quantity * fill.side:
                raise SafeError("protected entry quantity mismatch; account remains paused")
            await self.broker.protect(symbol, fill.side, trigger, stop["key"])
            await self.broker.verify_liquidation(float(intent["stop_fraction"]))
        elif stop_fill.quantity != fill.quantity or actual != 0:
            raise SafeError("original protective exit is incomplete; account remains paused")
        journal = {
            **journal,
            "fills": [fill.model_dump(mode="json")],
            "execution_complete": True,
            "recovered_after_protection_timeout": True,
        }
        with self.store.transaction() as conn:
            self.store.put("trade:" + intent["id"], journal, conn)
            self.store.audit(
                "simple_pending_entry_verified",
                {"trade_id": intent["id"], "symbol": symbol, "hosted_stop_filled": stop_fill is not None},
                conn,
            )
        return journal

    async def recover_execution(self) -> bool:
        """Release a resolved execution fault, preserving every financial and manual lock."""
        if not isinstance(self.broker, LiveBroker) or self.store.get("state") != State.ERROR_SAFE:
            return False
        reason = self.store.get("state_reason", "")
        if "restart required" in reason or self.store.get("local_flatten_request"):
            return False
        now_ms = self.broker.now_ms()
        if now_ms < self.store.get("simple_execution_recovery_retry_ms", 0):
            return False
        attempts = min(6, int(self.store.get("simple_execution_recovery_attempts", 0)))
        self.store.put("simple_execution_recovery_attempts", attempts + 1)
        self.store.put("simple_execution_recovery_retry_ms", now_ms + min(60_000, 5_000 * 2**attempts))
        self.broker.require_authorization()
        # Preflight rejects unknown exchange orders/stops and changed credentials.
        # Restore reconciles original orders and verifies every remaining hosted stop.
        await self.broker.preflight()
        await self.restore(recover_state=False)
        await self.require_journal()
        with self.store.transaction() as conn:
            lock = self.store.get("risk_lock", None, conn)
            manual = self.store.get("manual_lock", False, conn)
            self.store.transition(
                lock["state"] if lock else State.PAUSED if manual else State.DEGRADED,
                lock["reason"]
                if lock
                else "execution recovered; manual pause retained"
                if manual
                else "execution reconciled and hosted protection verified; checking fresh data before resume",
                conn,
            )
            self.store.put("simple_execution_recovery_attempts", 0, conn)
            self.store.put("simple_execution_recovery_retry_ms", 0, conn)
            self.store.audit(
                "simple_execution_recovered",
                {"previous_reason": reason, "financial_locks_reset": False, "manual_pause_retained": manual},
                conn,
            )
        return True

    async def resume(
        self,
        now_ms: int,
        *,
        override_locks: bool = False,
        override_resume_market_checks: bool = False,
        before_reset: Callable[[], None] | None = None,
    ) -> None:
        if override_resume_market_checks and not override_locks:
            raise SafeError("market-check override requires an explicit operator reset")
        if override_locks:
            # Recover known journaled executions and verify hosted stops before
            # acknowledging financial locks. Unknown outcomes still fail closed.
            await self.restore()
        elif not await self.coordinator.reconcile():
            raise SafeError("simple reconciliation failed")
        await self.require_journal()
        armed = verify_simple_arm(self.s) if self.s.trading_mode == "live" else False
        if isinstance(self.broker, LiveBroker):
            if override_locks:
                # Recovery can take longer than quote TTL, and the short-interval
                # watchlist may include symbols not yet polled by the monitor.
                # Refresh after recovery, not from the pre-recovery cache.
                broker = self.broker
                symbols = sorted(set(self.positions()) | set(self.store.get("simple_selected", [])))
                semaphore = asyncio.Semaphore(3)

                async def refresh(symbol: str) -> Quote:
                    async with semaphore:
                        return await broker.refresh_quote(symbol)

                results = await asyncio.gather(
                    *(refresh(s) for s in symbols), return_exceptions=True
                )
                for symbol, result in zip(symbols, results, strict=True):
                    if isinstance(result, BaseException):
                        if not override_resume_market_checks or not isinstance(result, SafeError):
                            raise result
                        # A failed read never becomes a fabricated quote. The
                        # next entry must fetch and validate actual market data.
                        broker.quotes.pop(symbol, None)
                        self.store.audit(
                            "simple_force_resume_quote_unavailable", {"symbol": symbol}
                        )
                await self.broker.refresh()
            now_ms = self.broker.now_ms()
        if before_reset is not None:
            before_reset()
        self.risk.resume(
            self.health(now_ms),
            datetime.fromtimestamp(now_ms / 1000, UTC),
            armed,
            override_locks=override_locks,
            equity=float(self.broker.equity()) if override_locks else None,
            override_resume_market_checks=override_resume_market_checks,
        )

    async def require_journal(self) -> None:
        if isinstance(self.broker, LiveBroker):
            actual = await self.broker.positions()
            journaled = {
                symbol: Decimal(trade["quantity"]) * trade["side"]
                for symbol, trade in self.positions().items()
            }
            if (
                actual != journaled
                or self.store.get("simple_pending")
                or self.store.get("simple_closing")
            ):
                raise SafeError("simple execution journal incomplete; entries remain blocked")

    async def recover_closings(self) -> None:
        """Settle persisted close intents using the original order's terminal result."""
        for symbol, closing in self.store.get("simple_closing", {}).items():
            order = Order.model_validate(closing["order"])
            trade = self.positions().get(symbol)
            if (
                trade is None
                or not order.reduce_only
                or order.symbol != symbol
                or order.quantity != Decimal(trade["quantity"])
                or order.side != -trade["side"]
            ):
                raise SafeError("persisted close intent does not match the remaining position")
            fill = await self.coordinator.submit_once(order)
            self.record_reduction(symbol, closing, fill)
            if symbol not in self.positions():
                await self.broker.cancel_protection(symbol)

    def record_reduction(self, symbol: str, closing: dict[str, Any], fill: Fill) -> None:
        """Atomically account a terminal IOC, including zero and partial fills."""
        order = Order.model_validate(closing["order"])
        with self.store.transaction() as conn:
            pending = self.store.get("simple_closing", {}, conn)
            if symbol not in pending:
                return  # The fill and removal of its intent commit together.
            if pending[symbol] != closing:
                raise SafeError("close intent changed during reconciliation")
            positions = self.store.get("simple_positions", {}, conn)
            trade = positions.get(symbol)
            if (
                trade is None
                or not order.reduce_only
                or order.symbol != symbol
                or order.quantity != Decimal(trade["quantity"])
                or order.side != -trade["side"]
                or fill.client_id != order.client_id
                or fill.symbol != symbol
                or fill.side != order.side
                or fill.status not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
                or fill.quantity > order.quantity
                or (fill.quantity > 0 and fill.price <= 0)
                or (fill.quantity == 0 and fill.fee != 0)
            ):
                raise SafeError("close fill does not match the remaining position and intent")
            if fill.quantity == order.quantity:
                self.finish(symbol, fill, closing["timestamp_ms"], closing["reason"], conn)
            elif fill.quantity > 0:
                trade.setdefault("initial_quantity", trade["quantity"])
                trade.setdefault("partial_exit_fills", []).append(
                    {
                        "fill": fill.model_dump(mode="json"),
                        "timestamp_ms": closing["timestamp_ms"],
                        "reason": closing["reason"],
                        "quote_reference": closing.get("quote_reference"),
                    }
                )
                trade["quantity"] = str(order.quantity - fill.quantity)
                positions[symbol] = trade
                self.store.put("simple_positions", positions, conn)
                self.store.audit(
                    "simple_partial_close",
                    {
                        "trade_id": trade["id"],
                        "symbol": symbol,
                        "client_id": fill.client_id,
                        "closed_quantity": str(fill.quantity),
                        "remaining_quantity": trade["quantity"],
                        "hosted_protection_retained": True,
                    },
                    conn,
                )
            pending.pop(symbol)
            self.store.put("simple_closing", pending, conn)

    def finish(
        self,
        symbol: str,
        fill: Fill,
        now_ms: int,
        reason: str,
        conn: Connection | None = None,
        *,
        exit_reference: dict[str, Any] | None = None,
    ) -> None:
        if conn is None:
            with self.store.transaction() as connection:
                self.finish(symbol, fill, now_ms, reason, connection, exit_reference=exit_reference)
            return
        positions = dict(self.store.get("simple_positions", {}, conn))
        if symbol not in positions:
            return  # A recovered close must never count twice.
        trade = positions[symbol]
        if (
            fill.symbol != symbol
            or fill.quantity != Decimal(trade["quantity"])
            or fill.side != -trade["side"]
        ):
            raise SafeError("complete position reduction required before counting an outcome")
        observe(trade, float(fill.price), now_ms)
        if exit_reference is None:
            closing = self.store.get("simple_closing", {}, conn).get(symbol, {})
            exit_reference = closing.get("quote_reference")
        exits = [Fill.model_validate(p["fill"]) for p in trade.get("partial_exit_fills", [])]
        exits.append(fill)
        total_quantity = sum((f.quantity for f in exits), Decimal(0))
        if total_quantity != Decimal(trade.get("initial_quantity", trade["quantity"])):
            raise SafeError("complete exit quantities required before counting an outcome")
        exit_notional = sum((f.quantity * f.price for f in exits), Decimal(0))
        exit_fee = sum((f.fee for f in exits), Decimal(0))
        gross_pnl = (float(exit_notional) - trade["entry_price"] * float(total_quantity)) * trade["side"]
        pnl = (
            gross_pnl
            - float(exit_fee)
            - trade["entry_fee"]
            + trade["funding"]
        )
        self.risk.closed(trade["id"], pnl, datetime.fromtimestamp(now_ms / 1000, UTC), conn)
        record = {
            "timestamp_ms": now_ms,
            "confidence_version": trade.get("confidence_version"),
            "profit_target_fraction": trade.get("profit_target_fraction"),
            "profit_runner": trade.get("profit_runner"),
            "opposite_confirmation_version": trade.get("opposite_confirmation_version"),
            "opposite_level": trade.get("opposite_level"),
            "entry_exchange_id": trade.get("entry_exchange_id"),
            "exit_exchange_id": fill.exchange_id,
            "exit_exchange_ids": [f.exchange_id for f in exits],
            "exit_fills": [f.model_dump(mode="json") for f in exits],
            "entry_ms": trade["opened_ms"],
            "symbol": symbol,
            "direction": trade["side"],
            "pnl": pnl,
            "reason": reason,
            "fees": float(exit_fee) + trade["entry_fee"],
            "funding": trade["funding"],
            "turnover": float(exit_notional) + float(total_quantity) * trade["entry_price"],
            "ai_rule_id": trade.get("ai_rule_id"),
            "journal_version": trade.get("journal_version", 0),
            "trade_id": trade["id"],
            "strategy_choice": trade.get("strategy_choice", "current"),
            "strategy_epoch": trade.get("strategy_epoch", 0),
            "entry_context": trade.get("entry_context"),
            "entry_price": trade["entry_price"],
            "exit_price": float(exit_notional / total_quantity),
            "quantity": float(total_quantity),
            "entry_fee": trade["entry_fee"],
            "exit_fee": float(exit_fee),
            "gross_pnl": gross_pnl,
            "net_return": pnl / (trade["entry_price"] * float(total_quantity)),
            "holding_seconds": (now_ms - trade["opened_ms"]) / 1000,
            "initial_stop_distance": trade.get("initial_stop_distance"),
            "final_stop_price": trade.get("stop_price"),
            "mfe_fraction": trade.get("mfe_fraction"),
            "mae_fraction": trade.get("mae_fraction"),
            "last_observed_ms": trade.get("last_observed_ms"),
            "entry_execution_cost": trade.get("entry_execution_cost"),
            "exit_execution_cost": execution_cost(fill, exit_reference),
            "exit_quote_reference": exit_reference,
            "partial_exit_fills": trade.get("partial_exit_fills", []),
        }
        records = self.store.get("simple_closed", [], conn)
        records.append(record)
        self.store.put("simple_closed", records, conn)
        rotation.closed(self.store, trade, pnl, now_ms, conn)
        confidence.enqueue(self.store, record, conn)
        if record.get("confidence_version") == confidence.VERSION and isinstance(
            self.broker, PaperBroker
        ):
            confidence.settle(self.store, record, pnl, conn)
        del positions[symbol]
        self.store.put("simple_positions", positions, conn)
        self.notice(
            trade["id"] + "-close",
            f"CLOSE {symbol} {'LONG' if trade['side'] > 0 else 'SHORT'} at {fill.price}. PnL with estimated fees and recorded funding: {pnl:.2f} {'BNFCR' if self.s.futures_account_mode == 'credits' else 'USDT'}. Why: {reason}.",
            conn,
        )

    def record_stop(
        self, fill: Fill, now_ms: int, *, exit_reference: dict[str, Any] | None = None
    ) -> None:
        if (
            fill.symbol not in self.positions()
            or Decimal(self.positions()[fill.symbol]["quantity"]) != fill.quantity
            or fill.side != -self.positions()[fill.symbol]["side"]
        ):
            raise SafeError("unexpected or partial protective stop fill")
        with self.store.transaction() as conn:
            expected = self.store.get("expected_positions", {}, conn)
            expected[fill.symbol] = str(
                Decimal(expected.get(fill.symbol, "0")) + fill.quantity * fill.side
            )
            self.store.put("expected_positions", expected, conn)
            self.store.audit("simple_hosted_stop", fill.model_dump(mode="json"), conn)
            self.finish(
                fill.symbol,
                fill,
                now_ms,
                "protective stop or volatility trailing stop reached",
                conn,
                exit_reference=exit_reference,
            )

    async def quote(self, quote: Quote, now_ms: int) -> None:
        if not 0 <= now_ms - quote.timestamp_ms <= self.s.market_stale_seconds * 1000:
            raise SafeError("simple quote stale or future dated")
        if self.store.get("simple_closing"):
            await self.recover_closings()
        positions = self.positions()
        if quote.symbol in positions:
            observe(positions[quote.symbol], float(quote.mark), quote.timestamp_ms)
            self.store.put("simple_positions", positions)
        for fill in await self.broker.quote(quote):
            # Only a simulated stop is known to execute on this exact quote.
            self.record_stop(
                fill,
                now_ms,
                exit_reference=reference(quote) if isinstance(self.broker, PaperBroker) else None,
            )
            await self.broker.cancel_protection(fill.symbol)

    def funding(self, symbol: str, timestamp_ms: int, rate: Decimal, mark: Decimal) -> None:
        positions = self.positions()
        # Missing polls across a settlement block entries in the runtime until this is accounted.
        if symbol not in positions or timestamp_ms <= positions[symbol]["opened_ms"]:
            return
        amount = self.broker.funding(symbol, timestamp_ms, rate, mark)
        positions[symbol]["funding"] += float(amount)
        self.store.put("simple_positions", positions)

    async def close(
        self, symbol: str, now_ms: int, reason: str, *, profit_target: float | None = None
    ) -> None:
        if self.store.get("simple_closing"):
            await self.recover_closings()
            return  # Re-evaluate any remainder on the next fresh monitor pass.
        if isinstance(self.broker, LiveBroker):
            # Entry refresh may postdate the caller's loop timestamp. Check the
            # current exchange clock without weakening stale/future quote limits.
            now_ms = self.broker.now_ms()
        trade = self.positions()[symbol]
        q, f = self.broker.quotes[symbol], self.filters[symbol]
        if not 0 <= now_ms - q.timestamp_ms <= self.s.market_stale_seconds * 1000:
            raise SafeError("fresh quote required for reduction")
        quantity = (await self.broker.positions()).get(symbol, Decimal(0))
        if quantity != Decimal(trade["quantity"]) * trade["side"]:
            raise SafeError("position quantity disagrees with the close journal")
        order = Order(
            client_id=client_id(trade["id"], f"close-{now_ms}"),
            symbol=symbol,
            side=-trade["side"],
            quantity=abs(quantity),
            limit_price=(
                confidence.limit_price(self.s, trade, profit_target, f.tick)
                if profit_target is not None
                else f.round_price(
                    q.bid * Decimal(".98") if quantity > 0 else q.ask * Decimal("1.02")
                )
            ),
            reduce_only=True,
        )
        closing = self.store.get("simple_closing", {})
        closing[symbol] = {
            "order": order.model_dump(mode="json"),
            "timestamp_ms": now_ms,
            "reason": reason,
            "quote_reference": reference(q),
            "profit_target": profit_target,
        }
        self.store.put("simple_closing", closing)
        fill = await self.coordinator.submit_once(order)
        self.record_reduction(symbol, closing[symbol], fill)
        if symbol not in self.positions():
            await self.broker.cancel_protection(symbol)

    async def monitor(
        self,
        now_ms: int,
        rows: dict[str, dict[str, Any]],
        crisis: bool = False,
        *,
        awaiting_initial_scan: bool = False,
    ) -> None:
        now = datetime.fromtimestamp(now_ms / 1000, UTC)
        if self.store.get("simple_closing"):
            await self.recover_closings()
        if not await self.coordinator.reconcile():
            raise SafeError("simple reconciliation mismatch")
        await self.require_journal()
        self.risk.anchors(float(self.broker.equity()), now)
        for symbol, trade in self.positions().items():
            q = self.broker.quotes.get(symbol)
            if q is None or not 0 <= now_ms - q.timestamp_ms <= self.s.market_stale_seconds * 1000:
                raise SafeError("held market stale; entries blocked")
            row = rows.get(symbol)
            if (
                trade.get("entry_timeframe", "15m") == "15m"
                and row is not None
                and isinstance(row.get("risk_context"), dict)
            ):
                row = row["risk_context"]
            # During restart the slow risk scan can finish before entry candles.
            # Never interpret another timeframe's trend as this trade's reversal.
            if row is not None and row.get("entry_timeframe", "15m") != trade.get(
                "entry_timeframe", "15m"
            ):
                row = None
            reason = (
                "portfolio loss/risk limit reached"
                if self.store.get("risk_lock")
                else "BTC market crisis detected"
                if crisis
                else exit_reason(learning.position_budget(trade, self.store.get("simple_market_trend"), now_ms, self.s.simple_max_hold_hours), float(q.mark), now_ms, row, self.rules)
            )
            if not awaiting_initial_scan and (
                row is None
                or now_ms - row["timestamp_ms"]
                >= 2
                * {"1m": MINUTE_MS, "5m": 5 * MINUTE_MS, "8m": 8 * MINUTE_MS, "15m": BAR_MS}.get(
                    row.get("entry_timeframe", "15m"), BAR_MS
                )
            ):
                reason = reason or "closed-candle monitoring data unavailable"
            if not reason and self.s.simple_confidence_enabled:
                activation = min(
                    float(trade.get("profit_target_fraction") or confidence.SMALL),
                    confidence.target(self.store),
                )
                trade["profit_target_fraction"] = activation
                if confidence.update_runner(
                    trade, confidence.net_return(self.s, trade, q), activation, now_ms
                ):
                    reason = (
                        "profit runner retracement: net return reached trailing floor "
                        f"{trade['profit_runner']['floor_net_return']:.2%}"
                    )
                positions = self.positions()
                positions[symbol] = trade
                self.store.put("simple_positions", positions)
            if reason:
                # A gap can cross the floor. A marketable reduction takes priority
                # over an unreachable profit IOC limit; hosted protection remains.
                await self.close(symbol, now_ms, reason)
            elif row is not None:
                stop = tighten_stop(trade, float(q.mark), float(row["atr"]), self.rules)
                if stop != trade["stop_price"]:
                    await self.broker.protect(
                        symbol, trade["side"], Decimal(str(stop)), client_id(trade["id"], "stop-0")
                    )
                    positions = self.positions()
                    positions[symbol]["stop_price"] = stop
                    self.store.put("simple_positions", positions)
        self.risk.anchors(float(self.broker.equity()), now)

    def entry_selection(self, row: dict[str, Any], now_ms: int) -> dict[str, Any]:
        from . import routing
        state = rotation.selection(self.store)
        if state["auto"]:
            route = routing.choose(self.store, row, now_ms)
            return {**state, "active": route["choice"], "auto_route": route}
        return state

    def entry_signal(self, row: dict[str, Any], now_ms: int, spread: float,
                     selected_strategy: dict[str, Any] | None = None) -> tuple[int, str]:
        if row.get("entry_timeframe", "15m") != self.entry_timeframe:
            return 0, "entry candle timeframe does not match selected policy"
        learned_veto = learning.veto(self.store, self.s, now_ms)
        if learned_veto:
            return 0, learned_veto
        one_way_cost = self.s.fee_rate + self.s.slippage_bps / 10000 + spread / 2
        from . import routing
        selected_strategy = selected_strategy or self.entry_selection(row, now_ms)
        if selected_strategy["auto"]:
            if selected_strategy["active"] is None:
                return 0, selected_strategy["auto_route"]["reason"]
            if not routing.permitted(self.store, selected_strategy["active"]):
                return 0, "chosen per-trade strategy lacks validated evidence in strict auto"
        opposite = selected_strategy["active"] == "opposite"
        if opposite:
            if self.s.simple_entry_mode != "trend":
                return 0, "confirmed opposite strategy requires normal trend mode"
            actual, reason, _ = opposite_entry(row, one_way_cost, self.rules)
            direction = -actual  # Existing broad trend/BTC gates check the old setup.
            if direction and self.s.simple_ai_enabled:
                allowed, ai_reason = adaptive_signal(
                    self.s, self.store, row, now_ms, self.rules, one_way_cost
                )
                if allowed != direction:
                    return 0, "opposite confirmation lacks matching AI permission; " + ai_reason
        else:
            minute_side, minute_reason = (0, "")
            if self.entry_timeframe in {"1m", "5m", "8m"}:
                if row.get("entry_timeframe") != self.entry_timeframe:
                    return 0, "entry candle timeframe does not match configuration"
                minute_side, minute_reason = minute_direction(row, self.rules)
                if not minute_side:
                    return 0, minute_reason
            direction, reason = (
                adaptive_signal(self.s, self.store, row, now_ms, self.rules, one_way_cost)
                if self.s.simple_ai_enabled
                else entry(
                    row,
                    one_way_cost,
                    self.s.simple_entry_mode,
                    replace(self.rules, entry_style="trend_follow")
                    if self.entry_timeframe in {"1m", "5m", "8m"}
                    else self.rules,
                )
            )
            if self.entry_timeframe in {"1m", "5m", "8m"} and direction:
                if direction != minute_side:
                    return 0, "AI entry direction conflicts with the closed short-interval slope"
                reason = minute_reason + "; " + reason
        if direction and self.s.simple_btc_reference_enabled:
            btc_reason = btc_entry_gate(self.s, self.store, row, direction, now_ms)
            if btc_reason:
                return 0, btc_reason
        if direction:
            market_reason = market_entry_gate(self.s, self.store, row, direction, now_ms, selected_strategy["active"])
            if market_reason:
                return 0, market_reason
        if direction and opposite:
            direction = -direction
        if selected_strategy["auto"]:
            reason = f"AUTO {selected_strategy['active']}: {selected_strategy['auto_route']['reason']}; {reason}"
        return direction, reason

    async def enter(
        self, symbol: str, row: dict[str, Any], now_ms: int, maximum_signal_age_ms: int = BAR_MS
    ) -> None:
        if rotation.selection(self.store)["auto"]:
            researched = self.store.get("simple_learning_research", {}).get("symbols", [])
            if symbol not in researched:
                return
        if symbol == BTC:
            raise SafeError("BTC is reference-only; new BTC positions are forbidden")
        if symbol in self.positions() or symbol not in self.store.get("simple_selected", []):
            return
        await self.require_journal()
        if isinstance(self.broker, LiveBroker):
            now_ms = self.broker.now_ms()
        q = self.broker.quotes.get(symbol)
        if not BAR_MS <= maximum_signal_age_ms <= 2 * BAR_MS:
            raise SafeError("invalid experimental signal delay")
        if self.entry_timeframe in {"1m", "5m", "8m"}:
            maximum_signal_age_ms = self.entry_bar_ms
        if (
            row["timestamp_ms"] > now_ms
            or now_ms - row["timestamp_ms"] >= maximum_signal_age_ms
            or (q is None and not isinstance(self.broker, LiveBroker))
        ):
            raise SafeError("new entry needs a fresh closed candle and quote")
        if isinstance(self.broker, LiveBroker):
            # Cheap candidate screening only. Cached spread must not discard a setup
            # before its current quote is fetched. All execution gates run below.
            spread = 0.0
        else:
            assert q is not None
            self.risk.require_health(self.health(now_ms))
            spread = float((q.ask - q.bid) / q.mid)
            if spread > self.s.max_spread:
                raise SafeError("simple spread veto")
        selected_strategy = self.entry_selection(row, now_ms)
        direction, reason = self.entry_signal(row, now_ms, spread, selected_strategy)
        self.store.put(
            "last_signal",
            {"symbol": symbol, "reason": reason, "timestamp_ms": int(row["timestamp_ms"])},
        )
        if not direction:
            return
        strategy_label = self.strategy + (
            rotation.VERSION if selected_strategy["active"] == "opposite" else ""
        )
        signal_key = f"{strategy_label}:{symbol}:{int(row['timestamp_ms'])}"
        if self.entry_timeframe in {"1m", "5m", "8m"}:
            signal_key += ":" + self.entry_timeframe
        trade_id = hashlib.sha256(signal_key.encode()).hexdigest()[:24]
        if self.store.get("simple_signal:" + trade_id):
            return
        if isinstance(self.broker, LiveBroker):
            self.broker.require_authorization()
            await self.broker.exchange.prepare_simple_symbol(symbol)
            # Exchange setup can take several requests. Refresh after it, then
            # size and price the exact order from current market/account snapshots.
            _, q = await asyncio.gather(self.broker.refresh(), self.broker.refresh_quote(symbol))
            assert q is not None
            now_ms = self.broker.now_ms()
            if not 0 <= now_ms - row["timestamp_ms"] < maximum_signal_age_ms:
                raise SafeError("new entry needs a fresh closed candle and quote")
            self.risk.require_health(self.health(now_ms))
            spread = float((q.ask - q.bid) / q.mid)
            if spread > self.s.max_spread:
                raise SafeError("simple spread veto")
            refreshed_selection = self.entry_selection(row, now_ms)
            if (refreshed_selection["active"] != selected_strategy["active"]
                    or refreshed_selection["epoch"] != selected_strategy["epoch"]):
                return
            selected_strategy = refreshed_selection
            direction, reason = self.entry_signal(row, now_ms, spread, selected_strategy)
            self.store.put(
                "last_signal",
                {"symbol": symbol, "reason": reason, "timestamp_ms": int(row["timestamp_ms"])},
            )
            if not direction:
                return
            self.store.audit(
                "simple_entry_refreshed",
                {
                    "symbol": symbol,
                    "quote_asof_ms": q.timestamp_ms,
                    "signal_asof_ms": int(row["timestamp_ms"]),
                    "refreshed_at_ms": now_ms,
                },
            )
        normal_direction = rotation.execution_side(direction, selected_strategy)
        activity_probe = learning.is_probe(self.store)
        allocation_scale = activity.SIZE_SCALE if activity_probe else 1.0
        assert q is not None
        if self.entry_timeframe in {"1m", "5m", "8m"}:
            reason_veto = (minute_veto(row, normal_direction, now_ms, float(q.mark), self.rules)
                           if selected_strategy["active"] != "opposite" else None)
            if reason_veto:
                raise SafeError(reason_veto)
            if self.store.get("simple_minute:" + symbol) != row:
                raise SafeError("short-interval slope changed before entry")
        self.store.put("simple_signal:" + trade_id, True)
        equity = float(self.broker.equity())
        stop = min(
            self.rules.maximum_stop_fraction,
            max(self.rules.minimum_stop_fraction, self.rules.stop_atr * row["atr_fraction"]),
        )
        costs = stop + 2 * self.s.fee_rate + 2 * self.s.slippage_bps / 10000 + spread + 0.002
        visible = q.ask_size if direction > 0 else q.bid_size
        # Bound total unhedged notional using the existing net-beta cap, without assuming offsets.
        outstanding = sum(
            abs(float(t["quantity"])) * float(self.broker.quotes[k].mark)
            for k, t in self.positions().items()
        )
        caps = {
            "per_trade_risk": equity
            * self.s.risk_per_trade
            * self.store.get("risk_multiplier", 1.0)
            / costs,
            "target_notional": equity * self.s.max_target_notional,
            "net_exposure": max(0, equity * self.s.max_net_beta - outstanding),
        }
        available_margin: float | None = None
        if self.s.simple_sizing_mode == "equity_range":
            available_margin = (
                float(self.broker.available)
                if isinstance(self.broker, LiveBroker)
                else max(0, equity - outstanding / self.s.exchange_leverage)
            )
            bounds = equity_margin_bounds(self.s, equity, available_margin)
            open_risk = sum(
                float(item["risk"])
                for item in self.store.rows("entries", 100000)
                if not item["closed"]
            )
            caps.update(
                position_margin=bounds.maximum_notional
                / (
                    1
                    + self.s.simple_position_margin_cap * self.s.exchange_leverage * self.s.fee_rate
                ),
                available_margin=bounds.available_notional,
                portfolio_margin=max(
                    0,
                    equity * self.s.simple_total_margin_cap * self.s.exchange_leverage
                    - outstanding,
                ),
                gross_exposure=max(0, equity * self.s.max_gross_notional - outstanding),
                open_risk=max(0, equity * self.s.max_open_risk - open_risk) / costs,
            )
        else:
            caps["liquidity"] = float(visible * q.mid) * self.s.max_participation
        notional = min(caps.values())
        if self.s.simple_sizing_mode == "margin":
            # Limits use current equity (USD/BNFCR aggregate), never the sum of
            # collateral asset rows. Available margin also bounds each live order.
            notional = min(
                notional,
                equity * self.s.simple_position_margin_cap * self.s.exchange_leverage,
                max(
                    0,
                    equity * self.s.simple_total_margin_cap * self.s.exchange_leverage
                    - outstanding,
                ),
            )
            if isinstance(self.broker, LiveBroker):
                notional = min(
                    notional,
                    float(self.broker.available) / (1 / self.s.exchange_leverage + self.s.fee_rate),
                )
        notional *= allocation_scale
        if not math.isfinite(notional) or notional <= 0:
            raise SafeError("simple exposure budget exhausted")
        f = self.filters[symbol]
        price = f.round_price(
            q.ask * Decimal("1.001") if direction > 0 else q.bid * Decimal(".999")
        )
        # A short limit is a minimum sale price. Use the conservative ask-side
        # price for its allocation too, so a better short fill cannot inflate size.
        allocation_price = (
            max(price, f.round_price(q.ask * Decimal("1.001")))
            if self.s.simple_sizing_mode == "equity_range"
            else price
        )
        quantity = f.round_quantity(Decimal(str(notional)) / allocation_price)
        if quantity < f.min_quantity or quantity * price < f.min_notional:
            self.store.audit(
                "simple_entry_skipped",
                {
                    "symbol": symbol,
                    "reason": "allowed size below exchange minimum",
                    "allowed_notional": str(quantity * price),
                    "minimum_notional": str(f.min_notional),
                },
            )
            return
        if self.s.simple_sizing_mode == "equity_range":
            assert available_margin is not None
            allocated = float(quantity * allocation_price)
            try:
                bounds = require_equity_margin_allocation(
                    self.s, equity, allocated, available_margin, float(f.step * allocation_price),
                    allocation_scale,
                )
            except SafeError as exc:
                self.store.audit("simple_entry_skipped", {"symbol": symbol, "reason": str(exc)})
                return
            self.store.audit(
                "simple_equity_allocation",
                {
                    "symbol": symbol,
                    "equity": equity,
                    "available_margin": available_margin,
                    "planned_notional": allocated,
                    "planned_margin": allocated / self.s.exchange_leverage,
                    "margin_fraction": allocated / self.s.exchange_leverage / equity,
                    "remaining_balance": bounds.remaining_balance,
                    "limiting_cap": min(caps, key=lambda key: caps[key]),
                    "caps": caps,
                    "allocation_scale": allocation_scale,
                    "quote_asof_ms": q.timestamp_ms,
                },
            )
        order = Order(
            client_id=client_id(trade_id, symbol),
            symbol=symbol,
            side=direction,
            quantity=quantity,
            limit_price=price,
        )
        actual = float(quantity * allocation_price)
        liquidation_distance = 1.0
        if isinstance(self.broker, LiveBroker):
            self.broker.require_authorization()
            f.validate(order)
            liquidation_distance = await self.broker.exchange.liquidation_buffer([order])
        proposal = Proposal(
            trade_id,
            symbol,
            equity,
            actual,
            actual,
            actual * costs,
            direction * actual / equity,
            stop,
            liquidation_distance,
            float(quantity / max(visible, Decimal(".00000001"))),
            available_margin,
            float(f.step * allocation_price),
            activity_probe,
        )
        intent: dict[str, Any] = {
            "id": trade_id,
            "confidence_version": confidence.VERSION if self.s.simple_confidence_enabled else None,
            "profit_target_fraction": (confidence.SMALL if selected_strategy["auto"] else confidence.target(self.store))
            if self.s.simple_confidence_enabled
            else None,
            "strategy_choice": selected_strategy["active"],
            "strategy_epoch": selected_strategy["epoch"],
            "side": direction,
            "decision_side": normal_direction,
            "entry_mode": self.s.simple_entry_mode,
            "entry_timeframe": self.entry_timeframe,
            "max_hold_hours": rotation.hold_hours(self.store, self.s),
            "adaptive_holding": selected_strategy["auto"],
            "activity_probe": activity_probe,
            "opened_ms": now_ms,
            "stop_fraction": stop,
            "journal_version": 1,
            "entry_context": {
                "strategy": strategy_label,
                "decision_side": normal_direction,
                "strategy_choice": selected_strategy["active"],
                "strategy_epoch": selected_strategy["epoch"],
                "config_hash": self.s.config_hash,
                "rules": self.rules.public(),
                "mode": self.s.trading_mode,
                "currency": "BNFCR" if self.s.futures_account_mode == "credits" else "USDT",
                "entry_mode": self.s.simple_entry_mode,
                "entry_profile": self.s.simple_entry_profile,
                "entry_timeframe": self.entry_timeframe,
                "max_hold_hours": rotation.hold_hours(self.store, self.s),
                "learning_decision": self.store.get(learning.KEY) if selected_strategy["auto"] else None,
                "auto_route": selected_strategy.get("auto_route"),
                "activity_probe": activity_probe,
                "allocation_scale": allocation_scale,
                "ai_enabled": self.s.simple_ai_enabled,
                "reason": reason,
                "indicators": indicators(row),
                "market_trend": self.store.get("simple_market_trend")
                if self.s.simple_market_trend_enabled
                else None,
                "quote_reference": reference(q),
                "fee_basis": "simulated" if self.s.trading_mode == "paper" else "estimated",
                "fee_rate": self.s.fee_rate,
                "funding_basis": "simulated"
                if self.s.trading_mode == "paper"
                else "exchange_income",
            },
        }
        if self.s.simple_market_trend_enabled:
            current_ms = self.broker.now_ms() if isinstance(self.broker, LiveBroker) else now_ms
            market_reason = market_entry_gate(self.s, self.store, row, normal_direction, current_ms, selected_strategy["active"])
            if market_reason:
                raise SafeError(market_reason)
            self.store.put(
                "simple_market_order:" + order.client_id,
                {
                    "config_hash": self.s.config_hash,
                    "row": {key: value for key, value in row.items() if key.startswith("day_")}
                    | {"timestamp_ms": int(row["timestamp_ms"])},
                    "order": order.model_dump(mode="json"),
                },
            )
        if self.s.simple_btc_reference_enabled:
            current_ms = self.broker.now_ms() if isinstance(self.broker, LiveBroker) else now_ms
            btc_reason = btc_entry_gate(self.s, self.store, row, normal_direction, current_ms)
            if btc_reason:
                raise SafeError(btc_reason)
            context = {key: value for key, value in row.items() if key.startswith("btc_")}
            context["timestamp_ms"] = int(row["timestamp_ms"])
            self.store.put(
                "simple_btc_order:" + order.client_id,
                {
                    "context": context,
                    "order": order.model_dump(mode="json"),
                },
            )
        if self.s.simple_ai_enabled:
            current_ms = self.broker.now_ms() if isinstance(self.broker, LiveBroker) else now_ms
            plan = active_plan(self.s, self.store, int(row["timestamp_ms"]), current_ms)
            if plan is None or plan.action != "trade":
                raise SafeError("adaptive AI permission expired before execution")
            intent["ai_rule_id"] = plan.digest
            self.store.put(
                "simple_ai_order:" + order.client_id,
                {
                    "digest": plan.digest,
                    "asof_ms": plan.asof_ms,
                    "order": order.model_dump(mode="json"),
                },
            )
        permission = {
            "active": selected_strategy["active"],
            "epoch": selected_strategy["epoch"],
            "order": order.model_dump(mode="json"),
            "decision_side": normal_direction,
            "activity_probe": activity_probe,
            "trade_id": trade_id,
        }
        if selected_strategy["auto"]:
            permission.update(auto_route=selected_strategy["auto_route"],
                              route_row={**indicators(row), "entry_timeframe": row.get("entry_timeframe", "15m")})
        if selected_strategy["active"] == "opposite":
            one_way_cost = self.s.fee_rate + self.s.slippage_bps / 10000 + spread / 2
            confirmed_side, _, level = opposite_entry(row, one_way_cost, self.rules)
            if confirmed_side != direction or level is None:
                raise SafeError("opposite confirmation changed before entry")
            intent.update(opposite_confirmation_version=1, opposite_level=level)
            intent["entry_context"].update(opposite_confirmation_version=1, opposite_level=level)
            permission.update(
                opposite_level=level,
                opposite_evidence={
                    "row": {**indicators(row), "entry_timeframe": row.get("entry_timeframe", "15m")},
                    "one_way_cost": one_way_cost,
                    "rules": self.rules.public(),
                },
            )
        self.store.put("simple_strategy_order:" + order.client_id, permission)
        current_ms = self.broker.now_ms() if isinstance(self.broker, LiveBroker) else now_ms
        rotation.require_order(self.store, order, current_ms, q)
        if self.s.simple_entry_mode == "failed_breakout":
            intent["rejection_level"] = float(row["rejection_level"])
        if self.entry_timeframe in {"1m", "5m", "8m"}:
            self.store.put(
                "simple_minute_order:" + order.client_id,
                {
                    "config_hash": self.s.config_hash,
                    "row": row,
                    "order": order.model_dump(mode="json"),
                },
            )
        if isinstance(self.broker, LiveBroker):
            pending = self.store.get("simple_pending", {})
            pending[symbol] = intent
            self.store.put("simple_pending", pending)
            try:
                fills = await self.coordinator.enter_single_exchange(
                    proposal,
                    order,
                    self.health(now_ms),
                    datetime.fromtimestamp(now_ms / 1000, UTC),
                    stop,
                )
            except (SafeError, TimeoutError):
                active = [
                    r
                    for r in self.store.rows("entries", 100000)
                    if r["id"] == trade_id and not r["closed"]
                ]
                if not active:
                    pending.pop(symbol, None)
                    self.store.put("simple_pending", pending)
                raise
        else:
            fills = await self.coordinator.enter_single_paper(
                proposal,
                order,
                self.health(now_ms),
                datetime.fromtimestamp(now_ms / 1000, UTC),
                stop,
            )
        fill = fills[0]
        self.record_entry(symbol, fill, intent)
        self.notice(
            trade_id + "-open",
            f"OPEN {symbol} {'LONG' if direction > 0 else 'SHORT'} at {fill.price}, size {fill.quantity}, stop {self.positions()[symbol]['stop_price']:.6g}. Strategy: {selected_strategy['active'].upper()}. Why: {reason}. Entry candles {self.entry_timeframe}; risk volatility {row['atr_fraction']:.2%} per 15-minute bar. {self.s.trading_mode.upper()} execution.",
        )

    def record_entry(self, symbol: str, fill: Fill, intent: dict[str, Any]) -> None:
        stop = float(intent["stop_fraction"])
        direction = int(intent["side"])
        trade_id = intent["id"]
        positions = self.positions()
        positions[symbol] = {
            "id": trade_id,
            "confidence_version": intent.get("confidence_version"),
            "profit_target_fraction": intent.get("profit_target_fraction"),
            "entry_exchange_id": fill.exchange_id,
            "opposite_confirmation_version": intent.get("opposite_confirmation_version"),
            "opposite_level": intent.get("opposite_level"),
            "strategy_choice": intent.get("strategy_choice", "current"),
            "strategy_epoch": intent.get("strategy_epoch", 0),
            "side": direction,
            "decision_side": intent.get("decision_side", direction),
            "entry_mode": intent.get("entry_mode", self.s.simple_entry_mode),
            "entry_timeframe": intent.get("entry_timeframe", "15m"),
            "max_hold_hours": intent.get("max_hold_hours", self.s.simple_max_hold_hours),
            "adaptive_holding": intent.get("adaptive_holding", False),
            "activity_probe": intent.get("activity_probe", False),
            "quantity": str(fill.quantity),
            "entry_price": float(fill.price),
            "entry_fee": float(fill.fee),
            "funding": 0.0,
            "opened_ms": intent["opened_ms"],
            "stop_price": float(fill.price) * (1 - direction * stop),
            "initial_stop_distance": float(fill.price) * stop,
            "ai_rule_id": intent.get("ai_rule_id"),
            "journal_version": intent.get("journal_version", 0),
            "entry_context": intent.get("entry_context"),
            "entry_execution_cost": execution_cost(
                fill, (intent.get("entry_context") or {}).get("quote_reference")
            ),
        }
        observe(positions[symbol], float(fill.price), intent["opened_ms"])
        if self.s.simple_entry_mode == "failed_breakout":
            positions[symbol]["rejection_level"] = intent["rejection_level"]
        with self.store.transaction() as conn:
            self.store.put("simple_positions", positions, conn)
            pending = self.store.get("simple_pending", {}, conn)
            pending.pop(symbol, None)
            self.store.put("simple_pending", pending, conn)
