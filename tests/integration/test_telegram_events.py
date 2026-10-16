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


async def test_fills_losses_and_cooldown_delivered_without_skipped_entry_spam(
    settings, store, health, now
):
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
    assert sum("Sold" in m and "SHORT" in m for m in messages) == 3
    assert sum("closed · loss" in m for m in messages) == 3
    assert sum("breather after 3 losses" in m and "automatically" in m for m in messages) == 1
    assert not any("order not found" in m for m in messages)
    assert not any("SOFT_RISK_LOCK" in m for m in messages)


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
        assert sum(m["chat_id"] == chat and "Running!" in m["text"] for m in sender.sent) == 1


async def test_failed_initial_snapshot_does_not_skip_later_events(settings, store):
    events, sender = worker(settings, store)
    sender.fail.add(123)
    await events.deliver()
    store.audit("state_transition", {"to": "RUNNING"})
    sender.fail.clear()
    await events.deliver()
    assert len(sender.sent) == 2
    assert "Telegram connected" in sender.sent[0]["text"]
    assert "Running!" in sender.sent[1]["text"]


async def test_startup_is_sent_on_each_restart_and_repeated_errors_are_suppressed(settings, store):
    events, sender = worker(settings, store)
    await events.deliver()
    for _ in range(2):
        store.audit("simple_process_started", {})
        store.audit("simple_monitor_degraded", {"reason": "exchange transport unavailable"})
        await events.deliver()
    assert sum("I'm awake!" in m["text"] for m in sender.sent) == 2
    assert not any("exchange transport unavailable" in m["text"] for m in sender.sent)


@pytest.mark.parametrize("pnl,label", [(1, "PROFIT"), (-1, "LOSS"), (0, "FLAT")])
async def test_close_labels(settings, store, pnl, label):
    events, sender = worker(settings, store)
    await events.deliver()
    store.audit(
        "trade_closed", {"id": "trade", "symbol": "SOLUSDT", "pnl": pnl, "consecutive_losses": 0}
    )
    await events.deliver()
    assert f"closed · {label.lower()}" in sender.sent[-1]["text"]


async def test_real_state_changes_include_reason_once_and_keep_audit_details(settings, store):
    events, sender = worker(settings, store)
    await events.deliver()
    for _ in range(3):
        store.transition("DEGRADED", "exchange transport unavailable")
        store.audit(
            "simple_entry_blocked", {"symbol": "SOLUSDT", "reason": "minimum notional not met"}
        )
    store.transition("RUNNING", "health and execution checks passed")
    store.transition("ERROR_SAFE", "order not found; absence does not authorize resubmission")
    await events.deliver()
    texts = [m["text"] for m in sender.sent]
    assert sum("Fresh Binance data is unavailable" in m for m in texts) == 1
    assert sum("Running!" in m for m in texts) == 1
    assert sum("hasn't confirmed an order" in m for m in texts) == 1
    assert not any("ERROR_SAFE" in m or "minimum notional" in m for m in texts)
    assert sum(row["kind"] == "simple_entry_blocked" for row in store.rows("audit")) == 3


async def test_simple_status_contains_only_status_and_open_positions(settings):
    from paperfrog.simple.service import SimpleService

    service = SimpleService(settings)
    service.store.put("state", "RUNNING")
    service.store.put(
        "simple_positions",
        {"SOLUSDT": {"side": 1, "quantity": "2", "entry_price": 100, "id": "private-journal-id"}},
    )
    assert await service.status("/status") == (
        "🟢 Running\n\nOpen positions:\n• SOLUSDT · LONG · 2 @ 100"
    )
    assert "loaded_settings" in json.loads(await service.diagnostics("/status"))
    await service.exchange.close()
    service.store.engine.dispose()
