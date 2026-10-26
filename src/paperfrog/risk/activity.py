"""Durable limits for explicitly enabled, reduced-size exploratory entries."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from paperfrog.domain.types import SafeError
from paperfrog.storage.database import Store

KEY = "simple_activity_budget"
SIZE_SCALE = 0.5
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MAX_ATTEMPTS = 6


def blocker(store: Store, now_ms: int, trade_id: str | None = None,
            conn: Connection | None = None) -> str | None:
    if conn is None:
        with store.engine.connect() as connection:
            return blocker(store, now_ms, trade_id, connection)
    history: list[dict[str, Any]] = store.get(KEY, [], conn)
    if any(item["ts"] > now_ms for item in history):
        return "active auto waits for a valid clock"
    active: list[str] = list(conn.execute(text("SELECT id FROM entries WHERE closed=0")).scalars())
    if any(entry != trade_id for entry in active):
        return "active auto probe requires an empty portfolio"
    recent = [item for item in history if now_ms - item["ts"] < DAY_MS]
    if trade_id is not None:
        if trade_id not in active or not any(item["id"] == trade_id for item in recent):
            return "active auto probe reservation missing or expired"
        return None
    if len(recent) >= MAX_ATTEMPTS:
        return "active auto probe limit: six attempts per rolling 24 hours"
    if recent and now_ms - recent[-1]["ts"] < HOUR_MS:
        return "active auto probe cooldown: one attempt per hour"
    return None


def reserve(store: Store, trade_id: str, now_ms: int, conn: Connection) -> None:
    reason = blocker(store, now_ms, conn=conn)
    if reason:
        raise SafeError(reason)
    history = [item for item in store.get(KEY, [], conn) if now_ms - item["ts"] < DAY_MS]
    record = {"id": trade_id, "ts": now_ms}
    store.put(KEY, [*history, record], conn)
    store.audit("simple_activity_reserved", record, conn)


def has_open_probe(store: Store, conn: Connection) -> bool:
    ids = {item["id"] for item in store.get(KEY, [], conn)}
    active: list[str] = list(conn.execute(text("SELECT id FROM entries WHERE closed=0")).scalars())
    return any(entry in ids for entry in active)
