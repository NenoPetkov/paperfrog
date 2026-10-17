import asyncio
import json
from contextlib import suppress
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_adaptive import fake_client, permit, plan
from test_minute_entries import BOUNDARY, minute_settings, raw_minutes, slow_context
from test_simple_live import setup_engine

from paperfrog.domain.types import Order, SafeError
from paperfrog.settings import Settings
from paperfrog.simple.adaptive import Advisor, active_plan, request_limit
from paperfrog.simple.minute import MINUTE_MS, frame
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS


def settings_for_minute_ai(settings, **changes):
    return minute_settings(settings, simple_ai_enabled=True, simple_ai_review_minutes=1, **changes)


def market(boundary=BOUNDARY, timeframe="1m"):
    interval_ms = MINUTE_MS if timeframe == "1m" else 5 * MINUTE_MS
    return {
        "SOLUSDT": frame(
            raw_minutes(boundary, interval_ms=interval_ms),
            slow_context(boundary),
            boundary,
            timeframe,
        )
    }


@pytest.mark.parametrize("minutes", [1, 5, 15])
def test_ai_review_interval_loads_from_dotenv(settings, tmp_path, minutes):
    env = tmp_path / "review.env"
    env.write_text(f"SIMPLE_AI_REVIEW_MINUTES={minutes}\n")
    values = {**settings.public(), "simple_entry_timeframe": "1m"}
    values.pop("simple_ai_review_minutes")
    loaded = Settings(_env_file=env, **values)
    assert loaded.simple_ai_review_minutes == minutes
    assert loaded.simple_ai_review_bar_ms == minutes * MINUTE_MS


@pytest.mark.parametrize("minutes", [0, 2, 3, 14, 16])
def test_other_ai_review_intervals_are_rejected(settings, minutes):
    with pytest.raises(ValidationError):
        minute_settings(settings, simple_ai_review_minutes=minutes)


def test_minute_ai_is_explicit_and_requires_minute_entry_data(settings):
    assert settings.simple_ai_review_minutes == 15
    assert request_limit(settings) == 96
    s = settings_for_minute_ai(settings)
    assert s.trading_mode == "paper" and s.simple_ai_review_bar_ms == MINUTE_MS
    assert request_limit(s) == 1440
    assert s.simple_parameters()["simple_ai_review_minutes"] == 1
    with pytest.raises(ValidationError):
        settings_for_minute_ai(settings, simple_entry_timeframe="15m")


def test_minute_plan_is_valid_only_until_next_minute(settings, store):
    s, p = settings_for_minute_ai(settings), plan(BOUNDARY)
    assert BOUNDARY % BAR_MS != 0
    permit(s, store, p)
    assert active_plan(s, store, BOUNDARY, BOUNDARY + MINUTE_MS - 1) == p
    assert active_plan(s, store, BOUNDARY, BOUNDARY - 1) is None
    assert active_plan(s, store, BOUNDARY, BOUNDARY + MINUTE_MS) is None
    assert active_plan(s, store, BOUNDARY + MINUTE_MS, BOUNDARY + MINUTE_MS) is None
    with pytest.raises(ValidationError):
        plan(BOUNDARY + 1)


async def test_reviews_run_on_successive_minutes_and_do_not_repeat_after_restart(settings, store):
    s, clock = settings_for_minute_ai(settings), {"now": BOUNDARY + 1000}
    client = fake_client(plan(BOUNDARY))
    advisor = Advisor(s, store, client, lambda: clock["now"])
    await advisor.review(market(), ["SOLUSDT"], BOUNDARY)
    await Advisor(s, store, client, lambda: clock["now"]).review(market(), ["SOLUSDT"], BOUNDARY)
    assert client.responses.parse.await_count == 1
    first = store.get("simple_ai_active")
    assert first["permission_expires_ms"] == BOUNDARY + MINUTE_MS
    assert first["review_minutes"] == 1
    sent = json.loads(client.responses.parse.call_args.kwargs["input"][1]["content"])
    assert sent["entry_policy"]["review_minutes"] == 1
    assert sent["entry_policy"]["max_requests_per_utc_day"] == 1440
    assert sent["entry_policy"]["forecast_minutes"] == 15
    later = BOUNDARY + MINUTE_MS
    clock["now"] = later + 1000
    client.responses.parse.return_value.output_parsed = plan(later)
    await advisor.review(market(later), ["SOLUSDT"], later)
    assert client.responses.parse.await_count == 2
    assert store.get("simple_ai_active")["plan"]["asof_ms"] == later
    assert store.get("simple_ai_budget")["count"] == 2
    assert store.get("simple_ai_history")[0]["forecast_results"] is None


async def test_one_minute_daily_cap_preserves_usage_and_is_durable(settings, store):
    s = settings_for_minute_ai(settings)
    day = BOUNDARY // 86_400_000
    store.put("simple_ai_budget", {"day": day, "count": 1439})
    client = fake_client(plan(BOUNDARY))
    await Advisor(s, store, client, lambda: BOUNDARY + 1000).review(market(), ["SOLUSDT"], BOUNDARY)
    assert store.get("simple_ai_budget") == {"day": day, "count": 1440}
    later = BOUNDARY + MINUTE_MS
    await Advisor(s, store, client, lambda: later + 1000).review(market(later), ["SOLUSDT"], later)
    assert client.responses.parse.await_count == 1
    assert store.get("simple_ai_active") is None
    assert store.get("simple_ai_status") == "daily_request_cap"
    assert store.get("simple_ai_budget")["count"] == 1440


