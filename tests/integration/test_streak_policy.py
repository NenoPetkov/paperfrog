"""Consecutive outcomes, exact inversion, bracket dispatch and restart persistence."""
import time
from decimal import Decimal
from pathlib import Path

import pytest
from test_early_momentum import early, tape
from test_fixed_roe import options
from test_minute_entries import slow_context
from test_simple_live import setup_engine
from test_strategy_rotation import settle_result

from paperfrog.domain.types import EntryVeto, SafeError
from paperfrog.simple import rotation
from paperfrog.simple.engine import Engine
from paperfrog.simple.fixed_roe import position_trigger, reason
from paperfrog.simple.minute import frame
from paperfrog.simple.momentum import classify, policy_rules
from paperfrog.simple.selection import pool_hash
from paperfrog.storage.database import Store


@pytest.fixture(autouse=True)
def stable_clock(monkeypatch):
    fixed = int(time.time() * 1000) // 900000 * 900000 + 60000
    monkeypatch.setattr(time, "time", lambda: fixed / 1000)


def test_wins_losses_neutral_and_repeated_cycles_survive_restart(store, settings):
    rotation.activate_streak(store, 1)
    assert rotation.timeframe(store, settings) == "15m"
    for i, target in enumerate([.04, .03, .03]):
        settle_result(store, f"win{i}", 1)
        assert rotation.parameters(store)["profit_roe"] == target
    assert rotation.timeframe(store, settings) == "5m"
    first = settle_result(store, "loss1", -1)
    assert rotation.parameters(store)["stop_roe"] == .04
    assert rotation.timeframe(store, settings) == "15m"
    rotation.closed(store, first, -1, 200)  # duplicate cannot advance the streak
    assert rotation.parameters(store)["losses"] == 1
    settle_result(store, "loss2", -1)
    assert rotation.parameters(store)["stop_roe"] == .03
    saved = rotation.selection(store)
    reopened = Store(Path(store.engine.url.database))
    try:
        rotation.initialize(reopened, "base", 200)
        rotation.activate_streak(reopened, 300)  # operator retry is idempotent
        assert rotation.selection(reopened) == saved
        settle_result(reopened, "loss3", -1)
        assert rotation.selection(reopened)["active"] == "opposite"
        assert rotation.parameters(reopened)["losses"] == 0
        for cycle in range(6):
            expected = "current" if cycle % 2 == 0 else "opposite"
            for loss in range(3):
                settle_result(reopened, f"cycle{cycle}-{loss}", -1)
            assert rotation.selection(reopened)["active"] == expected
        settle_result(reopened, "win-later", 1)
        settle_result(reopened, "flat", 0)
        assert rotation.parameters(reopened)["wins"] == 0
        assert rotation.parameters(reopened)["losses"] == 0
    finally:
        reopened.engine.dispose()


def test_activation_preserves_positions_risk_and_excludes_old_epoch(store):
    rotation.initialize(store, "current", 0)
    old = {"id": "old", "strategy_epoch": 0, "strategy_choice": "current"}
    retained = {"simple_positions": {"SOLUSDT": {**old, "stop_price": 97.5}},
                "risk_lock": {"reason": "daily_loss"}, "manual_lock": True,
                "consecutive_losses": 3, "anchors": {"equity": 500}}
    for k, v in retained.items():
        store.put(k, v)
    rotation.activate_streak(store, 1)
    rotation.closed(store, old, -1, 2)
    assert rotation.parameters(store)["losses"] == 0
    assert {k: store.get(k) for k in retained} == retained
    with pytest.raises(SafeError, match="locally managed"):
        rotation.configure(store, "auto", 3)


