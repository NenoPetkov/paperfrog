import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from paperfrog.settings import Settings
from paperfrog.simple.authorization import directory
from paperfrog.simple.daily_review import (
    DailyReview,
    Proposal,
    Reviewer,
    bounds,
    build_snapshot,
    deployed_fixes,
    due_day,
    render_messages,
)
from paperfrog.storage.database import Store
from paperfrog.storage.lock import InstanceLock

DAY = date(2026, 10, 16)
ZONE = "Europe/Sofia"


def review():
    return DailyReview.model_validate(
        {
            "summary": "Small sample; investigate costs before changing entry rules.",
            "findings": [
                {
                    "title": "Fees need review",
                    "evidence_refs": ["E001"],
                    "interpretation": "This is descriptive, not a causal result.",
                }
            ],
            "proposals": [
                {
                    "title": "Measure execution cost coverage",
                    "evidence_refs": ["E001"],
                    "interpretation": "Missing execution diagnostics can hide costs.",
                    "area": "observability",
                    "confidence": "low",
                    "priority": "P2",
                    "risk_effect": "unchanged",
                    "change_spec": "Add per-side counts of missing execution reference quotes to the daily report.",
                    "acceptance_criteria": "Missing quotes remain unknown and are counted separately.",
                    "validation_plan": [
                        "Test missing entry and exit quotes independently.",
                        "Verify known costs and net PnL remain unchanged.",
                    ],
                    "rollback": "Revert the reporting-only patch.",
                }
            ],
            "limitations": ["Insufficient sample for threshold optimization."],
        }
    )


@pytest.fixture
def trading(settings):
    store = Store(directory(settings) / "paperfrog.sqlite")
    start, end = bounds(DAY, ZONE)
    trade = {
        "timestamp_ms": start,
        "entry_ms": start - 600_000,
        "pnl": -2.34,
        "gross_pnl": -2.24,
        "fees": 0.1,
        "funding": 0,
        "entry_price": 100,
        "quantity": 10,
        "net_return": -0.00234,
        "symbol": "SOLUSDT",
        "direction": 1,
        "holding_seconds": 600,
        "reason": "trend reversal",
        "journal_version": 1,
        "entry_context": {
            "entry_timeframe": "5m",
            "currency": "USDT",
            "indicators": {"volume_ratio": 0.7},
        },
    }
    store.put("simple_closed", [trade])
    store.put("state", "RUNNING")
    store.put("simple_positions", {"PRIVATE_POSITION": {"quantity": "private"}})
    yield store, trade, start, end
    store.engine.dispose()


@pytest.fixture
def reviewer(settings, tmp_path):
    s = Settings(
        _env_file=None,
        **{
            **settings.public(),
            "telegram_bot_token": "private-token",
            "telegram_allowed_chat_ids": [123],
        },
    )
    state = Store(tmp_path / "review-state.sqlite")
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(return_value=SimpleNamespace(output_parsed=review()))
        )
    )
    worker = Reviewer(s, Path(__file__).resolve().parents[2], state, client)
    yield worker
    state.engine.dispose()


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_calendar_day_handles_dst(day, hours):
    start, end = bounds(day, ZONE)
    assert end - start == hours * 3600_000


def test_schedule_is_local_ten_am_and_catches_up_without_early_send():
    first = date(2026, 10, 18)
    assert due_day(datetime(2026, 10, 17, 18, tzinfo=UTC), ZONE, 10, first) is None
    assert due_day(datetime(2026, 10, 18, 6, 59, tzinfo=UTC), ZONE, 10, first) is None
    assert due_day(datetime(2026, 10, 18, 7, tzinfo=UTC), ZONE, 10, first) == date(2026, 10, 17)
    assert due_day(datetime(2026, 10, 18, 12, tzinfo=UTC), ZONE, 10, first) == date(2026, 10, 17)
    assert due_day(datetime(2026, 10, 25, 8, tzinfo=UTC), ZONE, 10, first) == date(2026, 10, 24)


def test_snapshot_uses_local_exit_day_and_does_not_mutate_trading_ledger(settings, trading):
    store, trade, start, end = trading
    store.put(
        "simple_closed",
        [trade, {**trade, "timestamp_ms": start - 1}, {**trade, "timestamp_ms": end}],
    )
    before = store.get("simple_closed")
    public, local = build_snapshot(settings, DAY, ZONE)
    assert local["trades"] == 1
    assert public["evidence"][0]["trades"] == 1
    weekly = next(
        e
        for e in public["evidence"]
        if e.get("period") == "previous_7_days_excluding_day" and e.get("group") == "overall"
    )
    assert weekly["trades"] == 1
    serialized = json.dumps(public)
    assert all(
        private not in serialized
        for private in ["PRIVATE_POSITION", '"quantity"', '"entry_price"', '"pnl"', '"order_id"']
    )
    assert store.get("simple_closed") == before
    assert store.get("state") == "RUNNING"


