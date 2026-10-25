from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from test_minute_entries import minute_row
from test_opposite_confirmation import confirmed_row
from test_simple import engine_fixture, quote
from test_simple_live import setup_engine

from paperfrog.domain.types import EntryVeto, Order
from paperfrog.simple import learning, learning_replay, rotation
from paperfrog.simple.engine import Engine
from paperfrog.simple.minute import timing_indicators
from paperfrog.simple.strategy import BAR_MS, RULES, exit_reason, opposite_entry
from paperfrog.storage.database import Store

NOW = 20000 * BAR_MS


def evidence(now=NOW, strategy="opposite", timeframe="5m", hold=2, net=.012):
    rows = []
    for block in range(18):
        opened = now - (19 - block) * 6 * 3_600_000
        for coin in range(3):
            rows.append({"id": f"{strategy}:{timeframe}:{hold}:{block}:{coin}",
                         "version": learning.VERSION, "strategy": strategy, "timeframe": timeframe,
                         "hold_hours": hold, "pattern": "BULL:persistent", "opened_ms": opened,
                         "closed_ms": opened + 3_600_000, "net_return": net, "cost": .002})
    return rows


def market(settings, now=NOW, **updates):
    return {"timestamp_ms": now, "config_hash": settings.config_hash, "regime": "BULL",
            "median_return_4h": .02, "median_return_1h": .008, "bull_index": 75, **updates}


def seed(store, settings, records=None, now=NOW):
    store.put(learning.EVIDENCE, evidence() if records is None else records)
    store.put("simple_learning_research", {"config_hash": settings.config_hash, "asof_ms": now,
                                           "errors": [], "symbols": ["SOLUSDT"]})


def test_pattern_distinguishes_persistence_fading_and_mixed(settings):
    assert learning.pattern(market(settings)) == "BULL:persistent"
    assert learning.pattern(market(settings, median_return_1h=.001)) == "BULL:fading"
    assert learning.pattern(market(settings, regime="MIXED", median_return_1h=-.001)) == "MIXED:reversal"
    assert learning.pattern(market(settings, regime="MIXED")) == "MIXED:disagreement"
    assert learning.pattern(market(settings, bull_index=float("nan"))) == "unknown"


def test_ranking_deduplicates_and_excludes_unclosed_future_and_other_regimes():
    records = evidence()
    expected = learning.score(records, NOW, "BULL:persistent")
    assert expected[0]["eligible"]
    extra = [dict(records[0], id="future", opened_ms=NOW, closed_ms=NOW + 1, net_return=100),
             dict(records[0], id="open", closed_ms=NOW + 1),
             dict(records[0], id="wrong-market", pattern="BEAR:persistent", net_return=100),
             dict(records[0], id="nonfinite", net_return=float("nan"))]
    assert learning.score([*records, *records, *extra], NOW, "BULL:persistent") == expected


@pytest.mark.parametrize("damage", ["fees", "few", "one_block", "holdout", "overlap"])
def test_insufficient_negative_or_unstable_evidence_cannot_activate(damage):
    rows = evidence()
    if damage == "fees":
        rows = [dict(r, cost=.02) for r in rows]
    elif damage == "few":
        rows = rows[:5]
    elif damage == "one_block":
        rows = [dict(r, opened_ms=NOW - 3_600_000, closed_ms=NOW - 1) for r in rows]
    elif damage == "holdout":
        for r in rows[-18:]:
            r["net_return"] = -.02
    else:
        for r in rows:
            r["closed_ms"] = NOW - 1
    assert not any(r["eligible"] for r in learning.score(rows, NOW, "BULL:persistent"))


def test_auto_changes_three_parameters_only_after_confirmation_and_dwell(settings, store):
    seed(store, settings)
    rotation.configure(store, "auto", NOW - learning.DWELL_MS)
    protected = {"manual_lock": True, "risk_lock": {"reason": "daily_loss"},
                 "anchors": {"equity": 500}, "simple_positions": {"SOLUSDT": {"side": 1}}}
    for key, value in protected.items():
        store.put(key, value)
    first = learning.review(store, settings, market(settings), NOW)
    assert first["confirmations"] == 1 and not first["allowed"]
    assert learning.review(store, settings, market(settings), NOW + 1) == first
    second = learning.review(store, settings, market(settings, NOW + BAR_MS), NOW + BAR_MS)
    assert second["allowed"]
    assert rotation.timeframe(store, settings) == "5m"
    assert rotation.hold_hours(store, settings) == 2
    assert rotation.selection(store)["active"] == "opposite"
    assert {k: store.get(k) for k in protected} == protected
    reopened = Store(Path(store.engine.url.database))
    rotation.initialize(reopened, "base", NOW + BAR_MS + 1)
    assert rotation.selection(reopened) == rotation.selection(store)
    reopened.engine.dispose()
    assert learning.veto(store, settings, NOW + BAR_MS + 1) is None
    assert learning.veto(store, settings, NOW + 2 * BAR_MS)


