"""Offline distance-based pairs research with monthly past-only formation.

This is a log-price-ratio strategy, not a claim of tested cointegration.
Both futures legs open and close together in the model; live legging is not modeled.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any

import numpy as np

from .redesign import DAY, FRICTION, FUTURE_FEE, HOUR, History


@dataclass(frozen=True)
class PairSpec:
    lookback: int
    entry_z: float
    max_days: int = 14

    @property
    def key(self) -> str:
        return f"pairs-{self.lookback}-{self.entry_z:g}-{self.max_days}"


SPECS = tuple(PairSpec(days, z) for days in (30, 60) for z in (2.0, 2.5))


def formation(history: History, at_hour: int, days: int) -> list[dict[str, Any]]:
    # Monthly formation uses only days completed before that formation boundary.
    end = (at_hour // (30 * 24)) * 30
    if end < days:
        return []
    values = np.log(history.daily["close"][end - days : end])
    returns = np.diff(values, axis=0)
    models = []
    for a, b in combinations(range(len(history.symbols)), 2):
        if (
            min(history.daily["volume"][end - 1, a], history.daily["volume"][end - 1, b])
            < 10_000_000
        ):
            continue
        if min(np.std(returns[:, a]), np.std(returns[:, b])) <= 0:
            continue
        correlation = float(np.corrcoef(returns[:, a], returns[:, b])[0, 1])
        spread = values[:, a] - values[:, b]
        x, y = spread[:-1], np.diff(spread)
        variance = float(np.var(x))
        if variance <= 0:
            continue
        slope = float(np.mean((x - x.mean()) * (y - y.mean())) / variance)
        half_life = -np.log(2) / slope if slope < 0 else np.inf
        deviation = float(np.std(spread, ddof=1))
        if correlation < 0.5 or not 1 <= half_life <= 20 or deviation <= 0:
            continue
        models.append(
            {
                "a": a,
                "b": b,
                "mean": float(spread.mean()),
                "std": deviation,
                "correlation": correlation,
                "half_life_days": float(half_life),
                "formed_before_ms": history.start + end * DAY,
            }
        )
    return sorted(models, key=lambda m: (m["std"], m["a"], m["b"]))[:5]


def replay_pairs(
    history: History,
    spec: PairSpec,
    start: int,
    end: int,
    *,
    cost_multiplier: float = 1,
    delay_hours: int = 0,
) -> dict[str, Any]:
    if (
        start < history.start + 60 * DAY
        or end > history.end
        or start >= end
        or start % DAY
        or end % DAY
        or cost_multiplier < 1
        or delay_hours not in (0, 1)
    ):
        raise ValueError("invalid pairs research bounds")
    first, last = (start - history.start) // HOUR, (end - history.start) // HOUR
    cash, peak, drawdown = 10000.0, 10000.0, 0.0
    fees = friction = funding_total = 0.0
    positions, trades, daily = [], [], []
    models_cache, cooldown = {}, {}
    slip, fee_rate = FRICTION * cost_multiplier, FUTURE_FEE * cost_multiplier

    def mark(p, prices):
        return float(np.dot(p["q"], prices - p["entry"]))

    def current_equity(i, field="open"):
        return cash + sum(mark(p, history.future[field][i, p["indices"]]) for p in positions)

    def close(p, prices, i, when, reason):
        nonlocal cash, fees, friction
        fills = prices * (1 - np.sign(p["q"]) * slip)
        commissions = float(np.dot(abs(p["q"]), fills) * fee_rate)
        gain = mark(p, fills) - commissions
        cash += gain
        fees += commissions
        friction += float(np.dot(abs(p["q"]), prices) * slip)
        trades.append(
            {
                "symbols": [history.symbols[j] for j in p["indices"]],
                "opened_ms": p["opened_ms"],
                "closed_ms": when,
                "reason": reason,
                "net": gain - p["entry_fees"] + p["funding"],
            }
        )
        positions[:] = [position for position in positions if position is not p]
        cooldown[tuple(p["indices"])] = i + 24

    for i in range(first, last):
        now = history.start + i * HOUR
        for p in list(positions):
            prior = history.future["close"][i - 1, p["indices"]]
            z = (np.log(prior[0] / prior[1]) - p["model"]["mean"]) / p["model"]["std"]
            prices = history.future["open"][i, p["indices"]]
            net = mark(p, prices) + p["funding"] - p["entry_fees"]
            reason = (
                "spread convergence"
                if abs(z) <= 0.5 or z * p["entry_z"] <= 0
                else "spread divergence"
                if abs(z) >= 4
                else "pair loss limit"
                if net <= -0.04 * p["gross"]
                else "pair time limit"
                if now - p["opened_ms"] >= spec.max_days * DAY
                else ""
            )
            if reason:
                close(p, prices, i, now, reason)
        signal_i = i - delay_hours
        if signal_i % 6 == 0 and now + DAY < end and len(positions) < 2:
            formed = signal_i // (30 * 24)
            if formed not in models_cache:
                models_cache[formed] = formation(history, signal_i, spec.lookback)
            candidates = []
            for model in models_cache[formed]:
                indices = [model["a"], model["b"]]
                prices = history.future["close"][signal_i - 1, indices]
                z = float((np.log(prices[0] / prices[1]) - model["mean"]) / model["std"])
                # Expected convergence must exceed both legs' round trips plus 20bps.
                if (
                    spec.entry_z <= abs(z) < 4
                    and (abs(z) - 0.5) * model["std"] > 4 * (FUTURE_FEE + FRICTION) + 0.002
                ):
                    candidates.append((abs(z), z, indices, model))
            for _, z, indices, model in sorted(candidates, key=lambda v: -v[0]):
                used = {j for p in positions for j in p["indices"]}
                if len(positions) >= 2:
                    break
                if used.intersection(indices) or i < cooldown.get(tuple(indices), 0):
                    continue
                eq = current_equity(i)
                if eq <= 0:
                    break
                prices = history.future["open"][i, indices]
                sides = np.array([-np.sign(z), np.sign(z)])
                entry = prices * (1 + sides * slip)
                q = sides * eq * 0.2 / entry
                gross = float(np.dot(abs(q), entry))
                entry_fees = gross * fee_rate
                if cash - sum(p["gross"] for p in positions) < gross + entry_fees:
                    continue
                cash -= entry_fees
                fees += entry_fees
                friction += float(np.dot(abs(q), prices) * slip)
                positions.append(
                    {
                        "indices": indices,
                        "q": q,
                        "entry": entry,
                        "model": model,
                        "entry_z": z,
                        "opened_ms": now,
                        "entry_fees": entry_fees,
                        "funding": 0.0,
                        "gross": gross,
                    }
                )
        for p in list(positions):
            indices, q = p["indices"], p["q"]
            positive, negative = (
                history.funding_positive[i, indices],
                history.funding_negative[i, indices],
            )
            payment = float(-np.dot(q, positive + negative))
            debits = float(np.dot(abs(q), np.where(q > 0, positive, -negative)))
            payment -= (cost_multiplier - 1) * debits
            cash += payment
            funding_total += payment
            p["funding"] += payment
            adverse = np.where(
                q > 0, history.future["low"][i, indices], history.future["high"][i, indices]
            )
            if mark(p, adverse) + p["funding"] - p["entry_fees"] <= -0.04 * p["gross"]:
                close(p, adverse, i, now + HOUR, "conservative intrabar pair stop")
        eq = current_equity(i, "close")
        peak = max(peak, eq)
        drawdown = max(drawdown, 1 - eq / peak)
        if (i + 1) % 24 == 0:
            daily.append(eq)
    for p in list(positions):
        close(
            p, history.future["close"][last - 1, p["indices"]], last - 1, end, "period liquidation"
        )
    daily[-1] = cash
    drawdown = max(drawdown, 1 - cash / peak)
    profits = [t["net"] for t in trades]
    if not np.isclose(sum(profits), cash - 10000, atol=1e-7):
        raise AssertionError("pair cash does not reconcile to net outcomes")
    wins, losses = sum(p for p in profits if p > 0), -sum(p for p in profits if p < 0)
    return {
        "candidate": asdict(spec),
        "start_ms": start,
        "end_ms": end,
        "return": cash / 10000 - 1,
        "net": cash - 10000,
        "trades": len(trades),
        "fees": fees,
        "estimated_friction": friction,
        "funding": funding_total,
        "gross_price_pnl_before_friction": cash - 10000 + fees + friction - funding_total,
        "win_rate": float(np.mean(np.array(profits) > 0)) if profits else None,
        "profit_factor": wins / losses if losses else None,
        "max_drawdown_hourly_close": drawdown,
        "median_hold_hours": float(
            np.median([(t["closed_ms"] - t["opened_ms"]) / HOUR for t in trades])
        )
        if trades
        else None,
        "cost_multiplier": cost_multiplier,
        "delay_hours": delay_hours,
        "daily_equity": daily,
        "trade_records": trades,
    }
