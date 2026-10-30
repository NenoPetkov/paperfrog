"""Durable frequency limits for explicitly enabled, reduced-size auto entries."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from paperfrog.domain.types import SafeError
from paperfrog.storage.database import Store

KEY = "simple_activity_budget"
SIZE_SCALE = 0.5
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MAX_ATTEMPTS = 24
MAX_CONCURRENT = 4
BURST_MS = 15 * 60_000
BURST_ATTEMPTS = 4
DESCRIPTION = "half normal size; up to four positions; four attempts per rolling 15m; 24 per rolling 24h (subject to configured risk limits)"


def blocker(store: Store, now_ms: int, trade_id: str | None = None,
            conn: Connection | None = None, *, max_positions: int = MAX_CONCURRENT) -> str | None:
    if conn is None:
        with store.engine.connect() as connection:
            return blocker(store, now_ms, trade_id, connection, max_positions=max_positions)
    history: list[dict[str, Any]] = store.get(KEY, [], conn)
    if any(item["ts"] > now_ms for item in history):
        return "active auto waits for a valid clock"
    active: list[str] = list(conn.execute(text("SELECT id FROM entries WHERE closed=0")).scalars())
    limit = min(MAX_CONCURRENT, max_positions)
    recent = [item for item in history if now_ms - item["ts"] < DAY_MS]
    if trade_id is not None:
        if trade_id not in active or not any(item["id"] == trade_id for item in recent):
            return "active auto probe reservation missing or expired"
        # This order already owns a slot and consumes a frequency reservation.
        # Rechecking must not count it twice or reject the last allowed order.
        if len(active) > limit:
            return "active auto concurrent position limit exceeded"
        return None
    if len(active) >= limit:
        return f"active auto concurrent position limit: {limit}"
    if len(recent) >= MAX_ATTEMPTS:
        return "active auto probe limit: 24 attempts per rolling 24 hours"
    if sum(now_ms - item["ts"] < BURST_MS for item in recent) >= BURST_ATTEMPTS:
        return "active auto probe limit: four attempts per rolling 15 minutes"
    return None


def reserve(store: Store, trade_id: str, now_ms: int, conn: Connection,
            *, max_positions: int = MAX_CONCURRENT) -> None:
    reason = blocker(store, now_ms, conn=conn, max_positions=max_positions)
    if reason:
        raise SafeError(reason)
    history = [item for item in store.get(KEY, [], conn) if now_ms - item["ts"] < DAY_MS]
    record = {"id": trade_id, "ts": now_ms}
    store.put(KEY, [*history, record], conn)
    store.audit("simple_activity_reserved", record, conn)
