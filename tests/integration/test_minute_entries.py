import copy
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import numpy as np
import pytest
from pandas.testing import assert_frame_equal
from pydantic import ValidationError
from test_adaptive import fake_client, permit, plan
from test_bitcoin_reference import context as btc_context
from test_simple import engine_fixture, quote, row
from test_simple_live import setup_engine

from paperfrog.domain.types import EntryVeto, Order, SafeError
from paperfrog.settings import Settings
from paperfrog.simple.adaptive import Advisor, active_plan
from paperfrog.simple.backtest import replay
from paperfrog.simple.bitcoin import entry_gate
from paperfrog.simple.engine import Engine
from paperfrog.simple.minute import HISTORY_BARS, MINUTE_MS, direction, frame, require_order, veto
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS, execution_rules

BOUNDARY = 200 * BAR_MS + 7 * MINUTE_MS


def minute_settings(settings, **changes):
    return Settings(
        _env_file=None,
        **{
            **settings.public(),
            "simple_entry_timeframe": "1m",
            **changes,
        },
    )


def slow_context(boundary=BOUNDARY, side=1):
    return {
        **row(boundary // BAR_MS * BAR_MS, side),
        "return_2h": 0.02 * side,
        "return_12h": 0.03 * side,
    }


def raw_minutes(boundary=BOUNDARY, side=1, interval_ms=MINUTE_MS):
    closes = 100 * np.exp(side * (np.arange(HISTORY_BARS) - HISTORY_BARS + 1) * 0.0008)
    opens = np.r_[closes[0] / np.exp(side * 0.0008), closes[:-1]]
    return [
        [
            boundary - (HISTORY_BARS - i) * interval_ms,
            str(op),
            str(max(op, close) * 1.0003),
            str(min(op, close) * 0.9997),
            str(close),
            "100",
            boundary - (HISTORY_BARS - i - 1) * interval_ms - 1,
            "1000000",
            100,
            "70",
            "700000" if side > 0 else "300000",
            "0",
        ]
        for i, (op, close) in enumerate(zip(opens, closes, strict=True))
    ]


def minute_row(boundary=BOUNDARY, side=1, timeframe="1m"):
    interval_ms = MINUTE_MS if timeframe == "1m" else 5 * MINUTE_MS
    return (
        frame(
            raw_minutes(boundary, side, interval_ms),
            slow_context(boundary, side),
            boundary,
            timeframe,
        )
        .iloc[-1]
        .to_dict()
    )


@pytest.mark.parametrize("side", [1, -1])
def test_one_minute_slopes_keep_original_risk_context(settings, side):
    obs = minute_row(side=side)
    assert direction(obs, execution_rules(settings))[0] == side
    assert obs["return_1h"] == pytest.approx(np.exp(side * 60 * 0.0008) - 1)
    assert obs["atr"] == 0.8 and obs["atr_fraction"] == 0.008
    assert obs["minute_atr"] < obs["atr"]
    assert veto(obs, side, BOUNDARY + 1000, 100, execution_rules(settings)) is None


@pytest.mark.parametrize("side", [1, -1])
def test_rebound_blocks_entry_even_while_ema_still_points_down_or_up(settings, side):
    raw = raw_minutes(side=side)
    previous = float(raw[-2][4])
    raw[-1][4] = str(previous * np.exp(-side * 0.0005))
    raw[-1][2] = str(max(float(raw[-1][1]), float(raw[-1][4])) * 1.0003)
    raw[-1][3] = str(min(float(raw[-1][1]), float(raw[-1][4])) * 0.9997)
    obs = frame(raw, slow_context(side=side), BOUNDARY).iloc[-1].to_dict()
    assert side * obs["trend_atr"] > 0.4
    assert direction(obs, execution_rules(settings))[0] == 0


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "ohlc", "nonfinite", "flow", "old_context"]
)
def test_minute_history_fails_closed_on_invalid_inputs(damage):
    raw, context = raw_minutes(), slow_context()
    if damage == "missing":
        raw.pop(40)
    elif damage == "duplicate":
        raw[40] = copy.deepcopy(raw[39])
    elif damage == "ohlc":
        raw[-1][2] = "1"
    elif damage == "nonfinite":
        raw[-1][4] = "nan"
    elif damage == "flow":
        raw[-1][10] = "2000000"
    else:
        context["timestamp_ms"] -= BAR_MS
    with pytest.raises(SafeError):
        frame(raw, context, BOUNDARY)