def test_transaction_rollback_and_nonfinite_results(store):
    rotation.activate_streak(store, 1)
    trade = settle_result(store, "first", -1)
    before = rotation.selection(store)
    with pytest.raises(RuntimeError), store.transaction() as conn:
        rotation.closed(store, {**trade, "id": "rollback"}, -1, 10, conn)
        raise RuntimeError()
    assert rotation.selection(store) == before
    for pnl in (float("nan"), float("inf")):
        with pytest.raises(SafeError):
            rotation.closed(store, {**trade, "id": "invalid"}, pnl, 10)
    assert rotation.selection(store) == before


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("wins,losses", [(0, 0), (1, 0), (2, 0), (3, 0), (0, 1), (0, 2)])
@pytest.mark.parametrize("opposite", [False, True])
async def test_live_dispatch_exact_inverse_and_immutable_brackets(settings, store, filters, side, wins, losses, opposite):
    engine, sim, adapter, _, now = await setup_engine(
        settings, store, filters, **options(), simple_momentum_entry_policy="confirmed_short")
    try:
        rotation.activate_streak(store, now)
        if opposite:
            for i in range(3):
                settle_result(store, f"invert{i}", -1)
        for i in range(wins):
            settle_result(store, f"w{i}", 1)
        for i in range(losses):
            settle_result(store, f"l{i}", -1)
        params = rotation.parameters(store)
        interval = 300000 if params["timeframe"] == "5m" else 900000
        boundary = now // interval * interval
        obs = early(side, open=100-side*.2, close=100, fast=100-side*.5, slow=100-side*1.5,
                    timestamp_ms=boundary, entry_timeframe=params["timeframe"],
                    momentum_trend_started_ms=boundary-2*interval)
        store.put("simple_pool", ["SOLUSDT"])
        store.put("simple_selected", ["SOLUSDT"])
        store.put("simple_minute:SOLUSDT", obs)
        store.put("simple_candidate_ranking", {"asof_ms": boundary, "config_hash": engine.s.config_hash,
                  "pool_hash": pool_hash(["SOLUSDT"]), "selected": ["SOLUSDT"],
                  "entry_timeframe": rotation.timeframe(store, engine.s),
                  "streak_revision": rotation.parameters(store)["revision"]})
        await engine.enter("SOLUSDT", obs, now)
        p = engine.positions()["SOLUSDT"]
        assert p["side"] == (-side if opposite else side)
        assert p["decision_side"] == side
        assert p["roe_target"] == params["profit_roe"]
        assert p["stop_roe"] == params["stop_roe"]
        for profit, field, mapping in [(True, "take_profit_price", "live_takes"), (False, "stop_price", "live_stops")]:
            expected = position_trigger(Decimal(str(p["entry_price"])), p["side"], filters["SOLUSDT"].tick, p, profit=profit)
            assert Decimal(str(p[field])) == expected
            assert Decimal(store.get(mapping)["SOLUSDT"]["trigger"]) == expected
        assert reason(p, p["take_profit_price"]).startswith("fixed +")
        settle_result(store, "unrelated-result", -1)
        restored = Engine(engine.s, store, filters, broker=engine.broker)
        await restored.restore()
        assert restored.positions()["SOLUSDT"]["stop_price"] == p["stop_price"]
        assert restored.positions()["SOLUSDT"]["take_profit_price"] == p["take_profit_price"]
        # An already authorized order becomes unusable after a new outcome.
        import json

        from sqlalchemy import text

        from paperfrog.domain.types import Order
        with store.engine.connect() as conn:
            raw = conn.execute(text("SELECT intent FROM orders LIMIT 1")).scalar_one()
        with pytest.raises(EntryVeto):
            rotation.require_order(store, Order.model_validate(json.loads(raw)))
    finally:
        await adapter.close()


def test_fifteen_minute_frame_uses_completed_candles_and_same_hour_age_limit(store, settings):
    from paperfrog.simple.strategy import EARLY_MOMENTUM_RULES
    boundary = 300 * 900000
    raw = tape(boundary)
    for i, item in enumerate(raw):
        item[0] = boundary - (len(raw)-i) * 900000
        item[6] = item[0]+900000-1
    context = slow_context(boundary)
    data = frame(raw, context, boundary, "15m", early_momentum=True)
    assert data.iloc[-1].timestamp_ms == boundary
    assert data.iloc[-1].entry_timeframe == "15m"
    assert "return_15m" in data and "return_45m" in data
    rotation.activate_streak(store, 1)
    rules = policy_rules(EARLY_MOMENTUM_RULES, store)
    fresh = early(entry_timeframe="15m", timestamp_ms=boundary, momentum_trend_started_ms=boundary-2*900000)
    assert classify(fresh, .001, rules)[1] == 1
    stale = {**fresh, "momentum_trend_age_bars": 5, "momentum_trend_started_ms": boundary-5*900000}
    assert classify(stale, .001, rules)[1] == 0


