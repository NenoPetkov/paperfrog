"""Immutable per-position gross margin brackets; bounded to 3%, 4% or 5%."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from paperfrog.domain.types import SafeError

TARGET = Decimal("0.05")
LEVERAGE = 2
PRICE_FRACTION = TARGET / LEVERAGE


def target(position: dict[str, Any], *, profit: bool) -> Decimal:
    value = Decimal(str(position.get("roe_target" if profit else "stop_roe", 0.05)))
    if not value.is_finite() or value not in {Decimal("0.03"), Decimal("0.04"), TARGET}:
        raise SafeError("invalid bounded margin-return target")
    return value


def position_trigger(entry: Decimal, side: int, tick: Decimal, position: dict[str, Any], *, profit: bool) -> Decimal:
    return trigger(entry, side, tick, profit=profit, roe=target(position, profit=profit))


def trigger(entry: Decimal, side: int, tick: Decimal, *, profit: bool, roe: Decimal = TARGET) -> Decimal:
    if (
        not roe.is_finite() or roe not in {Decimal("0.03"), Decimal("0.04"), TARGET}
        or not entry.is_finite()
        or not tick.is_finite()
        or min(entry, tick) <= 0
        or side not in {-1, 1}
    ):
        raise SafeError("fixed-return bracket inputs invalid")
    raw = entry * (1 + roe / LEVERAGE * side * (1 if profit else -1))
    # Round away from entry so tick precision cannot trigger inside the captured target.
    upward = (side > 0) if profit else (side < 0)
    return (raw / tick).to_integral_value(rounding=ROUND_UP if upward else ROUND_DOWN) * tick


def reason(position: dict[str, Any], mark: float) -> str | None:
    try:
        price = Decimal(str(mark))
        entry = Decimal(str(position["entry_price"]))
        stop = Decimal(str(position["stop_price"]))
        take = Decimal(str(position["take_profit_price"]))
        side = int(position["side"])
        profit_roe, stop_roe = target(position, profit=True), target(position, profit=False)
        if (
            position.get("entry_leverage") != LEVERAGE
            or "roe_target" not in position
            or side not in {-1, 1}
            or not all(v.is_finite() and v > 0 for v in (price, entry, stop, take))
            or side * (stop - entry) >= 0
            or side * (take - entry) <= 0
        ):
            raise ValueError
        if side * (price - stop) <= 0:
            return f"fixed -{stop_roe:.0%} gross margin-return stop reached"
        if side * (price - take) >= 0:
            return f"fixed +{profit_roe:.0%} gross margin-return target reached"
        return None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise SafeError(
            "fixed-return position evidence invalid; retain hosted protection and reconcile"
        ) from None
