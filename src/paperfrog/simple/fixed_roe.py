"""Fixed gross margin-return brackets, independent of trend or holding time."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from paperfrog.domain.types import SafeError

TARGET = Decimal("0.05")
LEVERAGE = 2
PRICE_FRACTION = TARGET / LEVERAGE


def trigger(entry: Decimal, side: int, tick: Decimal, *, profit: bool) -> Decimal:
    if (
        not entry.is_finite()
        or not tick.is_finite()
        or min(entry, tick) <= 0
        or side not in {-1, 1}
    ):
        raise SafeError("fixed-return bracket inputs invalid")
    raw = entry * (1 + PRICE_FRACTION * side * (1 if profit else -1))
    # Round away from entry so tick precision cannot trigger below 5% gross ROE.
    upward = (side > 0) if profit else (side < 0)
    return (raw / tick).to_integral_value(rounding=ROUND_UP if upward else ROUND_DOWN) * tick


def reason(position: dict[str, Any], mark: float) -> str | None:
    try:
        price = Decimal(str(mark))
        entry = Decimal(str(position["entry_price"]))
        stop = Decimal(str(position["stop_price"]))
        take = Decimal(str(position["take_profit_price"]))
        side = int(position["side"])
        if (
            position.get("entry_leverage") != LEVERAGE
            or Decimal(str(position.get("roe_target"))) != TARGET
            or side not in {-1, 1}
            or not all(v.is_finite() and v > 0 for v in (price, entry, stop, take))
            or side * (stop - entry) >= 0
            or side * (take - entry) <= 0
        ):
            raise ValueError
        if side * (price - stop) <= 0:
            return "fixed -5% gross margin-return stop reached"
        if side * (price - take) >= 0:
            return "fixed +5% gross margin-return target reached"
        return None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise SafeError(
            "fixed-return position evidence invalid; retain hosted protection and reconcile"
        ) from None