@pytest.mark.parametrize("damage", [{"win_streak": -1}, {"loss_streak": 3}, {"auto": True},
                                   {"streak_revision": None}, {"streak_policy": 2}])
def test_corrupt_persisted_controller_fails_closed(store, damage):
    rotation.activate_streak(store, 1)
    store.put(rotation.KEY, {**rotation.selection(store), **damage})
    with pytest.raises(SafeError, match="invalid saved"):
        rotation.initialize(store, "base", 2)


async def test_service_really_fetches_fifteen_minute_candles_and_discards_changed_scan(settings, monkeypatch):
    from unittest.mock import AsyncMock

    from paperfrog.simple.service import SimpleService
    boundary = 300 * 900000
    s = settings.model_copy(update=options())
    service = SimpleService(s)
    service.now_ms = lambda: boundary + 1000
    rotation.activate_streak(service.store, 1)
    service.context_rows = {symbol: slow_context(boundary) for symbol in ("BTCUSDT", "SOLUSDT")}
    service.store.put("simple_pool", ["SOLUSDT"])
    raw = tape(boundary, known_start=False)
    for i, item in enumerate(raw):
        item[0] = boundary - (len(raw)-i) * 900000
        item[6] = item[0]+900000-1
    service.exchange.request = AsyncMock(return_value=raw)
    try:
        assert await service.refresh_minutes(boundary)
        params = service.exchange.request.call_args.args[2]
        assert params["interval"] == "15m" and params["limit"] == 144
        assert params["endTime"] == boundary - 1
        assert service.store.get("simple_candidate_ranking")["pool_size"] == 1
        assert service.store.get("simple_minute:SOLUSDT")["entry_timeframe"] == "15m"
        assert service.scan_complete
        original = service.entry_frame
        async def changed(*args):
            data = await original(*args)
            settle_result(service.store, "while-awaiting-data", 1)
            return data
        monkeypatch.setattr(service, "entry_frame", changed)
        assert not await service.refresh_minutes(boundary)
    finally:
        await service.exchange.close()
        service.store.engine.dispose()


async def test_full_close_counts_net_result_once_and_telegram_is_concise(settings, store, filters):
    from test_telegram_events import worker
    engine, sim, adapter, _, now = await setup_engine(settings, store, filters, **options())
    rotation.activate_streak(store, now)
    boundary = now // 900000 * 900000
    obs = early(1, open=99.8, close=100, fast=99.5, slow=98.5, timestamp_ms=boundary,
                entry_timeframe="15m", momentum_trend_started_ms=boundary-1800000)
    store.put("simple_pool", ["SOLUSDT"])
    store.put("simple_selected", ["SOLUSDT"])
    store.put("simple_minute:SOLUSDT", obs)
    store.put("simple_candidate_ranking", {"asof_ms": boundary, "config_hash": engine.s.config_hash,
              "pool_hash": pool_hash(["SOLUSDT"]), "selected": ["SOLUSDT"],
                  "entry_timeframe": rotation.timeframe(store, engine.s),
                  "streak_revision": rotation.parameters(store)["revision"]})
    events, sender = worker(engine.s, store)
    try:
        await events.deliver()
        await engine.enter("SOLUSDT", obs, now)
        await events.deliver()
        assert any("CURRENT · STREAK · 15m" in x["text"] and "TP +5% / SL -5%" in x["text"] for x in sender.sent)
        # Flat prices plus actual simulated fees create a net loss, not a win.
        await engine.close("SOLUSDT", now, "test completed close")
        assert rotation.parameters(store)["losses"] == 1
        assert rotation.parameters(store)["wins"] == 0
        await engine.restore()
        assert rotation.parameters(store)["losses"] == 1
        await events.deliver()
        assert sum("closed · loss" in x["text"] for x in sender.sent) == 1
        assert not any("reducing position" in x["text"] for x in sender.sent)
        assert events.render("simple_process_started", {}) is None
    finally:
        await adapter.close()


