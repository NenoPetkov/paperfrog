import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_active_auto import close_reservation, probe_proposal, select_probe
from test_five_minute import BOUNDARY, five_settings
from test_historical_learning import NOW, market
from test_per_trade_routing import stable
from test_simple import engine_fixture, quote

from paperfrog.domain.types import SafeError
from paperfrog.risk import activity
from paperfrog.risk.engine import RiskEngine
from paperfrog.simple import learning, rotation
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS


def four_settings(settings, **updates):
    return settings.model_copy(update={"max_concurrent_strategy_trades": 4,
                                       "max_strategy_entries_per_day": 40, **updates})


def test_simultaneous_reservations_never_exceed_slots_or_consume_failed_budget(settings, store, health, now):
    risk = RiskEngine(four_settings(settings), store)
    def reserve(i):
        try:
            risk.reserve(probe_proposal(str(i), f"COIN{i}USDT"), health, now)
            return True
        except SafeError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(results) == 4
    assert len(store.get(activity.KEY)) == 4
    assert len([r for r in store.rows("entries") if not r["closed"]]) == 4


def test_aggregate_risk_still_caps_concurrent_probes(settings, store, health, now):
    risk = RiskEngine(four_settings(settings, max_open_risk=.002), store)
    for i in range(2):
        risk.reserve(replace(probe_proposal(str(i), f"COIN{i}USDT"), risk=8), health, now)
    with pytest.raises(SafeError, match="open risk cap"):
        risk.reserve(replace(probe_proposal("third", "OTHERUSDT"), risk=8), health, now)
    assert len(store.get(activity.KEY)) == 2


def test_lower_configured_position_limit_is_honored_at_dispatch(settings, store, health, now):
    risk = RiskEngine(settings.model_copy(update={"max_concurrent_strategy_trades": 1}), store)
    risk.reserve(probe_proposal(), health, now)
    assert activity.blocker(store, int(now.timestamp()*1000), max_positions=1)
    assert activity.blocker(store, int(now.timestamp()*1000), "probe", max_positions=1) is None
    with pytest.raises(SafeError, match="concurrent trade cap"):
        risk.reserve(probe_proposal("second", "OTHERUSDT"), health, now)


def test_validated_entry_can_share_portfolio_with_probe(settings, store, health, now):
    risk = RiskEngine(settings, store)
    risk.reserve(probe_proposal(), health, now)
    risk.reserve(replace(probe_proposal("validated", "ETHUSDT"), activity_probe=False), health, now)
    assert len([r for r in store.rows("entries") if not r["closed"]]) == 2
    assert len(store.get(activity.KEY)) == 1
    close_reservation(store, "validated")
    store.put("risk_lock", {"reason": "daily_loss"})
    with pytest.raises(SafeError, match="risk lock"):
        risk.reserve(probe_proposal("locked", "OTHERUSDT"), health, now)


def test_active_timeframe_change_does_not_pause_entries_during_confirmation_or_dwell(settings, store):
    s = settings.model_copy(update={"simple_market_trend_enabled": True})
    assert select_probe(store, s)["allowed"]
    first = learning.review(store, s, market(s, NOW + BAR_MS, regime="MIXED"), NOW + BAR_MS)
    assert first["allowed"] and first["candidate"][1] == "5m" and first["timeframe"] == "15m"
    assert "replacement awaits" in first["reason"]
    # Different mixed label, same requested candle size: count this confirmation.
    next_market = market(s, NOW + 2*BAR_MS, regime="MIXED", median_return_1h=-.001)
    second = learning.review(store, s, next_market, NOW + 2*BAR_MS)
    assert second["allowed"] and second["confirmations"] == 2
    # Minimum dwell still prevents candle-size thrashing, not trading itself.
    assert second["timeframe"] == "15m"
    for i in (3, 4):
        result = learning.review(store, s, {**next_market, "timestamp_ms": NOW+i*BAR_MS}, NOW+i*BAR_MS)
        assert result["allowed"]
    assert result["timeframe"] == "5m"
    bad = learning.review(store, s, None, NOW+5*BAR_MS)
    assert not bad["allowed"] and learning.veto(store, s, NOW+5*BAR_MS)


async def test_two_paper_entries_keep_individual_strategy_choices(settings, store, filters):
    from test_market_trend import breadth
    from test_opposite_confirmation import confirmed_row
    s = four_settings(settings, simple_market_trend_enabled=True)
    engine = await engine_fixture(s, store, filters)
    select_probe(store, s)
    breadth(store, s, NOW)
    research = store.get("simple_learning_research")
    research["symbols"] = ["SOLUSDT", "ETHUSDT"]
    store.put("simple_learning_research", research)
    store.put("simple_selected", ["SOLUSDT", "ETHUSDT"])
    for symbol in ("SOLUSDT", "ETHUSDT"):
        await engine.quote(quote(symbol, now_ms=NOW), NOW)
    await engine.enter("SOLUSDT", stable(), NOW)
    await engine.enter("ETHUSDT", {**stable(), **confirmed_row(NOW)}, NOW)
    positions = engine.positions()
    assert len(positions) == 2
    assert positions["SOLUSDT"]["strategy_choice"] == "current"
    assert positions["ETHUSDT"]["strategy_choice"] == "opposite"
    assert positions["SOLUSDT"]["strategy_epoch"] == positions["ETHUSDT"]["strategy_epoch"]
    assert len(store.get(activity.KEY)) == 2
    assert rotation.selection(store)["auto"]


async def test_minute_research_retries_same_bar_when_context_or_epoch_changes(settings):
    service = SimpleService(five_settings(settings))
    service.context_rows = {"BTCUSDT": {"timestamp_ms": BOUNDARY - BAR_MS}}
    service.now_ms = lambda: BOUNDARY + 1000
    calls = []
    try:
        assert await service.refresh_minutes(BOUNDARY) is False
        async def refresh(boundary):
            calls.append(boundary)
            if len(calls) == 1:
                return False
            service.stop.set()
            return True
        service.refresh_minutes = refresh
        await asyncio.wait_for(service.minute_research(), timeout=3)
        assert calls == [BOUNDARY, BOUNDARY]
        assert not any(r["kind"] == "simple_minute_degraded" for r in service.store.rows("audit"))
    finally:
        await service.exchange.close()
        service.store.engine.dispose()
