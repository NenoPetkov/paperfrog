"""Durable strategy selection. Never modifies authorization, positions or risk limits."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Connection

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.storage.database import Store

KEY = "simple_strategy_rotation"
CHOICES = {"current", "dip_peak"}


def features(frame: pd.DataFrame) -> pd.DataFrame:
    """Local extremes from prior completed bars, with a six-bar recovery window."""
    x = frame.copy()
    x["dip_floor"] = x.low.shift(1).rolling(24).min()
    x["peak_ceiling"] = x.high.shift(1).rolling(24).max()
    x["dip_recent_low"] = x.low.shift(1).rolling(6).min()
    x["peak_recent_high"] = x.high.shift(1).rolling(6).max()
    return x


def dip_peak_gate(row: dict[str, Any], side: int) -> str | None:
    keys = ("close", "open", "dip_floor", "peak_ceiling", "dip_recent_low", "peak_recent_high")
    atr_key = "minute_atr" if row.get("entry_timeframe", "15m") != "15m" else "atr"
    if not all(math.isfinite(float(row.get(k, math.nan))) for k in (*keys, atr_key)):
        return "dip/peak needs complete closed-candle extremes"
    atr = float(row[atr_key])
    close, floor, ceiling = float(row["close"]), float(row["dip_floor"]), float(row["peak_ceiling"])
    if atr <= 0 or ceiling - floor < 2 * atr or side not in {-1, 1}:
        return "dip/peak range too narrow or invalid"
    location = (close - floor) / (ceiling - floor)
    if side * (close - float(row["open"])) <= 0:
        return "dip/peak waiting for a confirming candle"
    if side == 1 and (
        float(row["dip_recent_low"]) <= floor + 0.5 * atr
        and 0 <= location <= 0.45
        and close - float(row["dip_recent_low"]) >= 0.5 * atr
    ):
        return None
    if side == -1 and (
        float(row["peak_recent_high"]) >= ceiling - 0.5 * atr
        and 0.55 <= location <= 1
        and float(row["peak_recent_high"]) - close >= 0.5 * atr
    ):
        return None
    return "dip/peak waiting for recovery from a recent local low or rejection of a local high"


def selection(store: Store, conn: Connection | None = None) -> dict[str, Any]:
    return dict(
        store.get(KEY, {"active": "current", "epoch": 0, "auto": False, "outcomes": []}, conn)
    )


def switch(
    store: Store, state: dict[str, Any], target: str, now_ms: int, reason: str, conn: Connection
) -> None:
    event = {
        "timestamp_ms": now_ms,
        "from": state["active"],
        "to": target,
        "reason": reason,
        "losses": sum(x["pnl"] < 0 for x in state["outcomes"]),
        "sample": len(state["outcomes"]),
        "auto": state["auto"],
    }
    state.update(active=target, epoch=state["epoch"] + 1, outcomes=[], changed_ms=now_ms)
    history = store.get("simple_strategy_history", [], conn)
    store.put("simple_strategy_history", [*history, event][-100:], conn)
    store.audit("simple_strategy_changed", event, conn)


def evaluate(store: Store, state: dict[str, Any], now_ms: int, conn: Connection) -> None:
    outcomes = state["outcomes"]
    if state["auto"] and len(outcomes) == 8 and sum(x["pnl"] < 0 for x in outcomes) >= 6:
        switch(
            store,
            state,
            "dip_peak" if state["active"] == "current" else "current",
            now_ms,
            "automatic: more than 70% losses in the last 8 closed positions",
            conn,
        )


def initialize(store: Store, base_strategy: str, now_ms: int) -> None:
    with store.transaction() as conn:
        if store.get(KEY, None, conn) is not None:
            return
        outcomes = []
        seen = set()
        for trade in sorted(store.get("simple_closed", [], conn), key=lambda x: x["timestamp_ms"]):
            context = trade.get("entry_context") or {}
            identity = trade.get("trade_id")
            if (
                identity
                and identity not in seen
                and context.get("strategy", base_strategy) == base_strategy
                and math.isfinite(float(trade["pnl"]))
            ):
                outcomes.append({"id": identity, "pnl": float(trade["pnl"])})
                seen.add(identity)
        state = {"active": "current", "epoch": 0, "auto": True, "outcomes": outcomes[-8:]}
        evaluate(store, state, now_ms, conn)
        store.put(KEY, state, conn)


def closed(
    store: Store, trade: dict[str, Any], pnl: float, now_ms: int, conn: Connection | None = None
) -> None:
    if conn is None:
        with store.transaction() as connection:
            closed(store, trade, pnl, now_ms, connection)
        return
    state = store.get(KEY, None, conn)
    if state is None:
        return  # Offline replay has no runtime strategy controller.
    if (
        trade.get("strategy_epoch", 0) != state["epoch"]
        or trade.get("strategy_choice", "current") != state["active"]
        or any(x["id"] == trade["id"] for x in state["outcomes"])
    ):
        return
    if not math.isfinite(pnl):
        raise SafeError("finite net result required for strategy rotation")
    state["outcomes"] = [*state["outcomes"], {"id": trade["id"], "pnl": pnl}][-8:]
    evaluate(store, state, now_ms, conn)
    store.put(KEY, state, conn)


def configure(store: Store, choice: str, now_ms: int) -> None:
    if choice not in CHOICES | {"auto"}:
        raise SafeError("use /strategy current, /strategy dip_peak or /strategy auto")
    with store.transaction() as conn:
        state = store.get(KEY, None, conn)
        if state is None:
            raise SafeError("strategy controller is not ready")
        state["auto"] = choice == "auto"
        if choice == "auto":
            evaluate(store, state, now_ms, conn)
        elif choice != state["active"]:
            switch(store, state, choice, now_ms, "Telegram manual selection", conn)
        store.put(KEY, state, conn)
        store.audit(
            "simple_strategy_policy", {"active": state["active"], "auto": state["auto"]}, conn
        )


def describe(store: Store, timezone: str) -> str:
    state = selection(store)
    outcomes = state["outcomes"]
    lines = [
        f"Strategy: {state['active']} · {'AUTO' if state['auto'] else 'MANUAL'}",
        f"Current stint: {sum(x['pnl'] < 0 for x in outcomes)}/{len(outcomes)} losses (net fees/funding).",
        "Auto switches at 6+ losses out of 8 closed positions; each switch starts a new sample.",
        "current = original strategy; dip_peak = confirmed dip/peak within existing trend/AI gates.",
        "/strategy current | /strategy dip_peak = manual; /strategy auto = automatic.",
        "Recent switches:",
    ]
    for event in reversed(store.get("simple_strategy_history", [])[-8:]):
        stamp = datetime.fromtimestamp(event["timestamp_ms"] / 1000, ZoneInfo(timezone))
        lines.append(
            f"{stamp:%d %b %H:%M %Z}: {event['from']} → {event['to']} "
            f"({event['losses']}/{event['sample']} losses); {event['reason']}"
        )
    return "\n".join(lines)


def require_order(store: Store, order: Order) -> None:
    if order.reduce_only or store.get(KEY) is None:
        return
    state = selection(store)
    permission = store.get("simple_strategy_order:" + order.client_id, {})
    if (
        permission.get("order") != order.model_dump(mode="json")
        or permission.get("active") != state["active"]
        or permission.get("epoch") != state["epoch"]
    ):
        raise EntryVeto("strategy selection changed or order permission missing")
    if state["active"] == "dip_peak":
        reason = dip_peak_gate(permission.get("row", {}), order.side)
        if reason:
            raise EntryVeto(reason)
