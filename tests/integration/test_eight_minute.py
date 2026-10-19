import copy
import json
from unittest.mock import AsyncMock

import numpy as np
import pytest
from pandas.testing import assert_frame_equal
from pydantic import ValidationError
from test_adaptive import fake_client, plan
from test_minute_entries import raw_minutes, slow_context
from test_monitor_timeframe import opened
from test_simple import engine_fixture, quote

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings
from paperfrog.simple.adaptive import Advisor, active_plan, request_limit, review_prompt
from paperfrog.simple.engine import Engine
from paperfrog.simple.minute import HISTORY_BARS, MINUTE_MS, direction, frame, veto
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS, execution_rules

INTERVAL = 8 * MINUTE_MS
BOUNDARY = 200 * BAR_MS + INTERVAL


def eight_settings(settings, **changes):
    return Settings(
        _env_file=None,
        **{
            **settings.public(),
            "simple_entry_timeframe": "8m",
            "simple_ai_review_minutes": 8,
            **changes,
        },
    )


def source(boundary=BOUNDARY, side=1):
    bars = HISTORY_BARS * 8
    closes = 100 * np.exp(side * (np.arange(bars) - bars + 1) * 0.0001)
    opens = np.r_[closes[0] / np.exp(side * 0.0001), closes[:-1]]
    return [
        [
            boundary - (bars - i) * MINUTE_MS,
            str(op),
            str(max(op, close) * 1.0003),
            str(min(op, close) * 0.9997),
            str(close),
            "100",
            boundary - (bars - i - 1) * MINUTE_MS - 1,
            "1000000",
            100,
            "70",
            "700000" if side > 0 else "300000",
            "0",
        ]
        for i, (op, close) in enumerate(zip(opens, closes, strict=True))
    ]


def market(boundary=BOUNDARY, side=1):
    return frame(source(boundary, side), slow_context(boundary, side), boundary, "8m")


def test_eight_minute_settings_are_paired_and_preserve_paper_default(settings, tmp_path):
    env = tmp_path / "eight.env"
    env.write_text("SIMPLE_ENTRY_TIMEFRAME=8m\nSIMPLE_AI_REVIEW_MINUTES=8\n")
    values = settings.public()
    values.pop("simple_entry_timeframe")
    values.pop("simple_ai_review_minutes")
    s = Settings(_env_file=env, **values)
    assert s.simple_entry_bar_ms == s.simple_ai_review_bar_ms == INTERVAL
    assert s.trading_mode == "paper" and not s.live_trading_enabled
    assert request_limit(s) == 180
    for changes in [
        {"simple_entry_timeframe": "5m"},
        {"simple_ai_review_minutes": 5},
        {"simple_ai_review_minutes": 15},
        {"simple_entry_mode": "reverse"},
    ]:
        with pytest.raises(ValidationError):
            eight_settings(settings, **changes)


