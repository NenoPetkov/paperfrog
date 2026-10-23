"""Read-only plain-language explanation of live entry gates."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from paperfrog.settings import Settings
from paperfrog.storage.database import Store
from paperfrog.telegram.formatting import state_label

from . import rotation
from .adaptive import active_plan, request_limit
from .bitcoin import direction_gate
from .market_trend import (
    CONFIRMATION_HOURS,
    MIN_COVERAGE,
    MIN_MARKETS,
    TREND_HOURS,
    regime,
    regime_allows,
)
from .strategy import BAR_MS

READINGS = (
    ("median_return_4h", "Market median 4h"),
    ("btc_return_4h", "Bitcoin 4h"),
    ("eth_return_4h", "Ethereum 4h"),
    ("median_return_1h", "Market median 1h"),
    ("btc_return_1h", "Bitcoin 1h"),
    ("eth_return_1h", "Ethereum 1h"),
)


def explain(s: Settings, store: Store, now_ms: int, crisis: bool, armed: bool) -> str:
    """No API requests, generated advice, order actions or risk resets."""
    try:
        zone = ZoneInfo(s.display_timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")

    def stamp(at: float) -> str:
        return datetime.fromtimestamp(at / 1000, zone).strftime("%d %b %H:%M:%S %Z")

    day = datetime.fromtimestamp(now_ms / 1000, UTC).date().isoformat()
    keys = (
        "state",
        "manual_lock",
        "risk_lock",
        "profit_entry_lock",
        "local_flatten_request",
        "simple_market_trend",
        "simple_positions",
        "simple_selected",
        "heartbeat",
        "reconciled",
        "simple_ai_budget",
        "last_signal",
        rotation.KEY,
    )
    with store.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN")
        values = {key: store.get(key, None, conn) for key in keys}
        selection_json = conn.execute(
            text("SELECT payload FROM audit WHERE kind='simple_selection' ORDER BY id DESC LIMIT 1")
        ).scalar()
        selection = json.loads(selection_json) if selection_json else {}
        counts: dict[str, int] = {
            str(symbol): int(count)
            for symbol, count in conn.execute(
                text("SELECT symbol,count(*) FROM entries WHERE day=:day GROUP BY symbol"),
                {"day": day},
            ).all()
        }
        unresolved: int = conn.execute(
            text("SELECT count(*) FROM orders WHERE state IN ('UNKNOWN','INTENDED')")
        ).scalar_one()
        reserved: int = conn.execute(
            text("SELECT count(*) FROM entries WHERE closed=0")
        ).scalar_one()
        cooldowns = {
            key.removeprefix("cooldown:"): float(json.loads(value))
            for key, value in conn.execute(
                text("SELECT key,value FROM kv WHERE key LIKE 'cooldown:%'")
            )
            if float(json.loads(value)) > now_ms / 1000
        }

    blockers: list[str] = []
    notes: list[str] = []
    lines = ["Trading check", f"As of {stamp(now_ms)}", state_label(values["state"] or "BOOTING")]
    if values["manual_lock"]:
        blockers.append("Manual pause is active.")
    if values["local_flatten_request"]:
        blockers.append("A request to close positions is still pending.")
    lock = values["risk_lock"]
    if lock:
        reason = {
            "three_losses": "loss streak cooldown",
            "daily_loss": "daily loss limit",
            "weekly_local_review": "weekly loss limit",
            "high_water": "account drawdown limit",
            "profit_giveback": "profit protection",
        }.get(lock.get("reason"), "account risk lock")
        until = lock.get("until")
        if until is None:
            blockers.append(f"Risk lock: {reason}; local review required.")
        elif float(until) * 1000 > now_ms:
            blockers.append(f"Risk lock: {reason}, until {stamp(float(until) * 1000)}.")
        else:
            notes.append(
                "The loss/risk cooldown has expired; its record clears on normal automatic resume after a valid setup and health checks."
            )
    if values["profit_entry_lock"]:
        blockers.append("Daily profit protection blocks new entries.")
    if values["state"] in {"ERROR_SAFE", "DEGRADED", "BOOTING", "SYNCING", "EMERGENCY_FLATTENING"}:
        blockers.append("Startup, execution or data checks are not ready.")
    if values["reconciled"] is not True or unresolved:
        blockers.append("Exchange/order reconciliation is incomplete.")
    heartbeat = values["heartbeat"]
    if heartbeat is None or now_ms / 1000 - float(heartbeat) >= 30:
        blockers.append("The bot heartbeat is missing or stale.")
    if s.trading_mode == "live" and not armed:
        blockers.append("Local live authorization is unavailable.")
    if crisis:
        blockers.append("The BTC crisis check is active or its reference is unavailable.")
    opened = max(len(values["simple_positions"] or {}), int(reserved))
    lines.append(f"Open positions: {opened}/{s.max_concurrent_strategy_trades}")
    if opened >= s.max_concurrent_strategy_trades:
        blockers.append("The maximum number of open positions has been reached.")
    if sum(counts.values()) >= s.max_strategy_entries_per_day:
        blockers.append(
            f"Daily entry limit reached: {sum(counts.values())}/{s.max_strategy_entries_per_day} (resets 00:00 UTC)."
        )

    strategy = (values[rotation.KEY] or {}).get("active", "current")
    market = values["simple_market_trend"]
    fresh_market = False
    if s.simple_market_trend_enabled:
        try:
            nums = [float(market[key]) for key in ("bull_index", *(key for key, _ in READINGS))]
            fresh_market = (
                all(math.isfinite(value) for value in nums)
                and 0 <= nums[0] <= 100
                and market.get("config_hash") == s.config_hash
                and market.get("trend_hours") == TREND_HOURS
                and market.get("confirmation_hours") == CONFIRMATION_HOURS
                and 0 <= now_ms - int(market["timestamp_ms"]) < BAR_MS
                and MIN_MARKETS <= int(market["covered_coins"]) <= int(market["pool_coins"])
                and int(market["covered_coins"]) >= MIN_COVERAGE * int(market["pool_coins"])
                and market["regime"] == regime(*nums)
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            fresh_market = False
        if not fresh_market:
            lines += ["", "Market: waiting for fresh 4h/1h data."]
            blockers.append("Market trend data is missing, stale or from an older rule version.")
        else:
            lines += [
                "",
                f"Market: {market['regime']} · candles closed {stamp(market['timestamp_ms'])}",
                f"Coins up over 4h: {market['bull_index']:.1f}% · coverage {market['covered_coins']}/{market['pool_coins']}",
                f"Market median: 4h {market['median_return_4h']:+.2%} | 1h {market['median_return_1h']:+.2%}",
                f"Bitcoin: 4h {market['btc_return_4h']:+.2%} | 1h {market['btc_return_1h']:+.2%}",
                f"Ethereum: 4h {market['eth_return_4h']:+.2%} | 1h {market['eth_return_1h']:+.2%}",
            ]
            for side, label in ((1, "LONG"), (-1, "SHORT")):
                if market["regime"] == "MIXED" and regime_allows("MIXED", side, strategy):
                    lines.append(f"{label} market check: passed — MIXED allowed in opposite strategy")
                    continue
                failed = []
                index = float(market["bull_index"])
                if side * (index - 50) <= 0:
                    failed.append(
                        f"4h advancing coins must be {'above' if side > 0 else 'below'} 50% (now {index:.1f}%)"
                    )
                failed += [
                    f"{name} is {'flat' if float(market[key]) == 0 else 'negative' if float(market[key]) < 0 else 'positive'} ({float(market[key]):+.2%})"
                    for key, name in READINGS
                    if side * float(market[key]) <= 0
                ]
                lines.append(
                    f"{label} market check: "
                    + ("blocked — " + "; ".join(failed) if failed else "passed")
                )
            if market["regime"] == "MIXED":
                if regime_allows("MIXED", 1, strategy):
                    notes.append(
                        "Opposite strategy permits MIXED-market entries; coin trend, BTC and risk checks still apply."
                    )
                else:
                    blockers.append(
                        "Market signals disagree or are flat: MIXED permits neither LONG nor SHORT."
                    )
            elif s.simple_btc_reference_enabled:
                side = 1 if market["regime"] == "BULL" else -1
                btc_reason = direction_gate(s, store, side, now_ms)
                if btc_reason:
                    blockers.append(btc_reason + ".")
    else:
        lines += ["", "The 4h/1h market filter is disabled."]

    selected = values["simple_selected"] or []
    current_scan = (
        selection.get("entry_timeframe", "15m") == s.simple_entry_timeframe
        and selection.get("trend_hours") == TREND_HOURS
        and selection.get("confirmation_hours") == CONFIRMATION_HOURS
        and 0 <= now_ms - int(selection.get("asof_ms", 0)) < s.simple_entry_bar_ms
    )
    if s.simple_market_trend_enabled:
        current_scan = (
            current_scan
            and fresh_market
            and (int(selection.get("asof_ms", 0)) // BAR_MS * BAR_MS == market["timestamp_ms"])
        )
    lines += ["", f"Eligible candidates: {len(selected)}"]
    if not current_scan:
        blockers.append("Waiting for the next fresh entry scan using the 4h/1h rules.")
    elif not selected:
        blockers.append("No coin passed the entry and trend filters in the latest scan.")
    if current_scan:
        lines.append(
            f"Last {s.simple_entry_timeframe} scan: {stamp(selection['asof_ms'])}; {selection.get('usable_size', 0)}/{selection.get('pool_size', 0)} coins usable."
        )
        rejected = selection.get("day_trend_blocks") or {}
        if rejected:
            lines.append(f"Trend rejections: {len(rejected)}. Examples:")
            lines.extend(
                f"• {symbol}: {str(reason)[:220]}" for symbol, reason in list(rejected.items())[:5]
            )
        strategy_rejected = selection.get("strategy_blocks") or {}
        if strategy_rejected:
            lines.append(f"Dip/peak rejections: {len(strategy_rejected)}. Examples:")
            lines.extend(
                f"• {symbol}: {str(reason)[:220]}"
                for symbol, reason in list(strategy_rejected.items())[:5]
            )
    for symbol in selected:
        if counts.get(symbol, 0) >= s.max_entries_per_symbol_per_day:
            blockers.append(f"{symbol}: daily per-coin entry limit reached.")
    if cooldowns:
        lines.append(f"Coin cooldowns: {len(cooldowns)} active.")
        lines.extend(
            f"• {symbol}: until {stamp(until * 1000)}"
            for symbol, until in sorted(cooldowns.items())[:5]
        )
        for symbol in selected:
            if symbol in cooldowns:
                blockers.append(f"{symbol}: coin cooldown is active.")

    if s.simple_ai_enabled:
        plan = active_plan(
            s, store, now_ms // s.simple_ai_review_bar_ms * s.simple_ai_review_bar_ms, now_ms
        )
        if plan is not None and plan.action == "pause":
            blockers.append("AI requested a pause for the current review.")
            lines.append("AI explanation: " + plan.explanation[:350])
        elif plan is None:
            blockers.append(
                "AI has no current entry permission; waiting for eligible candidates and the next successful review."
            )
        else:
            lines.append(
                "AI: current entry permission is active; each coin must still match its conditions."
            )
        budget = values["simple_ai_budget"] or {}
        if budget.get("day") == now_ms // 86_400_000 and budget.get("count", 0) >= request_limit(s):
            blockers.append("The daily AI review limit has been reached (resets 00:00 UTC).")
    else:
        lines.append("AI: disabled; deterministic entries apply.")

    last = values["last_signal"] or {}
    if 0 <= now_ms - int(last.get("timestamp_ms", 0)) < s.simple_entry_bar_ms:
        lines.append(
            f"Latest evaluated signal · {last.get('symbol', 'coin')}: {str(last.get('reason', 'waiting'))[:250]}"
        )
    lines += ["", "Entry blockers now:"]
    lines += (
        [f"• {message}" for message in blockers]
        if blockers
        else [
            "No general blocker found; waiting for a coin to pass its signal and final execution checks."
        ]
    )
    lines += notes
    lines += [
        "",
        f"Market refresh: 15 minutes. Entry candles: {s.simple_entry_timeframe}. AI review schedule: {s.simple_ai_review_minutes} minutes.",
    ]
    return "\n".join(lines)
