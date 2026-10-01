from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from paperfrog.backtest.report import metrics
from paperfrog.domain.types import Filters, Order, Quote, SafeError, State
from paperfrog.execution.coordinator import Coordinator, client_id
from paperfrog.paper.broker import PaperBroker
from paperfrog.portfolio.sizing import estimate_hedge, size_pair
from paperfrog.risk.engine import Health, RiskEngine
from paperfrog.settings import Settings
from paperfrog.storage.database import Store
from paperfrog.strategy.engine import Episodes, entry_decision, exit_decision


@dataclass(frozen=True)
class Stress:
    name: str = "baseline"
    cost_multiplier: float = 1.0
    delay_bars: int = 1
    missed_fraction: float = 0.0
    spread_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    excluded_symbol: str | None = None
    excluded_month: str | None = None


BASELINE_STRESS = Stress()

STRESSES = [
    Stress(),
    Stress("2x_cost", 2),
    Stress("3x_cost", 3),
    Stress("one_extra_bar_delay", delay_bars=2),
    Stress("miss_10pct", missed_fraction=0.1),
    Stress("miss_20pct", missed_fraction=0.2),
    Stress("spread_shock", spread_multiplier=4),
    Stress("slippage_shock", slippage_multiplier=5),
]


async def replay(
    panel: pd.DataFrame,
    s: Settings,
    output: Path,
    filters: dict[str, Filters],
    stress: Stress = BASELINE_STRESS,
    strategy: str = "ml",
) -> dict[str, Any]:
    required = {
        "timestamp_ms",
        "symbol",
        "bid",
        "ask",
        "bid_size",
        "ask_size",
        "mark",
        "perp_mid",
        "basis",
        "basis_z_30d",
        "funding_rate",
        "funding_z",
        "funding_persistence",
        "oi_change_30m",
        "oi_z_4h",
        "volume_ratio_z",
        "perp_imbalance",
        "spread",
        "liquid",
        "universe_asof_ms",
        "universe_expires_ms",
        "in_universe",
    }
    if not required <= set(panel) or panel.duplicated(["timestamp_ms", "symbol"]).any():
        raise SafeError(
            "replay requires validated historical features, quotes, universe and filters"
        )
    if strategy == "ml" and not {
        "probability",
        "predicted_return",
        "prediction_std",
        "baseline_probability",
        "out_of_sample",
    } <= set(panel):
        raise SafeError("ML replay requires out-of-sample predictions joined at candidate times")
    if output.exists():
        raise SafeError("replay output exists; use an immutable new run directory")
    output.mkdir(parents=True)
    store = Store(output / "replay.sqlite")
    risk = RiskEngine(s, store)
    broker = PaperBroker(
        store,
        filters,
        Decimal(str(s.paper_equity)),
        s.paper_seed,
        Decimal(str(s.fee_rate * stress.cost_multiplier)),
        s.slippage_bps * stress.cost_multiplier * stress.slippage_multiplier,
    )
    coordinator = Coordinator(broker, store, risk, filters)
    episodes = Episodes(store)
    store.put("state", State.RUNNING)
    store.put("reconciled", True)
    history: dict[str, pd.DataFrame] = {}
    active: dict[str, Any] = {}
    pending: list[dict[str, Any]] = []
    equities, trades = [], []
    rng = random.Random(s.paper_seed)
    groups = list(panel.sort_values(["timestamp_ms", "symbol"]).groupby("timestamp_ms"))
    for step, (timestamp, rows) in enumerate(groups):
        timestamp = int(timestamp)
        now = datetime.fromtimestamp(timestamp / 1000, UTC)
        by_symbol = {str(r.symbol): r for r in rows.itertuples()}
        for r in rows.itertuples():
            mid = (r.bid + r.ask) / 2
            half = (r.ask - r.bid) * stress.spread_multiplier / 2
            await broker.quote(
                Quote(
                    r.symbol,
                    Decimal(str(mid - half)),
                    Decimal(str(mid + half)),
                    Decimal(str(r.bid_size)),
                    Decimal(str(r.ask_size)),
                    Decimal(str(r.mark)),
                    timestamp,
                )
            )
            if getattr(r, "funding_settlement", False):
                broker.funding(
                    r.symbol, timestamp, Decimal(str(r.funding_rate)), Decimal(str(r.mark))
                )
            history[r.symbol] = pd.concat(
                [history.get(r.symbol, pd.DataFrame()), rows[rows.symbol == r.symbol]],
                ignore_index=True,
            ).tail(3000)
        equity = float(broker.equity())
        risk.anchors(equity, now)
        # Hosted stop fills update the ledger before strategy exits.
        paper_positions = await broker.positions()
        store.put("expected_positions", {k: str(v) for k, v in paper_positions.items()})
        for trade_id, t in list(active.items()):
            r = by_symbol.get(t["symbol"])
            if r is None:
                raise SafeError("held symbol has missing replay observations")
            a = store.get("paper_account")
            pnl = sum(
                float(
                    Decimal(a["positions"].get(o.symbol, "0"))
                    * (broker.quotes[o.symbol].mark - Decimal(a["average"].get(o.symbol, "0")))
                )
                for o in t["orders"]
            )
            decision = exit_decision(
                held_minutes=(timestamp - t["timestamp"]) / 60000,
                pnl_r=pnl / t["risk"],
                basis=r.basis,
                initial_basis=t["basis"],
                basis_z=r.basis_z_30d,
                basis_change_sigma=0.001,
                partial_taken=t["partial"],
                portfolio_veto=bool(store.get("risk_lock")),
            )
            if not paper_positions.get(t["symbol"]):
                decision = type(decision)(1.0, "hosted_stop")
            if not decision.fraction:
                continue
            for o in t["orders"]:
                qty = abs((await broker.positions()).get(o.symbol, Decimal(0)))
                qty = filters[o.symbol].round_quantity(qty * Decimal(str(decision.fraction)))
                if not qty:
                    continue
                q = broker.quotes[o.symbol]
                price = filters[o.symbol].round_price(
                    q.bid * Decimal(".98") if o.side > 0 else q.ask * Decimal("1.02")
                )
                await coordinator.submit_once(
                    Order(
                        client_id=client_id(trade_id, f"exit-{step}-{o.symbol}"),
                        symbol=o.symbol,
                        side=-o.side,
                        quantity=qty,
                        limit_price=price,
                        reduce_only=True,
                    )
                )
            if decision.fraction < 1:
                t["partial"] = True
            else:
                remaining = await broker.positions()
                if any(remaining.get(o.symbol) for o in t["orders"]):
                    continue
                pnl_final = float(broker.equity()) - t["equity_before"]
                account = store.get("paper_account")
                fills = [
                    v
                    for v in account["orders"].values()
                    if v["client_id"] not in t["prior_order_ids"]
                ]
                trades.append(
                    {
                        "timestamp_ms": timestamp,
                        "symbol": t["symbol"],
                        "pnl": pnl_final,
                        "reason": decision.reason,
                        "fees": sum(float(v["fee"]) for v in fills),
                        "funding": sum(
                            float(v)
                            for k, v in account["funding"].items()
                            if k not in t["prior_funding_ids"]
                        ),
                        "turnover": sum(float(v["quantity"]) * float(v["price"]) for v in fills),
                    }
                )
                risk.closed(trade_id, pnl_final, now)
                del active[trade_id]
        # Execute only on a subsequent quote event, never at the signal's close.
        for item in list(pending):
            if step < item["due"]:
                continue
            pending.remove(item)
            if active or store.get("risk_lock") or store.get("state") != State.RUNNING:
                continue
            symbol = item["symbol"]
            try:
                returns = pd.DataFrame(
                    {
                        asset: history[asset].set_index("timestamp_ms").perp_mid.pct_change()
                        for asset in (symbol, "BTCUSDT", "ETHUSDT")
                    }
                ).dropna()
                hedge = estimate_hedge(returns.to_numpy())
                proposal, orders = size_pair(
                    s,
                    item["id"],
                    symbol,
                    item["direction"],
                    float(broker.equity()),
                    0.01,
                    hedge,
                    broker.quotes,
                    filters,
                    float(store.get("risk_multiplier", 1.0)),
                )
                a = store.get("paper_account")
                before = float(broker.equity())
                await coordinator.enter(proposal, orders, Health(True, True, True, 0, 0), now, 0.01)
                active[item["id"]] = {
                    "symbol": symbol,
                    "orders": orders,
                    "timestamp": timestamp,
                    "basis": by_symbol[symbol].basis,
                    "risk": proposal.risk,
                    "equity_before": before,
                    "partial": False,
                    "prior_order_ids": set(a["orders"]),
                    "prior_funding_ids": set(a["funding"]),
                }
            except (SafeError, ValueError, KeyError) as exc:
                store.audit("entry_rejected", {"symbol": symbol, "reason": type(exc).__name__})
        universe = {
            r.symbol
            for r in rows.itertuples()
            if r.in_universe and r.universe_asof_ms <= timestamp < r.universe_expires_ms
        }
        for symbol in sorted(universe):
            if (
                symbol in {"BTCUSDT", "ETHUSDT"}
                or symbol == stress.excluded_symbol
                or now.strftime("%Y-%m") == stress.excluded_month
            ):
                continue
            candidate = episodes.update(
                symbol, history[symbol], universe, crisis=bool(store.get("risk_lock"))
            )
            if strategy == "cash" or not candidate or rng.random() < stress.missed_fraction:
                continue
            r = by_symbol[symbol]
            if strategy == "ml":
                if not bool(r.out_of_sample) or not pd.notna(r.probability):
                    continue
                # Regressor already predicts net return; only uncertainty/safety added here.
                allowed, _, _ = entry_decision(
                    [r.probability],
                    [r.predicted_return],
                    r.baseline_probability,
                    0,
                    0,
                    0,
                    0,
                    0.0005 + 0.5 * r.prediction_std,
                )
                if not allowed:
                    continue
            pending.append(
                {
                    "id": candidate.episode_id,
                    "symbol": symbol,
                    "direction": -candidate.crowded_side,
                    "due": step + stress.delay_bars,
                }
            )
        equities.append({"timestamp_ms": timestamp, "equity": float(broker.equity())})
    if await broker.positions():
        if not await coordinator.flatten(broker.quotes, "end-of-replay"):
            raise SafeError("replay ended with unresolved exposure")
        for trade_id, t in active.items():
            account = store.get("paper_account")
            fills = [
                v for v in account["orders"].values() if v["client_id"] not in t["prior_order_ids"]
            ]
            pnl_final = float(broker.equity()) - t["equity_before"]
            trades.append(
                {
                    "timestamp_ms": int(groups[-1][0]),
                    "symbol": t["symbol"],
                    "pnl": pnl_final,
                    "reason": "end_of_replay",
                    "fees": sum(float(v["fee"]) for v in fills),
                    "funding": sum(
                        float(v)
                        for k, v in account["funding"].items()
                        if k not in t["prior_funding_ids"]
                    ),
                    "turnover": sum(float(v["quantity"]) * float(v["price"]) for v in fills),
                }
            )
            risk.closed(trade_id, pnl_final, datetime.fromtimestamp(int(groups[-1][0]) / 1000, UTC))
        equities.append({"timestamp_ms": int(groups[-1][0]) + 1, "equity": float(broker.equity())})
    equity_frame, trade_frame = pd.DataFrame(equities), pd.DataFrame(trades)
    equity_frame.to_parquet(output / "equity.parquet", index=False)
    trade_frame.to_parquet(output / "trades.parquet", index=False)
    result = metrics(equity_frame, trade_frame)
    result.update(
        stress=stress.name,
        strategy=strategy,
        limitations=[
            "No fabricated historical LLM veto",
            "Conservative one active paired position; shared hedge attribution excluded",
        ],
    )
    import json

    (output / "metrics.json").write_text(json.dumps(result, indent=2))
    return result