async def test_switching_to_minute_reviews_does_not_erase_existing_daily_spend(settings, store):
    s = settings_for_minute_ai(settings)
    store.put("simple_ai_budget", {"day": BOUNDARY // 86_400_000, "count": 96})
    client = fake_client(plan(BOUNDARY))
    await Advisor(s, store, client, lambda: BOUNDARY + 1000).review(market(), ["SOLUSDT"], BOUNDARY)
    assert client.responses.parse.await_count == 1
    assert store.get("simple_ai_budget")["count"] == 97


@pytest.mark.parametrize("failure", ["late", "refused", "future", "failed"])
async def test_failed_or_late_minute_review_never_reuses_previous_permission(
    settings, store, failure
):
    s, clock = settings_for_minute_ai(settings), {"now": BOUNDARY + 1000}
    permit(s, store, plan(BOUNDARY - MINUTE_MS))
    client = fake_client(plan(BOUNDARY))

    async def response(**kwargs):
        if failure == "late":
            clock["now"] = BOUNDARY + MINUTE_MS
        if failure == "failed":
            raise TimeoutError("not retained in audit")
        parsed = (
            None
            if failure == "refused"
            else plan(BOUNDARY + MINUTE_MS if failure == "future" else BOUNDARY)
        )
        return type("Response", (), {"output_parsed": parsed})()

    client.responses.parse.side_effect = response
    advisor = Advisor(s, store, client, lambda: clock["now"])
    await advisor.review(market(), ["SOLUSDT"], BOUNDARY)
    await advisor.review(market(), ["SOLUSDT"], BOUNDARY)
    assert client.responses.parse.await_count == 1
    assert store.get("simple_ai_active") is None
    assert store.get("simple_ai_budget")["count"] == 1
    assert "not retained in audit" not in str(store.rows("audit"))


async def test_fifteen_minute_mode_refuses_a_one_minute_boundary(settings, store):
    s = minute_settings(settings, simple_ai_enabled=True)
    client = fake_client(plan(BOUNDARY))
    await Advisor(s, store, client, lambda: BOUNDARY + 1000).review(market(), ["SOLUSDT"], BOUNDARY)
    client.responses.parse.assert_not_called()
    assert store.get("simple_ai_active") is None


@pytest.mark.parametrize("minutes", [1, 5])
async def test_service_reviews_each_minute_without_holding_monitoring_mutex(settings, minutes):
    interval_ms = minutes * MINUTE_MS
    boundary = BOUNDARY // interval_ms * interval_ms
    timeframe = f"{minutes}m"
    s = minute_settings(
        settings,
        simple_entry_timeframe=timeframe,
        simple_ai_enabled=True,
        simple_ai_review_minutes=minutes,
        openai_api_key="mock-local-api-key",
    )
    service = SimpleService(s)
    clock, calls = {"now": boundary + 1000}, []
    started, released, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    service.now_ms = lambda: clock["now"]
    service.minute_frames, service.selected = market(boundary, timeframe), ["SOLUSDT"]

    async def delayed_review(frames, selected, boundary):
        calls.append(boundary)
        if len(calls) == 1:
            started.set()
            await released.wait()
        else:
            second.set()

    service.advisor.review = delayed_review
    task = asyncio.create_task(service.minute_ai_reviews())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.wait_for(service.mutex.acquire(), timeout=1)
        service.mutex.release()
        later = boundary + interval_ms
        service.minute_frames = market(later, timeframe)
        clock["now"] = later + 1000
        released.set()
        await asyncio.wait_for(second.wait(), timeout=3)
        assert calls[:2] == [boundary, later]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await service.advisor.close()
        await service.exchange.close()
        service.store.engine.dispose()


@pytest.mark.parametrize("minutes", [1, 5])
async def test_live_broker_rejects_prior_minute_ai_permission_at_dispatch(
    settings, store, filters, minutes
):
    engine, simulation, adapter, _, now = await setup_engine(
        settings,
        store,
        filters,
        simple_entry_timeframe=f"{minutes}m",
        simple_ai_review_minutes=minutes,
        simple_ai_enabled=True,
    )
    interval_ms = minutes * MINUTE_MS
    boundary = now // interval_ms * interval_ms
    p = plan(boundary)
    permit(engine.s, store, p)
    order = Order(
        client_id="pf-minute-ai-expired",
        symbol="SOLUSDT",
        side=1,
        quantity=Decimal("0.01"),
        limit_price=Decimal(100),
    )
    store.put(
        "simple_ai_order:" + order.client_id,
        {
            "digest": p.digest,
            "asof_ms": boundary,
            "order": order.model_dump(mode="json"),
        },
    )
    engine.broker.now_ms = lambda: boundary + interval_ms
    try:
        with pytest.raises(SafeError, match="AI order permission"):
            await engine.broker.submit(order)
        assert not simulation.quantity
        assert not any(
            method == "POST" and path.endswith("/order") for method, path in simulation.calls
        )
    finally:
        await adapter.close()
