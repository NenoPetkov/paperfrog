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
POLICY_VERSION = 5
LOSSES_TO_SWITCH = 2
RESULT_PREFIX = "simple_strategy_result:"


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
        store.get(
            KEY,
            {"active": "current", "epoch": 0, "auto": True, "outcomes": [], "loss_streak": 0},
            conn,
        )
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
        "consecutive_losses": state.get("loss_streak", 0),
    }
    state.update(active=target, epoch=state["epoch"] + 1, outcomes=[], loss_streak=0, changed_ms=now_ms)
    history = store.get("simple_strategy_history", [], conn)
    store.put("simple_strategy_history", [*history, event][-100:], conn)
    store.audit("simple_strategy_changed", event, conn)


def evaluate(store: Store, state: dict[str, Any], now_ms: int, conn: Connection) -> None:
    if state["auto"] and state["loss_streak"] >= LOSSES_TO_SWITCH:
        switch(
            store,
            state,
            "opposite" if state["active"] == "current" else "current",
            now_ms,
            "automatic: two consecutive net losses",
            conn,
        )


def initialize(store: Store, base_strategy: str, now_ms: int) -> None:
    with store.transaction() as conn:
        previous = store.get(KEY, None, conn)
        if (
            previous is not None
            and previous.get("policy_version") == POLICY_VERSION
            and previous.get("active") in CHOICES
            and isinstance(previous.get("auto"), bool)
            and isinstance(previous.get("loss_streak"), int)
            and previous["loss_streak"] >= 0
        ):
            return
        if previous is not None and previous.get("policy_version") == POLICY_VERSION:
            raise SafeError("invalid saved strategy mode or loss streak; local review required")
        # Upgrades and restarts retain the selected strategy. Existing positions
        # keep their recorded execution side, exits and strategy epoch.
        state: dict[str, Any] = {
            "active": "current",
            "epoch": 0,
            "auto": True,
            "outcomes": [],
            "loss_streak": 0,
            "policy_version": POLICY_VERSION,
            "changed_ms": now_ms,
        }
        if previous is not None:
            state.update(previous)
            if state["active"] not in CHOICES:
                raise SafeError("invalid saved strategy selection; local review required")
            # The operator requested this new automatic policy. Start counting
            # future closures without reusing historical losses or changing side.
            # Keep the epoch so existing positions in this stint can count once.
            for outcome in state["outcomes"]:
                store.put(
                    RESULT_PREFIX + outcome["id"],
                    {"strategy": state["active"], "epoch": state["epoch"], "pnl": outcome["pnl"]},
                    conn,
                )
            state.update(auto=True, outcomes=[], loss_streak=0, changed_ms=now_ms)
            state["policy_version"] = POLICY_VERSION
        store.put(KEY, state, conn)
        store.audit(
            "simple_strategy_policy_changed",
            {"active": state["active"], "auto": True, "losses_to_switch": LOSSES_TO_SWITCH},
            conn,
        )


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
        or store.get(RESULT_PREFIX + trade["id"], None, conn) is not None
    ):
        return
    if not math.isfinite(pnl):
        raise SafeError("finite net result required for strategy rotation")
    state["outcomes"] = [*state["outcomes"], {"id": trade["id"], "pnl": pnl}][-8:]
    state["loss_streak"] = state.get("loss_streak", 0) + 1 if pnl < 0 else 0
    store.put(
        RESULT_PREFIX + trade["id"],
        {"strategy": state["active"], "epoch": state["epoch"], "pnl": pnl},
        conn,
    )
    evaluate(store, state, now_ms, conn)
    store.put(KEY, state, conn)


def configure(store: Store, choice: str, now_ms: int) -> None:
    if choice not in CHOICES | {"auto"}:
        raise SafeError("use /strategy current, /strategy opposite or /strategy auto")
    initialize(store, "current", now_ms)
    with store.transaction() as conn:
        state = selection(store, conn)
        automatic = choice == "auto"
        target = state["active"] if automatic else choice
        if state["active"] != target:
            state["auto"] = False
            switch(store, state, target, now_ms, "manual selection via /strategy", conn)
            store.put(KEY, state, conn)
        elif state["auto"] != automatic:
            state.update(auto=automatic, outcomes=[], loss_streak=0, changed_ms=now_ms)
            store.put(KEY, state, conn)
            store.audit(
                "simple_strategy_mode_changed",
                {"active": state["active"], "auto": automatic, "reason": "manual selection via /strategy"},
                conn,
            )


def describe(store: Store, timezone: str) -> str:
    state = selection(store)
    outcomes = state["outcomes"]
    lines = [
        f"Strategy: {state['active']} · {'AUTO' if state['auto'] else 'MANUAL'}",
        f"Current stint: {sum(x['pnl'] < 0 for x in outcomes)}/{len(outcomes)} losses (estimated fees and recorded funding).",
        f"Consecutive losses: {state.get('loss_streak', 0)}/{LOSSES_TO_SWITCH}.",
        "AUTO: switch current ↔ opposite after two consecutive net losses; a win or breakeven resets the streak."
        if state["auto"]
        else "Automatic switching paused by manual selection.",
        "/strategy auto — enable automatic switching from the selected strategy.",
        "/strategy current — manual normal execution: LONG → LONG, SHORT → SHORT.",
        "/strategy opposite — manual reverse execution: LONG → SHORT, SHORT → LONG.",
        "Opposite also permits MIXED markets; current requires BULL/BEAR alignment.",
        "Mode, strategy and loss streak survive restarts. Switches apply to new entries; existing positions keep their exits.",
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
