"""Small, plain-language Telegram views; machine diagnostics stay separate."""

from __future__ import annotations

from typing import Any


def state_label(state: str) -> str:
    return {
        "RUNNING": "🟢 Running",
        "PAUSED": "⏸ Paused",
        "DEGRADED": "⏳ Waiting for healthy data",
        "ERROR_SAFE": "🛑 Safety pause",
        "SOFT_RISK_LOCK": "☕ Loss cooldown",
        "DAILY_RISK_LOCK": "⏸ Daily risk limit",
        "WEEKLY_RISK_LOCK": "⏸ Weekly risk limit",
        "HARD_RISK_LOCK": "🛑 Account risk limit",
        "EMERGENCY_FLATTENING": "🚨 Closing positions",
        "BOOTING": "🐸 Starting up",
        "SYNCING": "🐸 Syncing with Binance",
    }.get(state, "🐸 Waiting for startup checks")


def readable_reason(reason: str) -> str:
    if "-1021" in reason:
        return "Binance rejected the request timestamp. I'm syncing the clock and will retry."
    if "-2015" in reason:
        return "Binance rejected API access. Check the API key permissions and IP allowlist."
    if reason in {"exchange transport unavailable", "stale futures mark data"}:
        return "Fresh Binance data is unavailable. I'll retry automatically."
    if "journal" in reason or "reconciliation" in reason or "do not match" in reason:
        return "The exchange and my trade records need reconciliation. Entries stay paused until they agree."
    if "hosted stop" in reason or "protection" in reason:
        return "I couldn't confirm the protective stop on Binance. I'm blocking entries while execution is checked."
    if "order not found" in reason or "unknown" in reason or "not terminal" in reason:
        return "Binance hasn't confirmed an order's outcome. I'll check the original order; I won't send a duplicate."
    return reason.rstrip(".") + "."


def positions_text(positions: dict[str, Any]) -> str:
    if not positions:
        return "Open positions: none 💤"
    lines = ["Open positions:"]
    for symbol, trade in positions.items():
        direction = "LONG" if trade["side"] > 0 else "SHORT"
        lines.append(
            f"• {symbol} · {direction} · {trade['quantity']} @ {float(trade['entry_price']):.8g}"
        )
    return "\n".join(lines)


def status_text(state: str, positions: dict[str, Any]) -> str:
    return state_label(state) + "\n\n" + positions_text(positions)
