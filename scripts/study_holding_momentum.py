"""Offline holding counterfactuals and chronological A/B/C portfolio screening.

No account mutation. Fixed 19-coin research universe is not historical top-100
membership. Fees/fills are estimates; portfolio replay is not the live engine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from bisect import bisect_right
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Any

import pandas as pd

from paperfrog.settings import Settings
from paperfrog.simple.learning_replay import FIVE, SYMBOLS, prepare
from paperfrog.simple.minute import validate_history, validate_ohlc
from paperfrog.simple.momentum import classify, reversal
from paperfrog.simple.strategy import execution_rules, tighten_stop

HOUR = 3_600_000
DAY = 24 * HOUR
FEE = 0.0005
SLIP = 0.0007  # 5 bps half-spread plus 2 bps slippage on each fill.
FUNDING = 0.0001  # Adverse reserve per hour; not historical funding.


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = [r["pnl"] for r in rows]
    wins, losses = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    return {
        "trades": len(rows),
        "net": sum(pnl),
        "win_rate": mean(p > 0 for p in pnl) if pnl else None,
        "profit_factor": wins / losses if losses else None,
        "mean_net_return": mean(r["net_return"] for r in rows) if rows else None,
        "median_hold_minutes": median(r["holding_seconds"] / 60 for r in rows) if rows else None,
    }


def counterfactual(
    trades: list[dict[str, Any]], history: dict[str, pd.DataFrame], end: int
) -> dict[str, Any]:
    matched = []
    omitted = Counter()
    for t in trades:
        if t["entry_ms"] < end - 14 * DAY:
            continue
        x = history.get(t["symbol"])
        if x is None or not all(
            k in t for k in ("entry_price", "quantity", "initial_stop_distance")
        ):
            omitted["missing_history_or_journal"] += 1
            continue
        start = t["entry_ms"] // FIVE * FIVE
        finish = (t["entry_ms"] + 8 * HOUR + FIVE - 1) // FIVE * FIVE
        path = x[(x.open_ms >= start) & (x.timestamp_ms <= finish)]
        if (
            finish > end
            or len(path) != (finish - start) // FIVE
            or path.empty
            or int(path.open_ms.iloc[0]) != start
            or int(path.timestamp_ms.iloc[-1]) != finish
        ):
            omitted["incomplete_eight_hour_path"] += 1
            continue
        matched.append((t, path))
    output = {}
    for minutes in (30, 60, 120, 240, 480):
        for protected in (False, True):
            outcomes = []
            for t, path in matched:
                side, entry, quantity = t["direction"], t["entry_price"], t["quantity"]
                target = (t["entry_ms"] + minutes * 60000 + FIVE - 1) // FIVE * FIVE
                segment = path[path.timestamp_ms <= target]
                stop = entry - side * t["initial_stop_distance"]
                price, closed = float(segment.iloc[-1].close), target
                if protected:
                    hits = segment[side * (segment["low" if side > 0 else "high"] - stop) <= 0]
                    if not hits.empty:
                        bar = hits.iloc[0]
                        price = (
                            min(stop, float(bar.open)) if side > 0 else max(stop, float(bar.open))
                        )
                        closed = int(bar.timestamp_ms)
                fill = price * (1 - side * SLIP)
                held = (closed - t["entry_ms"]) / HOUR
                pnl = (
                    side * (fill - entry) * quantity
                    - t["entry_fee"]
                    - fill * quantity * FEE
                    - entry * quantity * FUNDING * held
                )
                outcomes.append(
                    {
                        "pnl": pnl,
                        "net_return": pnl / (entry * quantity),
                        "holding_seconds": held * 3600,
                    }
                )
            output[f"{minutes}m_{'initial_stop' if protected else 'no_stop_diagnostic'}"] = stats(
                outcomes
            )
    return {
        "matched": len(matched),
        "omitted": dict(omitted),
        "actual_same_trades": stats([t for t, _ in matched]),
        "scenarios": output,
        "notes": [
            "Paired sample: identical actual entries, sides and quantities across every horizon.",
            "Holding starts at entry, rounded up to the next 5m close; not extra time after exit.",
            "Whole entry-bar extremes may precede the fill: stop scenario is conservative on that bar.",
            "No-stop outcomes are diagnostics, not executable proposals. Initial stops are never widened.",
            "Independent trades can overlap beyond available margin: these money totals are not portfolio returns.",
            "Actual entry fee retained; exit fee 5bps, exit spread/slippage 7bps; adverse funding reserve 1bp/hour.",
        ],
    }


def portfolio(
    tapes: dict[str, dict[int, dict[str, Any]]],
    features: dict[str, dict[int, dict[str, Any]]],
    start: int,
    end: int,
    hold: int,
    policy: str,
    leverage: int = 2,
    stress: float = 1,
    *,
    friction: float = SLIP,
    funding: dict[str, list[dict[str, Any]]] | None = None,
    max_positions: int = 4,
    daily_cap: int = 40,
    symbol_cap: int = 4,
    schedule: dict[int, list[tuple[float, str, dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    rules = execution_rules(Settings(_env_file=None))
    cash, peak, drawdown = 10000.0, 10000.0, 0.0
    positions: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    counts: Counter[tuple[int, str]] = Counter()
    cooldown: dict[str, int] = {}
    last_used: dict[str, int] = {}
    daily_start = cash
    locked = False
    funding_curves = {}
    if funding is not None:
        for symbol in tapes:
            times, positive, negative = [], [0.0], [0.0]
            for event in sorted(funding[symbol], key=lambda e: e["timestamp_ms"]):
                times.append(event["timestamp_ms"])
                amount = event["funding_rate"] * event["mark"]
                positive.append(positive[-1] + max(0, amount))
                negative.append(negative[-1] + min(0, amount))
            funding_curves[symbol] = times, positive, negative

    def net(p: dict[str, Any], price: float, now: int) -> float:
        fill = price * (1 - p["side"] * friction * stress)
        funding_pnl = -p["notional"] * FUNDING * stress * (now - p["opened_ms"]) / HOUR
        if funding is not None:
            times, positive, negative = funding_curves[p["symbol"]]
            a, b = bisect_right(times, p["opened_ms"]), bisect_right(times, now)
            pos, neg = positive[b] - positive[a], negative[b] - negative[a]
            funding_pnl = -p["side"] * p["quantity"] * (pos + neg)
            funding_pnl -= (stress - 1) * p["quantity"] * (pos if p["side"] > 0 else -neg)
        return (
            p["side"] * (fill - p["entry_price"]) * p["quantity"]
            - fill * p["quantity"] * FEE * stress
            + funding_pnl
        )

    def close(symbol: str, price: float, now: int, reason: str) -> None:
        nonlocal cash
        p = positions.pop(symbol)
        profit = net(p, price, now)
        cash += profit
        total = profit - p["entry_fee"]
        records.append(
            {
                "pnl": total,
                "net_return": total / p["notional"],
                "holding_seconds": (now - p["opened_ms"]) / 1000,
                "opened_ms": p["opened_ms"],
                "closed_ms": now,
                "reason": reason,
            }
        )
        cooldown[symbol] = now + 30 * 60000

    for now in range(start, end, FIVE):
        scheduled = schedule.get(now, []) if schedule is not None else None
        needed = set(tapes) if scheduled is None else set(positions) | {s for _, s, _ in scheduled}
        bars = {s: tapes[s][now] for s in needed}
        equity = cash + sum(net(p, bars[s]["open"], now) for s, p in positions.items())
        if now == start or now % DAY == 0:
            daily_start, locked = equity, False
        if equity <= daily_start * (1 - 0.125):
            locked = True
        exited = set()
        for symbol, p in list(positions.items()):
            bar, row = bars[symbol], features[symbol].get(now)
            price = bar["open"]
            reason = ""
            if p["side"] * (price - p["stop_price"]) <= 0:
                reason = "gap stop"
            elif locked:
                reason = "daily loss limit"
            elif now - p["opened_ms"] >= hold * HOUR:
                reason = "time limit"
            elif row is not None:
                if policy == "confirmed" and reversal(p, row):
                    reason = "confirmed reversal"
                elif policy == "single" and p["side"] * row["trend_atr"] < 0:
                    reason = "single reversal"
                elif policy == "rsi" and p["side"] * (row["rsi_14"] - 50) >= 0:
                    reason = "RSI returned to neutral"
            if reason:
                close(symbol, price, now, reason)
                exited.add(symbol)
            elif row is not None:
                p["stop_price"] = tighten_stop(p, price, row["atr"], rules)
        # Selection uses only features available at this boundary; fill at next open.
        candidates = scheduled if scheduled is not None else [
            (row["momentum_score"], s, row)
            for s, rows in features.items()
            if (row := rows.get(now)) is not None
            and row["momentum_side"]
            and row["daily_volume"] >= 10_000_000
        ]
        for _, symbol, row in sorted(candidates, key=lambda v: (-v[0], v[1]))[:10]:
            if (
                locked
                or len(positions) >= max_positions
                or symbol in positions
                or symbol in exited
                or now < cooldown.get(symbol, 0)
                or last_used.get(symbol) == now
                or counts[now // DAY, "all"] >= daily_cap
                or counts[now // DAY, symbol] >= symbol_cap
                or now + hold * HOUR >= end
            ):
                continue
            equity = cash + sum(net(p, bars[s]["open"], now) for s, p in positions.items())
            if equity <= 0:
                continue
            side = row["momentum_side"]
            if side * (bars[symbol]["open"] - row["close"]) < -0.5 * row.get(
                "minute_atr", row["atr"]
            ):
                continue
            price = bars[symbol]["open"] * (1 + side * friction * stress)
            stop_fraction = min(0.02, max(0.006, 2 * row["atr_fraction"]))
            notional = equity * 0.1 * leverage
            risk = notional * (stop_fraction + 2 * (FEE + friction) * stress + 0.002)
            if (
                risk > equity * 0.04
                or risk + sum(p["risk"] for p in positions.values()) > equity * 0.05
            ):
                continue
            fee = notional * FEE * stress
            cash -= fee
            positions[symbol] = {
                "symbol": symbol,
                "side": side,
                "entry_price": price,
                "quantity": notional / price,
                "stop_price": price * (1 - side * stop_fraction),
                "initial_stop_distance": price * stop_fraction,
                "entry_fee": fee,
                "notional": notional,
                "opened_ms": now,
                "risk": risk,
            }
            last_used[symbol] = now
            counts[now // DAY, "all"] += 1
            counts[now // DAY, symbol] += 1
        # Include the entry candle. Stop fills take precedence over future favorable closes.
        worst = cash + sum(
            net(p, bars[s]["low" if p["side"] > 0 else "high"], now + FIVE)
            for s, p in positions.items()
        )
        drawdown = max(drawdown, 1 - worst / peak)
        for symbol, p in list(positions.items()):
            bar = bars[symbol]
            if p["side"] * (bar["low" if p["side"] > 0 else "high"] - p["stop_price"]) <= 0:
                price = (
                    min(bar["open"], p["stop_price"])
                    if p["side"] > 0
                    else max(bar["open"], p["stop_price"])
                )
                close(symbol, price, now + FIVE, "stop")
        equity = cash + sum(net(p, bars[s]["close"], now + FIVE) for s, p in positions.items())
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    for symbol in list(positions):
        close(symbol, tapes[symbol][end - FIVE]["close"], end, "end liquidation")
    return {
        **stats(records),
        "return": cash / 10000 - 1,
        "max_drawdown_conservative": drawdown,
        "exit_reasons": dict(Counter(r["reason"] for r in records)),
        "positive_days": sum(
            v > 0
            for v in pd.Series(
                [r["pnl"] for r in records],
                index=[r["closed_ms"] // DAY for r in records],
                dtype=float,
            )
            .groupby(level=0)
            .sum()
        ),
        "leverage": leverage,
        "cost_multiplier": stress,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    history = {symbol: pd.read_parquet(args.folder / f"{symbol}.parquet") for symbol in SYMBOLS}
    end = min(int(x.timestamp_ms.max()) for x in history.values()) // DAY * DAY
    start, cutoff = end - 21 * DAY, end - 7 * DAY
    settings = Settings(_env_file=None)
    rules = execution_rules(settings)
    prepared, tapes = {}, {}
    for symbol, x in history.items():
        validate_history(x, int(x.timestamp_ms.max()), FIVE, len(x))
        validate_ohlc(x)
        if int(x.open_ms.min()) > start - 2 * DAY:
            raise ValueError("insufficient warmup coverage")
        prepared[symbol] = prepare(x, settings)
        prepared[symbol]["15m"]["return_1h"] = prepared[symbol]["15m"].close.pct_change(4)
        tapes[symbol] = {int(r["open_ms"]): r for r in x.to_dict("records")}
    variants = []
    feature_sets = {}
    for timeframe in ("5m", "15m"):
        features = {}
        for symbol, frames in prepared.items():
            features[symbol] = {}
            for row in frames[timeframe].to_dict("records"):
                pattern, side, score = classify(row, FEE + SLIP, rules)
                row.update(momentum_pattern=pattern, momentum_side=side, momentum_score=score)
                features[symbol][int(row["timestamp_ms"])] = row
        feature_sets[timeframe] = features
        for hold in (1, 2, 4, 8):
            for policy in ("single", "confirmed", "stop_only"):
                result = {
                    "timeframe": timeframe,
                    "hold_hours": hold,
                    "exit_policy": policy,
                    "train": portfolio(tapes, features, start, cutoff, hold, policy),
                    "validation": portfolio(tapes, features, cutoff, end, hold, policy),
                }
                variants.append(result)
                print(json.dumps(result), flush=True)
    winner = max(variants, key=lambda r: r["train"]["return"])
    features = feature_sets[winner["timeframe"]]
    sensitivity = [
        portfolio(
            tapes,
            features,
            cutoff,
            end,
            winner["hold_hours"],
            winner["exit_policy"],
            leverage,
            stress,
        )
        for leverage, stress in ((2, 2), (4, 1), (4, 2))
    ]
    trades = json.loads(args.trades.read_text())
    report = {
        "start_ms": start,
        "cutoff_ms": cutoff,
        "end_ms": end,
        "history_sha256": {
            s: hashlib.sha256((args.folder / f"{s}.parquet").read_bytes()).hexdigest()
            for s in SYMBOLS
        },
        "actual_last_14_days": stats(
            [t for t in trades if end - 14 * DAY <= t["timestamp_ms"] < end]
        ),
        "counterfactual": counterfactual(trades, history, end),
        "variants": variants,
        "selected_on_training_only": winner,
        "selected_sensitivity": sensitivity,
        "notes": [
            "14 training days then 7 untouched days; positions finish before each split, no labels cross the split.",
            "24 prespecified variants. Validation ranks are descriptive, not a second optimization.",
            "Fixed 19-liquid-coin universe, top 10 by momentum, maximum 4 open, margin 10% per trade.",
            "A long, B no trade, C short. No BTC/ETH directional gate, AI, automatic reversal or profit runner.",
            "Fees 5bps/side, spread+slippage 7bps/side; adverse funding reserve 1bp/hour; stress doubles all costs.",
            "4x sensitivity doubles notional at fixed margin; no exchange leverage is changed.",
            "No historical exchange lot sizes, order-book depth, liquidation or full live portfolio risk-lock replay.",
            "Conservative drawdown marks all concurrent positions at adverse intrabar extremes; their simultaneity is unknown.",
            "Missing intervals fail the study. Historical membership and delistings are not reconstructed.",
            "This is retrospective screening, not proof of future profits or deployment approval.",
        ],
    }
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
