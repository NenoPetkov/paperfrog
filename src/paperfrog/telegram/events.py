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
from paperfrog.telegram.formatting import readable_reason, state_label, status_text


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
            action = "Bought" if payload["side"] > 0 else "Sold"
            direction = "LONG" if payload["side"] > 0 else "SHORT"
            detail = (
                " · reducing position"
                if reducing
                else f" · {direction}"
                if reducing is False
                else ""
            )
            return (
                f"{'🛬' if reducing else '🐸'} {action} {payload['symbol']}{detail}\n"
                f"{payload['quantity']} at {payload['price']}"
            )
        if kind == "trade_closed":
            pnl = float(payload["pnl"])
            label = "profit" if pnl > 0 else "loss" if pnl < 0 else "flat"
            unit = "BNFCR" if self.s.futures_account_mode == "credits" else "USDT"
            return (
                f"{'✅' if pnl >= 0 else '🧾'} {payload.get('symbol', payload['id'])} closed · {label}\n"
                f"Net result: {pnl:+.6f} {unit} (estimated fees)."
            )
        if kind == "risk_lock":
            expiry = payload.get("until")
            until = (
                datetime.fromtimestamp(float(expiry), ZoneInfo(self.s.display_timezone)).strftime(
                    "%d %b, %H:%M %Z"
                )
                if expiry is not None
                else None
            )
            if payload["reason"] == "three_losses":
                return (
                    "☕ Taking a breather after 3 losses in a row.\n"
                    f"New entries can resume automatically after {until}, once checks pass.\n"
                    "Use /resume to reset the cooldown now, once execution and health checks pass."
                )
            reasons = {
                "daily_loss": "Today's loss limit was reached",
                "profit_giveback": "The daily profit protection limit was reached",
                "weekly_local_review": "The weekly loss limit was reached",
                "high_water": "The account drawdown limit was reached",
            }
            reason = reasons.get(payload["reason"], "An account risk limit was reached")
            followup = (
                f"I'll recheck after {until}."
                if until
                else "Automatic recovery requires an operator risk reset."
            )
            return (
                f"⏸ {reason}. New entries are paused.\n{followup}\n"
                "Use /resume to reset financial locks and their equity baselines, "
                "once execution and health checks pass."
            )
        if kind == "simple_hosted_stop":
            action = "Bought" if payload["side"] > 0 else "Sold"
            return (
                f"🛟 Stop filled: {action.lower()} {payload['symbol']}\n"
                f"{payload['quantity']} at {payload['price']}"
            )
        if kind == "state_transition":
            state = payload["to"]
            if state.endswith("RISK_LOCK") and payload.get("reason") in {
                "three_losses",
                "daily_loss",
                "profit_giveback",
                "weekly_local_review",
                "high_water",
            }:
                return None  # The risk_lock event includes the cause and expiry.
            if state == "RUNNING":
                return "🐸 Running! I'm watching for trades and managing open positions."
            return f"{state_label(state)}\n{readable_reason(payload.get('reason', 'Checking entry permissions'))}"
        if kind == "simple_entry_aborted":
            return (
                f"🛟 {payload['symbol']}: the entry didn't complete, so I closed it back out.\n"
                f"{readable_reason(payload['reason'])} Position closure verified on Binance."
            )
        if kind == "local_risk_review":
            return "🐸 Risk review recorded. Entries stay paused until resume checks pass."
        if kind == "simple_process_started":
            return "🐸 I'm awake! Checking Binance, positions and fresh data before trading."
        # Skipped candidates and repeated polling failures remain in the local audit.
        # State transitions provide actionable alerts without per-symbol error spam.
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
                    snapshot = "🐸 Telegram connected!\n" + status_text(
                        self.store.get("state", "BOOTING", conn), positions
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
                    repeated = row["kind"] == "risk_lock"
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
                        await self.send(chat, f"{message}\n🕒 {stamp:%d %b, %H:%M %Z}")
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
            {
                "chat_id": chat,
                "text": f"🐸 paperfrog · {self.s.trading_mode.upper()}\n{message}"[:4000],
            },
        )
