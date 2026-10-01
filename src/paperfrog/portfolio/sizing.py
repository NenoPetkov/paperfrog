from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray
from sklearn.linear_model import Ridge

from paperfrog.domain.types import Filters, Order, Quote, SafeError
from paperfrog.execution.coordinator import client_id
from paperfrog.risk.engine import Proposal
from paperfrog.settings import Settings


@dataclass(frozen=True)
class Hedge:
    btc: float
    eth: float
    r_squared: float
    instability: float


def estimate_hedge(returns: NDArray[np.float64], minimum: int = 960) -> Hedge:
    if len(returns) < minimum or returns.shape[1] != 3 or not np.isfinite(returns).all():
        raise SafeError("insufficient finite historical hedge returns")
    sample = returns[-2880:]
    x, y = sample[:, 1:] * 100, sample[:, 0] * 100
    model = Ridge(alpha=10.0).fit(x, y)
    recent = Ridge(alpha=10.0).fit(x[len(x) // 2 :], y[len(x) // 2 :])
    beta = model.coef_
    instability = float(np.max(np.abs(beta - recent.coef_)))
    if np.any(beta < 0) or np.any(beta > 2) or beta.sum() > 3 or instability > 0.5:
        raise SafeError("unstable or nonsensical hedge coefficients")
    return Hedge(float(beta[0]), float(beta[1]), float(model.score(x, y)), instability)


def size_pair(
    s: Settings,
    trade_id: str,
    symbol: str,
    direction: int,
    equity: float,
    stop_fraction: float,
    hedge: Hedge,
    quotes: dict[str, Quote],
    filters: dict[str, Filters],
    risk_multiplier: float = 1.0,
    liquidation_distance: float = 0.25,
) -> tuple[Proposal, list[Order]]:
    if not 0 < risk_multiplier <= 1 or not 0 < stop_fraction < 0.2 or direction not in {-1, 1}:
        raise SafeError("invalid sizing input")
    if min(hedge.btc, hedge.eth) < 0 or max(hedge.btc, hedge.eth) > 2:
        raise SafeError("invalid hedge")
    total_coefficient = 1 + hedge.btc + hedge.eth
    # Reserve stop loss and round-trip taker costs on every leg.
    loss_fraction = total_coefficient * (stop_fraction + 2 * s.fee_rate + 0.002)
    target = min(
        equity * s.risk_per_trade * risk_multiplier / loss_fraction,
        equity * s.max_target_notional,
        equity * s.max_gross_notional / total_coefficient,
    )
    orders: list[Order] = []
    for asset, coefficient, side in (
        (symbol, 1.0, direction),
        ("BTCUSDT", hedge.btc, -direction),
        ("ETHUSDT", hedge.eth, -direction),
    ):
        if coefficient == 0:
            continue
        q, f = quotes[asset], filters[asset]
        if float((q.ask - q.bid) / q.mid) > s.max_spread:
            raise SafeError("spread veto")
        limit = f.round_price(q.ask * Decimal("1.001") if side > 0 else q.bid * Decimal(".999"))
        qty = f.round_quantity(Decimal(str(target * coefficient)) / limit)
        order = Order(
            client_id=client_id(trade_id, asset),
            symbol=asset,
            side=side,
            quantity=qty,
            limit_price=limit,
        )
        f.validate(order)
        orders.append(order)
    actual = {o.symbol: float(o.quantity * o.limit_price) for o in orders}
    actual_target = actual[symbol]
    gross = sum(actual.values())
    beta = (
        direction
        * (
            actual_target * (hedge.btc + hedge.eth)
            - actual.get("BTCUSDT", 0)
            - actual.get("ETHUSDT", 0)
        )
        / equity
    )
    participation = max(
        float(
            o.quantity
            / max(Decimal(".0000001"), min(quotes[o.symbol].bid_size, quotes[o.symbol].ask_size))
        )
        for o in orders
    )
    proposal = Proposal(
        trade_id,
        symbol,
        equity,
        actual_target,
        gross,
        gross * (stop_fraction + 2 * s.fee_rate + 0.002),
        beta,
        stop_fraction,
        liquidation_distance,
        participation,
    )
    return proposal, orders
