import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from paperfrog.domain.types import SafeError
from paperfrog.simple.authorization import directory
from paperfrog.simple.daily_summary import deliver, run
from paperfrog.simple.performance import (
    DAY_MS,
    build_report,
    messages,
    render_report,
    scheduled_end,
    summary_text,
)
from paperfrog.storage.database import Store
from paperfrog.telegram.control import Controls, Telegram


@pytest.fixture
def ledger(settings):
    store = Store(directory(settings) / "paperfrog.sqlite")
    store.put("manual_lock", False)
    store.put("profit_entry_lock", False)
    store.put("risk_lock", None)
    store.put("state", "RUNNING")
    yield store
    store.engine.dispose()


def event(store, ms, kind, payload):
    with store.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit(ts,kind,payload) VALUES (:ts,:kind,:payload)"),
            {"ts": ms, "kind": kind, "payload": json.dumps(payload)},
        )


def trade(ms, pnl, currency="USDT"):
    return {
        "timestamp_ms": ms,
        "pnl": pnl,
        "fees": 0.1,
        "funding": 0.02,
        "entry_context": {"currency": currency},
    }


def test_exact_rolling_window_money_by_currency_missing_values_and_read_only(settings, ledger):
    end = datetime.now(UTC) - timedelta(seconds=1)
    end_ms = int(end.timestamp() * 1000)
    start = end_ms - DAY_MS
    ledger.put(
        "simple_closed",
        [
            trade(start - 1, 500),
            trade(start, 12),
            trade(start + 1, -5),
            trade(start + 2, 0),
            trade(start + 3, 7, "BNFCR"),
            trade(start + 4, None),
            trade(end_ms - 1, -2),
            trade(end_ms, 900),
        ],
    )
    before = ledger.get("simple_closed")
    report = build_report(settings, end)
    assert report["end_ms"] - report["start_ms"] == DAY_MS
    assert report["trades"] == 6
    assert report["outcomes"] == {"wins": 2, "losses": 2, "breakeven": 1, "unknown": 1}
    assert report["currencies"]["USDT"]["won"] == 12
    assert report["currencies"]["USDT"]["lost"] == -7
    assert report["currencies"]["BNFCR"]["won"] == 7
    output = render_report(report)
    assert "Known PnL subtotal +5.0000" in output
    assert "BNFCR: won +7.0000" in output
    assert ledger.get("simple_closed") == before
    assert ledger.rows("audit") == []
    assert not (directory(settings) / "daily-summary").exists()


def test_lock_spans_start_boundary_errors_blocks_and_current_cooldowns(settings, ledger):
    end = datetime.now(UTC) - timedelta(seconds=1)
    start = int(end.timestamp() * 1000) - DAY_MS
    event(
        ledger, start - 1000, "state_transition", {"to": "SOFT_RISK_LOCK", "reason": "three_losses"}
    )
    event(
        ledger, start + 3_600_000, "state_transition", {"to": "RUNNING", "reason": "checks passed"}
    )
    event(
        ledger, start + 100, "simple_monitor_degraded", {"reason": "exchange transport unavailable"}
    )
    event(
        ledger, start + 200, "simple_monitor_degraded", {"reason": "exchange transport unavailable"}
    )
    event(ledger, start + 300, "simple_entry_blocked", {"reason": "concurrent trade cap"})
    event(ledger, start + 400, "risk_lock", {"reason": "three_losses"})
    event(ledger, start + 500, "trade_closed", {"symbol": "ETHUSDT"})
    event(ledger, start + 600, "operator_risk_reset", {})
    event(ledger, start + 700, "telegram_command", {"command": "/pause"})
    ledger.put("cooldown:ETHUSDT", datetime.now(UTC).timestamp() + 3600)
    ledger.put("cooldown:BTCUSDT", 1)
    ledger.put("profit_entry_lock", True)
    report = build_report(settings, end)
    assert report["blocked_periods"] == [
        {"state": "SOFT_RISK_LOCK", "reason": "three_losses", "milliseconds": 3_600_000}
    ]
    assert report["errors"][0]["count"] == 2
    assert report["blocks"] == {"concurrent trade cap": 1}
    assert report["resets"] == report["manual_requests"] == 1
    assert report["cooldown_activations"] == {"ETHUSDT": 1}
    output = render_report(report)
    assert "1h 0m 0s" in output and "Profit entry: ACTIVE" in output
    active = output.split("Active symbol cooldowns: ")[1]
    assert "ETHUSDT until" in active and "BTCUSDT until" not in active
    assert report["initial_state_known"] is True


def test_empty_and_unknown_pnl_do_not_invent_money(settings, ledger):
    assert "Won: 0 | Lost: 0 | Net: 0" in summary_text(settings)
    ledger.put("simple_closed", [trade(int(datetime.now(UTC).timestamp() * 1000) - 1000, None)])
    assert "won/lost/net unknown" in summary_text(settings)
    assert "coverage is incomplete" in summary_text(settings)


def test_missing_database_is_not_created(settings):
    with pytest.raises(SafeError, match="ledger unavailable"):
        build_report(settings)
    assert not directory(settings).exists()


def test_error_details_redact_secrets_and_urls(settings, ledger):
    s = settings.model_copy(update={"telegram_bot_token": SecretStr("secret-token")})
    event(
        ledger,
        int(datetime.now(UTC).timestamp() * 1000) - 1000,
        "simple_ai_unavailable",
        {"reason": "secret-token https://example.org/token=abc"},
    )
    output = summary_text(s)
    assert "secret-token" not in output and "https://" not in output
    assert "[redacted]" in output


