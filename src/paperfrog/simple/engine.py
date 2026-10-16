from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection

from paperfrog.domain.types import Fill, Filters, Order, Quote, SafeError, State
from paperfrog.execution.coordinator import Coordinator, client_id
from paperfrog.paper.broker import PaperBroker
from paperfrog.risk.engine import Health, Proposal, RiskEngine
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .adaptive import active_plan
from .adaptive import signal as adaptive_signal
from .authorization import verify_simple_arm
from .bitcoin import BTC
from .bitcoin import entry_gate as btc_entry_gate
from .live_broker import LiveBroker
from .strategy import (
    BAR_MS,
    RULES,
    Rules,
    entry,
    execution_rules,
    exit_reason,
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
        ages = [
            (now_ms - q.timestamp_ms) / 1000
            for q in self.broker.quotes.values()
            if q.symbol in self.positions() or q.symbol in self.store.get("simple_selected", [])
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

    async def restore(self) -> None:
        if isinstance(self.broker, LiveBroker):
            # Reconcile uncertain IOC submissions without ever submitting them again.
            if self.store.unresolved_orders() and not await self.coordinator.reconcile():
                raise SafeError("unresolved startup execution; account remains paused")
            for symbol, intent in self.store.get("simple_pending", {}).items():
                if symbol in self.positions():
                    continue
                journal = self.store.get("trade:" + intent["id"], {})
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
            for symbol, closing in self.store.get("simple_closing", {}).items():
                if symbol not in self.positions():
                    continue
                fill = await self.coordinator.submit_once(Order.model_validate(closing["order"]))
                if fill.quantity != Decimal(self.positions()[symbol]["quantity"]):
                    raise SafeError("incomplete reduction after restart; account remains paused")
                with self.store.transaction() as conn:
                    self.finish(symbol, fill, closing["timestamp_ms"], closing["reason"], conn)
                    records = self.store.get("simple_closing", {}, conn)
                    records.pop(symbol, None)
                    self.store.put("simple_closing", records, conn)
                await self.broker.cancel_protection(symbol)
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
        lock = self.store.get("risk_lock")
        self.store.transition(
            lock["state"] if lock else State.PAUSED,
            lock["reason"]
            if lock
            else "startup reconciliation complete; waiting for fresh market data",
        )

    async def resume(self, now_ms: int, *, override_locks: bool = False) -> None:
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
                await self.broker.refresh()
            now_ms = self.broker.now_ms()
        self.risk.resume(
            self.health(now_ms),
            datetime.fromtimestamp(now_ms / 1000, UTC),
            armed,
            override_locks=override_locks,
            equity=float(self.broker.equity()) if override_locks else None,
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

    def finish(
        self, symbol: str, fill: Fill, now_ms: int, reason: str, conn: Connection | None = None
    ) -> None:
        positions = dict(self.store.get("simple_positions", {}, conn))
        trade = positions[symbol]
        pnl = (
            (float(fill.price) - trade["entry_price"]) * float(fill.quantity) * trade["side"]
            - float(fill.fee)
            - trade["entry_fee"]
            + trade["funding"]
        )
        self.risk.closed(trade["id"], pnl, datetime.fromtimestamp(now_ms / 1000, UTC), conn)
        record = {
            "timestamp_ms": now_ms,
            "entry_ms": trade["opened_ms"],
            "symbol": symbol,
            "direction": trade["side"],
            "pnl": pnl,
            "reason": reason,
            "fees": float(fill.fee) + trade["entry_fee"],
            "funding": trade["funding"],
            "turnover": float(fill.quantity * fill.price)
            + float(trade["quantity"]) * trade["entry_price"],
            "ai_rule_id": trade.get("ai_rule_id"),
        }
        records = self.store.get("simple_closed", [], conn)
        records.append(record)
        self.store.put("simple_closed", records, conn)
        del positions[symbol]
        self.store.put("simple_positions", positions, conn)
        self.notice(
            trade["id"] + "-close",
            f"CLOSE {symbol} {'LONG' if trade['side'] > 0 else 'SHORT'} at {fill.price}. PnL with estimated fees and recorded funding: {pnl:.2f} {'BNFCR' if self.s.futures_account_mode == 'credits' else 'USDT'}. Why: {reason}.",
            conn,
        )

    def record_stop(self, fill: Fill, now_ms: int) -> None:
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
            )

    async def quote(self, quote: Quote, now_ms: int) -> None:
        if not 0 <= now_ms - quote.timestamp_ms <= self.s.market_stale_seconds * 1000:
            raise SafeError("simple quote stale or future dated")
        for fill in await self.broker.quote(quote):
            self.record_stop(fill, now_ms)
            await self.broker.cancel_protection(fill.symbol)

    def funding(self, symbol: str, timestamp_ms: int, rate: Decimal, mark: Decimal) -> None:
        positions = self.positions()
        # Missing polls across a settlement block entries in the runtime until this is accounted.
        if symbol not in positions or timestamp_ms <= positions[symbol]["opened_ms"]:
            return
        amount = self.broker.funding(symbol, timestamp_ms, rate, mark)
        positions[symbol]["funding"] += float(amount)
        self.store.put("simple_positions", positions)

    async def close(self, symbol: str, now_ms: int, reason: str) -> None:
        trade = self.positions()[symbol]
        q, f = self.broker.quotes[symbol], self.filters[symbol]
        if not 0 <= now_ms - q.timestamp_ms <= self.s.market_stale_seconds * 1000:
            raise SafeError("fresh quote required for reduction")
        quantity = (await self.broker.positions()).get(symbol, Decimal(0))
        if quantity == 0:
            raise SafeError("position disappeared without a recorded close")
        order = Order(
            client_id=client_id(trade["id"], f"close-{now_ms}"),
            symbol=symbol,
            side=-trade["side"],
            quantity=abs(quantity),
            limit_price=f.round_price(
                q.bid * Decimal(".98") if quantity > 0 else q.ask * Decimal("1.02")
            ),
            reduce_only=True,
        )
        closing = self.store.get("simple_closing", {})
        closing[symbol] = {
            "order": order.model_dump(mode="json"),
            "timestamp_ms": now_ms,
            "reason": reason,
        }
        self.store.put("simple_closing", closing)
        fill = await self.coordinator.submit_once(order)
        if fill.quantity != abs(quantity):
            self.store.transition(State.ERROR_SAFE, "partial reduction; reconciliation required")
            raise SafeError("partial reduction; reconciliation required")
        with self.store.transaction() as conn:
            self.finish(symbol, fill, now_ms, reason, conn)
            closing = self.store.get("simple_closing", {}, conn)
            closing.pop(symbol, None)
            self.store.put("simple_closing", closing, conn)
        await self.broker.cancel_protection(symbol)

    async def monitor(
        self, now_ms: int, rows: dict[str, dict[str, Any]], crisis: bool = False
    ) -> None:
        now = datetime.fromtimestamp(now_ms / 1000, UTC)
        if not await self.coordinator.reconcile():
            raise SafeError("simple reconciliation mismatch")
        await self.require_journal()
        self.risk.anchors(float(self.broker.equity()), now)
        for symbol, trade in self.positions().items():
            q = self.broker.quotes.get(symbol)
            if q is None or not 0 <= now_ms - q.timestamp_ms <= self.s.market_stale_seconds * 1000:
                raise SafeError("held market stale; entries blocked")
            row = rows.get(symbol)
            reason = (
                "portfolio loss/risk limit reached"
                if self.store.get("risk_lock")
                else "BTC market crisis detected"
                if crisis
                else exit_reason(trade, float(q.mark), now_ms, row, self.rules)
            )
            if row is None or now_ms - row["timestamp_ms"] >= 2 * BAR_MS:
                reason = reason or "closed-candle monitoring data unavailable"
            if reason:
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

    async def enter(
        self, symbol: str, row: dict[str, Any], now_ms: int, maximum_signal_age_ms: int = BAR_MS
    ) -> None:
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
        if (
            q is None
            or row["timestamp_ms"] > now_ms
            or now_ms - row["timestamp_ms"] >= maximum_signal_age_ms
        ):
            raise SafeError("new entry needs a fresh closed candle and quote")
        self.risk.require_health(self.health(now_ms))
        spread = float((q.ask - q.bid) / q.mid)
        if spread > self.s.max_spread:
            raise SafeError("simple spread veto")
        direction, reason = entry(
            row,
            self.s.fee_rate + self.s.slippage_bps / 10000 + spread / 2,
            self.s.simple_entry_mode,
            self.rules,
        )
        if self.s.simple_ai_enabled:
            direction, reason = adaptive_signal(
                self.s,
                self.store,
                row,
                now_ms,
                self.rules,
                self.s.fee_rate + self.s.slippage_bps / 10000 + spread / 2,
            )
        if direction and self.s.simple_btc_reference_enabled:
            btc_reason = btc_entry_gate(self.s, self.store, row, direction, now_ms)
            if btc_reason:
                direction, reason = 0, btc_reason
        self.store.put(
            "last_signal",
            {"symbol": symbol, "reason": reason, "timestamp_ms": int(row["timestamp_ms"])},
        )
        if not direction:
            return
        trade_id = hashlib.sha256(
            f"{self.strategy}:{symbol}:{int(row['timestamp_ms'])}".encode()
        ).hexdigest()[:24]
        if self.store.get("simple_signal:" + trade_id):
            return
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
        notional = min(
            equity * self.s.risk_per_trade * self.store.get("risk_multiplier", 1.0) / costs,
            equity * self.s.max_target_notional,
            max(0, equity * self.s.max_net_beta - outstanding),
            float(visible * q.mid) * self.s.max_participation,
        )
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
        if not math.isfinite(notional) or notional <= 0:
            raise SafeError("simple exposure budget exhausted")
        f = self.filters[symbol]
        price = f.round_price(
            q.ask * Decimal("1.001") if direction > 0 else q.bid * Decimal(".999")
        )
        quantity = f.round_quantity(Decimal(str(notional)) / price)
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
        order = Order(
            client_id=client_id(trade_id, symbol),
            symbol=symbol,
            side=direction,
            quantity=quantity,
            limit_price=price,
        )
        actual = float(quantity * price)
        liquidation_distance = 1.0
        if isinstance(self.broker, LiveBroker):
            self.broker.require_authorization()
            f.validate(order)
            await self.broker.exchange.prepare_simple_symbol(symbol)
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
            float(quantity / visible),
        )
        intent: dict[str, Any] = {
            "id": trade_id,
            "side": direction,
            "entry_mode": self.s.simple_entry_mode,
            "opened_ms": now_ms,
            "stop_fraction": stop,
        }
        if self.s.simple_btc_reference_enabled:
            current_ms = self.broker.now_ms() if isinstance(self.broker, LiveBroker) else now_ms
            btc_reason = btc_entry_gate(self.s, self.store, row, direction, current_ms)
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
        if self.s.simple_entry_mode == "failed_breakout":
            intent["rejection_level"] = float(row["rejection_level"])
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
            f"OPEN {symbol} {'LONG' if direction > 0 else 'SHORT'} at {fill.price}, size {fill.quantity}, stop {self.positions()[symbol]['stop_price']:.6g}. Why: {reason}. Ranked by recent liquidity and trend; volatility {row['atr_fraction']:.2%} per 15-minute bar. {self.s.trading_mode.upper()} execution.",
        )

    def record_entry(self, symbol: str, fill: Fill, intent: dict[str, Any]) -> None:
        stop = float(intent["stop_fraction"])
        direction = int(intent["side"])
        trade_id = intent["id"]
        positions = self.positions()
        positions[symbol] = {
            "id": trade_id,
            "side": direction,
            "entry_mode": self.s.simple_entry_mode,
            "quantity": str(fill.quantity),
            "entry_price": float(fill.price),
            "entry_fee": float(fill.fee),
            "funding": 0.0,
            "opened_ms": intent["opened_ms"],
            "stop_price": float(fill.price) * (1 - direction * stop),
            "initial_stop_distance": float(fill.price) * stop,
            "ai_rule_id": intent.get("ai_rule_id"),
        }
        if self.s.simple_entry_mode == "failed_breakout":
            positions[symbol]["rejection_level"] = intent["rejection_level"]
        with self.store.transaction() as conn:
            self.store.put("simple_positions", positions, conn)
            pending = self.store.get("simple_pending", {}, conn)
            pending.pop(symbol, None)
            self.store.put("simple_pending", pending, conn)
