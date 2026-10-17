import pytest
from test_minute_entries import minute_row, minute_settings, slow_context
from test_simple import engine_fixture, quote

from paperfrog.simple.engine import Engine
from paperfrog.simple.strategy import BAR_MS


async def opened(settings, store, filters, timeframe, side=1):
    s = minute_settings(settings, simple_entry_timeframe=timeframe)
    engine = await engine_fixture(s, store, filters)
    boundary = 200 * BAR_MS
    obs = minute_row(boundary, side=side, timeframe=timeframe)
    store.put("simple_minute:SOLUSDT", obs)
    await engine.quote(quote(now_ms=boundary + 1000), boundary + 1000)
    await engine.enter("SOLUSDT", obs, boundary + 1000)
    assert "SOLUSDT" in engine.positions()
    restored = Engine(s, store, filters)
    await restored.restore()
    await restored.quote(quote(now_ms=boundary + 2000), boundary + 2000)
    return restored, boundary + 2000


@pytest.mark.parametrize("timeframe", ["1m", "5m"])
@pytest.mark.parametrize("side", [1, -1])
async def test_restart_waits_for_original_timeframe_before_reversal_exit(
    settings, store, filters, timeframe, side
):
    engine, now = await opened(settings, store, filters, timeframe, side)
    slow = slow_context(200 * BAR_MS, side=-side)
    await engine.monitor(now, {"SOLUSDT": slow}, awaiting_initial_scan=True)
    assert "SOLUSDT" in engine.positions()
    matching = minute_row(200 * BAR_MS, side=side, timeframe=timeframe)
    await engine.monitor(now, {"SOLUSDT": {**matching, "risk_context": slow}})
    assert "SOLUSDT" in engine.positions()
    await engine.monitor(now, {"SOLUSDT": {**matching, "trend_atr": -side}})
    assert not engine.positions()
    assert store.get("simple_closed")[-1]["reason"] == "recent trend reversed against the position"


@pytest.mark.parametrize("timeframe", ["1m", "5m"])
async def test_wrong_timeframe_after_startup_uses_missing_data_exit(
    settings, store, filters, timeframe
):
    engine, now = await opened(settings, store, filters, timeframe)
    await engine.monitor(now, {"SOLUSDT": slow_context(200 * BAR_MS, side=-1)})
    assert not engine.positions()
    assert store.get("simple_closed")[-1]["reason"] == "closed-candle monitoring data unavailable"


@pytest.mark.parametrize("trigger", ["stop", "holding_limit", "risk", "crisis"])
async def test_startup_timeframe_guard_preserves_independent_exits(
    settings, store, filters, trigger
):
    engine, now = await opened(settings, store, filters, "5m")
    positions = engine.positions()
    if trigger == "stop":
        positions["SOLUSDT"]["stop_price"] = 101
    elif trigger == "holding_limit":
        positions["SOLUSDT"]["opened_ms"] = now - 4 * 3600_000
    elif trigger == "risk":
        store.put("risk_lock", {"reason": "test loss limit"})
    store.put("simple_positions", positions)
    await engine.monitor(
        now,
        {"SOLUSDT": slow_context(200 * BAR_MS)},
        crisis=trigger == "crisis",
        awaiting_initial_scan=True,
    )
    assert not engine.positions()
    reason = store.get("simple_closed")[-1]["reason"]
    assert (
        reason
        == {
            "stop": "protective stop or volatility trailing stop reached",
            "holding_limit": "4-hour holding limit reached",
            "risk": "portfolio loss/risk limit reached",
            "crisis": "BTC market crisis detected",
        }[trigger]
    )
