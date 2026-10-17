import asyncio
import copy
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import numpy as np
import pytest
from pandas.testing import assert_frame_equal
from pydantic import ValidationError
from test_adaptive import fake_client, plan
from test_bitcoin_reference import context as btc_context
from test_minute_entries import minute_row, minute_settings, raw_minutes, slow_context
from test_simple import engine_fixture, quote

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.settings import Settings
from paperfrog.simple.adaptive import Advisor, active_plan, request_limit, review_prompt
from paperfrog.simple.backtest import replay
from paperfrog.simple.bitcoin import entry_gate
from paperfrog.simple.minute import MINUTE_MS, direction, frame, require_order, veto
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS, execution_rules

INTERVAL = 5 * MINUTE_MS
BOUNDARY = 200 * BAR_MS + INTERVAL


def five_settings(settings, **changes):
    return minute_settings(settings, simple_entry_timeframe="5m", **changes)


def market(boundary=BOUNDARY, side=1):
    return frame(
        raw_minutes(boundary, side, INTERVAL), slow_context(boundary, side), boundary, "5m"
    )


def test_five_minute_dotenv(settings, tmp_path):
    env = tmp_path / "five.env"
    env.write_text("SIMPLE_ENTRY_TIMEFRAME=5m\nSIMPLE_AI_REVIEW_MINUTES=5\n")
    values = settings.public()
    for key in ("simple_entry_timeframe", "simple_ai_review_minutes"):
        values.pop(key)
    s = Settings(_env_file=env, **values)
    assert s.simple_entry_bar_ms == s.simple_ai_review_bar_ms == INTERVAL
    assert s.trading_mode == "paper" and not s.live_trading_enabled
    assert request_limit(s) == 288


@pytest.mark.parametrize(
    "changes",
    [
        {"simple_ai_review_minutes": 1},
        {"simple_entry_mode": "reverse"},
        {"strategy_profile": "legacy"},
    ],
)
def test_five_minute_rejects_incompatible_modes(settings, changes):
    with pytest.raises(ValidationError):
        five_settings(settings, **changes)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, simple_entry_timeframe="15m", simple_ai_review_minutes=5)


@pytest.mark.parametrize("side", [1, -1])
def test_five_minute_features_and_expiry(settings, side):
    obs = market(side=side).iloc[-1].to_dict()
    rules = execution_rules(settings)
    assert direction(obs, rules)[0] == side
    for minutes, bars in [(5, 1), (15, 3), (25, 5)]:
        assert obs[f"return_{minutes}m"] == pytest.approx(np.exp(side * bars * 0.0008) - 1)
    assert obs["return_1h"] == pytest.approx(np.exp(side * 12 * 0.0008) - 1)
    assert obs["atr"] == 0.8 and obs["atr_fraction"] == 0.008
    assert veto(obs, side, BOUNDARY + INTERVAL - 1, 100, rules) is None
    for now in (BOUNDARY - 1, BOUNDARY + INTERVAL):
        assert veto(obs, side, now, 100, rules) is not None
    assert direction({**obs, "return_5m": -side * 0.01}, rules)[0] == 0
    assert veto(obs, side, BOUNDARY + 1000, 100 - side, rules) is not None


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "unclosed", "misaligned", "context", "one_minute", "ohlc"]
)
def test_five_minute_history_fails_closed(damage):
    raw, context = raw_minutes(BOUNDARY, interval_ms=INTERVAL), slow_context(BOUNDARY)
    boundary = BOUNDARY
    if damage == "missing":
        raw.pop(40)
    elif damage == "duplicate":
        raw[40] = copy.deepcopy(raw[39])
    elif damage == "unclosed":
        raw[-1][6] += INTERVAL
    elif damage == "misaligned":
        boundary += MINUTE_MS
        raw = raw_minutes(boundary, interval_ms=INTERVAL)
    elif damage == "context":
        context["timestamp_ms"] -= BAR_MS
    elif damage == "one_minute":
        raw = raw_minutes(BOUNDARY)
    else:
        raw[-1][2] = "1"
    with pytest.raises(SafeError):
        frame(raw, context, boundary, "5m")


def test_future_five_minute_candle_is_ignored():
    raw = raw_minutes(BOUNDARY, interval_ms=INTERVAL)
    future = raw_minutes(BOUNDARY + INTERVAL, interval_ms=INTERVAL)[-1]
    future[4] = "999999"
    assert_frame_equal(market(), frame(raw + [future], slow_context(BOUNDARY), BOUNDARY, "5m"))


@pytest.mark.parametrize("damage", [None, "expired", "timeframe", "binding", "changed", "price"])
def test_five_minute_dispatch_permissions(settings, store, damage):
    s = five_settings(settings)
    obs = market().iloc[-1].to_dict()
    if damage == "timeframe":
        obs = minute_row(BOUNDARY)
    order = Order(
        client_id="pf-five", symbol="SOLUSDT", side=1, quantity=Decimal(1), limit_price=Decimal(100)
    )
    store.put("simple_minute:SOLUSDT", obs)
    store.put(
        "simple_minute_order:" + order.client_id,
        {
            "row": obs,
            "config_hash": s.config_hash,
            "order": order.model_dump(mode="json"),
        },
    )
    now = BOUNDARY + (INTERVAL if damage == "expired" else INTERVAL - 1)
    if damage == "binding":
        order = order.model_copy(update={"quantity": Decimal(2)})
    if damage == "changed":
        store.put("simple_minute:SOLUSDT", None)
    q = quote(price=99 if damage == "price" else 100, now_ms=now)
    if damage:
        with pytest.raises(EntryVeto):
            require_order(s, store, order, now, q)
    else:
        require_order(s, store, order, now, q)


