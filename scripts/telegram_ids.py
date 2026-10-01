"""Local-only identity discovery. Never prints bot token or arbitrary message contents."""

import asyncio
import os

import httpx


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Export TELEGRAM_BOT_TOKEN first; do not place it in the command line.")
    async with httpx.AsyncClient(timeout=35) as client:
        try:
            response = await client.post(
                "https://api.telegram.org/bot" + token + "/getUpdates",
                json={"timeout": 25, "allowed_updates": ["message"]},
            )
            data = response.json()
            if not data.get("ok"):
                raise SystemExit(
                    "Telegram rejected request; stop other pollers and check configuration."
                )
            for update in data["result"]:
                message = update.get("message", {})
                if message.get("chat", {}).get("type") == "private":
                    print("user_id=", message["from"]["id"], "chat_id=", message["chat"]["id"])
        except httpx.HTTPError:
            raise SystemExit("Telegram transport unavailable.") from None


asyncio.run(main())