async def test_one_ai_call_survives_restart_and_proposals_are_actionable(reviewer, trading):
    record = await reviewer.generate(DAY, ZONE)
    again = Reviewer(reviewer.s, reviewer.root, reviewer.state, reviewer.client)
    assert await again.generate(DAY, ZONE) == record
    reviewer.client.responses.parse.assert_awaited_once()
    request = reviewer.client.responses.parse.call_args.kwargs
    assert request["store"] is False and request["max_output_tokens"] == 4000
    assert "tools" not in request and request["prompt_cache_options"] == {"mode": "explicit"}
    p = record["proposals"][0]
    assert p["id"] == "DR-20261016-01" and p["status"] == "proposed_not_applied"
    assert p["source_sha256"] and p["evidence"] and p["validation_plan"] and p["rollback"]
    saved = reviewer.folder / "proposals/DR-20261016-01.json"
    assert json.loads(saved.read_text()) == p
    assert saved.stat().st_mode & 0o077 == 0
    saved.unlink()
    await again.generate(DAY, ZONE)
    assert saved.exists()


@pytest.mark.parametrize("damage", ["error", "refusal", "evidence"])
async def test_bad_review_falls_back_without_retry_or_secret_leak(reviewer, trading, damage):
    if damage == "error":
        reviewer.client.responses.parse.side_effect = ValueError("private-token private API body")
    elif damage == "refusal":
        reviewer.client.responses.parse.return_value.output_parsed = None
    else:
        invalid = review().model_dump()
        invalid["proposals"][0]["evidence_refs"] = ["E999"]
        reviewer.client.responses.parse.return_value.output_parsed = DailyReview.model_validate(
            invalid
        )
    result = await reviewer.generate(DAY, ZONE)
    assert result["review"] is None and result["proposals"] == []
    await reviewer.generate(DAY, ZONE)
    reviewer.client.responses.parse.assert_awaited_once()
    assert "private-token" not in json.dumps(result) + str(reviewer.state.rows("audit"))


async def test_crash_after_reservation_cannot_replay_billable_call(reviewer, trading):
    reviewer.client.responses.parse.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await reviewer.generate(DAY, ZONE)
    record = await reviewer.generate(DAY, ZONE)
    reviewer.client.responses.parse.assert_awaited_once()
    assert "interrupted" in record["fallback_reason"]


async def test_zero_trades_has_no_ai_cost(reviewer, trading):
    trading[0].put("simple_closed", [])
    record = await reviewer.generate(DAY, ZONE)
    reviewer.client.responses.parse.assert_not_called()
    assert record["statistics"]["trades"] == 0
    assert "no AI request" in render_messages(record)[0]


async def test_telegram_deduplicates_successful_parts_and_retries_failed_delivery(
    reviewer, trading
):
    record = await reviewer.generate(DAY, ZONE)
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 2:
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    now = datetime(2026, 10, 17, 7, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await reviewer.deliver(record, client, now)
        assert len(calls) == 2
        await reviewer.deliver(record, client, now + timedelta(seconds=60))
        assert len(calls) == 2
        await reviewer.deliver(record, client, now + timedelta(minutes=5))
        assert len(calls) == 3
        await reviewer.deliver(record, client, now + timedelta(minutes=10))
        assert len(calls) == 3
    assert calls[0]["text"] != calls[2]["text"]
    assert calls[1]["text"] == calls[2]["text"]
    assert all(p["chat_id"] == 123 and len(p["text"]) <= 4000 for p in calls)
    assert "Apply DR-20261016-01" in calls[2]["text"]
    reviewer.client.responses.parse.assert_awaited_once()


def test_schema_cannot_authorize_risk_increase_or_executable_code():
    data = review().proposals[0].model_dump()
    for bad in ({"risk_effect": "increased"}, {"area": "live_arm"}, {"execute": "shell"}):
        with pytest.raises(ValidationError):
            Proposal.model_validate({**data, **bad})


def test_separate_report_lock_prevents_concurrent_workers(tmp_path):
    with InstanceLock(tmp_path):
        with pytest.raises(Exception, match="another service"):
            with InstanceLock(tmp_path):
                pytest.fail("concurrent review lock accepted")


async def test_daily_delivery_respects_disabled_groups(reviewer, trading):
    reviewer.s = reviewer.s.model_copy(update={"telegram_allowed_chat_ids": (-123,)})
    record = await reviewer.generate(DAY, ZONE)
    client = SimpleNamespace(post=AsyncMock())
    await reviewer.deliver(record, client, datetime(2026, 10, 17, 7, tzinfo=UTC))
    client.post.assert_not_awaited()


def test_deployed_fix_requires_current_source_hash(settings):
    from paperfrog.simple import daily_review

    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.reports_dir / "test-deployment.json"
    engine = Path(daily_review.__file__).with_name("engine.py")
    record = {
        "completed_at": "2026-10-17T18:43:26Z",
        "deployment_incident": {"fix": "Match exit data to original entry timeframe."},
        "source_sha256": {
            "src/paperfrog/simple/engine.py": hashlib.sha256(engine.read_bytes()).hexdigest()
        },
    }
    path.write_text(json.dumps(record))
    assert deployed_fixes(settings) == [
        {
            "completed_at": record["completed_at"],
            "description": record["deployment_incident"]["fix"],
        }
    ]
    record["source_sha256"]["src/paperfrog/simple/engine.py"] = "stale"
    path.write_text(json.dumps(record))
    assert deployed_fixes(settings) == []