def test_unclosed_future_candle_is_ignored():
    raw = raw_minutes()
    future = raw_minutes(BOUNDARY + MINUTE_MS)[-1]
    future[4] = "999999"
    assert_frame_equal(
        frame(raw, slow_context(), BOUNDARY), frame(raw + [future], slow_context(), BOUNDARY)
    )


def test_pepe_style_mixed_minute_momentum_cannot_open_short(settings):
    obs = minute_row(side=-1)
    # Public 1000PEPE futures returns/flow at the 2026-10-16 18:30 Sofia close.
    # An observation-level regression, not an execution or profitability backtest.
    obs.update(
        return_1m=-0.0018276958513807617,
        return_3m=-0.0013276220535557437,
        return_5m=0.0018595768206262075,
        flow=0.02871630287651561,
    )
    assert obs["trend_atr"] < -0.4
    assert direction(obs, execution_rules(settings))[0] == 0


@pytest.mark.parametrize("side", [1, -1])
async def test_paper_entries_use_fresh_minutes_and_keep_stops_and_daily_caps(
    settings, store, filters, side
):
    s = minute_settings(settings)
    engine = await engine_fixture(s, store, filters)
    obs = minute_row(side=side)
    store.put("simple_minute:SOLUSDT", obs)
    await engine.quote(quote(now_ms=BOUNDARY + 1000), BOUNDARY + 1000)
    await engine.enter("SOLUSDT", obs, BOUNDARY + 1000)
    trade = engine.positions()["SOLUSDT"]
    assert trade["side"] == side and trade["entry_timeframe"] == "1m"
    assert trade["initial_stop_distance"] / trade["entry_price"] == pytest.approx(0.016)
    assert engine.s.max_strategy_entries_per_day == settings.max_strategy_entries_per_day
    assert engine.s.risk_per_trade == settings.risk_per_trade


@pytest.mark.parametrize("damage", ["expired", "future", "price_rebound", "changed"])
async def test_paper_refuses_expired_or_changed_minute_signal(settings, store, filters, damage):
    engine = await engine_fixture(minute_settings(settings), store, filters)
    obs = minute_row(side=-1)
    now = BOUNDARY + 1000
    if damage == "expired":
        now = BOUNDARY + MINUTE_MS
    elif damage == "future":
        now = BOUNDARY - 1
    price = 101 if damage == "price_rebound" else 100
    store.put("simple_minute:SOLUSDT", {} if damage == "changed" else obs)
    await engine.quote(quote(price=price, now_ms=now), now)
    with pytest.raises(SafeError):
        await engine.enter("SOLUSDT", obs, now)
    assert not engine.positions() and not store.rows("entries")


async def test_ai_rules_refresh_every_fifteen_minutes_but_evaluate_fresh_minute_rows(
    settings, store, filters
):
    s = minute_settings(settings, simple_ai_enabled=True)
    p = plan(200 * BAR_MS)
    permit(s, store, p)
    engine = await engine_fixture(s, store, filters)
    obs = minute_row()
    assert active_plan(s, store, BOUNDARY, BOUNDARY + 1000) == p
    assert engine.entry_signal(obs, BOUNDARY + 1000, 0.001)[0] == 1
    assert engine.entry_signal({**obs, "return_1m": -0.01}, BOUNDARY + 1000, 0.001)[0] == 0
    assert active_plan(s, store, 201 * BAR_MS, 201 * BAR_MS) is None
    store.put("simple_minute:SOLUSDT", obs)
    await engine.quote(quote(now_ms=BOUNDARY + 1000), BOUNDARY + 1000)
    await engine.enter("SOLUSDT", obs, BOUNDARY + 1000)
    assert engine.positions()["SOLUSDT"]["ai_rule_id"] == p.digest


def test_btc_checks_keep_fifteen_minute_pair_context_and_independent_minute_gate(settings, store):
    s = minute_settings(settings, simple_btc_reference_enabled=True)
    btc_context(store, s, 200 * BAR_MS, now_ms=BOUNDARY)
    obs = {
        **minute_row(),
        "btc_pair_valid": True,
        "btc_pair_asof_ms": 200 * BAR_MS,
        "btc_lag_valid": False,
    }
    assert entry_gate(s, store, obs, 1, BOUNDARY + 1000) is None
    assert entry_gate(s, store, obs, -1, BOUNDARY + 1000) is not None
    assert entry_gate(s, store, {**obs, "btc_pair_asof_ms": 199 * BAR_MS}, 1, BOUNDARY) is not None


