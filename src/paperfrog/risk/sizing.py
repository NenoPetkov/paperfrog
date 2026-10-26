from __future__ import annotations

import math
from dataclasses import dataclass

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings

MINIMUM_EQUITY_MARGIN = 0.10
MAXIMUM_EQUITY_MARGIN = 0.50


@dataclass(frozen=True)
class EquityMarginBounds:
    minimum_notional: float
    maximum_notional: float
    available_notional: float
    remaining_balance: bool


def equity_margin_bounds(s: Settings, equity: float, available_margin: float) -> EquityMarginBounds:
    if (
        s.simple_sizing_mode != "equity_range"
        or not all(math.isfinite(v) for v in (equity, available_margin))
        or equity <= 0
        or available_margin < 0
        or not MINIMUM_EQUITY_MARGIN <= s.simple_position_margin_cap <= MAXIMUM_EQUITY_MARGIN
    ):
        raise SafeError("invalid equity margin sizing inputs")
    leverage = s.exchange_leverage
    minimum = equity * MINIMUM_EQUITY_MARGIN * leverage
    maximum = equity * s.simple_position_margin_cap * leverage
    available = available_margin / (1 / leverage + s.fee_rate)
    return EquityMarginBounds(minimum, maximum, available, available < minimum - 1e-9)


def require_equity_margin_allocation(
    s: Settings,
    equity: float,
    notional: float,
    available_margin: float,
    quantity_step_notional: float,
    allocation_scale: float = 1.0,
) -> EquityMarginBounds:
    if not math.isfinite(allocation_scale) or not 0 < allocation_scale <= 1:
        raise SafeError("allocation scale must reduce or preserve position size")
    # Validate reduced orders against the same unscaled allocation, including
    # the available-balance rule. Never round a small probe up to the normal floor.
    notional /= allocation_scale
    quantity_step_notional /= allocation_scale
    bounds = equity_margin_bounds(s, equity, available_margin)
    if (
        not all(math.isfinite(v) for v in (notional, quantity_step_notional))
        or notional <= 0
        or quantity_step_notional < 0
        or quantity_step_notional > notional
    ):
        raise SafeError("invalid equity margin allocation")
    if notional > bounds.maximum_notional + 1e-9:
        raise SafeError("equity margin allocation exceeds 50% ceiling")
    if notional > bounds.available_notional + 1e-9:
        raise SafeError("equity margin allocation exceeds available funds after entry fee")
    if bounds.remaining_balance:
        if bounds.available_notional - notional > quantity_step_notional + 1e-9:
            raise SafeError("remaining margin must be fully allocated within quantity rounding")
    elif notional < bounds.minimum_notional - 1e-9:
        raise SafeError("equity margin allocation below 10% minimum")
    return bounds
