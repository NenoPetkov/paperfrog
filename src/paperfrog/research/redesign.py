"""Independent offline portfolio algorithms; no live engine or exchange client.

Hourly fills and risk checks, completed daily signals, actual historical funding.
Spot/futures carry holds equal coin quantities. All costs are explicit estimates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

HOUR = 3_600_000
DAY = 24 * HOUR
FUTURE_FEE = 0.0005
SPOT_FEE = 0.001
FRICTION = 0.00035


@dataclass(frozen=True)
class Candidate:
    family: str
    lookback: int
    holding: int
    stop_atr: float = 3.0

    @property
    def key(self) -> str:
        return f"{self.family}-{self.lookback}-{self.holding}-{self.stop_atr:g}"


CANDIDATES = (
    *(Candidate("carry", lookback, hold) for lookback in (7, 30) for hold in (7, 30)),
    *(
        Candidate("channel", lookback, exit_days, stop)
        for lookback, exit_days in ((20, 10), (55, 20))
        for stop in (2.0, 3.0)
    ),
    *(Candidate("relative_momentum", lookback, 7) for lookback in (7, 30)),
    *(Candidate("relative_reversal", lookback, hold) for lookback in (1, 7) for hold in (1, 7)),
)


class History:
    def __init__(
        self,
        futures: dict[str, pd.DataFrame],
        spot: dict[str, pd.DataFrame],
        funding: dict[str, list[dict[str, Any]]],
    ):
        self.symbols = tuple(sorted(futures))
        if set(spot) != set(futures) or set(funding) != set(futures):
            raise ValueError("all venues and funding must share a universe")
        times = futures[self.symbols[0]].open_ms.to_numpy(dtype=np.int64)
        if (
            len(times) < 60 * 24
            or times[0] % DAY
            or len(times) % 24
            or (np.diff(times) != HOUR).any()
        ):
            raise ValueError("complete continuous days and at least 60 warmup days required")
        self.start, self.end = int(times[0]), int(times[-1]) + HOUR
        for frame in (*futures.values(), *spot.values()):
            if not np.array_equal(frame.open_ms, times):
                raise ValueError("venues must have identical hourly timestamps")
            values = frame[["open", "high", "low", "close", "volume"]].to_numpy()
            if (
                not np.isfinite(values).all()
                or (values[:, :4] <= 0).any()
                or (values[:, 4] < 0).any()
                or (frame.high < frame[["open", "low", "close"]].max(axis=1)).any()
                or (frame.low > frame[["open", "high", "close"]].min(axis=1)).any()
            ):
                raise ValueError("invalid history observations")
        self.future = {
            k: np.column_stack([futures[s][k] for s in self.symbols])
            for k in ("open", "high", "low", "close", "volume")
        }
        self.spot = {
            k: np.column_stack([spot[s][k] for s in self.symbols])
            for k in ("open", "high", "low", "close")
        }
        n = len(times)
        self.rates, self.funding_positive, self.funding_negative = (
            np.zeros((n, len(self.symbols))) for _ in range(3)
        )
        for j, symbol in enumerate(self.symbols):
            events = funding[symbol]
            if any(
                b["timestamp_ms"] <= a["timestamp_ms"]
                for a, b in zip(events, events[1:], strict=False)
            ):
                raise ValueError("funding events must be ordered and unique")
            for event in events:
                rate, mark = float(event["funding_rate"]), float(event["mark"])
                if not np.isfinite([rate, mark]).all() or mark <= 0:
                    raise ValueError("invalid funding event")
                # Events in (bar open, bar close]; entry-instant event is excluded.
                i = (int(event["timestamp_ms"]) - self.start - 1) // HOUR
                if 0 <= i < n:
                    self.rates[i, j] += rate
                    self.funding_positive[i, j] += max(0, rate * mark)
                    self.funding_negative[i, j] += min(0, rate * mark)
        self.rate_sum = np.vstack([np.zeros(len(self.symbols)), self.rates.cumsum(axis=0)])
        self.daily = {}
        for key, values in self.future.items():
            grouped = values.reshape(-1, 24, len(self.symbols))
            self.daily[key] = (
                grouped[:, -1]
                if key == "close"
                else grouped[:, 0]
                if key == "open"
                else grouped.max(axis=1)
                if key == "high"
                else grouped.min(axis=1)
                if key == "low"
                else grouped.sum(axis=1)
            )
        previous = np.roll(self.daily["close"], 1, axis=0)
        previous[0] = self.daily["open"][0]
        tr = np.maximum(
            self.daily["high"] - self.daily["low"],
            np.maximum(abs(self.daily["high"] - previous), abs(self.daily["low"] - previous)),
        )
        self.atr = pd.DataFrame(tr).rolling(14).mean().to_numpy()
        self.upper = {
            days: pd.DataFrame(self.daily["high"]).rolling(days).max().shift(1).to_numpy()
            for days in (10, 20, 55)
        }
        self.lower = {
            days: pd.DataFrame(self.daily["low"]).rolling(days).min().shift(1).to_numpy()
            for days in (10, 20, 55)
        }

    def past_funding(self, i: int, days: int) -> np.ndarray:
        # One-hour publication lag: never use the boundary's just-settled rate.
        return (self.rate_sum[max(0, i - 1)] - self.rate_sum[max(0, i - days * 24)]) / days

    def targets(self, candidate: Candidate, i: int) -> list[tuple[int, int, float]]:
        d = i // 24 - 1
        if d < max(candidate.lookback + 1, 14):
            return []
        eligible = self.daily["volume"][d] >= 10_000_000
        if candidate.family == "static_carry":
            # Fixed major assets, no ranking by realized profit or future funding.
            rates = self.past_funding(i, 30)
            return [
                (j, -1, float(rates[j]))
                for j, symbol in enumerate(self.symbols)
                if symbol in {"BTCUSDT", "ETHUSDT"} and eligible[j] and rates[j] > 0
            ]
        if candidate.family == "carry":
            expected = self.past_funding(i, candidate.lookback) * candidate.holding
            pair_roundtrip = 2 * (FUTURE_FEE + SPOT_FEE + 2 * FRICTION)
            indices = np.flatnonzero(eligible & (expected > 2 * pair_roundtrip))
            return [
                (int(j), -1, float(expected[j]))
                for j in sorted(indices, key=lambda j: (-expected[j], self.symbols[j]))
            ]
        returns = self.daily["close"][d] / self.daily["close"][d - candidate.lookback] - 1
        if candidate.family == "channel":
            close = self.daily["close"][d]
            sides = np.where(
                close > self.upper[candidate.lookback][d],
                1,
                np.where(close < self.lower[candidate.lookback][d], -1, 0),
            )
            indices = np.flatnonzero(eligible & (sides != 0))
            return [
                (int(j), int(sides[j]), float(abs(returns[j])))
                for j in sorted(indices, key=lambda j: (-abs(returns[j]), self.symbols[j]))
            ]
        indices = sorted(np.flatnonzero(eligible), key=lambda j: (returns[j], self.symbols[j]))
        if len(indices) < 4 or returns[indices[-1]] == returns[indices[0]]:
            return []
        direction = 1 if candidate.family == "relative_momentum" else -1
        return [(int(j), direction, float(returns[j])) for j in indices[-2:]] + [
            (int(j), -direction, float(-returns[j])) for j in indices[:2]
        ]


def replay(
    history: History,
    candidate: Candidate,
    start: int,
    end: int,
    *,
    cost_multiplier: float = 1.0,
    delay_hours: int = 0,
) -> dict[str, Any]:
    if (
        start % DAY
        or end % DAY
        or start < history.start + 60 * DAY
        or end > history.end
        or end <= start
        or cost_multiplier < 1
        or delay_hours not in (0, 1)
    ):
        raise ValueError("invalid replay boundaries, warmup or execution assumptions")
    cash = 10000.0
    positions: dict[int, dict[str, Any]] = {}
    trades, daily_equity = [], []
    fees_paid = friction_paid = funding_total = 0.0
    peak, drawdown, adverse_drawdown = cash, 0.0, 0.0
    cooldown: dict[int, int] = {}
    first, last = (start - history.start) // HOUR, (end - history.start) // HOUR
    slip = FRICTION * cost_multiplier
    ffee, sfee = FUTURE_FEE * cost_multiplier, SPOT_FEE * cost_multiplier
    static_carry = candidate.family == "static_carry"
    carry = candidate.family in {"carry", "static_carry"}

    def equity(i: int, point: str = "open") -> float:
        total = cash
        for j, p in positions.items():
            total += p["quantity"] * (history.future[point][i, j] - p["entry"])
            if carry:
                total += abs(p["quantity"]) * history.spot[point][i, j]
        return float(total)

    def close(j: int, i: int, fp: float, sp: float, reason: str, when: int) -> None:
        nonlocal cash, fees_paid, friction_paid
        p = positions.pop(j)
        q, side = p["quantity"], np.sign(p["quantity"])
        fill = fp * (1 - side * slip)
        fee = abs(q) * fill * ffee
        profit = q * (fill - p["entry"]) - fee
        fees_paid += fee
        friction_paid += abs(q) * fp * slip
        cash += profit
        if carry:
            spot_fill = sp * (1 - slip)
            spot_fee = abs(q) * spot_fill * sfee
            cash += abs(q) * spot_fill - spot_fee
            profit += abs(q) * (spot_fill - p["spot_entry"]) - spot_fee
            fees_paid += spot_fee
            friction_paid += abs(q) * sp * slip
        trades.append(
            {
                "symbol": history.symbols[j],
                "side": int(side),
                "net": float(profit - p["entry_fees"] + p["funding"]),
                "opened_ms": p["opened_ms"],
                "closed_ms": when,
                "reason": reason,
            }
        )
        cooldown[j] = i + 24

    def enter(j: int, side: int, i: int, now: int, eq: float) -> None:
        nonlocal cash, fees_paid, friction_paid
        fp, sp = history.future["open"][i, j], history.spot["open"][i, j]
        if carry and not -0.001 <= fp / sp - 1 <= 0.005:
            return
        q = (
            (eq * (0.18 if static_carry else 0.12) / (sp * (1 + slip)))
            if carry
            else eq * 0.2 / (fp * (1 + side * slip))
        )
        entry = fp * (1 + side * slip)
        spot_entry = sp * (1 + slip) if carry else 0.0
        fee = q * entry * ffee + (q * spot_entry * sfee if carry else 0)
        margin = q * spot_entry * 1.5 if carry else q * entry
        reserved = sum(p["margin"] for p in positions.values())
        if cash - reserved < margin + (q * spot_entry if carry else 0) + fee:
            return
        cash -= fee + (q * spot_entry if carry else 0)
        fees_paid += fee
        friction_paid += q * (fp + (sp if carry else 0)) * slip
        d = i // 24 - 1
        stop_fraction = min(0.10, max(0.02, candidate.stop_atr * history.atr[d, j] / fp))
        positions[j] = {
            "quantity": float(side * q),
            "entry": float(entry),
            "spot_entry": float(spot_entry),
            "entry_fees": float(fee),
            "funding": 0.0,
            "margin": float(margin),
            "opened_ms": now,
            "opened_i": i,
            "stop": float(entry * (1 - side * stop_fraction)),
        }

    for i in range(first, last):
        now = history.start + i * HOUR
        d = i // 24 - 1
        decision = i % 24 == delay_hours
        # Daily signals are deliberately independent of the previous bot's gates.
        rebalance = decision and (i - first - delay_hours) // 24 % candidate.holding == 0
        for j, p in list(positions.items()):
            fp, sp = history.future["open"][i, j], history.spot["open"][i, j]
            side = int(np.sign(p["quantity"]))
            reason = ""
            if carry:
                pair_pnl = (
                    p["quantity"] * (fp - p["entry"])
                    + abs(p["quantity"]) * (sp - p["spot_entry"])
                    + p["funding"]
                    - p["entry_fees"]
                )
                if pair_pnl < -0.03 * abs(p["quantity"]) * p["spot_entry"]:
                    reason = "basis loss limit"
                elif (
                    not static_carry
                    and decision
                    and (
                        now - p["opened_ms"] >= candidate.holding * DAY
                        or history.past_funding(i, 3)[j] <= 0
                    )
                ):
                    reason = "carry horizon or funding reversal"
            elif side * (fp - p["stop"]) <= 0:
                reason = "gap stop"
            elif candidate.family.startswith("relative") and rebalance:
                reason = "scheduled rebalance"
            elif candidate.family == "channel" and decision:
                price = history.daily["close"][d, j]
                if (side > 0 and price < history.lower[candidate.holding][d, j]) or (
                    side < 0 and price > history.upper[candidate.holding][d, j]
                ):
                    reason = "opposite exit channel"
                else:
                    trail = price - side * candidate.stop_atr * history.atr[d, j]
                    p["stop"] = max(p["stop"], trail) if side > 0 else min(p["stop"], trail)
                    if side * (fp - p["stop"]) <= 0:
                        reason = "tightened stop at open"
            if reason:
                close(j, i, fp, sp, reason, now)
        if decision and (carry or candidate.family == "channel" or rebalance):
            eq = equity(i)
            for j, side, _ in history.targets(candidate, i):
                if static_carry and i != first + delay_hours:
                    continue
                if len(positions) >= (2 if static_carry else 3 if carry else 4) or eq <= 0:
                    break
                # Rebalance may renew an unchanged leg; charge full costs conservatively.
                if j in positions or (
                    i < cooldown.get(j, 0)
                    and not (candidate.family.startswith("relative") and rebalance)
                ):
                    continue
                if not static_carry and now + (candidate.holding * DAY if carry else DAY) >= end:
                    continue
                enter(j, side, i, now, eq)
        adverse = cash
        for j, p in list(positions.items()):
            q = p["quantity"]
            payment = -q * (history.funding_positive[i, j] + history.funding_negative[i, j])
            debit = abs(q) * (
                history.funding_positive[i, j] if q > 0 else -history.funding_negative[i, j]
            )
            payment -= (cost_multiplier - 1) * debit
            cash += payment
            funding_total += payment
            p["funding"] += payment
            adverse += payment
            fp = history.future["low" if q > 0 else "high"][i, j]
            sp = history.spot["low"][i, j]
            adverse += q * (fp - p["entry"]) + (abs(q) * sp if carry else 0)
            if carry:
                if p["margin"] + q * (fp - p["entry"]) < 0.02 * abs(q) * fp:
                    close(j, i, fp, sp, "conservative margin buffer exit", now + HOUR)
            elif np.sign(q) * (fp - p["stop"]) <= 0:
                # A newly tightened stop can be beyond this open; fill at the adverse open.
                opened = history.future["open"][i, j]
                fill = min(opened, p["stop"]) if q > 0 else max(opened, p["stop"])
                close(j, i, fill, sp, "protective stop", now + HOUR)
        eq = equity(i, "close")
        adverse_drawdown = max(adverse_drawdown, 1 - adverse / peak)
        peak = max(peak, eq)
        drawdown = max(drawdown, 1 - eq / peak)
        if (i + 1) % 24 == 0:
            daily_equity.append(eq)
    for j in list(positions):
        close(
            j,
            last - 1,
            history.future["close"][last - 1, j],
            history.spot["close"][last - 1, j],
            "period liquidation",
            end,
        )
    daily_equity[-1] = cash
    drawdown = max(drawdown, 1 - cash / peak)
    profits = [t["net"] for t in trades]
    wins, losses = sum(p for p in profits if p > 0), -sum(p for p in profits if p < 0)
    if not np.isclose(sum(profits), cash - 10000, atol=1e-7):
        raise AssertionError("portfolio cash does not reconcile to trade outcomes")
    return {
        "candidate": asdict(candidate),
        "start_ms": start,
        "end_ms": end,
        "return": cash / 10000 - 1,
        "net": cash - 10000,
        "trades": len(trades),
        "win_rate": float(np.mean(np.array(profits) > 0)) if profits else None,
        "profit_factor": wins / losses if losses else None,
        "fees": fees_paid,
        "estimated_friction": friction_paid,
        "funding": funding_total,
        "gross_price_pnl_before_friction": cash - 10000 + fees_paid + friction_paid - funding_total,
        "max_drawdown_hourly_close": drawdown,
        "max_drawdown_adverse_extremes": adverse_drawdown,
        "median_hold_hours": float(
            np.median([(t["closed_ms"] - t["opened_ms"]) / HOUR for t in trades])
        )
        if trades
        else None,
        "cost_multiplier": cost_multiplier,
        "delay_hours": delay_hours,
        "daily_equity": daily_equity,
        "trade_records": trades,
    }
