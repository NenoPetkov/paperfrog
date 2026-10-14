from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from paperfrog.domain.types import Fill, Filters, Order, Quote, SafeError, State
from paperfrog.execution.coordinator import Coordinator, client_id
from paperfrog.paper.broker import PaperBroker
from paperfrog.risk.engine import Health, Proposal, RiskEngine
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .strategy import (
    BAR_MS,
    RULES,
    Rules,
    entry,
    exit_reason,
    require_paper,
    strategy_name,
    tighten_stop,
)


class Engine:
    """Same directional decision/position logic for historical replay and forward PAPER."""

    def __init__(
        self, s: Settings, store: Store, filters: dict[str, Filters], rules: Rules = RULES
    ):
        require_paper(s)
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
        self.risk = RiskEngine(s, store, profit_guard_scale=max(1.0, s.risk_per_trade / 0.002))
        self.broker = PaperBroker(
            store,
            filters,
            Decimal(str(s.paper_equity)),
            s.paper_seed,
            Decimal(str(s.fee_rate)),
            s.slippage_bps,
            0,
        )
        self.coordinator = Coordinator(self.broker, store, self.risk, filters)
        self.store.put("simple_rules", public_rules)
        self.store.put("simple_entry_mode", s.simple_entry_mode)

    def positions(self) -> dict[str, dict[str, Any]]:
        return dict(self.store.get("simple_positions", {}))

    def notice(self, key: str, message: str) -> None:
        notices = self.store.get("simple_notices", {})
        notices[key] = message
        self.store.put("simple_notices", notices)
        self.store.audit("simple_explanation", {"message": message})

    def health(self, now_ms: int) -> Health:
        ages = [
            (now_ms - q.timestamp_ms) / 1000
            for q in self.broker.quotes.values()
            if q.symbol in self.positions() or q.symbol in self.store.get("simple_selected", [])
        ]
        # model_valid means frozen research rules here; this engine is structurally PAPER-only.
        return Health(
            bool(self.store.get("reconciled")),
            True,
            bool(self.filters),
            max(ages, default=float("inf")),
            0,
        )

    async def restore(self) -> None:
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
        self.store.put("state", State.PAUSED)

    async def resume(self, now_ms: int) -> None:
        if not await self.coordinator.reconcile():
            raise SafeError("simple reconciliation failed")
        self.risk.resume(self.health(now_ms), datetime.fromtimestamp(now_ms / 1000, UTC))

    def finish(self, symbol: str, fill: Fill, now_ms: int, reason: str) -> None:
        positions = self.positions()
        trade = positions[symbol]
        pnl = (
            (float(fill.price) - trade["entry_price"]) * float(fill.quantity) * trade["side"]
            - float(fill.fee)
            - trade["entry_fee"]
            + trade["funding"]
        )
        self.risk.closed(trade["id"], pnl, datetime.fromtimestamp(now_ms / 1000, UTC))
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
        }
        records = self.store.get("simple_closed", [])
        records.append(record)
        self.store.put("simple_closed", records)
        del positions[symbol]
        self.store.put("simple_positions", positions)
        self.notice(
            trade["id"] + "-close",
            f"CLOSE {symbol} {'LONG' if trade['side'] > 0 else 'SHORT'} at {fill.price}. PnL after fees/funding: {pnl:.2f} USDT. Why: {reason}.",
        )

    async def quote(self, quote: Quote, now_ms: int) -> None:
        if not 0 <= now_ms - quote.timestamp_ms <= self.s.market_stale_seconds * 1000:
            raise SafeError("simple quote stale or future dated")
        for fill in await self.broker.quote(quote):
            # These are confirmed hosted PAPER fills, not an exchange position assumption.
            expected = self.store.get("expected_positions", {})
            expected[fill.symbol] = str(
                Decimal(expected.get(fill.symbol, "0")) + fill.quantity * fill.side
            )
            self.store.put("expected_positions", expected)
            self.store.audit("simple_hosted_stop", fill.model_dump(mode="json"))
            if (
                fill.symbol not in self.positions()
                or Decimal(self.positions()[fill.symbol]["quantity"]) != fill.quantity
            ):
                raise SafeError("unexpected or partial protective stop fill")
            self.finish(
                fill.symbol, fill, now_ms, "protective stop or volatility trailing stop reached"
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
        fill = await self.coordinator.submit_once(order)
        if fill.quantity != abs(quantity):
            self.store.put("state", State.ERROR_SAFE)
            raise SafeError("partial reduction; reconciliation required")
        self.finish(symbol, fill, now_ms, reason)
        await self.broker.cancel_protection(symbol)

    async def monitor(
        self, now_ms: int, rows: dict[str, dict[str, Any]], crisis: bool = False
    ) -> None:
        now = datetime.fromtimestamp(now_ms / 1000, UTC)
        if not await self.coordinator.reconcile():
            raise SafeError("simple reconciliation mismatch")
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
        if symbol in self.positions() or symbol not in self.store.get("simple_selected", []):
            return
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
        if not math.isfinite(notional) or notional <= 0:
            raise SafeError("simple exposure budget exhausted")
        f = self.filters[symbol]
        price = f.round_price(
            q.ask * Decimal("1.001") if direction > 0 else q.bid * Decimal(".999")
        )
        quantity = f.round_quantity(Decimal(str(notional)) / price)
        order = Order(
            client_id=client_id(trade_id, symbol),
            symbol=symbol,
            side=direction,
            quantity=quantity,
            limit_price=price,
        )
        actual = float(quantity * price)
        proposal = Proposal(
            trade_id,
            symbol,
            equity,
            actual,
            actual,
            actual * costs,
            direction * actual / equity,
            stop,
            1,
            float(quantity / visible),
        )
        fills = await self.coordinator.enter_single_paper(
            proposal, order, self.health(now_ms), datetime.fromtimestamp(now_ms / 1000, UTC), stop
        )
        fill = fills[0]
        positions = self.positions()
        positions[symbol] = {
            "id": trade_id,
            "side": direction,
            "entry_mode": self.s.simple_entry_mode,
            "quantity": str(fill.quantity),
            "entry_price": float(fill.price),
            "entry_fee": float(fill.fee),
            "funding": 0.0,
            "opened_ms": now_ms,
            "stop_price": float(fill.price) * (1 - direction * stop),
            "initial_stop_distance": float(fill.price) * stop,
        }
        if self.s.simple_entry_mode == "failed_breakout":
            positions[symbol]["rejection_level"] = float(row["rejection_level"])
        self.store.put("simple_positions", positions)
        self.notice(
            trade_id + "-open",
            f"OPEN {symbol} {'LONG' if direction > 0 else 'SHORT'} at {fill.price}, size {fill.quantity}, stop {positions[symbol]['stop_price']:.6g}. Why: {reason}. Ranked by recent liquidity and trend; volatility {row['atr_fraction']:.2%} per 15-minute bar. PAPER simulation.",
        )