def test_research_failure_or_staleness_revokes_auto_without_clearing_risk(settings, store):
    seed(store, settings, evidence(strategy="current", timeframe="15m", hold=4))
    rotation.configure(store, "auto", NOW - learning.DWELL_MS)
    assert learning.review(store, settings, market(settings), NOW)["allowed"]
    store.put("simple_learning_research", {"asof_ms": NOW, "config_hash": settings.config_hash,
                                          "errors": [{"symbol": "SOLUSDT"}]})
    assert not learning.review(store, settings, market(settings, NOW + BAR_MS), NOW + BAR_MS)["allowed"]
    assert learning.veto(store, settings, NOW + BAR_MS)


def test_manual_selection_disables_auto_and_invalidates_order_epoch(settings, store):
    rotation.configure(store, "auto", NOW)
    state = rotation.selection(store)
    order = Order(client_id="pf-auto-permission", symbol="SOLUSDT", side=1,
                  quantity=Decimal(1), limit_price=Decimal(100))
    store.put("simple_strategy_order:" + order.client_id, {"order": order.model_dump(mode="json"),
              "active": "current", "epoch": state["epoch"], "decision_side": 1})
    rotation.configure(store, "current", NOW + 1)
    with pytest.raises(EntryVeto):
        rotation.require_order(store, order)
    rotation.require_order(store, order.model_copy(update={"reduce_only": True}))
    assert learning.veto(store, settings, NOW + 1) is None


def test_holding_budget_shortens_with_trend_but_never_extends_original_cap(settings):
    position = {"side": 1, "adaptive_holding": True, "max_hold_hours": 4,
                "opened_ms": NOW - 2 * 3_600_000, "entry_price": 100, "stop_price": 98}
    assert learning.position_budget(position, market(settings), NOW, 8)["max_hold_hours"] == 4
    weak = market(settings, median_return_1h=.001)
    assert learning.position_budget(position, weak, NOW, 8)["max_hold_hours"] == 2
    hostile = market(settings, median_return_1h=-.001)
    budget = learning.position_budget(position, hostile, NOW, 8)
    assert exit_reason(budget, 100, NOW, None) == "1-hour holding limit reached"
    assert position["max_hold_hours"] == 4
    assert learning.position_budget({**position, "adaptive_holding": False}, hostile, NOW, 8)["max_hold_hours"] == 4


@pytest.mark.parametrize("side", [1, -1])
def test_five_minute_opposite_requires_confirmation_and_expires_at_five_minutes(store, side):
    row = {**confirmed_row(NOW, side), "entry_timeframe": "5m"}
    assert opposite_entry(row, .0007)[0] == -side
    rotation.configure(store, "opposite", NOW)
    order = Order(client_id="pf-confirm-five", symbol="SOLUSDT", side=-side,
                  quantity=Decimal(1), limit_price=Decimal(100))
    store.put("simple_strategy_order:" + order.client_id, {
        "order": order.model_dump(mode="json"), "active": "opposite",
        "epoch": rotation.selection(store)["epoch"], "decision_side": side,
        "opposite_level": opposite_entry(row, .0007)[2],
        "opposite_evidence": {"row": row, "rules": RULES.public(), "one_way_cost": .0007}})
    rotation.require_order(store, order, NOW + 299_999)
    with pytest.raises(EntryVeto):
        rotation.require_order(store, order, NOW + 300_000)


