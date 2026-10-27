from copy import deepcopy
from json import loads

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from test_active_auto import select_probe
from test_historical_learning import NOW, market, seed
from test_market_trend import breadth, day
from test_opposite_confirmation import confirmed_row
from test_simple import row, source
from test_simple_live import equity_range_options, setup_engine
from test_telegram_events import worker

from paperfrog.domain.types import EntryVeto, Order
from paperfrog.simple import learning, rotation, routing
from paperfrog.simple.strategy import BAR_MS, indicators


def stable(at=NOW):
    return {**row(at), **day(at), "previous_trend_atr": 1., "previous_flow": .4,
            "path_efficiency_1h": .8, "direction_changes_1h": 0}


def closed(identifier, pnl=-2., *, at=NOW, operational=False):
    return {"trade_id": identifier, "entry_ms": at - 8 * BAR_MS,
            "timestamp_ms": at - BAR_MS, "strategy_choice": "current", "entry_price": 100.,
            "quantity": 1., "pnl": pnl, "gross_pnl": pnl + .1, "fees": .1,
            "reason": "closed-candle monitoring data unavailable" if operational else "1-hour holding limit reached",
            "entry_context": {"entry_timeframe": "15m", "config_hash": "test",
                              "indicators": stable(at - 8 * BAR_MS)}}


def test_route_is_per_candidate_not_global_and_manual_remains_explicit(settings, store):
    store.put("simple_market_trend", market(settings))
    rotation.configure(store, "auto-active", NOW)
    before = rotation.selection(store)
    assert routing.choose(store, stable(), NOW)["choice"] == "current"
    dynamic = {**stable(), **confirmed_row(NOW)}
    assert routing.choose(store, dynamic, NOW)["choice"] == "opposite"
    assert rotation.selection(store) == before
    missing = stable()
    missing.pop("path_efficiency_1h")
    assert routing.choose(store, missing, NOW)["choice"] is None


def test_pattern_memory_is_causal_counts_winners_and_ignores_operational_losses(settings, store):
    store.put("simple_market_trend", {**market(settings), "config_hash": "test"})
    trades = [closed(str(i), at=NOW - i * 20 * BAR_MS) for i in range(3)]
    store.put("simple_closed", trades)
    route = routing.choose(store, stable(), NOW)
    assert route["choice"] == "opposite" and route["history"]["losses"] == 3
    assert "similar setups" in route["reason"]
    # Duplicated records cannot create additional evidence; open/future labels and
    # monitoring faults cannot teach the router a price reversal.
    extra = [dict(trades[0]), closed("future", at=NOW + 2 * BAR_MS), closed("fault", operational=True)]
    store.put("simple_closed", trades + extra)
    assert routing.choose(store, stable(), NOW) == route
    store.put("simple_closed", trades + [closed("winner" + str(i), pnl=3.) for i in range(4)])
    assert routing.choose(store, stable(), NOW)["choice"] == "current"


def test_path_features_do_not_use_future_candles():
    raw = source(240, 1)
    before = indicators(raw.iloc[:-1])
    after = indicators(raw)
    assert_frame_equal(before, after.iloc[:-1].reset_index(drop=True))
    assert after.iloc[-1].path_efficiency_1h == pytest.approx(1.)
    flat = pd.DataFrame({"close": [100.] * 8})
    from paperfrog.simple.strategy import path_features
    assert path_features(flat, 4).iloc[-1].path_efficiency_1h == 0


@pytest.mark.parametrize("context", ["mixed", "fading"])
def test_active_router_can_prepare_dynamic_market_with_five_minute_candles(settings, store, context):
    s = settings.model_copy(update={"simple_market_trend_enabled": True})
    rotation.configure(store, "auto-active", NOW - 2 * learning.DWELL_MS)
    seed(store, s, [])
    m = market(s, regime="MIXED") if context == "mixed" else market(s, median_return_1h=.001)
    learning.review(store, s, m, NOW)
    decision = learning.review(store, s, {**m, "timestamp_ms": NOW + BAR_MS}, NOW + BAR_MS)
    assert decision["allowed"] and decision["timeframe"] == "5m"
    assert decision["entry_policy"] == "probe" and decision["hold_hours"] == 1
    assert learning.veto(store, s, NOW + BAR_MS + 1) is None


