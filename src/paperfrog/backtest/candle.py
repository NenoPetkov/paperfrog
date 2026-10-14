from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from paperfrog.backtest.report import metrics
from paperfrog.domain.types import SafeError, State
from paperfrog.portfolio.sizing import estimate_hedge
from paperfrog.risk.engine import Health, Proposal, RiskEngine
from paperfrog.settings import Settings
from paperfrog.storage.database import Store
from paperfrog.strategy.candle import RULES, CandleRules, require_paper


@dataclass(frozen=True)
class CandleStress:
    name: str = "baseline"
    cost_multiplier: float = 1.0
    delay_bars: int = 1


STRESSES = (CandleStress(), CandleStress("2x_cost", 2), CandleStress("extra_bar_delay", 1, 2))
BASELINE_STRESS = STRESSES[0]


def replay_candles(
    frame: pd.DataFrame,
    s: Settings,
    symbol: str,
    start_ms: int,
    end_ms: int,
    output: Path,
    stress: CandleStress = BASELINE_STRESS,
    rules: CandleRules = RULES,
    cash_only: bool = False,
) -> dict[str, Any]:
    """Separate, idealized candle PAPER study; never constructs an exchange adapter."""
    require_paper(s)
    if output.exists() or stress.cost_multiplier < 1 or stress.delay_bars < 1:
        raise SafeError("use a new candle run directory and valid conservative stress")
    indices = np.flatnonzero(frame.timestamp_ms.between(start_ms + 900000, end_ms).to_numpy())
    if not len(indices):
        raise SafeError("no candles in evaluation range")
    output.mkdir(parents=True)
    store = Store(output / "paper-risk.sqlite")
    risk = RiskEngine(s, store)
    store.put("state", State.RUNNING)
    store.put("reconciled", True)
    # In this offline simulator the readiness flag denotes the frozen rules, not an
    # approved ML model. This state is never shared with execution or live arming.
    health = Health(True, True, True, 0, 0)
    cash = float(s.paper_equity)
    fee = s.fee_rate * stress.cost_multiplier
    impact = (s.slippage_bps + rules.assumed_half_spread_bps) / 10000 * stress.cost_multiplier
    closes = frame[["close", "btc_close", "eth_close"]].to_numpy(dtype=float)
    opens = frame[["open", "btc_open", "eth_open"]].to_numpy(dtype=float)
    highs = frame[["high", "btc_high", "eth_high"]].to_numpy(dtype=float)
    lows = frame[["low", "btc_low", "eth_low"]].to_numpy(dtype=float)
    volumes = frame[["quote_volume", "btc_quote_volume", "eth_quote_volume"]].to_numpy(dtype=float)
    returns = pd.DataFrame(closes).pct_change(fill_method=None).to_numpy(dtype=float)
    funding_open = frame[[f"{p}_funding_at_open" for p in ("target", "btc", "eth")]].to_numpy(
        dtype=float
    )
    funding_inside = frame[[f"{p}_funding_after_open" for p in ("target", "btc", "eth")]].to_numpy(
        dtype=float
    )
    times = frame.timestamp_ms.to_numpy(dtype="int64")
    z = frame.relative_z.to_numpy(dtype=float)
    signals = frame.direction.to_numpy(dtype=int)
    crisis = frame.crisis.to_numpy(dtype=bool)
    active: dict[str, Any] | None = None
    pending: tuple[int, int] | None = None
    lock: dict[str, Any] | None = None
    previous_day = ""
    skipped: Counter[str] = Counter()
    trades, equities = [], [{"timestamp_ms": start_ms, "equity": cash}]
    candidates = 0

    def marked(prices: np.ndarray) -> float:
        return (
            cash
            if active is None
            else cash + float(np.dot(active["quantity"], prices - active["entry_prices"]))
        )

    def charge_funding(costs: np.ndarray, conservative_stop: bool = False) -> None:
        nonlocal cash
        if active is None:
            return
        flows = -active["quantity"] * costs
        if conservative_stop:
            flows = np.minimum(flows, 0)
        amount = float(flows.sum())
        cash += amount
        active["funding"] += amount

    def close_trade(raw_prices: np.ndarray, timestamp: int, reason: str) -> None:
        nonlocal cash, active, lock
        if active is None:
            return
        quantity = active["quantity"]
        prices = raw_prices * (1 - np.sign(quantity) * impact)
        turnover = float(np.dot(np.abs(quantity), prices))
        exit_fee = turnover * fee
        cash += float(np.dot(quantity, prices - active["entry_prices"])) - exit_fee
        pnl = cash - active["equity_before"]
        risk.closed(active["id"], pnl, datetime.fromtimestamp(timestamp / 1000, UTC))
        trades.append(
            {
                "timestamp_ms": timestamp,
                "entry_ms": active["entry_ms"],
                "signal_ms": active["signal_ms"],
                "symbol": symbol,
                "direction": active["direction"],
                "pnl": pnl,
                "reason": reason,
                "fees": active["fees"] + exit_fee,
                "funding": active["funding"],
                "turnover": active["turnover"] + turnover,
                "planned_risk": active["risk"],
                "btc_hedge": active["btc_hedge"],
                "eth_hedge": active["eth_hedge"],
            }
        )
        active = None
        risk.anchors(cash, datetime.fromtimestamp(timestamp / 1000, UTC))
        lock = store.get("risk_lock")

    try:
        for raw_index in indices:
            i = int(raw_index)
            open_ms = int(times[i] - 900000)
            now = datetime.fromtimestamp(open_ms / 1000, UTC)
            # Existing holdings pay/receive the actual opening settlement. A new
            # position opened at the same boundary is never credited that settlement.
            charge_funding(funding_open[i])
            if now.date().isoformat() != previous_day or active is not None:
                risk.anchors(marked(opens[i]), now)
                previous_day = now.date().isoformat()
                lock = store.get("risk_lock")
            if (
                lock
                and lock.get("until") is not None
                and now.timestamp() >= lock["until"]
                and active is None
            ):
                try:
                    risk.resume(health, now)
                    lock = None
                except SafeError:
                    pass
            if active is not None:
                reason = None
                if lock:
                    reason = "portfolio_risk_lock"
                elif i > 0 and crisis[i - 1]:
                    reason = "btc_crisis"
                elif open_ms - active["entry_ms"] >= rules.max_hold_bars * 900000:
                    reason = "time_exit"
                elif i > 0 and np.isfinite(z[i - 1]) and abs(z[i - 1]) <= rules.exit_z:
                    reason = "convergence"
                if reason:
                    close_trade(opens[i], open_ms, reason)
            if pending is not None and i >= pending[1]:
                decision, _ = pending
                pending = None
                if active is not None or lock or (i > 0 and crisis[i - 1]):
                    skipped["position, risk lock or crisis"] += 1
                else:
                    try:
                        hedge = estimate_hedge(returns[max(1, decision - 2879) : decision + 1])
                        coefficients = np.array([1, hedge.btc, hedge.eth])
                        directions = int(signals[decision]) * np.array([1, -1, -1])
                        entry_prices = opens[i] * (1 + directions * impact)
                        costs = rules.stop_fraction + 2 * fee + 2 * impact + 0.002
                        multiplier = float(store.get("risk_multiplier", 1.0))
                        target = min(
                            cash * s.risk_per_trade * multiplier / (coefficients.sum() * costs),
                            cash * s.max_target_notional,
                            cash * s.max_gross_notional / coefficients.sum(),
                            min(
                                s.max_participation * volumes[decision, k] / c
                                for k, c in enumerate(coefficients)
                                if c > 0
                            ),
                        )
                        notionals = target * coefficients
                        gross = float(notionals.sum())
                        planned_risk = gross * costs
                        trade_id = hashlib.sha256(
                            f"{rules.version}:{symbol}:{times[decision]}".encode()
                        ).hexdigest()[:24]
                        participation = float(np.max(notionals / volumes[decision]))
                        proposal = Proposal(
                            trade_id,
                            symbol,
                            cash,
                            target,
                            gross,
                            planned_risk,
                            0,
                            rules.stop_fraction,
                            1,
                            participation,
                        )
                        risk.reserve(proposal, health, now)
                        entry_fee = gross * fee
                        before = cash
                        cash -= entry_fee
                        active = {
                            "id": trade_id,
                            "quantity": directions * notionals / entry_prices,
                            "entry_prices": entry_prices,
                            "equity_before": before,
                            "entry_ms": open_ms,
                            "signal_ms": int(times[decision]),
                            "direction": int(signals[decision]),
                            "fees": entry_fee,
                            "funding": 0.0,
                            "turnover": gross,
                            "risk": planned_risk,
                            "btc_hedge": hedge.btc,
                            "eth_hedge": hedge.eth,
                        }
                    except SafeError as exc:
                        skipped[str(exc)] += 1
            if active is not None:
                quantity = active["quantity"]
                trigger = active["entry_prices"] * (1 - np.sign(quantity) * rules.stop_fraction)
                touched = ((quantity > 0) & (lows[i] <= trigger)) | (
                    (quantity < 0) & (highs[i] >= trigger)
                )
                if touched.any():
                    charge_funding(funding_inside[i], conservative_stop=True)
                    # Intrabar ordering is unknown. Use each leg's adverse candle
                    # extreme to avoid favorable assumptions about hedge flattening.
                    adverse = np.where(quantity > 0, lows[i], highs[i])
                    close_trade(adverse, int(times[i]), "conservative_intrabar_stop")
                else:
                    charge_funding(funding_inside[i])
                    risk.anchors(marked(closes[i]), datetime.fromtimestamp(times[i] / 1000, UTC))
                    lock = store.get("risk_lock")
            if not cash_only and signals[i] and active is None and pending is None:
                candidates += 1
                pending = (i, i + stress.delay_bars)
            equities.append({"timestamp_ms": int(times[i]), "equity": marked(closes[i])})
        if active is not None:
            close_trade(closes[int(indices[-1])], int(times[indices[-1]]), "end_of_evaluation")
            equities[-1]["equity"] = cash
        equity = pd.DataFrame(equities)
        trade_frame = pd.DataFrame(
            trades,
            columns=[
                "timestamp_ms",
                "entry_ms",
                "signal_ms",
                "symbol",
                "direction",
                "pnl",
                "reason",
                "fees",
                "funding",
                "turnover",
                "planned_risk",
                "btc_hedge",
                "eth_hedge",
            ],
        )
        result = metrics(equity, trade_frame)
        result.update(
            {
                "strategy": rules.version,
                "strategy_hash": rules.fingerprint,
                "paper_only": True,
                "live_permitted": False,
                "execution_model": "idealized_candle_assumptions",
                "stress": "cash" if cash_only else stress.name,
                "candidates": candidates,
                "skipped_entries": dict(skipped),
                "ending_equity": cash,
                "risk_lock_at_end": store.get("risk_lock"),
            }
        )
        equity.to_parquet(output / "equity.parquet", index=False)
        trade_frame.to_parquet(output / "trades.parquet", index=False)
        equity.assign(
            month=pd.to_datetime(equity.timestamp_ms, unit="ms", utc=True).dt.strftime("%Y-%m")
        ).groupby("month").equity.last().to_csv(output / "monthly-equity.csv")
        (output / "metrics.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        return result
    finally:
        store.engine.dispose()


def evaluate_candles(frame: pd.DataFrame, s: Settings, symbol: str, output: Path) -> dict[str, Any]:
    require_paper(s)
    if output.exists():
        raise SafeError("experiment output exists; use an immutable new directory")
    output.mkdir(parents=True)
    start = int(frame.timestamp_ms.min()) - 900000
    end = int(frame.timestamp_ms.max())
    midpoint = start + ((end - start) // 900000 // 2) * 900000
    manifest = {
        "strategy": asdict(RULES),
        "strategy_hash": RULES.fingerprint,
        "input_hash": hashlib.sha256(
            pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes()
        ).hexdigest(),
        "created_at": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "start_ms": start,
        "end_ms": end,
        "later_period_start_ms": midpoint,
        "parameter_search": False,
        "paper_only": True,
        "live_permitted": False,
        "assumptions": [
            "Signals use closed candles and backward OI joins; fills use a later candle open.",
            "Fixed target selected today: historical universe and survivorship review absent.",
            "Fractional idealized fills; historical lot filters and order-book depth unavailable.",
            "5bp assumed half-spread plus configured slippage per fill; configured taker fees on all legs.",
            "Funding uses actual settlement rates and marks, with no entry-boundary credit.",
            "If a leg stop is touched, flatten all legs at adverse candle extremes; intrabar ordering unknown.",
            "Ridge hedge fit uses only up to 30 days of returns available at the signal.",
            "Core risk caps/locks apply; participation is a prior candle-volume proxy, not measured book depth.",
            "Later period is an untuned retrospective check, not independently verified live-readiness evidence.",
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    result: dict[str, Any] = {"manifest": manifest, "evaluations": {}}
    for period, begin in (("full_history", start), ("later_period", midpoint)):
        cases = {
            stress.name: replay_candles(
                frame, s, symbol, begin, end, output / period / stress.name, stress
            )
            for stress in STRESSES
        }
        cases["cash"] = replay_candles(
            frame, s, symbol, begin, end, output / period / "cash", cash_only=True
        )
        result["evaluations"][period] = cases
    result["live_permitted"] = False
    result["validation"] = "first_research_evaluation_only"
    (output / "evaluation.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
