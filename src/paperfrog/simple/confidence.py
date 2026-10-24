"""Durable confidence from fully closed positions, never position sizing or risk limits."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection

from paperfrog.domain.types import Quote, SafeError
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

VERSION = 1
KEY = "simple_confidence"
SMALL = 0.003
LARGE = 0.006
WINS_REQUIRED = 3


def state(store: Store, conn: Connection | None = None) -> dict[str, Any]:
    return dict(
        store.get(
            KEY,
            {
                "version": VERSION,
                "wins": 0,
                "losses": 0,
                "breakevens": 0,
                "streak": 0,
                "net_pnl": 0.0,
                "pending": [],
            },
            conn,
        )
    )


def target(store: Store) -> float:
    current = state(store)
    return LARGE if current["streak"] >= WINS_REQUIRED and not current["pending"] else SMALL


def enqueue(store: Store, record: dict[str, Any], conn: Connection) -> None:
    if (
        record.get("confidence_version") != VERSION
        or store.get("simple_confidence_result:" + record["trade_id"], None, conn) is not None
    ):
        return
    current = state(store, conn)
    if record["trade_id"] not in current["pending"]:
        current["pending"].append(record["trade_id"])
        store.put(KEY, current, conn)


def settle(store: Store, record: dict[str, Any], pnl: float, conn: Connection) -> None:
    """One position = one outcome. Transactional, durable, in closure order."""
    key = "simple_confidence_result:" + record["trade_id"]
    if store.get(key, None, conn) is not None:
        return
    if not math.isfinite(pnl):
        raise SafeError("finite net confidence result required")
    current = state(store, conn)
    if not current["pending"] or current["pending"][0] != record["trade_id"]:
        raise SafeError("confidence outcomes must settle in closure order")
    tolerance = max(1e-8, record["entry_price"] * record["quantity"] * 1e-8)
    outcome = "wins" if pnl > tolerance else "losses" if pnl < -tolerance else "breakevens"
    current[outcome] += 1
    current["streak"] = current["streak"] + 1 if outcome == "wins" else 0
    current["net_pnl"] += pnl
    current["pending"].pop(0)
    result = {"trade_id": record["trade_id"], "outcome": outcome, "net_pnl": pnl}
    store.put(key, result, conn)
    store.put(KEY, current, conn)
    store.audit("simple_confidence_result", {**result, "streak": current["streak"]}, conn)


def net_return(s: Settings, trade: dict[str, Any], quote: Quote) -> float:
    """Executable touch with exit slippage reserve; entry costs are already paid."""
    side = int(trade["side"])
    price = float(quote.bid if side > 0 else quote.ask) * (1 - side * s.slippage_bps / 10000)
    quantity = float(trade["quantity"])
    entry = float(trade["entry_price"])
    net = (price - entry) * quantity * side - price * quantity * s.fee_rate
    net += float(trade["funding"]) - float(trade["entry_fee"])
    realized, fees = partial_accounting(trade)
    net += float(realized - fees)
    return net / (entry * float(trade.get("initial_quantity", trade["quantity"])))


def update_runner(trade: dict[str, Any], net: float, activation: float, now_ms: int) -> bool:
    """Arm at the former take-profit; retain half the highest observed net gain.

    State stays on the position across restart/partial fills. The floor never
    decreases. This is a monitored exit, not a guaranteed exchange fill price.
    """
    if not math.isfinite(net) or not math.isfinite(activation) or activation <= 0:
        raise SafeError("finite profit runner observation and positive activation required")
    runner = trade.get("profit_runner")
    if runner is None:
        if net < activation:
            return False
        runner = {"version": 1, "armed_ms": now_ms, "activation_fraction": activation,
                  "peak_net_return": net, "floor_net_return": net / 2}
        trade["profit_runner"] = runner
    else:
        peak, floor = float(runner["peak_net_return"]), float(runner["floor_net_return"])
        if runner.get("version") != 1 or not all(math.isfinite(x) and x > 0 for x in (peak, floor)):
            raise SafeError("invalid saved profit runner state")
        runner["peak_net_return"] = max(peak, net)
        runner["floor_net_return"] = max(floor, runner["peak_net_return"] / 2)
    return net <= float(runner["floor_net_return"])


def partial_accounting(trade: dict[str, Any]) -> tuple[Decimal, Decimal]:
    gross, fees = Decimal(0), Decimal(0)
    for part in trade.get("partial_exit_fills", []):
        fill = part["fill"]
        gross += (
            (Decimal(fill["price"]) - Decimal(str(trade["entry_price"])))
            * Decimal(fill["quantity"])
            * trade["side"]
        )
        fees += Decimal(fill["fee"])
    return gross, fees


def limit_price(s: Settings, trade: dict[str, Any], fraction: float, tick: Decimal) -> Decimal:
    """IOC price boundary that preserves the requested estimated net return."""
    entry, quantity = Decimal(str(trade["entry_price"])), Decimal(trade["quantity"])
    realized, fees = partial_accounting(trade)
    costs = Decimal(str(trade["entry_fee"])) - Decimal(str(trade["funding"])) + fees - realized
    profit = entry * Decimal(trade.get("initial_quantity", trade["quantity"])) * Decimal(str(fraction))
    fee = Decimal(str(s.fee_rate))
    if trade["side"] > 0:
        price = (entry * quantity + costs + profit) / (quantity * (1 - fee))
        return (price / tick).to_integral_value(rounding="ROUND_CEILING") * tick
    price = (entry * quantity - costs - profit) / (quantity * (1 + fee))
    return (price / tick).to_integral_value(rounding="ROUND_FLOOR") * tick


async def reconcile(s: Settings, store: Store, exchange: Any, now_ms: int) -> None:
    """Verify one closed live position per pass. Missing accounting never earns confidence.

    Called after monitoring exits. API/accounting failures only hold the small target;
    they must never prevent protective exits. A minute allows exchange settlement.
    """
    pending = state(store)["pending"]
    if not pending or now_ms < store.get("simple_confidence_retry_ms", 0):
        return
    store.put("simple_confidence_retry_ms", now_ms + 60_000)
    records = store.get("simple_closed", [])
    record = next((r for r in records if r.get("trade_id") == pending[0]), None)
    if record is None or now_ms - record["timestamp_ms"] < 60_000:
        return
    try:
        fees = Decimal(0)
        realized = Decimal(0)
        fill_times: dict[str, list[int]] = {"entry": [], "exit": []}
        for label, side in (("entry", record["direction"]), ("exit", -record["direction"])):
            order_ids = (
                record.get("exit_exchange_ids") or [record.get("exit_exchange_id")]
                if label == "exit"
                else [record.get("entry_exchange_id")]
            )
            if not all(order_ids) or len(set(map(str, order_ids))) != len(order_ids):
                raise SafeError("exchange order identity missing")
            seen: set[str] = set()
            quantity = Decimal(0)
            for order_id in order_ids:
                rows = await exchange.request(
                    "GET",
                    "/fapi/v1/userTrades",
                    {"symbol": record["symbol"], "orderId": order_id, "limit": 1000},
                    signed=True,
                )
                if not isinstance(rows, list) or len(rows) >= 1000:
                    raise SafeError("complete exchange fills required")
                order_quantity = Decimal(0)
                for row in rows:
                    if str(row["orderId"]) != str(order_id):
                        continue
                    identity = str(row["id"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if row["symbol"] != record["symbol"] or row["side"] != (
                        "BUY" if side > 0 else "SELL"
                    ):
                        raise SafeError("exchange fill identity mismatch")
                    if row["commissionAsset"] not in {"BNFCR", "USDT", "USDC"}:
                        raise SafeError("commission currency conversion unavailable")
                    qty, fee, gross = (
                        Decimal(str(row[k])) for k in ("qty", "commission", "realizedPnl")
                    )
                    if not all(v.is_finite() for v in (qty, fee, gross)) or qty <= 0:
                        raise SafeError("invalid exchange accounting")
                    fill_times[label].append(int(row["time"]))
                    order_quantity += qty
                    fees += fee
                    realized += gross
                if order_quantity <= 0:
                    raise SafeError("exchange order fills missing")
                quantity += order_quantity
            if quantity != Decimal(str(record["quantity"])):
                raise SafeError("exchange fill quantities incomplete")
        entry_ms, exit_ms = min(fill_times["entry"]), max(fill_times["exit"])
        if entry_ms <= 0 or exit_ms < entry_ms:
            raise SafeError("invalid exchange fill times")
        rows = await exchange.request(
            "GET",
            "/fapi/v1/income",
            {
                "symbol": record["symbol"],
                "incomeType": "FUNDING_FEE",
                "startTime": entry_ms,
                "endTime": exit_ms,
                "limit": 1000,
            },
            signed=True,
        )
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise SafeError("complete funding history required")
        funding = Decimal(0)
        seen = set()
        for row in rows:
            if not entry_ms < int(row["time"]) <= exit_ms:
                continue
            if row.get("symbol") != record["symbol"] or row.get("incomeType") != "FUNDING_FEE":
                raise SafeError("funding identity mismatch")
            identity = str(row["tranId"])
            if identity in seen:
                continue
            seen.add(identity)
            amount = Decimal(str(row["income"]))
            if row["asset"] not in {"BNFCR", "USDT", "USDC"} or not amount.is_finite():
                raise SafeError("funding currency conversion unavailable")
            funding += amount
        pnl = float(realized - fees + funding)
        with store.transaction() as conn:
            records = store.get("simple_closed", [], conn)
            saved = next(r for r in records if r.get("trade_id") == record["trade_id"])
            saved.update(
                verified_entry_ms=entry_ms,
                verified_exit_ms=exit_ms,
                verified_net_pnl=pnl,
                verified_fees=float(fees),
                verified_funding=float(funding),
                accounting_basis="exchange_fills_and_income",
            )
            settle(store, saved, pnl, conn)
            store.put("simple_closed", records, conn)
    except (SafeError, ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        # No raw response/exception text: it could include credentials or signed URLs.
        store.audit(
            "simple_confidence_accounting_pending",
            {
                "trade_id": record["trade_id"],
                "error_type": type(exc).__name__,
            },
        )