async def test_research_supplies_real_slow_context_for_fifteen_minute_entry_scan(settings, monkeypatch):
    import asyncio
    import math
    from unittest.mock import AsyncMock

    from test_simple import source

    import paperfrog.simple.service as module
    boundary = 300 * 900000
    service = module.SimpleService(settings.model_copy(update=options()))
    service.now_ms = lambda: boundary + 1000
    rotation.activate_streak(service.store, 1)
    service.exchange.sync_time = AsyncMock()
    monkeypatch.setattr(module, "scan", AsyncMock(return_value={"symbols": ["SOLUSDT"]}))
    raw = source(288)
    raw["timestamp_ms"] += boundary-int(raw.iloc[-1].timestamp_ms)
    monkeypatch.setattr(module, "candles", AsyncMock(return_value=raw))
    audit = service.store.audit
    def finish(kind, payload, *args):
        audit(kind, payload, *args)
        if kind in {"simple_selection", "simple_risk_context"}:
            service.stop.set()
    monkeypatch.setattr(service.store, "audit", finish)
    try:
        await asyncio.wait_for(service.research(), timeout=10)
        ctx = service.context_rows["SOLUSDT"]
        assert all(math.isfinite(ctx[k]) for k in ("return_2h", "return_12h", "atr", "atr_fraction"))
        assert ctx["timestamp_ms"] == boundary
        assert service.selected == [] and not service.scan_complete
        entry_raw = tape(boundary, known_start=False)
        for i, item in enumerate(entry_raw):
            item[0] = boundary - (len(entry_raw)-i) * 900000
            item[6] = item[0]+900000-1
        service.exchange.request = AsyncMock(return_value=entry_raw)
        assert await service.refresh_minutes(boundary)
        assert service.store.get("simple_candidate_ranking")["usable_size"] == 1
    finally:
        await service.exchange.close()
        service.store.engine.dispose()


@pytest.mark.parametrize("pnl", [1, -1])
async def test_timeout_recovers_captured_brackets_after_controller_changes(settings, store, filters, monkeypatch, pnl):
    engine, sim, adapter, _, now = await setup_engine(settings, store, filters, **options())
    try:
        rotation.activate_streak(store, now)
        for i in range(2):
            settle_result(store, f"result-{i}", pnl)
        captured = rotation.parameters(store)
        boundary = now // 900000 * 900000
        obs = early(1, open=99.8, close=100, fast=99.5, slow=98.5, timestamp_ms=boundary,
                    entry_timeframe="15m", momentum_trend_started_ms=boundary-1800000)
        store.put("simple_pool", ["SOLUSDT"])
        store.put("simple_selected", ["SOLUSDT"])
        store.put("simple_minute:SOLUSDT", obs)
        store.put("simple_candidate_ranking", {"asof_ms": boundary, "config_hash": engine.s.config_hash,
                  "pool_hash": pool_hash(["SOLUSDT"]), "selected": ["SOLUSDT"],
                  "entry_timeframe": "15m", "streak_revision": captured["revision"]})
        original = engine.broker.query_stop
        async def timeout(key, **kw):
            if kw.get("mapping") == "live_takes":
                raise TimeoutError()
            return await original(key, **kw)
        monkeypatch.setattr(engine.broker, "query_stop", timeout)
        with pytest.raises(TimeoutError):
            await engine.enter("SOLUSDT", obs, now)
        assert store.get("simple_pending") and sim.quantity > 0
        settle_result(store, "while-pending", pnl)  # switch interval or direction
        monkeypatch.setattr(engine.broker, "query_stop", original)
        count = sum(method == "POST" for method, _ in sim.calls)
        assert await engine.recover_execution()
        assert sum(method == "POST" for method, _ in sim.calls) == count
        p = engine.positions()["SOLUSDT"]
        assert p["roe_target"] == captured["profit_roe"]
        assert p["stop_roe"] == captured["stop_roe"]
        assert p["side"] == 1 and p["entry_timeframe"] == "15m"
    finally:
        await adapter.close()
