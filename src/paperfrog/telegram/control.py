from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from paperfrog.domain.types import SafeError, State
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .commands import COMMANDS, menu_commands


class Controls:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        resume: Callable[[], Awaitable[None]],
        flatten: Callable[[str], Awaitable[bool]],
        status: Callable[[str], Awaitable[str]],
        *,
        force_resume: Callable[[], Awaitable[None]] | None = None,
        strategy: Callable[[str | None], Awaitable[str]] | None = None,
    ):
        self.s, self.store, self.resume, self.flatten, self.status = (
            settings,
            store,
            resume,
            flatten,
            status,
        )
        self.challenges: dict[tuple[int, int], tuple[str, float]] = {}
        self.force_resume = force_resume
        self.strategy = strategy
        self.rate: dict[tuple[int, int], list[float]] = {}

    async def handle(self, update: dict[str, Any]) -> tuple[int, str] | None:
        message = update.get("message")
        if (
            not message
            or "edited_message" in update
            or message.get("forward_origin")
            or message.get("forward_date")
        ):
            return None
        user, chat = message.get("from", {}), message.get("chat", {})
        uid, cid = user.get("id"), chat.get("id")
        if (
            uid not in self.s.telegram_allowed_user_ids
            or cid not in self.s.telegram_allowed_chat_ids
        ):
            self.store.audit("telegram_denied", {"reason": "allowlist"})
            return None
        if chat.get("type") != "private" and not self.s.telegram_allow_groups:
            return None
        if user.get("is_bot"):
            return None
        if abs(time.time() - message.get("date", 0)) > 120:
            return None
        key = (uid, cid)
        now = time.monotonic()
        self.rate[key] = [v for v in self.rate.get(key, []) if now - v < 60]
        if len(self.rate[key]) >= 12:
            return cid, "Command rate limit reached."
        self.rate[key].append(now)
        words = message.get("text", "").split()
        command = words[0] if words else ""
        if command == "/confirm_flatten" and len(words) == 1:
            return (
                cid,
                "Use /flatten first, then send /confirm_flatten CODE using its one-time confirmation code.",
            )
        valid_length = (
            len(words) in {1, 2}
            if command == "/strategy"
            else len(words) == (2 if command == "/confirm_flatten" else 1)
        )
        if command not in COMMANDS or not valid_length:
            return cid, "Unknown command. Use /help."
        event_key = "telegram-update:" + str(update["update_id"])
        if not self.store.once(event_key, {"command": command}):
            return None
        # Audit command names only: never serialize arbitrary user input or confirmation codes.
        self.store.audit("telegram_command", {"command": command})
        try:
            if command == "/help":
                response = "paperfrog | " + self.s.trading_mode + "\n" + " ".join(sorted(COMMANDS))
                response += (
                    "\n/summary shows the previous 24h: wins, losses, PnL, errors and locks."
                )
                response += "\n/why explains entry blockers and the 4h/1h market readings."
                response += "\n/strategy shows manual selection and profit runners. /strategy current selects normal execution; /strategy opposite requires a failed breakout or confirmed reversal. Use /strategy auto for historical selection of current/opposite, 5m/15m and holding limits; entries wait without validated evidence."
                response += "\n/strategy auto-active adds limited, half-size trend probes when no historical variant qualifies: one position, one attempt per hour and six per rolling 24h."
                response += "\n/resume resets financial locks and cooldowns after execution and health checks."
                response += "\n/refuse_force resets the same locks despite resume-time scan/crisis checks. Local authorization, reconciliation and order-execution gates still apply."
            elif command == "/summary":
                if self.s.strategy_profile != "simple":
                    raise SafeError("24h summary is only supported by the simple profile")
                from paperfrog.simple.performance import summary_text

                response = await asyncio.to_thread(summary_text, self.s)
            elif command == "/strategy":
                if self.strategy is None:
                    raise SafeError("strategy selection is only supported by the simple profile")
                response = await self.strategy(words[1] if len(words) == 2 else None)
            elif command in {"/stop", "/pause", "/lock"}:
                self.store.put("manual_lock", True)
                if not self.store.get("risk_lock"):
                    self.store.transition(State.PAUSED, "you asked me to pause")
                response = "⏸ Entries paused. I'm still watching open positions and exits."
            elif command in {"/resume", "/start_trading"}:
                await self.resume()
                response = "🐸 Financial locks and cooldowns reset. Trading is running."
            elif command == "/refuse_force":
                if self.force_resume is None:
                    raise SafeError("force resume is only supported by the simple profile")
                await self.force_resume()
                response = (
                    "🐸 Financial locks and cooldowns reset. Resume scan/crisis checks overridden. "
                    "Trading is enabled; new orders still require fresh executable data and strategy permission."
                )
            elif command == "/flatten":
                code = secrets.token_hex(4)
                self.challenges[key] = (hashlib.sha256(code.encode()).hexdigest(), now + 60)
                response = "Within 60 seconds: /confirm_flatten " + code
            elif command == "/confirm_flatten":
                challenge = self.challenges.pop(key, None)
                if (
                    not challenge
                    or challenge[1] < now
                    or not secrets.compare_digest(
                        challenge[0], hashlib.sha256(words[1].encode()).hexdigest()
                    )
                ):
                    response = "Confirmation invalid or expired."
                else:
                    closed = await self.flatten("telegram-" + str(update["update_id"]))
                    response = (
                        "Flatten verified; manual lock active."
                        if closed
                        else "URGENT: flatten incomplete; reconciliation required."
                    )
            else:
                response = await self.status(command)
        except SafeError as exc:
            response = "Refused: " + str(exc)
        self.store.audit(
            "telegram_response",
            {
                "command": command,
                "response": "confirmation issued" if command == "/flatten" else response,
            },
        )
        return cid, response


