"""Previous-24-hour ledger summary; no AI calls or trading-state writes."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings

from .analysis import number
from .authorization import directory

DAY_MS = 86_400_000


def clean(s: Settings, value: str) -> str:
    for secret in (s.openai_api_key, s.binance_api_key, s.binance_api_secret, s.telegram_bot_token):
        if secret.get_secret_value():
            value = value.replace(secret.get_secret_value(), "[redacted]")
    value = re.sub(r"https?://\S+", "[url redacted]", value)
    value = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[redacted]", value)
    return value


def stamp(ms: int, timezone: str) -> str:
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(timezone)).strftime("%d %b %Y %H:%M %Z")


def scheduled_end(now: datetime, timezone: str, hour: int) -> datetime | None:
    if now.tzinfo is None:
        raise SafeError("timezone-aware summary clock required")
    local = now.astimezone(ZoneInfo(timezone))
    due = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return due.astimezone(UTC) if local >= due else None


def build_report(s: Settings, end: datetime | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    end = end or now
    if end.tzinfo is None or end > now + timedelta(seconds=1):
        raise SafeError("summary requires a timezone-aware, non-future end")
    end_ms = int(end.timestamp() * 1000)
    start_ms = end_ms - DAY_MS
    path = directory(s) / "paperfrog.sqlite"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            # One consistent WAL snapshot, without constructing a writable Store.
            conn.execute("BEGIN")
            runtime = {
                key: json.loads(value)
                for key, value in conn.execute(
                    "SELECT key,value FROM kv WHERE key IN "
                    "('simple_closed','state','state_reason','manual_lock','risk_lock',"
                    "'profit_entry_lock','crisis','crisis_score','local_flatten_request') "
                    "OR substr(key,1,9)='cooldown:'"
                )
            }
            events = [
                (ts, kind, json.loads(payload))
                for ts, kind, payload in conn.execute(
                    "SELECT ts,kind,payload FROM audit WHERE ts>=? AND ts<? ORDER BY ts,id",
                    (start_ms, end_ms),
                )
            ]
            previous = conn.execute(
                "SELECT payload FROM audit WHERE kind='state_transition' AND ts<? "
                "ORDER BY ts DESC,id DESC LIMIT 1",
                (start_ms,),
            ).fetchone()
            first_event = conn.execute("SELECT min(ts) FROM audit").fetchone()[0]
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise SafeError("summary ledger unavailable (" + type(exc).__name__ + ")") from None

    trades = [
        trade
        for trade in runtime.get("simple_closed", [])
        if start_ms <= trade["timestamp_ms"] < end_ms
    ]
    currencies: dict[str, dict[str, Any]] = {}
    outcomes: Counter[str] = Counter()
    for trade in trades:
        currency = (trade.get("entry_context") or {}).get("currency", "unknown")
        currency = currency if currency in {"USDT", "USDC", "BNFCR"} else "unknown"
        group = currencies.setdefault(
            currency,
            {
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
                "unknown": 0,
                "won": 0.0,
                "lost": 0.0,
                "fees": 0.0,
                "funding": 0.0,
                "fees_known": 0,
                "funding_known": 0,
                "trades": 0,
            },
        )
        group["trades"] += 1
        pnl = number(trade.get("pnl"))
        outcome = (
            "unknown"
            if pnl is None
            else "wins"
            if pnl > 0
            else "losses"
            if pnl < 0
            else "breakeven"
        )
        group[outcome] += 1
        outcomes[outcome] += 1
        if pnl is not None:
            group["won" if pnl > 0 else "lost"] += pnl
        for field in ("fees", "funding"):
            if (value := number(trade.get(field))) is not None:
                group[field] += value
                group[field + "_known"] += 1

    errors: dict[tuple[str, str], dict[str, Any]] = {}
    blocks: Counter[str] = Counter()
    activations: Counter[str] = Counter()
    cooldowns: Counter[str] = Counter()
    resets = 0
    manual_requests = 0
    durations: dict[tuple[str, str], int] = defaultdict(int)
    state = json.loads(previous[0]) if previous else None
    cursor = start_ms
    for ts, kind, payload in events:
        detail = clean(
            s,
            str(
                payload.get("reason")
                or (
                    str(payload["error_type"]) + " (cause not recorded)"
                    if payload.get("error_type")
                    else "no further detail recorded"
                )
            ),
        )[:240]
        error = (
            any(word in kind for word in ("degraded", "unavailable", "failed"))
            or kind in {"unknown_order", "simple_entry_aborted", "simple_critical_task_stopped"}
            or kind == "reconciliation"
            and payload.get("consistent") is False
            or kind == "order_terminal"
            and payload.get("status") == "REJECTED"
            or kind == "flatten_result"
            and payload.get("closed") is False
            or kind == "state_transition"
            and payload.get("to") in {"ERROR_SAFE", "DEGRADED"}
        )
        if error:
            if kind == "reconciliation":
                detail = "exchange positions or orders do not match the journal"
            elif kind == "unknown_order":
                detail = "order outcome unknown; reconciliation required"
            elif kind == "flatten_result":
                detail = "position flattening incomplete"
            group = errors.setdefault((kind, detail), {"count": 0, "first_ms": ts, "last_ms": ts})
            group["count"] += 1
            group["last_ms"] = ts
        if kind in {"simple_entry_blocked", "simple_entry_skipped", "entry_veto"}:
            blocks[detail] += 1
        if kind == "risk_lock":
            activations[detail] += 1
        if kind == "trade_closed":
            cooldowns[clean(s, str(payload.get("symbol", "unknown")))[:40]] += 1
        if kind == "operator_risk_reset":
            resets += 1
        if kind == "telegram_command" and payload.get("command") in {"/pause", "/stop", "/lock"}:
            manual_requests += 1
        if kind == "state_transition":
            if state and state.get("to") != "RUNNING":
                durations[
                    (str(state.get("to")), clean(s, str(state.get("reason", "unknown")))[:240])
                ] += ts - cursor
            state, cursor = payload, ts
    if state and state.get("to") != "RUNNING":
        durations[(str(state.get("to")), clean(s, str(state.get("reason", "unknown")))[:240])] += (
            end_ms - cursor
        )

    return {
        "schema_version": 1,
        "mode": s.trading_mode,
        "timezone": s.display_timezone,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "snapshot_ms": int(now.timestamp() * 1000),
        "trades": len(trades),
        "outcomes": dict(outcomes),
        "currencies": currencies,
        "errors": [
            {"kind": kind, "detail": detail, **data}
            for (kind, detail), data in sorted(errors.items())
        ],
        "blocks": dict(sorted(blocks.items())),
        "risk_activations": dict(sorted(activations.items())),
        "cooldown_activations": dict(sorted(cooldowns.items())),
        "resets": resets,
        "manual_requests": manual_requests,
        "blocked_periods": [
            {"state": state, "reason": reason, "milliseconds": duration}
            for (state, reason), duration in sorted(durations.items())
            if duration > 0
        ],
        "initial_state_known": previous is not None,
        "audit_starts_after_window": first_event is None or first_event > start_ms,
        "current": {key: value for key, value in runtime.items() if key != "simple_closed"},
    }


def render_report(report: dict[str, Any]) -> str:
    zone = report["timezone"]
    outcomes = report["outcomes"]
    lines = [
        f"🐸 Previous 24h · {report['mode'].upper()}",
        f"{stamp(report['start_ms'], zone)} → {stamp(report['end_ms'], zone)} (end excluded)",
        f"Closed trades: {report['trades']} | Wins: {outcomes.get('wins', 0)} | Losses: {outcomes.get('losses', 0)} | Breakeven: {outcomes.get('breakeven', 0)}",
    ]
    if outcomes.get("unknown"):
        lines.append(f"Unknown PnL: {outcomes['unknown']}")
    if not report["trades"]:
        lines.append("Won: 0 | Lost: 0 | Net: 0 (no recorded closed trades)")
    for currency, group in sorted(report["currencies"].items()):
        label = "Known PnL subtotal" if group["unknown"] else "Net"
        lines.append(
            f"{currency}: won {group['won']:+.4f} | lost {group['lost']:+.4f} | {label} {group['won'] + group['lost']:+.4f}"
        )
        if group["unknown"] == group["trades"]:
            lines[-1] = f"{currency}: won/lost/net unknown (no recorded PnL)"
        fees = f"{group['fees']:.4f}" if group["fees_known"] else "unknown"
        funding = f"{group['funding']:+.4f}" if group["funding_known"] else "unknown"
        lines.append(
            f"  Fees: {fees} ({group['fees_known']}/{group['trades']} known) | Funding: {funding} ({group['funding_known']}/{group['trades']} known)"
        )
    lines.extend(
        [
            "",
            f"Errors / operational issues: {sum(e['count'] for e in report['errors'])} audit events",
        ]
    )
    if not report["errors"]:
        lines.append("None recorded in the audit.")
    for error in report["errors"]:
        lines.append(
            f"• {error['kind']}: {error['detail']} ×{error['count']} (last {stamp(error['last_ms'], zone)})"
        )
    lines.extend(["", "Locks during the 24h window:"])
    lines.append(
        "Risk-lock activations: "
        + (
            ", ".join(f"{reason} ×{count}" for reason, count in report["risk_activations"].items())
            or "0"
        )
    )
    lines.append(
        f"Manual pause commands: {report['manual_requests']} | Operator risk resets: {report['resets']}"
    )
    for period in report["blocked_periods"]:
        seconds = period["milliseconds"] // 1000
        lines.append(
            f"• {period['state']}: {seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s — {period['reason']}"
        )
    lines.append(
        "Symbol cooldowns started: "
        + (
            ", ".join(
                f"{symbol} ×{count}" for symbol, count in report["cooldown_activations"].items()
            )
            or "0"
        )
    )
    lines.append("Entry blocks / skips (attempts, not distinct locks):")
    lines.extend(f"• {reason} ×{count}" for reason, count in report["blocks"].items())
    if not report["blocks"]:
        lines.append("None recorded.")
    current = report["current"]
    lines.extend(["", f"Current locks at snapshot ({stamp(report['snapshot_ms'], zone)}):"])
    lines.append(
        f"State: {current.get('state', 'unknown')} — {current.get('state_reason', 'reason unavailable')}"
    )
    for key, label in (
        ("manual_lock", "Manual"),
        ("profit_entry_lock", "Profit entry"),
        ("local_flatten_request", "Emergency close pending"),
    ):
        value = current.get(key)
        lines.append(
            f"{label}: {'ACTIVE' if value else 'off' if key in current or key == 'local_flatten_request' else 'unknown'}"
        )
    lock = current.get("risk_lock")
    if lock:
        until = lock.get("until")
        expiry = stamp(int(until * 1000), zone) if until is not None else "operator reset required"
        lines.append(
            f"Risk: {lock.get('reason', 'unknown')} ({lock.get('state', 'unknown')}); until {expiry}"
        )
    else:
        lines.append("Risk: off" if "risk_lock" in current else "Risk: unknown")
    if (
        current.get("crisis_score")
        or current.get("crisis", {}).get("block_until", 0) * 1000 > report["snapshot_ms"]
    ):
        lines.append(f"Crisis gate: active, score {current.get('crisis_score', 'unknown')}")
    active = [
        (key[9:], value)
        for key, value in sorted(current.items())
        if key.startswith("cooldown:") and (number(value) or 0) * 1000 > report["snapshot_ms"]
    ]
    lines.append(
        "Active symbol cooldowns: "
        + (
            ", ".join(
                f"{symbol} until {stamp(int(until * 1000), zone)}" for symbol, until in active
            )
            or "none"
        )
    )
    lines.extend(
        [
            "",
            "Recorded closed-trade net PnL includes estimated fees and recorded funding; open positions are excluded.",
            "Repeated errors are audit events and can describe the same incident. Historical profit-lock changes are not separately journaled; blocked periods use recorded state transitions.",
        ]
    )
    if not report["initial_state_known"] or report["audit_starts_after_window"]:
        lines.append(
            "History coverage is incomplete at the start of this window; missing records are unknown."
        )
    return "\n".join(lines)


def messages(text: str, limit: int = 3500) -> list[str]:
    """Preserve every detail within Telegram's UTF-16 length limit, including emoji."""
    parts: list[str] = []
    part = ""
    size = 0
    for char in text:
        width = len(char.encode("utf-16-le")) // 2
        if size + width > limit:
            parts.append(part)
            part, size = "", 0
        part += char
        size += width
    if part:
        parts.append(part)
    return parts


def summary_text(s: Settings) -> str:
    return clean(s, render_report(build_report(s)))
