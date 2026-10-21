"""Durable strategy selection. Never modifies authorization, positions or risk limits."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.storage.database import Store

KEY = "simple_strategy_rotation"
CHOICES = {"current", "opposite"}
VERSION = "+opposite-v1"
POLICY_VERSION = 2


def execution_side(side: int, state: dict[str, Any]) -> int:
    """Invert only a qualifying normal decision; no signal stays no signal."""
    return -side if state["active"] == "opposite" else side


def decision_side(store: Store, order: Order) -> int:
    """Direction gates validate the normal decision bound to this exact order."""
    if store.get(KEY) is None or order.reduce_only:
        return order.side
    require_order(store, order)
    permission = store.get("simple_strategy_order:" + order.client_id, {})
    return int(permission["decision_side"])


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
            "opposite" if state["active"] == "current" else "current",
            now_ms,
            "automatic: more than 70% losses in the last 8 closed positions",
            conn,
        )


def initialize(store: Store, base_strategy: str, now_ms: int) -> None:
    with store.transaction() as conn:
        previous = store.get(KEY, None, conn)
        if previous is not None and previous.get("policy_version") == POLICY_VERSION:
            return
        # A new controller starts from trade one. Existing open positions retain
        # their original epoch and cannot contaminate the new strategy sample.
        state = {
            "active": "current",
            "epoch": 0,
            "auto": True,
            "outcomes": [],
            "policy_version": POLICY_VERSION,
            "changed_ms": now_ms,
        }
        if previous is not None:
            state.update(previous)
            state["auto"] = True
            switch(
                store,
                state,
                "current",
                now_ms,
                "replace dip/peak with normal/opposite; fresh sample",
                conn,
            )
            state["policy_version"] = POLICY_VERSION
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
        raise SafeError("use /strategy current, /strategy opposite or /strategy auto")
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
        "current = normal decision; opposite = LONG becomes SHORT and SHORT becomes LONG.",
        "/strategy current | /strategy opposite = manual; /strategy auto = automatic.",
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
    original = permission.get("decision_side")
    if original not in {-1, 1} or execution_side(original, state) != order.side:
        raise EntryVeto("strategy order direction does not match the normal decision")