async def test_five_minute_paper_entry_preserves_risk_context(settings, store, filters):
    engine = await engine_fixture(five_settings(settings), store, filters)
    obs, now = market().iloc[-1].to_dict(), BOUNDARY + 2 * MINUTE_MS
    assert engine.entry_signal(minute_row(BOUNDARY), now, 0.001)[0] == 0
    store.put("simple_minute:SOLUSDT", obs)
    await engine.quote(quote(now_ms=now), now)
    await engine.enter("SOLUSDT", obs, now)
    trade = engine.positions()["SOLUSDT"]
    assert trade["entry_timeframe"] == "5m"
    assert trade["initial_stop_distance"] / trade["entry_price"] == pytest.approx(0.016)


def test_five_minute_btc_pair_uses_fifteen_minute_context(settings, store):
    s = five_settings(settings, simple_btc_reference_enabled=True)
    btc_context(store, s, 200 * BAR_MS, now_ms=BOUNDARY)
    obs = {
        **market().iloc[-1].to_dict(),
        "btc_pair_valid": True,
        "btc_pair_asof_ms": 200 * BAR_MS,
        "btc_lag_valid": False,
    }
    assert entry_gate(s, store, obs, 1, BOUNDARY + 1000) is None
    assert entry_gate(s, store, obs, -1, BOUNDARY + 1000) is not None


async def test_service_fetches_stores_and_schedules_five_minute_candles(settings):
    service = SimpleService(five_settings(settings))
    service.context_rows = {symbol: slow_context(BOUNDARY) for symbol in ("BTCUSDT", "SOLUSDT")}
    service.store.put("simple_pool", ["SOLUSDT"])
    service.now_ms = lambda: BOUNDARY + 2 * MINUTE_MS
    service.exchange.request = AsyncMock(return_value=raw_minutes(BOUNDARY, interval_ms=INTERVAL))
    try:
        await service.refresh_minutes(BOUNDARY)
        params = service.exchange.request.call_args.args[2]
        assert params["interval"] == "5m"
        assert params["startTime"] == BOUNDARY - 120 * INTERVAL
        assert params["endTime"] == BOUNDARY - 1
        assert service.selected == ["SOLUSDT"]
        assert service.rows["SOLUSDT"]["entry_timeframe"] == "5m"
        assert len(service.lake.read("perp_5m", "SOLUSDT")) == 120
        diagnostics = json.loads(await service.diagnostics("/diagnostics"))
        assert diagnostics["breakout_minutes"] == 25
        calls = []

        async def refresh(boundary):
            calls.append(boundary)
            service.stop.set()

        service.refresh_minutes = refresh
        await asyncio.wait_for(service.minute_research(), timeout=3)
        assert calls == [BOUNDARY]
    finally:
        await service.exchange.close()
        service.store.engine.dispose()


async def test_five_minute_ai_expiry_budget_and_forecast_horizon(settings, store):
    s = five_settings(settings, simple_ai_enabled=True, simple_ai_review_minutes=5)
    clock = {"now": BOUNDARY + 1000}
    client = fake_client(plan(BOUNDARY))
    advisor = Advisor(s, store, client, lambda: clock["now"])
    await advisor.review({"SOLUSDT": market()}, ["SOLUSDT"], BOUNDARY)
    assert active_plan(s, store, BOUNDARY, BOUNDARY + INTERVAL - 1) is not None
    assert active_plan(s, store, BOUNDARY, BOUNDARY + INTERVAL) is None
    await Advisor(s, store, client, lambda: clock["now"]).review(
        {"SOLUSDT": market()}, ["SOLUSDT"], BOUNDARY
    )
    assert client.responses.parse.await_count == 1
    sent = json.loads(client.responses.parse.call_args.kwargs["input"][1]["content"])
    assert sent["entry_policy"]["entry_timeframe"] == "5m"
    assert sent["entry_policy"]["max_requests_per_utc_day"] == 288
    assert sent["entry_policy"]["forecast_minutes"] == 15
    assert "5/15/25-minute" in review_prompt(s)
    later = BOUNDARY + BAR_MS
    scored = market(later)
    scored.loc[scored.timestamp_ms == later, "close"] = 101
    advisor.snapshot({"SOLUSDT": scored}, ["SOLUSDT"], later)
    assert store.get("simple_ai_history")[-1]["forecast_results"][0]["actual"] == pytest.approx(
        0.01
    )
    clock["now"] = BOUNDARY + INTERVAL + 1000
    store.put("simple_ai_budget", {"day": BOUNDARY // 86_400_000, "count": 288})
    await advisor.review({"SOLUSDT": market(BOUNDARY + INTERVAL)}, ["SOLUSDT"], BOUNDARY + INTERVAL)
    assert client.responses.parse.await_count == 1
    assert store.get("simple_ai_active") is None
    assert store.get("simple_ai_status") == "daily_request_cap"


async def test_fifteen_minute_replay_refuses_five_minute_mode(settings, tmp_path):
    with pytest.raises(SafeError, match="recorded minute"):
        await replay({}, {}, five_settings(settings), BAR_MS, 2 * BAR_MS, tmp_path / "replay")
    assert not (tmp_path / "replay").exists()
