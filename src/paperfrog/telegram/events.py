"""Durable, per-chat delivery of execution and operational audit events."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings
from paperfrog.storage.database import Store
from paperfrog.telegram.control import Telegram


class EventNotifications:
    def __init__(self, s: Settings, store: Store, telegram: Telegram):
        self.s, self.store, self.telegram = s, store, telegram

    def render(self, kind: str, payload: dict[str, Any]) -> str | None:
        if kind == "order_terminal" and float(payload.get("quantity", 0)) > 0:
            with self.store.engine.connect() as conn:
                intent = conn.execute(
                    text("SELECT intent FROM orders WHERE id=:id"),
                    {"id": payload["client_id"]},
                ).scalar()
            reducing = bool(json.loads(intent)["reduce_only"]) if intent else None
            action = (
                "REDUCTION FILLED" if reducing else "ENTRY FILLED" if reducing is False else "FILL"
            )
            direction = "BUY" if payload["side"] > 0 else "SELL"
            position = "LONG" if payload["side"] > 0 else "SHORT"
            return (
                f"{action}: {payload['symbol']} {direction}"
                + (f" / {position}" if reducing is False else "")
                + f", quantity {payload['quantity']}, price {payload['price']}."
                + (
                    " Protection is not yet confirmed by this fill event."
                    if reducing is False
                    else ""
                )
            )
        if kind == "trade_closed":
            pnl = float(payload["pnl"])
            label = "LOSS" if pnl < 0 else "PROFIT" if pnl > 0 else "FLAT"
            unit = "BNFCR" if self.s.futures_account_mode == "credits" else "USDT"
            return (
                f"CLOSED — {label}: {payload.get('symbol', payload['id'])}; "
                f"recorded net PnL {pnl:+.6f} {unit} (configured fee estimates). "
                f"Consecutive losses: {payload.get('consecutive_losses', 'unknown')}."
            )
        if kind == "risk_lock":
            expiry = payload.get("until")
            until = (
                datetime.fromtimestamp(float(expiry), ZoneInfo(self.s.display_timezone)).isoformat()
                if expiry is not None
                else "local review required"
            )
            return f"RISK LOCK: {payload.get('state', 'locked')}; reason: {payload['reason']}; until: {until}. New entries blocked."
        if kind == "simple_explanation":
            message = str(payload["message"])
            # Closed PnL is delivered by trade_closed, including aborted entries.
            return None if message.startswith("CLOSE ") else message
        if kind == "simple_hosted_stop":
            return f"HOSTED STOP FILLED: {payload['symbol']}, quantity {payload['quantity']}, price {payload['price']}."
        if kind == "state_transition":
            return f"STATE: {payload['to']}."
        if kind == "local_risk_review":
            return "LOCAL RISK REVIEW: lock clearance requested; entries remain manually paused until resume succeeds."
        if kind == "unknown_order":
            return "URGENT: order outcome uncertain. Entries blocked; original order must be reconciled."
        if kind in {"simple_monitor_degraded", "simple_entry_blocked"}:
            return f"{kind.upper()}: {payload.get('symbol', 'account')}: {payload.get('reason', 'unavailable')}."
        if kind in {"simple_shutdown", "simple_critical_task_stopped", "simple_flatten_verified"}:
            return f"{kind.upper()}: {payload.get('action', 'flatten verified')}."
        if kind == "simple_process_started":
            return "BOT PROCESS STARTED: checking signals and entry permissions."
        if kind == "telegram_response" and payload.get("command") in {
            "/pause",
            "/stop",
            "/lock",
            "/confirm_flatten",
        }:
            return f"CONTROL: {payload['response']}"
        return None

    async def deliver(self) -> None:
        for chat in self.s.telegram_allowed_chat_ids:
            prefix = f"telegram_events:{chat}:"
            # Snapshot and subscription boundary are captured together; a failed
            # first send must not move the boundary past subsequent trade events.
            with self.store.transaction() as conn:
                cursor = self.store.get(prefix + "cursor", None, conn)
                if cursor is None:
                    cursor = conn.execute(
                        text("SELECT coalesce(max(id),0) FROM audit")
                    ).scalar_one()
                    positions = self.store.get("simple_positions", {}, conn)
                    lock = self.store.get("risk_lock", None, conn)
                    snapshot = (
                        f"NOTIFICATIONS CONNECTED. State: {self.store.get('state', 'unknown', conn)}. "
                        f"Recorded positions: {', '.join(positions) or 'none'}."
                    )
                    if lock:
                        snapshot += "\n" + str(self.render("risk_lock", lock))
                    self.store.put(prefix + "snapshot", snapshot, conn)
                    self.store.put(prefix + "cursor", cursor, conn)
            try:
                snapshot = self.store.get(prefix + "snapshot")
                if snapshot:
                    await self.send(chat, snapshot)
                    self.store.put(prefix + "snapshot", None)
                with self.store.engine.connect() as conn:
                    rows = list(
                        conn.execute(
                            text(
                                "SELECT id,ts,kind,payload FROM audit WHERE id>:id ORDER BY id LIMIT 200"
                            ),
                            {"id": cursor},
                        ).mappings()
                    )
                for row in rows:
                    message = self.render(row["kind"], json.loads(row["payload"]))
                    repeated = row["kind"] in {
                        "simple_monitor_degraded",
                        "simple_entry_blocked",
                        "risk_lock",
                    }
                    repeat_key = (
                        prefix
                        + "repeat:"
                        + row["kind"]
                        + ":"
                        + str(json.loads(row["payload"]).get("symbol", "account"))
                    )
                    previous = self.store.get(repeat_key, {}) if repeated else {}
                    if message and not (
                        previous.get("message") == message
                        and row["ts"] - previous.get("ts", 0) < 300_000
                    ):
                        stamp = datetime.fromtimestamp(
                            row["ts"] / 1000, ZoneInfo(self.s.display_timezone)
                        )
                        await self.send(chat, f"{stamp.isoformat()}\n{message}")
                        if repeated:
                            self.store.put(repeat_key, {"message": message, "ts": row["ts"]})
                    self.store.put(prefix + "cursor", row["id"])
            except SafeError:
                self.store.audit(
                    "simple_telegram_degraded",
                    {"reason": "notification unavailable; retry pending"},
                )

    async def send(self, chat: int, message: str) -> None:
        await self.telegram.call(
            "sendMessage",
            {"chat_id": chat, "text": f"paperfrog {self.s.trading_mode.upper()}\n{message}"[:4000]},
        )