async def test_auto_timeframe_and_budget_are_recorded_and_survive_restore(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    rotation.configure(store, "auto", NOW - 3_600_000)
    state = rotation.selection(store)
    state.update(timeframe="5m", hold_hours=2)
    store.put(rotation.KEY, state)
    seed(store, settings)
    store.put(learning.KEY, {"allowed": True, "config_hash": settings.config_hash,
                            "epoch": state["epoch"], "active": "current", "timeframe": "5m", "hold_hours": 2,
                            "asof_ms": NOW, "expires_ms": NOW + BAR_MS})
    row = minute_row(NOW, timeframe="5m")
    store.put("simple_minute:SOLUSDT", row)
    await engine.quote(quote(now_ms=NOW), NOW)
    await engine.enter("SOLUSDT", row, NOW)
    trade = engine.positions()["SOLUSDT"]
    assert trade["entry_timeframe"] == "5m" and trade["max_hold_hours"] == 2
    assert trade["adaptive_holding"]
    restored = Engine(settings, store, filters)
    await restored.restore()
    assert restored.positions()["SOLUSDT"]["max_hold_hours"] == 2


def test_shared_timing_is_invariant_to_future_rows_and_history_length():
    raw = pd.DataFrame([{"timestamp_ms": i * 300_000, "open": 100 + i * .1,
                         "close": 100.05 + i * .1, "high": 100.2 + i * .1,
                         "low": 99.9 + i * .1, "quote_volume": 1_000_000,
                         "taker_buy_quote": 600_000} for i in range(250)])
    full = timing_indicators(raw.copy(), 5)
    short = timing_indicators(raw.iloc[80:200].copy(), 5)
    for name in ("fast", "slow", "trend_atr", "previous_trend_atr", "prior_high", "flow"):
        assert full.iloc[199][name] == pytest.approx(short.iloc[-1][name])
    changed = raw.copy()
    changed.loc[200:, "close"] = 10000
    assert timing_indicators(changed, 5).iloc[199]["trend_atr"] == full.iloc[199]["trend_atr"]


def test_recorded_market_waits_for_actual_availability(store, settings):
    import json

    from sqlalchemy import text

    r = market(settings)
    with store.transaction() as conn:
        conn.execute(text("INSERT INTO audit(ts,kind,payload) VALUES (:ts,:kind,:payload)"),
                     {"ts": NOW + 10_000, "kind": "simple_market_trend", "payload": json.dumps(r)})
    result = learning_replay.recorded_markets(store, NOW + BAR_MS)
    assert result[NOW]["available_ms"] == NOW + 300_000
    assert learning_replay.recorded_markets(store, NOW) == {}


async def test_auto_five_minute_opposite_dispatches_and_keeps_its_budget(settings, store, filters):
    engine, sim, adapter, obs, now = await setup_engine(settings, store, filters)
    try:
        rotation.configure(store, "auto", now - 3_600_000)
        state = rotation.selection(store)
        state.update(active="opposite", timeframe="5m", hold_hours=1)
        store.put(rotation.KEY, state)
        seed(store, engine.s, now=now)
        store.put(learning.KEY, {"allowed": True, "config_hash": engine.s.config_hash,
                                "epoch": state["epoch"], "active": "opposite", "timeframe": "5m", "hold_hours": 1,
                                "asof_ms": now, "expires_ms": now + BAR_MS})
        obs.update(confirmed_row(now, 1), entry_timeframe="5m")
        store.put("simple_minute:SOLUSDT", obs)
        await engine.enter("SOLUSDT", obs, now)
        trade = engine.positions()["SOLUSDT"]
        assert trade["side"] == -1 and trade["entry_timeframe"] == "5m"
        assert trade["max_hold_hours"] == 1 and trade["adaptive_holding"]
        assert sim.quantity < 0
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        before = len(sim.orders)
        store.put(learning.KEY, {})
        assert engine.entry_signal(obs, now, 0)[0] == 0
        assert len(sim.orders) == before
        await engine.close("SOLUSDT", now + 1, "protective close despite unavailable research")
        assert not engine.positions()
    finally:
        await adapter.close()


def test_replay_is_causal_and_rejects_gapped_candle_paths(settings):
    import numpy as np

    from paperfrog.domain.types import SafeError

    start = NOW - 1200 * 300_000
    prices = 100 * np.exp(np.arange(1300) * .001)
    raw = pd.DataFrame([{"open_ms": start + i * 300_000,
                         "timestamp_ms": start + (i + 1) * 300_000,
                         "open": close / np.exp(.001), "close": close,
                         "high": close * 1.0006, "low": close / np.exp(.001) * .9994,
                         "quote_volume": 1_000_000, "taker_buy_quote": 700_000}
                        for i, close in enumerate(prices)])
    markets = {ts: market(settings, ts) for ts in range(start, NOW + BAR_MS, BAR_MS)}
    before = learning_replay.replay(raw, markets, settings, "SOLUSDT", NOW)
    assert before and all(r["opened_ms"] < r["closed_ms"] <= NOW for r in before)
    raw.loc[raw.timestamp_ms > NOW, "close"] = 1_000_000
    assert learning_replay.replay(raw, markets, settings, "SOLUSDT", NOW) == before
    with pytest.raises(SafeError, match="gaps"):
        learning_replay.replay(raw.drop(index=700), markets, settings, "SOLUSDT", NOW)