@pytest.mark.parametrize("side", [1, -1])
def test_aggregation_ohlc_volume_flow_exact_hour_and_signal_expiry(settings, side):
    raw = source(side=side)
    data = market(side=side)
    assert len(data) == 120
    row = data.iloc[-1].to_dict()
    assert row["timestamp_ms"] == BOUNDARY
    assert row["open_ms"] == BOUNDARY - INTERVAL
    assert row["open"] == float(raw[-8][1])
    assert row["high"] == max(float(r[2]) for r in raw[-8:])
    assert row["low"] == min(float(r[3]) for r in raw[-8:])
    assert row["close"] == float(raw[-1][4])
    assert row["quote_volume"] == 8_000_000
    assert row["taker_buy_quote"] == (5_600_000 if side > 0 else 2_400_000)
    assert row["return_1h"] == pytest.approx(np.exp(side * 60 * 0.0001) - 1)
    assert row["return_1h"] != pytest.approx(np.exp(side * 56 * 0.0001) - 1)
    for minutes in [8, 24, 40]:
        assert row[f"return_{minutes}m"] == pytest.approx(np.exp(side * minutes * 0.0001) - 1)
    rules = execution_rules(settings)
    assert direction(row, rules)[0] == side
    assert veto(row, side, BOUNDARY + INTERVAL - 1, 100, rules) is None
    for now in [BOUNDARY - 1, BOUNDARY + INTERVAL]:
        assert veto(row, side, now, 100, rules) is not None


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "unclosed",
        "misaligned",
        "negative_volume",
        "bad_ohlc",
        "excess_flow",
        "nonfinite",
        "stale_context",
        "future_context",
    ],
)
def test_bad_source_minutes_cannot_hide_inside_an_aggregate(damage):
    raw, context, boundary = source(), slow_context(BOUNDARY), BOUNDARY
    if damage == "missing":
        raw.pop(100)
    elif damage == "duplicate":
        raw[100] = copy.deepcopy(raw[99])
    elif damage == "unclosed":
        raw[-1][6] += MINUTE_MS
    elif damage == "misaligned":
        boundary += MINUTE_MS
    elif damage == "negative_volume":
        raw[100][7] = "-1"
    elif damage == "bad_ohlc":
        raw[100][2] = "1"
    elif damage == "excess_flow":
        raw[100][10] = "2000000"
    elif damage == "nonfinite":
        raw[100][4] = "nan"
    elif damage == "stale_context":
        context["timestamp_ms"] -= BAR_MS
    else:
        context["timestamp_ms"] += BAR_MS
    with pytest.raises(SafeError):
        frame(raw, context, boundary, "8m")


def test_future_minutes_are_excluded_from_bars_hour_returns_and_forecast_prices():
    raw = source()
    future = source(BOUNDARY + MINUTE_MS)[-1]
    future[4] = "999999"
    assert_frame_equal(market(), frame(raw + [future], slow_context(BOUNDARY), BOUNDARY, "8m"))
    assert BOUNDARY + MINUTE_MS not in market().attrs["minute_closes"]