async def test_auto_executes_opposite_without_global_switch_and_records_messages(settings, store, filters, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: (NOW + 2_000) / 1000)
    engine, sim, adapter, obs, now = await setup_engine(
        settings, store, filters, **equity_range_options(simple_market_trend_enabled=True))
    try:
        obs.update(stable())
        breadth(store, engine.s, NOW)
        select_probe(store, engine.s)
        assert engine.entry_signal(obs, now, .001)[0] == 1
        state = deepcopy(rotation.selection(store))
        obs.update(confirmed_row(NOW))
        assert engine.entry_signal(obs, now, .001)[0] == -1
        await engine.enter("SOLUSDT", obs, now)
        trade = engine.positions()["SOLUSDT"]
        assert trade["strategy_choice"] == "opposite" and trade["side"] == -1
        assert trade["activity_probe"] and trade["max_hold_hours"] == 1
        assert rotation.selection(store) == state  # no global switch/epoch reset
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        permission = next(r for r in store.rows("orders") if not loads(r["intent"])['reduce_only'])
        order = Order(**loads(permission["intent"]))
        events, sender = worker(engine.s, store)
        msg = events.render("order_terminal", {"client_id": order.client_id, "symbol": "SOLUSDT",
                            "quantity": "1", "price": "100", "side": -1})
        assert "Strategy: OPPOSITE (AUTO)" in msg and "flow changed direction" in msg
        # Freshness and tampering checked on the exact bound order.
        with pytest.raises(EntryVeto):
            rotation.require_order(store, order.model_copy(update={"side": 1}), now)
        with pytest.raises(EntryVeto):
            rotation.require_order(store, order, NOW + BAR_MS)
        route_permission = store.get("simple_strategy_order:" + order.client_id)
        route_permission["active"] = "current"
        store.put("simple_strategy_order:" + order.client_id, route_permission)
        with pytest.raises(EntryVeto):
            rotation.require_order(store, order, now)
        # Reduce-only protection is independent of entry routing and current mode.
        store.put(learning.KEY, {})
        await engine.close("SOLUSDT", now + 1, "test close")
        message = events.render("trade_closed", {"id": trade["id"], "symbol": "SOLUSDT", "pnl": -1})
        assert "Strategy: OPPOSITE (AUTO)" in message
        assert store.get(rotation.RESULT_PREFIX + trade["id"])["strategy"] == "opposite"
    finally:
        await adapter.close()


def test_loss_match_does_not_authorize_an_unconfirmed_inverse(settings, store):
    from paperfrog.simple.strategy import opposite_entry
    store.put("simple_market_trend", {**market(settings), "config_hash": "test"})
    store.put("simple_closed", [closed(str(i), at=NOW - i * 20 * BAR_MS) for i in range(3)])
    obs = stable()
    assert routing.choose(store, obs, NOW)["choice"] == "opposite"
    assert opposite_entry(obs, .0007)[0] == 0


def test_strict_auto_checks_actual_strategy_not_global_default(store):
    store.put(learning.KEY, {"entry_policy": "validated", "timeframe": "15m", "hold_hours": 2,
                            "ranking": [{"eligible": True, "strategy": "current", "timeframe": "15m", "hold_hours": 2}]})
    assert routing.permitted(store, "current")
    assert not routing.permitted(store, "opposite")


@pytest.mark.parametrize("updates,reason", [
    ({"previous_trend_atr": .2}, "not persisted"),
    ({"prior_high": 99.99}, "less than 0.15 ATR"),
    ({"path_efficiency_1h": .3}, "choppy"),
    ({"day_return_1h": -.01}, "disagrees"),
])
def test_individual_unstable_setups_prefer_opposite(settings, store, updates, reason):
    store.put("simple_market_trend", market(settings))
    decision = routing.choose(store, {**stable(), **updates}, NOW)
    assert decision["choice"] == "opposite" and reason in decision["reason"]


def test_controller_message_does_not_claim_global_direction_switch(settings, store):
    events, _ = worker(settings, store)
    message = events.render("simple_strategy_changed", {"auto": True, "reason": "5m context"})
    assert "separately for each new trade" in message