class Telegram:
    def __init__(
        self, s: Settings, store: Store, controls: Controls, client: httpx.AsyncClient | None = None
    ):
        self.s, self.store, self.controls = s, store, controls
        self.client = client or httpx.AsyncClient(timeout=40)
        self.base = "https://api.telegram.org/bot" + s.telegram_bot_token.get_secret_value()
        self.last_alert: dict[str, float] = {}

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self.client.post(self.base + "/" + method, json=payload)
            if response.is_error:
                raise SafeError("Telegram API unavailable")
            data = response.json()
            if not data.get("ok"):
                raise SafeError("Telegram request rejected")
            return data["result"]
        except (httpx.HTTPError, ValueError):
            raise SafeError("Telegram transport or response invalid") from None

    async def register_commands(self) -> None:
        await self.call("setMyCommands", {"commands": menu_commands()})

    async def poll(self) -> None:
        try:
            await self.register_commands()
        except SafeError:
            self.store.audit("telegram_menu_degraded", {"reason": "command menu unavailable"})
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    {
                        "offset": self.store.get("telegram_offset", 0),
                        "timeout": 25,
                        "allowed_updates": ["message"],
                    },
                )
                for update in updates:
                    reply = await self.controls.handle(update)
                    if reply:
                        from paperfrog.simple.performance import messages

                        for part in messages(reply[1]):
                            await self.call("sendMessage", {"chat_id": reply[0], "text": part})
                    self.store.put("telegram_offset", int(update["update_id"]) + 1)
                self.store.put("telegram_healthy_at", time.time())
            except SafeError:
                self.store.audit("telegram_degraded", {"reason": "poll unavailable"})
                await asyncio.sleep(5)

    async def notify(self, kind: str, text: str, severity: str = "INFO") -> None:
        key = kind + ":" + text
        now = time.monotonic()
        if now - self.last_alert.get(key, -1000) < 60:
            return
        for chat in self.s.telegram_allowed_chat_ids:
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": f"[{severity}] paperfrog {self.s.trading_mode}\n{text}"[:4000],
                },
            )
        self.last_alert[key] = now