async def test_eight_minute_ai_permission_budget_and_exact_fifteen_minute_forecast(settings, store):
    s = eight_settings(settings, simple_ai_enabled=True)
    client = fake_client(plan(BOUNDARY))
    advisor = Advisor(s, store, client, lambda: BOUNDARY + 1000)
    await advisor.review({"SOLUSDT": market()}, ["SOLUSDT"], BOUNDARY)
    await Advisor(s, store, client, lambda: BOUNDARY + 1000).review(
        {"SOLUSDT": market()}, ["SOLUSDT"], BOUNDARY
    )
    client.responses.parse.assert_awaited_once()
    assert active_plan(s, store, BOUNDARY, BOUNDARY + INTERVAL - 1) is not None
    assert active_plan(s, store, BOUNDARY, BOUNDARY + INTERVAL) is None
    sent = json.loads(client.responses.parse.call_args.kwargs["input"][1]["content"])
    assert sent["entry_policy"]["max_requests_per_utc_day"] == 180
    assert sent["entry_policy"]["review_minutes"] == 8
    assert "8/24/40-minute" in review_prompt(s)
    assert "exact minute closes 60 minutes apart" in review_prompt(s)
    later = BOUNDARY + 2 * INTERVAL
    raw = source(later)
    target = BOUNDARY + BAR_MS
    target_bar = next(r for r in raw if r[6] + 1 == target)
    target_bar[4], target_bar[2] = "101", "102"
    scored = frame(raw, slow_context(later), later, "8m")
    advisor.snapshot({"SOLUSDT": scored}, ["SOLUSDT"], later)
    assert store.get("simple_ai_history")[0]["forecast_results"][0]["actual"] == pytest.approx(0.01)
    store.put("simple_ai_budget", {"day": later // 86_400_000, "count": 180})
    await Advisor(s, store, client, lambda: later + 1000).review(
        {"SOLUSDT": scored}, ["SOLUSDT"], later
    )
    client.responses.parse.assert_awaited_once()
    assert store.get("simple_ai_active") is None


async def test_service_uses_one_minute_source_and_archives_only_new_source_rows(settings):
    service = SimpleService(eight_settings(settings))
    service.context_rows = {s: slow_context(BOUNDARY) for s in ["BTCUSDT", "SOLUSDT"]}
    service.store.put("simple_pool", ["SOLUSDT"])
    service.now_ms = lambda: BOUNDARY + 1000
    service.exchange.request = AsyncMock(return_value=source())
    try:
        await service.refresh_minutes(BOUNDARY)
        params = service.exchange.request.call_args.args[2]
        assert params == {
            "symbol": "SOLUSDT",
            "interval": "1m",
            "limit": 960,
            "startTime": BOUNDARY - 120 * INTERVAL,
            "endTime": BOUNDARY - 1,
        }
        assert service.rows["SOLUSDT"]["entry_timeframe"] == "8m"
        assert len(service.lake.read("perp_8m", "SOLUSDT")) == 120
        assert len(service.lake.read("perp_1m", "SOLUSDT")) == 960
        later = BOUNDARY + INTERVAL
        service.context_rows = {s: slow_context(later) for s in ["BTCUSDT", "SOLUSDT"]}
        service.now_ms = lambda: later + 1000
        service.exchange.request.return_value = source(later)
        await service.refresh_minutes(later)
        assert len(service.lake.read("perp_1m", "SOLUSDT")) == 968
        assert len(service.lake.read("perp_8m", "SOLUSDT")) == 121
    finally:
        await service.exchange.close()
        service.store.engine.dispose()


@pytest.mark.parametrize("side", [1, -1])
async def test_existing_five_minute_positions_keep_their_original_monitoring_after_switch(
    settings, store, filters, side
):
    original, _ = await opened(settings, store, filters, "5m", side)
    s = eight_settings(settings)
    engine = Engine(s, store, filters)
    await engine.restore()
    service = SimpleService(s)
    service.store = store
    service.engine = engine
    service.context_rows = {"SOLUSDT": slow_context(BOUNDARY, side)}
    service.now_ms = lambda: BOUNDARY + 1000
    held_boundary = BOUNDARY // (5 * MINUTE_MS) * 5 * MINUTE_MS
    service.exchange.request = AsyncMock(
        return_value=raw_minutes(held_boundary, side, 5 * MINUTE_MS)
    )
    try:
        await service.refresh_held_minutes()
        assert service.held_rows["SOLUSDT"]["entry_timeframe"] == "5m"
        assert service.exchange.request.call_args.args[2]["interval"] == "5m"
        await engine.quote(quote(now_ms=BOUNDARY + 1000), BOUNDARY + 1000)
        await engine.monitor(BOUNDARY + 1000, service.held_rows)
        assert "SOLUSDT" in engine.positions()
        assert engine.positions()["SOLUSDT"]["entry_timeframe"] == "5m"
    finally:
        await service.exchange.close()


async def test_partial_original_timeframe_warmup_keeps_all_positions_during_startup(
    settings, store, filters, monkeypatch
):
    import time

    from test_minute_entries import minute_row, minute_settings

    import paperfrog.simple.service as service_module

    old = minute_settings(settings, simple_entry_timeframe="5m")
    engine = await engine_fixture(old, store, filters)
    old_boundary = 200 * BAR_MS
    obs = minute_row(old_boundary, timeframe="5m")
    store.put("simple_selected", ["SOLUSDT", "ETHUSDT"])
    for symbol in ["SOLUSDT", "ETHUSDT"]:
        store.put("simple_minute:" + symbol, obs)
        await engine.quote(quote(symbol=symbol, now_ms=old_boundary + 1000), old_boundary + 1000)
    for symbol in ["SOLUSDT", "ETHUSDT"]:
        await engine.enter(symbol, obs, old_boundary + 1000)
    assert len(engine.positions()) == 2
    service = SimpleService(eight_settings(settings))
    service.store.engine.dispose()
    service.store, service.engine = store, engine
    service.auto_start, service.crisis, service.scan_complete = False, False, True
    service.now_ms = lambda: BOUNDARY + 1000
    service.initial_scan_deadline = time.monotonic() + 60
    # Eight-minute entries have loaded; only one original-timeframe position has loaded.
    service.rows = {symbol: market().iloc[-1].to_dict() for symbol in engine.positions()}
    held_boundary = BOUNDARY // (5 * MINUTE_MS) * 5 * MINUTE_MS
    service.held_rows = {"SOLUSDT": minute_row(held_boundary, timeframe="5m")}
    for symbol in engine.positions():
        store.put("simple_funding_checked:" + symbol, BOUNDARY + 1000)
    service.fetch_quote = AsyncMock(
        side_effect=lambda symbol: quote(symbol=symbol, now_ms=BOUNDARY + 1000)
    )

    async def sleep(_):
        service.stop.set()

    monkeypatch.setattr(service_module.asyncio, "sleep", sleep)
    try:
        await service.monitor()
        assert set(engine.positions()) == {"SOLUSDT", "ETHUSDT"}
    finally:
        await service.exchange.close()


@pytest.mark.parametrize("side", [1, -1])
async def test_live_eight_minute_entries_have_hosted_stops_and_reduce_only_exits(
    settings, store, filters, side
):
    from test_simple_live import setup_engine

    engine, sim, adapter, _, now = await setup_engine(
        settings, store, filters, simple_entry_timeframe="8m", simple_ai_review_minutes=8
    )
    boundary = now // INTERVAL * INTERVAL
    obs = market(boundary, side).iloc[-1].to_dict()
    store.put("simple_minute:SOLUSDT", obs)
    try:
        await engine.enter("SOLUSDT", obs, now)
        assert engine.positions()["SOLUSDT"]["entry_timeframe"] == "8m"
        assert sim.stops and sim.quantity != 0
        store.put("simple_minute:SOLUSDT", None)
        await engine.close("SOLUSDT", engine.broker.now_ms(), "operator requested flatten")
        assert sim.quantity == 0 and not engine.positions()
    finally:
        await adapter.close()


async def test_eight_minute_permission_is_rechecked_immediately_before_live_submission(
    settings, store, filters
):
    from test_simple_live import setup_engine

    engine, sim, adapter, _, now = await setup_engine(
        settings, store, filters, simple_entry_timeframe="8m", simple_ai_review_minutes=8
    )
    obs = market(now // INTERVAL * INTERVAL).iloc[-1].to_dict()
    store.put("simple_minute:SOLUSDT", obs)
    request = adapter.request
    calls = 0

    async def revoke_on_dispatch(method, path, *args, **kwargs):
        nonlocal calls
        if path.endswith("/bookTicker"):
            calls += 1
            if calls == 2:
                store.put("simple_minute:SOLUSDT", None)
        return await request(method, path, *args, **kwargs)

    adapter.request = revoke_on_dispatch
    try:
        with pytest.raises(SafeError, match="slope changed"):
            await engine.enter("SOLUSDT", obs, now)
        assert calls == 2 and sim.quantity == 0
        assert not any(method == "POST" and path.endswith("/order") for method, path in sim.calls)
    finally:
        await adapter.close()


async def test_late_start_waits_for_next_eight_minute_bar_without_using_newer_context(settings):
    service = SimpleService(eight_settings(settings))
    next_boundary = BOUNDARY + INTERVAL
    service.context_rows = {
        symbol: slow_context(next_boundary) for symbol in ["BTCUSDT", "SOLUSDT"]
    }
    service.store.put("simple_pool", ["SOLUSDT"])
    context_at = service.context_rows["BTCUSDT"]["timestamp_ms"]
    assert BOUNDARY < context_at < next_boundary
    service.now_ms = lambda: context_at + 1000
    service.exchange.request = AsyncMock(return_value=source(next_boundary))
    try:
        await service.refresh_minutes(BOUNDARY)
        service.exchange.request.assert_not_awaited()
        assert service.rows == {} and service.selected == []
        assert service.store.get("simple_selected", []) == []
        service.now_ms = lambda: next_boundary + 1000
        await service.refresh_minutes(next_boundary)
        assert service.rows["SOLUSDT"]["timestamp_ms"] == next_boundary
        assert service.rows["SOLUSDT"]["entry_timeframe"] == "8m"
    finally:
        await service.exchange.close()
        service.store.engine.dispose()
