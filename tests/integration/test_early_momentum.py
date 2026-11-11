"""Early entries, no chasing, and exact dispatch permission for momentum v2."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import numpy as np
import pytest
from pandas.testing import assert_frame_equal
from test_abc_momentum import row
from test_abc_momentum import settings as momentum_settings
from test_minute_entries import slow_context
from test_simple_live import live_settings, setup_engine

from paperfrog.domain.types import EntryVeto, Order, Quote, SafeError
from paperfrog.simple.minute import HISTORY_BARS, MOMENTUM_HISTORY_BARS, frame, timing_indicators
from paperfrog.simple.momentum import classify, require_order, timing_veto
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import EARLY_MOMENTUM_RULES, RULES, execution_rules, exit_reason

BAR = 300_000
BOUNDARY = 300 * 900_000


def early(side=1, age=2, **changes):
    return row(
        side,
        **{
            "timestamp_ms": BOUNDARY,
            "momentum_trend_started_ms": BOUNDARY - age * BAR,
            "momentum_trend_age_bars": age,
            "rsi_14": 55 if side > 0 else 45,
            **changes,
        },
    )


def tape(boundary=BOUNDARY, *, known_start=True):
    # A rising regime followed by a recent falling regime. All OHLC values
    # and flow are observable at the candle's close, including the transition.
    n = MOMENTUM_HISTORY_BARS
    close = (
        np.r_[100 + np.arange(n - 8) * 0.02, 102.4 - np.arange(8) * 0.08]
        if known_start
        else 110 - np.arange(n) * 0.02
    )
    opening = np.r_[close[0] + 0.01, close[:-1]]
    return [
        [
            boundary - (n - i) * BAR,
            str(o),
            str(max(o, c) + 0.03),
            str(min(o, c) - 0.03),
            str(c),
            "10",
            boundary - (n - i - 1) * BAR - 1,
            "1000000",
            1,
            "2",
            "200000",
            "0",
        ]
        for i, (o, c) in enumerate(zip(opening, close, strict=True))
    ]


@pytest.mark.parametrize("side", [-1, 1])
def test_new_trend_does_not_wait_for_old_hour_or_twenty_five_minute_direction(side):
    obs = early(
        side,
        return_1h=-side * 0.01,
        return_25m=-side * 0.005,
        path_efficiency_1h=0.1,
        previous_trend_atr=-side,
    )
    assert classify(obs, 0.0012, EARLY_MOMENTUM_RULES)[1] == side
    assert classify(obs, 0.0012, replace(RULES, entry_style="trend_follow"))[1] == 0
    assert execution_rules(momentum_settings()).version == EARLY_MOMENTUM_RULES.version


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("age", [0, 12])
def test_only_fresh_confirmed_trends_are_eligible(side, age):
    assert classify(early(side, age), 0.0012, EARLY_MOMENTUM_RULES)[1] == side


@pytest.mark.parametrize(
    "damage",
    [
        {"momentum_trend_age_bars": 13, "momentum_trend_started_ms": BOUNDARY - 13 * BAR},
        {"momentum_trend_age_bars": -1, "momentum_trend_started_ms": -1},
        {"momentum_trend_age_bars": 1.5},
        {"momentum_trend_started_ms": BOUNDARY + BAR},
        {"momentum_trend_started_ms": float("nan")},
        {"momentum_trend_age_bars": None},
        {"path_efficiency_15m": 0.1},
        {"rsi_14": float("inf")},
        {"minute_atr": 0},
        {"return_5m": -0.01},
        {"return_15m": -0.01},
        {"fast_change": -1},
        {"flow": -0.2},
    ],
)
def test_missing_old_invalid_or_counter_move_evidence_cannot_authorize_entry(damage):
    obs = {**early(), **damage}
    assert classify(obs, 0.0012, EARLY_MOMENTUM_RULES) == ("B", 0, 0.0)


@pytest.mark.parametrize("side,rsi", [(-1, 29.9), (1, 70.1)])
def test_exhausted_moves_do_not_get_new_entries(side, rsi):
    obs = early(side, rsi_14=rsi)
    assert "exhausted" in timing_veto(obs, EARLY_MOMENTUM_RULES)
    assert classify(obs, 0.0012, EARLY_MOMENTUM_RULES)[1] == 0


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -0.001])
def test_invalid_cost_cannot_open_early_entries(cost):
    assert classify(early(), cost, EARLY_MOMENTUM_RULES)[1] == 0


@pytest.mark.parametrize("side", [-1, 1])
def test_overextended_candle_and_price_wait(side):
    assert "overextended" in timing_veto(
        early(side, close=100 + side * 2, open=100 + side * 1.8), EARLY_MOMENTUM_RULES
    )
    assert "overextended" in timing_veto(
        early(side, close=100 + side, open=100 - side), EARLY_MOMENTUM_RULES
    )


def test_transition_is_proven_by_extra_history_and_live_window_matches_replay():
    raw = tape()
    data = frame(raw, slow_context(BOUNDARY), BOUNDARY, "5m", early_momentum=True)
    obs = data.iloc[-1]
    assert 0 <= obs.momentum_trend_age_bars <= 12
    assert obs.timestamp_ms - obs.momentum_trend_started_ms == obs.momentum_trend_age_bars * BAR
    assert obs.momentum_trend_started_ms % BAR == 0
    old = frame(raw[-HISTORY_BARS:], slow_context(BOUNDARY), BOUNDARY, "5m").iloc[-1]
    assert old.fast == obs.fast and old.slow == obs.slow
    assert old.minute_atr == obs.minute_atr
    # A longer one-shot replay must compute the same bounded EMA and trend age.
    extra = tape(BOUNDARY - 144 * BAR, known_start=False)
    from paperfrog.simple.data import normalize

    full = timing_indicators(normalize(extra + raw, BOUNDARY, BAR), 5).iloc[-1]
    assert full.fast == pytest.approx(obs.fast)
    assert full.slow == pytest.approx(obs.slow)
    assert full.momentum_trend_started_ms == obs.momentum_trend_started_ms


def test_unknown_trend_start_is_explicit_and_serializable(store):
    obs = (
        frame(tape(known_start=False), slow_context(BOUNDARY), BOUNDARY, "5m", early_momentum=True)
        .iloc[-1]
        .to_dict()
    )
    assert obs["momentum_trend_age_bars"] == -1
    assert classify(obs, 0.0012, EARLY_MOMENTUM_RULES)[1] == 0
    store.put("unknown-trend", obs)


def test_future_candle_does_not_change_past_timing():
    raw = tape()
    future = tape(BOUNDARY + BAR)[-1]
    future[4] = "99999"
    before = frame(raw, slow_context(BOUNDARY), BOUNDARY, "5m", early_momentum=True)
    after = frame(raw + [future], slow_context(BOUNDARY), BOUNDARY, "5m", early_momentum=True)
    assert_frame_equal(before, after)
    with pytest.raises(SafeError, match="incomplete"):
        frame(raw[-120:], slow_context(BOUNDARY), BOUNDARY, "5m", early_momentum=True)


@pytest.mark.parametrize("damage", [None, "old", "exhausted", "rebound", "changed"])
def test_dispatch_independently_rechecks_timing_and_rebound(settings, store, damage):
    s = live_settings(settings, simple_entry_profile="momentum", simple_entry_timeframe="5m")
    obs = early(-1)
    if damage == "old":
        obs.update(momentum_trend_age_bars=13, momentum_trend_started_ms=BOUNDARY - 13 * BAR)
    if damage == "exhausted":
        obs["rsi_14"] = 20
    order = Order(
        client_id="pf-early-test",
        symbol="SOLUSDT",
        side=-1,
        quantity=Decimal(1),
        limit_price=Decimal("98.99"),
    )
    store.put("simple_minute:SOLUSDT", obs)
    store.put(
        "simple_minute_order:" + order.client_id,
        {"order": order.model_dump(mode="json"), "config_hash": s.config_hash, "row": obs},
    )
    if damage == "changed":
        store.put("simple_minute:SOLUSDT", {})
    mark = Decimal(100) if damage == "rebound" else Decimal(99)
    q = Quote(
        "SOLUSDT",
        mark - Decimal(".01"),
        mark + Decimal(".01"),
        Decimal(1000),
        Decimal(1000),
        mark,
        BOUNDARY + 1000,
    )
    if damage is None:
        require_order(s, store, order, BOUNDARY + 1000, q)
    else:
        with pytest.raises(EntryVeto):
            require_order(s, store, order, BOUNDARY + 1000, q)
    require_order(s, store, order.model_copy(update={"reduce_only": True}), BOUNDARY + 1000, None)


async def test_service_fetches_extra_closed_history_and_exposes_timing_rejections(settings):
    s = settings.model_copy(
        update={"simple_entry_profile": "momentum", "simple_entry_timeframe": "5m"}
    )
    service = SimpleService(s)
    service.now_ms = lambda: BOUNDARY + 1000
    service.context_rows = {symbol: slow_context(BOUNDARY) for symbol in ("BTCUSDT", "SOLUSDT")}
    service.store.put("simple_pool", ["SOLUSDT"])
    service.exchange.request = AsyncMock(return_value=tape(known_start=False))
    try:
        assert await service.refresh_minutes(BOUNDARY)
        params = service.exchange.request.call_args.args[2]
        assert params["limit"] == 144 and params["endTime"] == BOUNDARY - 1
        assert service.store.get("simple_momentum_timing")["blocked"]["SOLUSDT"]
    finally:
        await service.exchange.close()


async def test_early_live_entry_keeps_existing_financial_limits_and_protection(
    settings, store, filters
):
    engine, sim, adapter, _, now = await setup_engine(
        settings,
        store,
        filters,
        simple_entry_profile="momentum",
        simple_entry_timeframe="5m",
        simple_sizing_mode="margin",
        simple_position_margin_cap=0.1,
    )
    boundary = now // BAR * BAR
    obs = early(-1, open=100.2, close=100, fast=100.5, slow=101.5)
    obs.update(timestamp_ms=boundary, momentum_trend_started_ms=boundary - 2 * BAR)
    store.put("simple_minute:SOLUSDT", obs)
    try:
        await engine.enter("SOLUSDT", obs, now)
        assert sim.quantity < 0 and sim.stops
        trade = engine.positions()["SOLUSDT"]
        assert trade["momentum_exit_version"] == 1
        assert trade["entry_context"]["rules"]["version"] == EARLY_MOMENTUM_RULES.version
        assert (
            store.rows("entries")[0]["risk"]
            <= float(engine.broker.equity()) * engine.s.risk_per_trade
        )
        assert engine.s.exchange_leverage == 2
        # Entry age/exhaustion never vetoes an existing holding's reduction.
        assert "holding limit" in exit_reason(
            trade, trade["entry_price"], trade["opened_ms"] + 4 * 3_600_000, obs, engine.rules
        )
    finally:
        await adapter.close()


async def test_v1_to_v2_restart_keeps_open_positions_stops_and_financial_locks(
    settings, store, filters
):
    import copy

    from paperfrog.simple.authorization import directory
    from paperfrog.simple.engine import Engine

    engine, sim, adapter, _, now = await setup_engine(
        settings,
        store,
        filters,
        simple_entry_profile="momentum",
        simple_entry_timeframe="5m",
        simple_sizing_mode="margin",
        simple_position_margin_cap=0.1,
    )
    boundary = now // BAR * BAR
    obs = early(-1, open=100.2, close=100, fast=100.5, slow=101.5)
    obs.update(timestamp_ms=boundary, momentum_trend_started_ms=boundary - 2 * BAR)
    store.put("simple_minute:SOLUSDT", obs)
    try:
        await engine.enter("SOLUSDT", obs, now)
        held = engine.positions()
        held["SOLUSDT"]["entry_context"]["rules"].update(
            version="abc-momentum-v1", minimum_trend_atr=0.5
        )
        held["SOLUSDT"]["entry_context"]["strategy"] = "abc-momentum-v1"
        store.put("simple_positions", held)
        old_rules = store.get("simple_rules")
        old_rules.update(version="abc-momentum-v1", minimum_trend_atr=0.5)
        store.put("simple_rules", old_rules)
        saved = {
            "risk_lock": {"state": "HARD_RISK_LOCK", "reason": "high_water"},
            "manual_lock": True,
            "consecutive_losses": 3,
            "loss_cooldown_step": 2,
        }
        for key, value in saved.items():
            store.put(key, value)
        stops = copy.deepcopy(sim.stops)
        restored = Engine(engine.s, store, filters, broker=engine.broker)
        await restored.restore()
        assert restored.positions() == held
        assert sim.stops == stops
        assert {key: store.get(key) for key in saved} == saved
        # This exception is limited to entry timing; exit changes still fail.
        wrong = {**old_rules, "trailing_atr": 3}
        store.put("simple_rules", wrong)
        with pytest.raises(SafeError, match="changing strategy rules"):
            Engine(engine.s, store, filters, broker=engine.broker)
        store.put("simple_rules", old_rules)
        (directory(engine.s) / "live-arm.json").unlink()
        with pytest.raises(SafeError, match="arm missing"):
            Engine(engine.s, store, filters, broker=engine.broker)
    finally:
        await adapter.close()


def test_why_reports_actual_momentum_timing_rejections(settings, store):
    from paperfrog.simple.blockers import explain

    s = settings.model_copy(
        update={"simple_entry_profile": "momentum", "simple_entry_timeframe": "5m"}
    )
    store.audit(
        "simple_selection",
        {
            "asof_ms": BOUNDARY,
            "entry_timeframe": "5m",
            "trend_hours": 4,
            "confirmation_hours": 1,
            "pool_size": 18,
            "usable_size": 18,
        },
    )
    store.put(
        "simple_momentum_timing",
        {
            "version": EARLY_MOMENTUM_RULES.version,
            "asof_ms": BOUNDARY,
            "blocked": {"LINKUSDT": "momentum move is older than the sixty-minute entry window"},
        },
    )
    result = explain(s, store, BOUNDARY + 1000, False, False)
    assert "first 60 minutes" in result and "LINKUSDT: momentum move is older" in result