@pytest.mark.parametrize("damage", ["expired", "binding", "changed", "price", "flow"])
def test_dispatch_independently_requires_order_bound_fresh_minute_slope(settings, store, damage):
    s = minute_settings(settings)
    obs = minute_row()
    order = Order(
        client_id="pf-minute-entry",
        symbol="SOLUSDT",
        side=1,
        quantity=Decimal(1),
        limit_price=Decimal(100),
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
    now, price = BOUNDARY + 1000, 100
    if damage == "expired":
        now = BOUNDARY + MINUTE_MS
    elif damage == "binding":
        order = order.model_copy(update={"quantity": Decimal(2)})
    elif damage == "changed":
        store.put("simple_minute:SOLUSDT", None)
    elif damage == "price":
        price = 99
    elif damage == "flow":
        obs["flow"] = -0.2
        store.put("simple_minute:SOLUSDT", obs)
        permission = store.get("simple_minute_order:" + order.client_id)
        permission["row"] = obs
        store.put("simple_minute_order:" + order.client_id, permission)
    with pytest.raises(EntryVeto):
        require_order(s, store, order, now, quote(price=price, now_ms=now))


@pytest.mark.parametrize("timeframe", ["1m", "5m"])
async def test_live_minute_entry_rechecks_slope_after_fresh_exchange_quote(
    settings, store, filters, timeframe, monkeypatch
):
    # Simulated exchange calls must not cross a real candle boundary mid-test.
    fixed_time = time.time()
    monkeypatch.setattr(time, "time", lambda: fixed_time)
    engine, sim, adapter, _, now = await setup_engine(
        settings, store, filters, simple_entry_timeframe=timeframe
    )
    boundary = now // engine.s.simple_entry_bar_ms * engine.s.simple_entry_bar_ms
    obs = minute_row(boundary, timeframe=timeframe)
    store.put("simple_minute:SOLUSDT", obs)
    try:
        await engine.enter("SOLUSDT", obs, now)
        assert engine.positions()["SOLUSDT"]["entry_timeframe"] == timeframe
        assert sim.stops and sim.quantity > 0
        assert sum(path.endswith("/bookTicker") for _, path in sim.calls) >= 2
    finally:
        await adapter.close()


async def test_existing_positions_keep_original_trend_exit_after_timeframe_change(
    settings, store, filters
):
    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    updated = Engine(minute_settings(settings), store, filters)
    await updated.restore()
    now = BOUNDARY + 1000
    await updated.quote(quote(now_ms=now), now)
    await updated.monitor(now, {"SOLUSDT": {**minute_row(side=-1), "risk_context": slow_context()}})
    assert "SOLUSDT" in updated.positions()


async def test_watchlist_reranks_all_coins_each_minute_without_waiting_for_ai(
    settings, store, monkeypatch
):
    s = minute_settings(settings)
    service = SimpleService(s)
    service.store = store
    service.context_rows = {
        "BTCUSDT": slow_context(),
        "SOLUSDT": slow_context(),
        "ETHUSDT": slow_context(),
    }
    store.put("simple_pool", ["SOLUSDT", "ETHUSDT"])
    monkeypatch.setattr(service, "now_ms", lambda: BOUNDARY + 1000)
    responses = {"SOLUSDT": raw_minutes(), "ETHUSDT": raw_minutes(side=-1)}
    service.exchange.request = AsyncMock(
        side_effect=lambda method, path, params: responses[params["symbol"]]
    )
    try:
        await service.refresh_minutes(BOUNDARY)
        assert set(service.selected) == {"SOLUSDT", "ETHUSDT"}
        assert all(
            call.args[2]["interval"] == "1m" for call in service.exchange.request.call_args_list
        )
        later = BOUNDARY + MINUTE_MS
        responses["SOLUSDT"] = raw_minutes(later)
        responses["ETHUSDT"] = raw_minutes(later)
        responses["ETHUSDT"][-1][4] = responses["ETHUSDT"][-1][1]
        monkeypatch.setattr(service, "now_ms", lambda: later + 1000)
        await service.refresh_minutes(later)
        assert service.selected == ["SOLUSDT"]
        assert store.get("simple_minute:SOLUSDT")["timestamp_ms"] == later
        assert store.get("simple_ai_budget") is None
    finally:
        await service.exchange.close()


async def test_minute_ai_snapshot_and_forecast_keep_actual_fifteen_minute_horizon(settings, store):
    s = minute_settings(settings, simple_ai_enabled=True)
    boundary = 200 * BAR_MS
    data = frame(raw_minutes(boundary), slow_context(boundary), boundary)
    p, client = plan(boundary), fake_client(plan(boundary))
    advisor = Advisor(s, store, client, lambda: boundary + 1000)
    await advisor.review({"SOLUSDT": data}, ["SOLUSDT"], boundary)
    assert store.get("simple_ai_active")["digest"] == p.digest
    request = json.loads(client.responses.parse.call_args.kwargs["input"][1]["content"])
    assert request["entry_policy"]["entry_timeframe"] == "1m"
    assert "history_summary" in request["markets"][0]
    assert request["markets"][0]["recent_bars"][-1][0] == boundary
    later = boundary + BAR_MS
    scored = frame(raw_minutes(later), slow_context(later), later)
    # Change only the target close to distinguish 15-minute scoring from next-minute scoring.
    scored.loc[scored.timestamp_ms == later, "close"] = 101
    advisor.snapshot({"SOLUSDT": scored}, ["SOLUSDT"], later)
    assert store.get("simple_ai_history")[-1]["forecast_results"][0]["actual"] == pytest.approx(
        0.01
    )


def test_minute_mode_rejects_other_strategies(settings):
    for changes in [{"simple_entry_mode": "reverse"}, {"strategy_profile": "legacy"}]:
        with pytest.raises(ValidationError):
            minute_settings(settings, **changes)


async def test_fifteen_minute_replay_cannot_claim_one_minute_evidence(settings, tmp_path):
    with pytest.raises(SafeError, match="recorded minute"):
        await replay(
            {}, {}, minute_settings(settings), BAR_MS, 2 * BAR_MS, tmp_path / "no-fabrication"
        )
    assert not (tmp_path / "no-fabrication").exists()


@pytest.mark.parametrize("timeframe", ["1m", "5m"])
async def test_live_dispatch_blocks_a_turn_after_initial_quote_refresh(
    settings, store, filters, timeframe
):
    engine, sim, adapter, _, now = await setup_engine(
        settings, store, filters, simple_entry_timeframe=timeframe
    )
    obs = minute_row(
        now // engine.s.simple_entry_bar_ms * engine.s.simple_entry_bar_ms, timeframe=timeframe
    )
    store.put("simple_minute:SOLUSDT", obs)
    request = adapter.request
    quote_calls = 0

    async def changed_price(method, path, *args, **kwargs):
        nonlocal quote_calls
        if path.endswith("/bookTicker"):
            quote_calls += 1
            if quote_calls == 2:
                sim.bid, sim.ask = "98.95", "99.05"
        return await request(method, path, *args, **kwargs)

    adapter.request = changed_price
    try:
        with pytest.raises(SafeError, match="rebounded"):
            await engine.enter("SOLUSDT", obs, now)
        assert quote_calls == 2
        assert sim.quantity == 0 and not engine.positions()
        assert not any(method == "POST" and path.endswith("/order") for method, path in sim.calls)
    finally:
        await adapter.close()


@pytest.mark.parametrize("timeframe", ["1m", "5m"])
async def test_reduce_only_exit_survives_missing_minute_permission(
    settings, store, filters, timeframe
):
    engine, sim, adapter, _, now = await setup_engine(
        settings, store, filters, simple_entry_timeframe=timeframe
    )
    obs = minute_row(
        now // engine.s.simple_entry_bar_ms * engine.s.simple_entry_bar_ms, timeframe=timeframe
    )
    store.put("simple_minute:SOLUSDT", obs)
    try:
        await engine.enter("SOLUSDT", obs, now)
        store.put("simple_minute:SOLUSDT", None)
        await engine.close("SOLUSDT", engine.broker.now_ms(), "protective exit")
        assert sim.quantity == 0 and not engine.positions()
    finally:
        await adapter.close()
