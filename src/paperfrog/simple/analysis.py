"""Descriptive closed-trade reports. No model, strategy or risk mutations."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .journal import INDICATORS


def number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def context(trade: dict[str, Any]) -> dict[str, Any]:
    return dict(trade.get("entry_context") or {})


def metrics(trade: dict[str, Any]) -> dict[str, float | None]:
    pnl, fees, funding = (number(trade.get(k)) for k in ("pnl", "fees", "funding"))
    gross = number(trade.get("gross_pnl"))
    if gross is None and pnl is not None and fees is not None and funding is not None:
        gross = pnl + fees - funding
    duration = number(trade.get("holding_seconds"))
    if duration is None and "entry_ms" in trade:
        duration = (trade["timestamp_ms"] - trade["entry_ms"]) / 1000
    costs = [trade.get("entry_execution_cost"), trade.get("exit_execution_cost")]
    execution_costs: dict[str, float | None] = {}
    for name in ("spread_cost", "slippage_cost"):
        values = [
            value
            for cost in costs
            if isinstance(cost, dict) and (value := number(cost.get(name))) is not None
        ]
        execution_costs[name] = sum(values) if len(values) == 2 else None
    return {
        "net_pnl": pnl,
        "gross_pnl": gross,
        "fees": fees,
        "funding": funding,
        "net_return": number(trade.get("net_return")),
        "holding_seconds": duration,
        "mfe_fraction": number(trade.get("mfe_fraction")),
        "mae_fraction": number(trade.get("mae_fraction")),
        **execution_costs,
    }


def summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [metrics(t) for t in trades]
    pnls = [r["net_pnl"] for r in rows if r["net_pnl"] is not None]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    fields = (
        "net_pnl",
        "gross_pnl",
        "fees",
        "funding",
        "net_return",
        "holding_seconds",
        "mfe_fraction",
        "mae_fraction",
        "spread_cost",
        "slippage_cost",
    )
    stats = {}
    for key in fields:
        values = [value for r in rows if (value := r[key]) is not None]
        stats[key] = {
            "known_trades": len(values),
            "mean": mean(values),
            "total": sum(values) if values else None,
        }
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": sum(p == 0 for p in pnls),
        "unknown_pnl": len(trades) - len(pnls),
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "average_win": mean(wins),
        "average_loss": mean(losses),
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "cost_flipped_trades": sum(
            r["gross_pnl"] is not None
            and r["net_pnl"] is not None
            and r["gross_pnl"] > 0
            and r["net_pnl"] < 0
            for r in rows
        ),
        "metrics": stats,
    }


def comparison(trades: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: dict[str, list[dict[str, Any]]] = {"winners": [], "losers": [], "breakeven": []}
    for trade in trades:
        pnl = number(trade.get("pnl"))
        if pnl is not None:
            outcomes["winners" if pnl > 0 else "losers" if pnl < 0 else "breakeven"].append(trade)
    result = {}
    for outcome, members in outcomes.items():
        features = {}
        for feature in INDICATORS:
            if feature in {"timestamp_ms", "btc_pair_asof_ms", "close", "prior_high", "prior_low"}:
                continue
            values = [
                v
                for t in members
                if (v := number(context(t).get("indicators", {}).get(feature))) is not None
            ]
            features[feature] = {"known_trades": len(values), "mean": mean(values)}
        result[outcome] = {**summary(members), "entry_indicators": features}
    return result


def analysis(trades: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {
        name: defaultdict(list)
        for name in (
            "symbol",
            "direction",
            "strategy",
            "exit_reason",
            "entry_timeframe",
            "volume_regime",
            "ai_rule_id",
            "fee_basis",
            "currency",
        )
    }
    for trade in trades:
        ctx = context(trade)
        volume = number(ctx.get("indicators", {}).get("volume_ratio"))
        labels = {
            "symbol": trade.get("symbol", "unknown"),
            "direction": "long"
            if trade.get("direction") == 1
            else "short"
            if trade.get("direction") == -1
            else "unknown",
            "strategy": ctx.get("strategy", "unknown"),
            "exit_reason": trade.get("reason", "unknown"),
            "entry_timeframe": ctx.get("entry_timeframe", "unknown"),
            "volume_regime": "unknown"
            if volume is None
            else "below_1x"
            if volume < 1
            else "1x_to_2x"
            if volume < 2
            else "at_least_2x",
            "ai_rule_id": trade.get("ai_rule_id") or "none_or_unknown",
            "fee_basis": ctx.get("fee_basis", "unknown"),
            "currency": ctx.get("currency", "unknown"),
        }
        for name, label in labels.items():
            groups[name][str(label)].append(trade)
    return {
        "summary": summary(trades),
        "outcomes": comparison(trades),
        "groups": {
            name: {label: summary(members) for label, members in sorted(buckets.items())}
            for name, buckets in groups.items()
        },
    }


def daily_report(trades: list[dict[str, Any]], day: date, now_ms: int) -> dict[str, Any]:
    start = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    end = start + 86_400_000
    selected = sorted(
        (t for t in trades if start <= t["timestamp_ms"] < end and t["timestamp_ms"] <= now_ms),
        key=lambda t: (t["timestamp_ms"], t.get("trade_id", "")),
    )
    return {
        "schema_version": 1,
        "day": day.isoformat(),
        "timezone": "UTC",
        "period_complete": now_ms >= end,
        "coverage": {
            "rich_journal_trades": sum(t.get("journal_version") == 1 for t in selected),
            "legacy_trades": sum(t.get("journal_version", 0) != 1 for t in selected),
        },
        **analysis(selected),
        "notes": [
            "Descriptive observations, not causal findings, strategy changes or evidence of profitability.",
            "Days use UTC exit time; all lifetime trade costs are attributed to the close day. Open positions are excluded.",
            "Net PnL = fill-price gross PnL - fees + signed funding. Funding credits are positive.",
            "Spread and slippage are signed diagnostics already embedded in fill prices; do not subtract them again.",
            "Live/testnet fees are estimates from the configured fee rate, not reconciled commissions. Funding is recorded income; later postings may be absent.",
            "Excursions use observed marks and fills, not full intrabar paths. Gaps and downtime can miss extremes.",
            "Missing history is unknown, not zero. Metric counts show coverage. Small samples and repeated comparisons can mislead.",
            "Money totals assume a consistent account unit; inspect currency groups before comparing or combining different units.",
        ],
    }


def render_report(report: dict[str, Any]) -> str:
    def display(value: Any) -> str:
        return "unknown" if value is None else f"{value:.6g}"

    s = report["summary"]
    m = s["metrics"]
    lines = [
        f"Trade analysis: {report['day']} UTC ({'complete day' if report['period_complete'] else 'partial day'})",
        f"Closed trades: {s['trades']} | Winners: {s['wins']} | Losers: {s['losses']} | Breakeven: {s['breakeven']}",
        f"Net PnL: {display(m['net_pnl']['total'])} | Gross PnL: {display(m['gross_pnl']['total'])}",
        f"Fees: {display(m['fees']['total'])} | Signed funding: {display(m['funding']['total'])}",
        f"Average net PnL per trade: {display(m['net_pnl']['mean'])} | Profit factor: {display(s['profit_factor'])}",
        f"Gross winners turned into net losers by fees/funding: {s['cost_flipped_trades']}",
        f"Rich journal records: {report['coverage']['rich_journal_trades']} | Legacy records: {report['coverage']['legacy_trades']}",
        "",
        "Winners versus losers (means; fractions are not percentages):",
    ]
    for key in ("net_return", "holding_seconds", "mfe_fraction", "mae_fraction"):
        parts = [
            f"{name}: {display(report['outcomes'][name]['metrics'][key]['mean'])} "
            f"(n={report['outcomes'][name]['metrics'][key]['known_trades']})"
            for name in ("winners", "losers")
        ]
        lines.append(f"  {key}: " + " | ".join(parts))
    for key in ("trend_atr", "flow", "atr_fraction", "volume_ratio", "rsi_14"):
        parts = [
            f"{name}: {display(report['outcomes'][name]['entry_indicators'][key]['mean'])} "
            f"(n={report['outcomes'][name]['entry_indicators'][key]['known_trades']})"
            for name in ("winners", "losers")
        ]
        lines.append(f"  Entry {key}: " + " | ".join(parts))
    for name, buckets in report["groups"].items():
        lines.extend(["", f"By {name}:"])
        for label, group in buckets.items():
            lines.append(
                f"  {label}: n={group['trades']}, wins={group['wins']}, losses={group['losses']}, "
                f"net={display(group['metrics']['net_pnl']['total'])}"
            )
    lines.extend(["", *report["notes"]])
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for suffix, content in (
        ("json", json.dumps(report, indent=2, allow_nan=False) + "\n"),
        ("txt", render_report(report)),
    ):
        path = folder / f"{report['day']}.{suffix}"
        if path.exists() and path.read_text() == content:
            continue
        # A reader sees either the previous complete report or the new complete report.
        fd, temporary = tempfile.mkstemp(prefix=".daily-", dir=folder)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def refresh_reports(trades: list[dict[str, Any]], folder: Path, now_ms: int) -> None:
    today = datetime.fromtimestamp(now_ms / 1000, UTC).date()
    days: dict[date, list[dict[str, Any]]] = defaultdict(list)
    days[today] = []
    days[today - timedelta(days=1)] = []
    for trade in trades:
        if trade["timestamp_ms"] <= now_ms:
            days[datetime.fromtimestamp(trade["timestamp_ms"] / 1000, UTC).date()].append(trade)
    for day, members in sorted(days.items()):
        write_report(daily_report(members, day, now_ms), folder)
