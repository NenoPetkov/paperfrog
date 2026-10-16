from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import time
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets

from paperfrog.domain.types import Fill, Filters, Order, OrderNotFound, SafeError, UnknownOrder
from paperfrog.settings import Settings


class BinanceAdapter:
    """Native USD-M API. No automatic retry of a mutating request."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.base = (
            "https://demo-fapi.binance.com"
            if settings.binance_env == "testnet" and settings.trading_mode != "paper"
            else "https://fapi.binance.com"
        )
        self.ws = (
            "wss://demo-fstream.binance.com"
            if settings.binance_env == "testnet" and settings.trading_mode != "paper"
            else "wss://fstream.binance.com"
        )
        self.client = client or httpx.AsyncClient(timeout=10)
        self.offset_ms = 0
        self.filters: dict[str, Filters] = {}
        self.entry_authorized = False

    async def close(self) -> None:
        await self.client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        keyed: bool = False,
        spot: bool = False,
    ) -> Any:
        headers = {}
        if signed or keyed:
            if not self.s.binance_api_key.get_secret_value():
                raise SafeError("Binance credentials missing")
            headers["X-MBX-APIKEY"] = self.s.binance_api_key.get_secret_value()
        for attempt in range(4 if method == "GET" else 1):
            values = dict(params or {})
            if signed:
                if not self.s.binance_api_secret.get_secret_value():
                    raise SafeError("Binance signing secret missing")
                values.update(timestamp=int(time.time() * 1000) + self.offset_ms, recvWindow=5000)
            query = urlencode(values)
            if signed:
                signature = hmac.new(
                    self.s.binance_api_secret.get_secret_value().encode(),
                    query.encode(),
                    hashlib.sha256,
                ).hexdigest()
                query += "&signature=" + signature
            try:
                response = await self.client.request(
                    method,
                    ("https://api.binance.com" if spot else self.base) + path,
                    params=query,
                    headers=headers,
                )
            except httpx.HTTPError:
                if method != "GET":
                    raise UnknownOrder("exchange mutation result unknown; reconcile") from None
                raise SafeError("exchange transport unavailable") from None
            if response.status_code in (408, 500, 502, 503, 504) and method != "GET":
                raise UnknownOrder("exchange mutation result unknown; reconcile")
            if response.status_code in (418, 429) and method == "GET" and attempt < 3:
                try:
                    wait = max(1.0, float(response.headers.get("Retry-After", 2**attempt)))
                except ValueError:
                    wait = 60.0
                await asyncio.sleep(min(wait, 120) + random.random())
                continue
            if response.is_error:
                try:
                    error = response.json()
                    code = error.get("code") if isinstance(error, dict) else None
                except ValueError:
                    code = None
                if code == -1021 and signed and method == "GET" and attempt == 0:
                    # Only retry reads. A mutation is never replayed after a clock error.
                    await self.sync_time()
                    continue
                if code == -2013:
                    raise OrderNotFound("order not found; absence does not authorize resubmission")
                raise SafeError(
                    f"Binance request rejected: HTTP {response.status_code}, code {code}"
                )
            try:
                return response.json()
            except ValueError:
                raise SafeError("invalid exchange response") from None
        raise SafeError("exchange rate limit exhausted")

    async def sync_time(self) -> None:
        start = int(time.time() * 1000)
        result = await self.request("GET", "/fapi/v1/time")
        self.offset_ms = int(result["serverTime"]) - (start + int(time.time() * 1000)) // 2
        if abs(self.offset_ms) > 5000:
            raise SafeError("local clock skew exceeds five seconds")

    async def exchange_info(self) -> dict[str, Any]:
        info = await self.request("GET", "/fapi/v1/exchangeInfo")
        for symbol in info["symbols"]:
            if (
                symbol["status"] != "TRADING"
                or symbol["contractType"] != "PERPETUAL"
                or symbol["quoteAsset"] != "USDT"
            ):
                continue
            f = {x["filterType"]: x for x in symbol["filters"]}
            self.filters[symbol["symbol"]] = Filters(
                Decimal(f["LOT_SIZE"]["stepSize"]),
                Decimal(f["PRICE_FILTER"]["tickSize"]),
                Decimal(f["LOT_SIZE"]["minQty"]),
                Decimal(f["LOT_SIZE"]["maxQty"]),
                Decimal(f["MIN_NOTIONAL"]["notional"]),
            )
        return dict(info)

    async def submit(self, order: Order) -> Fill:
        if self.s.trading_mode == "paper":
            raise SafeError("paper mode cannot submit exchange orders")
        if not order.reduce_only and not self.entry_authorized:
            raise SafeError("exchange entries are not authorized")
        if order.symbol not in self.filters:
            raise SafeError("exchange filters missing")
        self.filters[order.symbol].validate(order)
        result = await self.request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": order.symbol,
                "side": "BUY" if order.side > 0 else "SELL",
                "positionSide": "BOTH",
                "type": "LIMIT",
                "timeInForce": "IOC",
                "quantity": str(order.quantity),
                "price": str(order.limit_price),
                "reduceOnly": str(order.reduce_only).lower(),
                "newClientOrderId": order.client_id,
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )
        try:
            return self._fill(result)
        except UnknownOrder:
            # A successful submission can still lack execution pricing. Query its
            # original identity; never repeat the mutating request.
            return await self.query(order.symbol, order.client_id)

    def _fill(self, result: dict[str, Any]) -> Fill:
        try:
            quantity = Decimal(result["executedQty"])
            price = Decimal(result.get("avgPrice", "0"))
            if not quantity.is_finite() or quantity < 0 or not price.is_finite() or price < 0:
                raise UnknownOrder("invalid execution pricing; reconcile original order")
            if quantity > 0 and price == 0:
                quote = Decimal(result.get("cumQuote", "0"))
                if not quote.is_finite() or quote <= 0:
                    raise UnknownOrder("execution price unavailable; reconcile original order")
                price = quote / quantity
        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise UnknownOrder("invalid execution pricing; reconcile original order") from None
        return Fill(
            client_id=result["clientOrderId"],
            symbol=result["symbol"],
            side=1 if result["side"] == "BUY" else -1,
            quantity=quantity,
            price=price,
            fee=quantity * price * Decimal(str(self.s.fee_rate)),
            status=result["status"],
            exchange_id=str(result["orderId"]),
        )

    async def query(self, symbol: str, client_id: str) -> Fill:
        return self._fill(
            await self.request(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": client_id},
                signed=True,
            )
        )

    async def account(self) -> dict[str, Any]:
        return dict(await self.request("GET", "/fapi/v3/account", signed=True))

    async def position_details(self) -> list[dict[str, Any]]:
        return list(await self.request("GET", "/fapi/v2/positionRisk", signed=True))

    async def positions(self) -> dict[str, Decimal]:
        rows = await self.position_details()
        if any(x["positionSide"] != "BOTH" for x in rows if Decimal(x["positionAmt"])):
            raise SafeError("hedge mode positions are unsupported")
        return {x["symbol"]: Decimal(x["positionAmt"]) for x in rows if Decimal(x["positionAmt"])}

    async def open_orders(self) -> list[dict[str, Any]]:
        return list(await self.request("GET", "/fapi/v1/openOrders", signed=True))

    async def open_stops(self) -> list[dict[str, Any]]:
        return list(await self.request("GET", "/fapi/v1/openAlgoOrders", signed=True))

    async def cancel_entries(self) -> None:
        for row in await self.open_orders():
            if not row.get("reduceOnly", False):
                await self.request(
                    "DELETE",
                    "/fapi/v1/order",
                    {"symbol": row["symbol"], "orderId": row["orderId"]},
                    signed=True,
                )

    async def protect(self, symbol: str, side: int, trigger: Decimal, key: str) -> None:
        await self.create_stop(symbol, side, trigger, key)

    async def create_stop(
        self, symbol: str, side: int, trigger: Decimal, key: str
    ) -> dict[str, Any]:
        if self.s.trading_mode == "paper":
            raise SafeError("paper mode cannot change exchange stops")
        # Close-all cannot be combined with quantity or reduceOnly per Binance.
        result = await self.request(
            "POST",
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": "SELL" if side > 0 else "BUY",
                "positionSide": "BOTH",
                "type": "STOP_MARKET",
                "triggerPrice": str(trigger),
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                "clientAlgoId": key,
            },
            signed=True,
        )
        return dict(result)

    async def verify_risk_settings(self, symbols: tuple[str, ...]) -> None:
        mode = await self.request("GET", "/fapi/v1/positionSide/dual", signed=True)
        multi = await self.request("GET", "/fapi/v1/multiAssetsMargin", signed=True)
        credits = self.s.futures_account_mode == "credits"
        if (
            mode.get("dualSidePosition") is not False
            or multi.get("multiAssetsMargin") is not credits
        ):
            raise SafeError("one-way position mode and configured asset mode required")
        details = {
            r["symbol"]: r for r in await self.request("GET", "/fapi/v1/symbolConfig", signed=True)
        }
        for symbol in symbols:
            r = details.get(symbol)
            if (
                r is None
                or r["marginType"].lower() != ("crossed" if credits else "isolated")
                or int(r["leverage"]) != 2
            ):
                raise SafeError(
                    "configured margin mode and 2x leverage must be confirmed for every symbol"
                )

    async def prepare_simple_symbol(self, symbol: str) -> None:
        if self.s.trading_mode == "paper" or self.s.strategy_profile != "simple":
            raise SafeError("exchange symbol preparation requires simple exchange mode")
        rows = await self.request("GET", "/fapi/v1/symbolConfig", {"symbol": symbol}, signed=True)
        matches = [r for r in rows if r.get("symbol") == symbol]
        wanted = "crossed" if self.s.futures_account_mode == "credits" else "isolated"
        if len(matches) != 1 or str(matches[0].get("marginType", "")).lower() != wanted:
            raise SafeError("symbol margin mode does not match configured account")
        if int(matches[0]["leverage"]) != 2:
            if (await self.positions()).get(symbol):
                raise SafeError("cannot prepare leverage for an existing position")
            await self.request(
                "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": 2}, signed=True
            )
        await self.verify_risk_settings((symbol,))

    async def configure_flat_account(self, symbols: tuple[str, ...]) -> None:
        if self.s.futures_account_mode == "credits":
            raise SafeError(
                "credits account modes are managed by Binance; isolated configuration unavailable"
            )
        if self.s.trading_mode == "paper":
            raise SafeError("paper mode cannot configure an exchange account")
        if await self.positions() or await self.open_orders() or await self.open_stops():
            raise SafeError("account must be flat before changing settings")
        mode = await self.request("GET", "/fapi/v1/positionSide/dual", signed=True)
        if mode["dualSidePosition"]:
            await self.request(
                "POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}, signed=True
            )
        details = {
            r["symbol"]: r for r in await self.request("GET", "/fapi/v1/symbolConfig", signed=True)
        }
        for symbol in symbols:
            if details.get(symbol, {}).get("marginType", "").lower() != "isolated":
                await self.request(
                    "POST",
                    "/fapi/v1/marginType",
                    {"symbol": symbol, "marginType": "ISOLATED"},
                    signed=True,
                )
            await self.request(
                "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": 2}, signed=True
            )
        await self.verify_risk_settings(symbols)

    async def listen_key(self) -> str:
        return str((await self.request("POST", "/fapi/v1/listenKey", keyed=True))["listenKey"])

    async def renew_listen_key(self) -> None:
        await self.request("PUT", "/fapi/v1/listenKey", keyed=True)

    async def stream(self, stream: str, *, private: bool = False) -> AsyncIterator[dict[str, Any]]:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.ws
                    + (
                        "/private"
                        if private
                        else "/public"
                        if "@depth" in stream or "@bookTicker" in stream
                        else "/market"
                    )
                    + "/ws/"
                    + stream,
                    ping_interval=20,
                    ping_timeout=20,
                ) as socket:
                    started = time.monotonic()
                    backoff = 1.0
                    async for message in socket:
                        payload = json.loads(message)
                        if payload.get("e") == "listenKeyExpired":
                            raise SafeError("user stream expired")
                        yield payload
                        if time.monotonic() - started > 23 * 3600:
                            break
            except (OSError, websockets.WebSocketException):
                if private:
                    raise SafeError(
                        "account stream disconnected; reconcile before reconnect"
                    ) from None
                yield {"e": "disconnect", "E": int(time.time() * 1000)}
                await asyncio.sleep(backoff + random.random())
                backoff = min(30.0, backoff * 2)

    async def liquidation_buffer(self, orders: list[Order]) -> float:
        """Conservative isolated 2x estimate; verify actual liquidation prices after fills."""
        distance = 1.0
        for order in orders:
            data = await self.request(
                "GET", "/fapi/v1/leverageBracket", {"symbol": order.symbol}, signed=True
            )
            row = data[0] if isinstance(data, list) else data
            notional = float(order.quantity * order.limit_price)
            tiers = [
                b
                for b in row["brackets"]
                if float(b["notionalFloor"]) <= notional < float(b["notionalCap"])
            ]
            if len(tiers) != 1 or int(tiers[0]["initialLeverage"]) < 2:
                raise SafeError("maintenance margin bracket unavailable")
            mmr = float(tiers[0]["maintMarginRatio"])
            if not 0 <= mmr < 0.25:
                raise SafeError("unsafe maintenance margin ratio")
            # Ignore beneficial cumulative maintenance deductions. Reserve 2% for costs.
            ratio = (0.5 + 0.02) / (1 - mmr) if order.side > 0 else (1.5 - 0.02) / (1 + mmr)
            distance = min(distance, 1 - ratio if order.side > 0 else ratio - 1)
        return distance

    async def verify_liquidation_buffers(self, minimum: float) -> None:
        for row in await self.position_details():
            if not Decimal(row["positionAmt"]):
                continue
            mark, liquidation = Decimal(row["markPrice"]), Decimal(row["liquidationPrice"])
            if mark <= 0 or liquidation <= 0 or float(abs(mark - liquidation) / mark) < minimum:
                raise SafeError("actual liquidation buffer is unsafe or unknown")

    async def cancel_protection(self, symbol: str) -> None:
        if (await self.positions()).get(symbol):
            raise SafeError("cannot remove protection from an open position")
        for stop in await self.open_stops():
            if stop["symbol"] == symbol and str(stop.get("clientAlgoId", "")).startswith("pf-"):
                await self.request(
                    "DELETE", "/fapi/v1/algoOrder", {"algoId": stop["algoId"]}, signed=True
                )
