from __future__ import annotations

import asyncio
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from paperfrog.domain.types import EntryVeto, Fill, Order, OrderNotFound, Quote, SafeError
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.reporting.readiness import credential_binding
from paperfrog.risk.sizing import require_equity_margin_allocation
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .adaptive import active_plan
from .authorization import verify_simple_arm
from .bitcoin import require_order as require_btc_order
from .data import quote_snapshot
from .minute import require_order as require_minute_order
from .strategy import require_execution


class LiveBroker:
    """Exchange ledger, cached authoritative equity and journaled hosted protection."""

    def __init__(self, s: Settings, store: Store, exchange: BinanceAdapter):
        require_execution(s)
        if s.trading_mode == "paper" or exchange.s != s:
            raise SafeError("live broker environment mismatch")
        self.s, self.store, self.exchange = s, store, exchange
        self.quotes: dict[str, Quote] = {}
        self.account_at_ms = 0
        self._equity = Decimal(0)
        self.available = Decimal(0)

    def require_authorization(self) -> None:
        require_execution(self.s)
        if self.s.trading_mode == "live":
            verify_simple_arm(self.s)

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.exchange.offset_ms

    async def refresh(self) -> None:
        account = await self.exchange.account()
        try:
            equity = Decimal(account["totalMarginBalance"])
            available = Decimal(account["availableBalance"])
            if not equity.is_finite() or equity <= 0 or not available.is_finite() or available < 0:
                raise SafeError("invalid exchange account equity")
            if self.s.futures_account_mode == "credits" and not any(
                row.get("asset") == "BNFCR" for row in account.get("assets", [])
            ):
                raise SafeError("configured BNFCR account not confirmed")
        except (KeyError, InvalidOperation, TypeError):
            raise SafeError("exchange account valuation unavailable") from None
        # Use Binance's USD total once. BNFCR and collateral asset rows must not be summed.
        self._equity, self.available = equity, available
        self.account_at_ms = self.now_ms()

    async def refresh_quote(self, symbol: str) -> Quote:
        quote = await quote_snapshot(self.exchange, symbol)
        self.quotes[symbol] = quote
        return quote

    def equity(self) -> Decimal:
        if not 0 <= self.now_ms() - self.account_at_ms <= self.s.account_stale_seconds * 1000:
            raise SafeError("exchange account valuation stale")
        return self._equity

    async def preflight(self) -> None:
        binding = credential_binding(self.s)
        previous = self.store.get("exchange_account_binding")
        if previous is not None and previous != binding:
            raise SafeError("exchange credentials changed; a separate account ledger is required")
        await self.refresh()
        config = await self.exchange.request("GET", "/fapi/v1/accountConfig", signed=True)
        if config.get("canTrade") is not True:
            raise SafeError("exchange account trading permission unavailable")
        await self.exchange.verify_risk_settings(())
        known = {row["id"] for row in self.store.rows("orders", 100000)}
        if any(row.get("clientOrderId") not in known for row in await self.exchange.open_orders()):
            raise SafeError("unknown exchange orders; reconciliation required")
        stops = self.store.get("live_stops", {})
        keys = {row["key"] for row in stops.values()}
        if any(row.get("clientAlgoId") not in keys for row in await self.exchange.open_stops()):
            raise SafeError("unknown exchange protective orders; reconciliation required")
        allowed_symbols = set(self.store.get("expected_positions", {})) | set(
            self.store.get("simple_pending", {})
        )
        allowed_symbols.update(row["symbol"] for row in self.store.unresolved_orders())
        if any(symbol not in allowed_symbols for symbol in await self.positions()):
            raise SafeError("unknown exchange positions; reconciliation required")
        self.store.put("exchange_account_binding", binding)

    async def submit(self, order: Order) -> Fill:
        if not order.reduce_only:
            if order.symbol == "BTCUSDT":
                raise SafeError("BTC is reference-only; new BTC positions are forbidden")
            self.require_authorization()
            await self.refresh()
            size_error = EntryVeto if self.s.simple_sizing_mode == "equity_range" else SafeError
            if order.quantity * order.limit_price / 2 > self.available:
                raise size_error("available margin insufficient")
            if self.s.simple_sizing_mode in {"margin", "equity_range"}:
                notional = order.quantity * order.limit_price
                equity = self.equity()
                if notional > equity * Decimal(str(self.s.simple_position_margin_cap * 2)):
                    raise size_error("position margin cap exceeded")
                occupied = sum(
                    (
                        abs(Decimal(row["positionAmt"])) * Decimal(row["markPrice"])
                        for row in await self.exchange.position_details()
                    ),
                    Decimal(0),
                )
                if occupied + notional > equity * Decimal(str(self.s.simple_total_margin_cap * 2)):
                    raise size_error("total margin cap exceeded")
                if notional * (Decimal("0.5") + Decimal(str(self.s.fee_rate))) > self.available:
                    raise size_error("available margin insufficient after estimated entry fee")
                if self.s.simple_sizing_mode == "equity_range":
                    quote = self.quotes.get(order.symbol)
                    if (
                        quote is None
                        or not 0
                        <= self.now_ms() - quote.timestamp_ms
                        <= self.s.market_stale_seconds * 1000
                    ):
                        raise EntryVeto("fresh quote required for equity margin allocation")
                    filters = self.exchange.filters[order.symbol]
                    allocation_price = max(
                        order.limit_price, filters.round_price(quote.ask * Decimal("1.001"))
                    )
                    try:
                        require_equity_margin_allocation(
                            self.s,
                            float(equity),
                            float(order.quantity * allocation_price),
                            float(self.available),
                            float(filters.step * allocation_price),
                        )
                    except SafeError as exc:
                        raise EntryVeto(str(exc)) from None
            if self.s.simple_ai_enabled:
                now_ms = self.now_ms()
                review_ms = self.s.simple_ai_review_bar_ms
                plan = active_plan(self.s, self.store, now_ms // review_ms * review_ms, now_ms)
                permission = self.store.get("simple_ai_order:" + order.client_id, {})
                if (
                    plan is None
                    or plan.action != "trade"
                    or permission.get("digest") != plan.digest
                    or permission.get("asof_ms") != plan.asof_ms
                    or permission.get("order") != order.model_dump(mode="json")
                ):
                    raise SafeError("adaptive AI order permission missing or expired")
            require_btc_order(self.s, self.store, order, self.now_ms())
            if self.s.simple_entry_timeframe == "1m":
                quote = await self.refresh_quote(order.symbol)
                require_minute_order(self.s, self.store, order, self.now_ms(), quote)
                if float((quote.ask - quote.bid) / quote.mid) > self.s.max_spread:
                    raise EntryVeto("simple spread veto at one-minute dispatch")
            self.exchange.entry_authorized = True
        return await self.exchange.submit(order)

    async def query(self, symbol: str, client_id: str) -> Fill:
        return await self.exchange.query(symbol, client_id)

    async def positions(self) -> dict[str, Decimal]:
        return await self.exchange.positions()

    async def verify_liquidation(self, stop_fraction: float) -> None:
        rows = await self.exchange.position_details()
        gross = Decimal(0)
        for row in rows:
            quantity = Decimal(row["positionAmt"])
            if not quantity:
                continue
            mark = Decimal(row["markPrice"])
            liquidation = Decimal(row["liquidationPrice"])
            if not mark.is_finite() or mark <= 0 or not liquidation.is_finite() or liquidation < 0:
                raise SafeError("actual liquidation data unavailable")
            gross += abs(quantity) * mark
            if liquidation == 0 and self.s.futures_account_mode == "credits":
                continue
            if liquidation <= 0 or abs(mark - liquidation) / mark < Decimal(
                str(max(0.25, 5 * stop_fraction))
            ):
                raise SafeError("actual liquidation buffer insufficient")
        if gross > self.equity() * Decimal(str(self.s.max_gross_notional)):
            raise SafeError("actual cross account exposure exceeded")

    async def cancel_entries(self) -> None:
        self.exchange.entry_authorized = False
        await self.exchange.cancel_entries()

    async def query_stop(self, key: str) -> dict[str, Any]:
        stop: dict[str, Any] = next(
            (v for v in self.store.get("live_stops", {}).values() if v["key"] == key), {}
        )
        params = {"algoId": stop["algo_id"]} if stop.get("algo_id") else {"clientAlgoId": key}
        for attempt in range(3):
            try:
                return dict(
                    await self.exchange.request("GET", "/fapi/v1/algoOrder", params, signed=True)
                )
            except OrderNotFound:
                if attempt == 2:
                    raise SafeError(
                        "hosted stop lookup failed; protection cannot be confirmed"
                    ) from None
                # A just-created stop can briefly be absent from the lookup index.
                # Retry only the lookup, within the coordinator's protection deadline.
                await asyncio.sleep(0.2 * (attempt + 1))
        raise SafeError("hosted protection unavailable")

    def validate_stop(self, result: dict[str, Any], symbol: str, stop: dict[str, Any]) -> None:
        if not (
            result.get("clientAlgoId") == stop["key"]
            and result.get("symbol") == symbol
            and result.get("side") == ("SELL" if stop["side"] > 0 else "BUY")
            and result.get("positionSide") == "BOTH"
            and result.get("orderType") == "STOP_MARKET"
            and result.get("closePosition") is True
            and result.get("workingType") == "MARK_PRICE"
            and Decimal(str(result.get("triggerPrice", "0"))) == Decimal(stop["trigger"])
        ):
            raise SafeError("exchange protective order does not match journal")

    async def protect(self, symbol: str, side: int, trigger: Decimal, key: str) -> None:
        stops = self.store.get("live_stops", {})
        existing = stops.get(symbol)
        if existing:
            if existing["key"] != key or existing["side"] != side:
                raise SafeError("protective order identity changed")
            result = await self.query_stop(key)
            self.validate_stop(result, symbol, existing)
            if result.get("algoStatus") != "NEW":
                raise SafeError("hosted protection is not active; reconcile execution")
            # Retain the hosted initial stop throughout the trade. Tighter ATR exits are
            # monitored by Engine; no cancel/recreate window can leave the position naked.
            return
        stops[symbol] = {"key": key, "side": side, "trigger": str(trigger), "state": "INTENDED"}
        self.store.put("live_stops", stops)
        created = await self.exchange.create_stop(symbol, side, trigger, key)
        self.validate_stop(created, symbol, stops[symbol])
        if created.get("algoId"):
            stops[symbol]["algo_id"] = str(created["algoId"])
            self.store.put("live_stops", stops)
        result = await self.query_stop(key)
        self.validate_stop(result, symbol, stops[symbol])
        if result.get("algoStatus") != "NEW":
            raise SafeError("protective order activation unconfirmed")
        stops[symbol]["state"] = "CONFIRMED"
        self.store.put("live_stops", stops)

    async def stop_fill(self, symbol: str) -> Fill | None:
        stop = self.store.get("live_stops", {}).get(symbol)
        if stop is None or symbol not in self.store.get("simple_positions", {}):
            return None
        result = await self.query_stop(stop["key"])
        self.validate_stop(result, symbol, stop)
        actual_id = result.get("actualOrderId")
        if actual_id and str(actual_id) != "0":
            order = await self.exchange.request(
                "GET", "/fapi/v1/order", {"symbol": symbol, "orderId": actual_id}, signed=True
            )
            fill = self.exchange._fill(order)
            if fill.status == "FILLED":
                if (
                    fill.symbol != symbol
                    or fill.side != -stop["side"]
                    or fill.quantity <= 0
                    or fill.price <= 0
                ):
                    raise SafeError("protective fill identity mismatch")
                return fill
            raise SafeError("protective execution pending or partial; reconcile before trading")
        if result.get("algoStatus") != "NEW":
            raise SafeError("hosted protection missing or inactive")
        return None

    async def quote(self, quote: Quote) -> list[Fill]:
        self.quotes[quote.symbol] = quote
        fill = await self.stop_fill(quote.symbol)
        return [fill] if fill is not None else []

    async def refresh_funding(self, now_ms: int) -> None:
        for symbol, trade in self.store.get("simple_positions", {}).items():
            last = self.store.get("live_funding_checked:" + trade["id"], trade["opened_ms"])
            if now_ms - last < 60000:
                continue
            # Account income is authoritative; never synthesize real funding from candles.
            rows = await self.exchange.request(
                "GET",
                "/fapi/v1/income",
                {
                    "symbol": symbol,
                    "incomeType": "FUNDING_FEE",
                    "startTime": max(trade["opened_ms"], last - 3600000),
                    "endTime": now_ms,
                    "limit": 1000,
                },
                signed=True,
            )
            if len(rows) >= 1000:
                raise SafeError("funding income history incomplete")
            with self.store.transaction() as conn:
                positions = self.store.get("simple_positions", {}, conn)
                seen = self.store.get("live_funding_ids:" + trade["id"], [], conn)
                for row in rows:
                    identity = str(row["tranId"])
                    if identity in seen or int(row["time"]) <= trade["opened_ms"]:
                        continue
                    if row.get("asset") not in {"BNFCR", "USDT", "USDC"}:
                        raise SafeError("unsupported funding income asset")
                    amount = Decimal(row["income"])
                    if not amount.is_finite():
                        raise SafeError("invalid funding income")
                    positions[symbol]["funding"] += float(amount)
                    seen.append(identity)
                self.store.put("simple_positions", positions, conn)
                self.store.put("live_funding_ids:" + trade["id"], seen, conn)
                self.store.put("live_funding_checked:" + trade["id"], now_ms, conn)

    def funding(self, symbol: str, timestamp_ms: int, rate: Decimal, mark: Decimal) -> Decimal:
        raise SafeError("live funding must use exchange income")

    async def cancel_protection(self, symbol: str) -> None:
        await self.exchange.cancel_protection(symbol)
        stops = self.store.get("live_stops", {})
        stops.pop(symbol, None)
        self.store.put("live_stops", stops)
