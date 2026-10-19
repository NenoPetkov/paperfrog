"""Deliver the previous 24h summary at 10:00 Sofia, without an AI review."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings, load_settings
from paperfrog.storage.database import Store
from paperfrog.storage.lock import InstanceLock

from .authorization import directory
from .performance import build_report, clean, messages, render_report, scheduled_end


def save(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".summary-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def deliver(
    s: Settings, state: Store, record: dict[str, Any], client: httpx.AsyncClient, now: datetime
) -> None:
    for chat in s.telegram_allowed_chat_ids:
        if chat < 0 and not s.telegram_allow_groups:
            continue
        for index, message in enumerate(record["messages"]):
            key = f"summary:{record['report']['end_ms']}:{chat}:{index}"
            status = state.get(key, {})
            if status.get("delivered"):
                continue
            if (
                status.get("attempts", 0) >= 3
                or now.timestamp() - status.get("attempted_at", 0) < 300
            ):
                break
            status = {"attempts": status.get("attempts", 0) + 1, "attempted_at": now.timestamp()}
            state.put(key, status)
            try:
                response = await client.post(
                    "https://api.telegram.org/bot"
                    + s.telegram_bot_token.get_secret_value()
                    + "/sendMessage",
                    json={
                        "chat_id": chat,
                        "text": clean(s, message),
                        "disable_web_page_preview": True,
                    },
                )
                data = response.json()
                if response.is_error or data.get("ok") is not True:
                    raise SafeError("summary Telegram delivery unavailable")
                state.put(
                    key, {**status, "delivered": True, "message_id": data["result"]["message_id"]}
                )
            except Exception as exc:
                state.audit(
                    "summary_delivery_unavailable",
                    {"part": index, "error_type": type(exc).__name__},
                )
                break


async def run(args: argparse.Namespace) -> None:
    s = load_settings()
    timezone = args.timezone or s.display_timezone
    s = s.model_copy(update={"display_timezone": timezone})
    now = datetime.now(UTC)
    end = now if args.preview else scheduled_end(now, timezone, args.hour)
    if end is None or end.astimezone(ZoneInfo(timezone)).date() < date.fromisoformat(
        args.first_delivery
    ):
        return
    if args.preview:
        print(clean(s, render_report(build_report(s, end))))
        return
    if not s.telegram_bot_token.get_secret_value() or not s.telegram_allowed_chat_ids:
        raise SafeError("daily summary needs configured Telegram recipients")
    folder = directory(s) / "daily-summary"
    with InstanceLock(folder):
        state = Store(folder / "deliveries.sqlite")
        try:
            day = end.astimezone(ZoneInfo(timezone)).date().isoformat()
            path = s.reports_dir / ("simple-" + s.trading_mode) / "daily-summary" / (day + ".json")
            if path.exists():
                record = json.loads(path.read_text())
            else:
                report = build_report(s, end)
                record = {"report": report, "messages": messages(clean(s, render_report(report)))}
                # Freeze both the time window and chunks before attempting delivery.
                save(path, json.loads(clean(s, json.dumps(record, ensure_ascii=False))))
            async with httpx.AsyncClient(timeout=20) as client:
                await deliver(s, state, record, client, now)
            print(
                json.dumps(
                    {
                        "summary_day": day,
                        "end_ms": record["report"]["end_ms"],
                        "parts": len(record["messages"]),
                    }
                )
            )
        finally:
            state.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hour", type=int, choices=range(24), default=10)
    parser.add_argument("--timezone")
    parser.add_argument("--first-delivery", default="1970-01-01")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Print a rolling 24h summary without sending or saving",
    )
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