@pytest.mark.parametrize(
    "at,expected",
    [
        (datetime(2026, 10, 19, 6, 59, tzinfo=UTC), None),
        (datetime(2026, 10, 19, 7, 0, tzinfo=UTC), datetime(2026, 10, 19, 7, 0, tzinfo=UTC)),
        (datetime(2026, 10, 19, 12, 0, tzinfo=UTC), datetime(2026, 10, 19, 7, 0, tzinfo=UTC)),
        (datetime(2026, 10, 25, 8, 0, tzinfo=UTC), datetime(2026, 10, 25, 8, 0, tzinfo=UTC)),
        (datetime(2026, 3, 29, 7, 0, tzinfo=UTC), datetime(2026, 3, 29, 7, 0, tzinfo=UTC)),
    ],
)
def test_schedule_sofia_and_dst(at, expected):
    assert scheduled_end(at, "Europe/Sofia", 10) == expected


def test_chunks_preserve_all_details_and_telegram_unicode_limit():
    value = "🐸" * 5000 + "\nlast lock detail"
    parts = messages(value)
    assert "".join(parts) == value
    assert all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts)
    assert parts[-1].endswith("last lock detail")


async def test_daily_retry_per_chat_and_part(settings, tmp_path):
    s = settings.model_copy(update={"telegram_allowed_chat_ids": (1, 2, -3)})
    state = Store(tmp_path / "delivery.sqlite")
    record = {"report": {"end_ms": 123}, "messages": ["first", "last lock detail"]}
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["chat_id"] == 1 and payload["text"] == "last lock detail" and len(calls) == 2:
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    now = datetime.now(UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await deliver(s, state, record, client, now)
        assert len(calls) == 4
        await deliver(s, state, record, client, now + timedelta(seconds=60))
        assert len(calls) == 4
        await deliver(s, state, record, client, now + timedelta(minutes=5))
        assert len(calls) == 5
        await deliver(s, state, record, client, now + timedelta(minutes=10))
        assert len(calls) == 5
    assert calls[-1] == calls[1]
    assert all(call["chat_id"] > 0 for call in calls)
    state.engine.dispose()


async def test_summary_command_authorized_exact_syntax_and_no_trading_mutations(settings, ledger):
    import time

    s = settings.model_copy(
        update={
            "strategy_profile": "simple",
            "telegram_allowed_user_ids": (1,),
            "telegram_allowed_chat_ids": (2,),
        }
    )
    controls = Controls(s, ledger, AsyncMock(), AsyncMock(), AsyncMock())

    def update(command, uid=1):
        return {
            "update_id": time.time_ns(),
            "message": {
                "from": {"id": uid},
                "chat": {"id": 2, "type": "private"},
                "date": time.time(),
                "text": command,
            },
        }

    assert await controls.handle(update("/summary", 99)) is None
    assert "Unknown" in (await controls.handle(update("/summary extra")))[1]
    request = update("/summary")
    response = await controls.handle(request)
    assert response[0] == 2 and "Previous 24h" in response[1]
    assert await controls.handle(request) is None
    controls.resume.assert_not_awaited()
    controls.flatten.assert_not_awaited()
    controls.status.assert_not_awaited()
    assert ledger.get("state") == "RUNNING"
    assert ledger.get("manual_lock") is False
    assert ledger.get("risk_lock") is None


async def test_poll_sends_every_part_before_advancing_offset(settings, ledger):
    controls = AsyncMock()
    value = "🐸" * 3000 + "last lock detail"
    controls.handle.return_value = (2, value)
    telegram = Telegram(settings, ledger, controls)
    calls = []

    async def call(method, payload):
        if method == "getUpdates":
            if calls:
                raise asyncio.CancelledError()
            return [{"update_id": 42}]
        calls.append(payload)
        return {}

    telegram.call = call
    with pytest.raises(asyncio.CancelledError):
        await telegram.poll()
    assert "".join(payload["text"] for payload in calls) == value
    assert ledger.get("telegram_offset") == 43
    await telegram.client.aclose()


async def test_daily_worker_freezes_report_and_does_not_resend(settings, ledger, monkeypatch):
    from paperfrog.simple import daily_summary

    s = settings.model_copy(
        update={"telegram_bot_token": SecretStr("test-token"), "telegram_allowed_chat_ids": (1,)}
    )
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(daily_summary, "load_settings", lambda: s)
    monkeypatch.setattr(
        daily_summary.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    args = argparse.Namespace(timezone="UTC", hour=0, first_delivery="1970-01-01", preview=False)
    await run(args)
    files = list(s.reports_dir.rglob("*.json"))
    assert len(files) == 1
    saved = files[0].read_text()
    assert json.loads(saved)["report"]["end_ms"] - json.loads(saved)["report"]["start_ms"] == DAY_MS
    count = len(calls)
    assert count > 0
    monkeypatch.setattr(
        daily_summary, "build_report", lambda *args: pytest.fail("cached report rebuilt")
    )
    await run(args)
    assert len(calls) == count
    assert files[0].read_text() == saved


async def test_preview_does_not_send_or_save(settings, ledger, monkeypatch, capsys):
    from paperfrog.simple import daily_summary

    monkeypatch.setattr(daily_summary, "load_settings", lambda: settings)
    monkeypatch.setattr(
        daily_summary.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("preview made network call"),
    )
    args = argparse.Namespace(
        timezone="Europe/Sofia", hour=10, first_delivery="1970-01-01", preview=True
    )
    await run(args)
    assert "Previous 24h" in capsys.readouterr().out
    assert not settings.reports_dir.exists()
    assert not (directory(settings) / "daily-summary").exists()
