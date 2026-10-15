import json
from dataclasses import replace

import pytest
from sqlalchemy import text

from paperfrog.domain.types import SafeError
from paperfrog.risk.engine import Proposal, RiskEngine
from paperfrog.telegram.events import EventNotifications


class Messages:
    def __init__(self):
        self.sent = []
        self.fail = set()

    async def call(self, method, payload):
        assert method == "sendMessage"
        if payload["chat_id"] in self.fail:
            raise SafeError("unavailable")
        self.sent.append(payload.copy())
        return {"message_id": len(self.sent)}


def worker(settings, store, chats=(123,)):
    sender = Messages()
    s = settings.model_copy(update={"telegram_allowed_chat_ids": chats})
    return EventNotifications(s, store, sender), sender


async def test_failed_entry_fills_loss_and_lock_are_all_delivered(settings, store, health, now):
    events, sender = worker(settings, store)
    await events.deliver()
    risk = RiskEngine(settings, store)
    p = Proposal("one", "SOLUSDT", 10000, 500, 1000, 13, 0, 0.01, 0.3, 0.001)
    for index in range(3):
        trade = replace(p, trade_id=str(index), symbol=f"COIN{index}")
        risk.reserve(trade, health, now)
        with store.transaction() as conn:
            conn.execute(
                text("INSERT INTO orders(id,symbol,intent,state) VALUES(:id,:s,:v,'FILLED')"),
                {"id": str(index), "s": trade.symbol, "v": json.dumps({"reduce_only": False})},
            )
            store.audit(
                "order_terminal",
                {
                    "client_id": str(index),
                    "symbol": trade.symbol,
                    "quantity": "1",
                    "price": "100",
                    "side": -1,
                },
                conn,
            )
        risk.closed(trade.trade_id, -1, now)
    store.audit(
        "simple_entry_blocked",
        {"symbol": "COIN2", "reason": "order not found; absence does not authorize resubmission"},
    )
    await events.deliver()
    messages = [m["text"] for m in sender.sent]
    assert sum("ENTRY FILLED" in m and "SHORT" in m for m in messages) == 3
    assert sum("CLOSED — LOSS" in m for m in messages) == 3
    assert sum("RISK LOCK" in m and "three_losses" in m for m in messages) == 1
    assert any("order not found" in m for m in messages)


async def test_failed_chat_retries_without_replaying_successful_chat(settings, store):
    events, sender = worker(settings, store, (123, 456))
    await events.deliver()
    sender.fail.add(456)
    store.audit("state_transition", {"to": "RUNNING"})
    await events.deliver()
    sender.fail.clear()
    # New worker instance simulates restart: delivery state lives in SQLite.
    await EventNotifications(events.s, store, sender).deliver()
    for chat in (123, 456):
        assert sum(m["chat_id"] == chat and "STATE: RUNNING" in m["text"] for m in sender.sent) == 1


async def test_failed_initial_snapshot_does_not_skip_later_events(settings, store):
    events, sender = worker(settings, store)
    sender.fail.add(123)
    await events.deliver()
    store.audit("state_transition", {"to": "RUNNING"})
    sender.fail.clear()
    await events.deliver()
    assert len(sender.sent) == 2
    assert "NOTIFICATIONS CONNECTED" in sender.sent[0]["text"]
    assert "STATE: RUNNING" in sender.sent[1]["text"]


async def test_startup_is_sent_on_each_restart_and_repeated_errors_are_suppressed(settings, store):
    events, sender = worker(settings, store)
    await events.deliver()
    for _ in range(2):
        store.audit("simple_process_started", {})
        store.audit("simple_monitor_degraded", {"reason": "exchange transport unavailable"})
        await events.deliver()
    assert sum("BOT PROCESS STARTED" in m["text"] for m in sender.sent) == 2
    assert sum("exchange transport unavailable" in m["text"] for m in sender.sent) == 1


@pytest.mark.parametrize("pnl,label", [(1, "PROFIT"), (-1, "LOSS"), (0, "FLAT")])
async def test_close_labels(settings, store, pnl, label):
    events, sender = worker(settings, store)
    await events.deliver()
    store.audit(
        "trade_closed", {"id": "trade", "symbol": "SOLUSDT", "pnl": pnl, "consecutive_losses": 0}
    )
    await events.deliver()
    assert f"CLOSED — {label}" in sender.sent[-1]["text"]
