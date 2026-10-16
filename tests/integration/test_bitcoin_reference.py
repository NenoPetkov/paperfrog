import asyncio
import time
from contextlib import suppress
from decimal import Decimal
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
from test_adaptive import observation, permit, plan
from test_simple import engine_fixture, quote, row
from test_simple_live import setup_engine

from paperfrog.domain.types import Order, SafeError, State
from paperfrog.settings import Settings
from paperfrog.simple.backtest import replay
from paperfrog.simple.bitcoin import (
    BTC,
    MINUTE_BARS,
    MINUTE_MS,
    closed_reference,
    entry_gate,
    fast_reference,
    pair_features,
)
from paperfrog.simple.data import scan
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS, choose, execution_rules, indicators


def btc_settings(settings, **changes):
    return Settings(
        _env_file=None, **{**settings.public(), "simple_btc_reference_enabled": True, **changes}
    )


def histories(n=320, side=1, seed=24):
    rng = np.random.default_rng(seed)
    b = np.zeros(n)
    b[0] = 0.0008
    for i in range(1, n):
        b[i] = 0.0004 + 0.6 * b[i - 1] + rng.normal(0, 0.0008)
    # Explicit synthetic BTC-led fixture, not a market-performance claim.
    a = 0.9 * np.roll(b, 1) + 0.3 * b + rng.normal(0, 0.00006, n)
    b[-1] = 0.002

    def frame(returns):
        prices = 100 * np.exp(np.cumsum(side * returns))
        opened = np.r_[prices[0], prices[:-1]]
        return pd.DataFrame(
            {
                "timestamp_ms": (np.arange(n) + 1) * BAR_MS,
                "open": opened,
                "high": np.maximum(opened, prices) * 1.002,
                "low": np.minimum(opened, prices) * 0.998,
                "close": prices,
                "quote_volume": 1_000_000.0,
                "taker_buy_quote": 700_000.0 if side > 0 else 300_000.0,
            }
        )

    return frame(a), frame(b)


def minute_rows(boundary, side=1):
    prices = 100 * np.exp(side * np.arange(MINUTE_BARS) * 0.0005)
    return [
        [
            boundary - (MINUTE_BARS - i) * MINUTE_MS,
            str(price),
            str(price * 1.001),
            str(price * 0.999),
            str(price),
            "100",
            boundary - (MINUTE_BARS - i - 1) * MINUTE_MS - 1,
            "100000",
            10,
            "70",
            "70000",
            "0",
        ]
        for i, price in enumerate(prices)
    ]


def context(store, s, asof_ms, side=1, now_ms=None):
    now_ms = asof_ms if now_ms is None else now_ms
    store.put(
        "simple_btc_closed",
        {"timestamp_ms": asof_ms, "direction": side, "config_hash": s.config_hash},
    )
    store.put(
        "simple_btc_fast",
        {
            **fast_reference(
                minute_rows(now_ms // MINUTE_MS * MINUTE_MS, side), now_ms // MINUTE_MS * MINUTE_MS
            ),
            "config_hash": s.config_hash,
        },
    )
    return {
        "btc_pair_valid": True,
        "btc_lag_valid": True,
        "btc_pair_asof_ms": asof_ms,
        "btc_expected_return_15m": side * 0.003,
        "btc_reference_score": 2.5,
    }


def test_reference_is_enabled_by_default_and_paper_stays_default():
    s = Settings(_env_file=None)
    assert s.simple_btc_reference_enabled
    assert s.simple_btc_direction_policy == "strict"
    assert s.trading_mode == "paper" and not s.live_trading_enabled


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("policy", ["strict", "neutral_allowed"])
@pytest.mark.parametrize("closed_direction", [-1, 0, 1])
@pytest.mark.parametrize("fast_direction", [-1, 0, 1])
def test_btc_direction_policy_handles_neutral_and_conflicting_timeframes(
    settings, store, side, policy, closed_direction, fast_direction
):
    s, asof = btc_settings(settings, simple_btc_direction_policy=policy), 200 * BAR_MS
    fields = context(store, s, asof, side)
    for key, direction in (
        ("simple_btc_closed", closed_direction),
        ("simple_btc_fast", fast_direction),
    ):
        value = store.get(key)
        value["direction"] = direction
        if key == "simple_btc_fast" and direction == 0:
            # Neutral BTC can include a small contrary minute without becoming a veto.
            value["return_1m"] = -side * value["atr_fraction"] * 0.75
        store.put(key, value)
    allowed = all(
        direction == side or (policy == "neutral_allowed" and direction == 0)
        for direction in (closed_direction, fast_direction)
    )
    assert (entry_gate(s, store, {**row(asof), **fields}, side, asof) is None) == allowed


@pytest.mark.parametrize("key", ["simple_btc_closed", "simple_btc_fast"])
@pytest.mark.parametrize("problem", ["missing", "old", "shock", "invalid_direction"])
def test_neutral_policy_preserves_btc_health_and_shock_vetoes(settings, store, key, problem):
    s = btc_settings(settings, simple_btc_direction_policy="neutral_allowed")
    asof = 200 * BAR_MS
    fields = context(store, s, asof)
    value = store.get(key)
    value["direction"] = 0
    if problem == "missing":
        value = None
    elif problem == "old":
        value["timestamp_ms"] = asof - BAR_MS
    elif problem == "shock":
        value["shock"] = True
    else:
        value["direction"] = None
    store.put(key, value)
    assert entry_gate(s, store, {**row(asof), **fields}, 1, asof)


async def test_scan_excludes_btc_before_the_hundred_coin_limit(settings, monkeypatch):
    s = btc_settings(settings)
    now_ms = int(time.time() * 1000)
    symbols = [BTC, *[f"ALT{i:03}USDT" for i in range(101)]]
    metadata = [
        {
            "symbol": symbol,
            "baseAsset": "BTC" if symbol == BTC else symbol[:-4],
            "underlyingType": "COIN",
            "onboardDate": 0,
        }
        for symbol in symbols
    ]
    tickers = [
        {"symbol": symbol, "quoteVolume": str(1e10 - i * 1e6), "closeTime": now_ms}
        for i, symbol in enumerate(symbols)
    ]
    adapter = AsyncMock()
    adapter.offset_ms = 0
    adapter.filters = dict.fromkeys(symbols)
    adapter.exchange_info.return_value = {"symbols": metadata}
    adapter.request.return_value = tickers
    snapshot = await scan(adapter, s)
    assert len(snapshot["symbols"]) == 100
    assert BTC not in snapshot["symbols"] and BTC not in snapshot["metadata"]
    assert snapshot["symbols"][-1] == "ALT099USDT"


def test_selection_never_reintroduces_btc_even_when_reference_filter_disabled(settings):
    assert choose({BTC: {**row(), "score": 1000}, "SOLUSDT": row()}, settings) == ["SOLUSDT"]


def test_btc_relationship_changes_ranking_without_changing_position_limits(settings):
    s = btc_settings(settings, simple_selected_count=1)
    rows = {
        "SOLUSDT": {**row(), "btc_pair_valid": True, "btc_reference_score": 1.4},
        "ETHUSDT": {**row(), "btc_pair_valid": True, "btc_reference_score": 2.5},
        "BADUSDT": {**row(), "score": 10000, "btc_pair_valid": False},
    }
    assert choose(rows, s) == ["ETHUSDT"]
    assert s.max_concurrent_strategy_trades == settings.max_concurrent_strategy_trades == 2


@pytest.mark.parametrize("side", [1, -1])
def test_lagged_predictor_fits_a_known_synthetic_relationship(side):
    alt, btc = histories(side=side)
    result = pair_features(alt, btc, int(alt.iloc[-1].timestamp_ms))
    assert result["btc_pair_valid"] is True
    assert result["btc_lag_valid"] is True
    assert result["btc_correlation"] >= 0.25 and result["btc_lag_correlation"] > 0.1
    assert result["btc_validation_skill"] > 0.01
    assert result["btc_lag_beta"] > 0
    assert side * result["btc_expected_return_15m"] > 0


def test_lagged_predictor_cannot_use_future_returns():
    alt, btc = histories(360)
    asof = 300 * BAR_MS
    original = pair_features(alt, btc, asof)
    alt.loc[alt.timestamp_ms > asof, "close"] *= 100
    btc.loc[btc.timestamp_ms > asof, "close"] *= 0.01
    assert pair_features(alt, btc, asof) == original
    assert (
        pair_features(alt[alt.timestamp_ms <= asof], btc[btc.timestamp_ms <= asof], asof)
        == original
    )


def test_weak_missing_or_negative_relationship_is_not_assumed_to_follow_btc():
    alt, btc = histories()
    asof = int(alt.iloc[-1].timestamp_ms)
    assert not pair_features(alt.iloc[-50:], btc, asof)["btc_pair_valid"]
    assert not pair_features(alt, btc.iloc[:-1], asof)["btc_pair_valid"]
    inverse = alt.copy()
    inverse["close"] = 1 / inverse.close
    assert not pair_features(inverse, btc, asof)["btc_pair_valid"]
    missing = btc.drop(index=200)
    assert not pair_features(alt, missing, asof)["btc_pair_valid"]


def test_immediate_correlation_is_not_mislabeled_as_a_predictive_lag():
    rng = np.random.default_rng(92)
    returns = rng.normal(0.0002, 0.001, 320)
    btc = pd.DataFrame(
        {"timestamp_ms": (np.arange(320) + 1) * BAR_MS, "close": 100 * np.exp(np.cumsum(returns))}
    )
    alt = btc.copy()
    alt["close"] = 50 * np.exp(np.cumsum(1.2 * returns + rng.normal(0, 0.0001, 320)))
    result = pair_features(alt, btc, 320 * BAR_MS)
    assert result["btc_pair_valid"] is True
    assert result["btc_lag_valid"] is False
    assert result["btc_expected_return_15m"] == 0


def test_no_supported_lag_still_requires_the_mandatory_btc_direction(settings, store):
    s, asof = btc_settings(settings), 200 * BAR_MS
    fields = context(store, s, asof)
    fields["btc_lag_valid"] = False
    fields["btc_expected_return_15m"] = 0
    assert entry_gate(s, store, {**row(), **fields}, 1, asof) is None
    assert entry_gate(s, store, {**row(), **fields}, -1, asof)


@pytest.mark.parametrize("side", [1, -1])
def test_closed_btc_regime_uses_both_trend_and_momentum(settings, side):
    _, raw = histories(side=side)
    rules = execution_rules(settings)
    result = closed_reference(indicators(raw, rules), rules)
    assert result["direction"] == side
    assert side * result["return_15m"] > 0 and side * result["return_1h"] > 0


@pytest.mark.parametrize("side", [1, -1])
def test_minute_reference_uses_closed_candles_only(side):
    boundary = 200 * BAR_MS
    raw = minute_rows(boundary, side)
    result = fast_reference(raw, boundary)
    forming = [boundary, "1", "100000", "0.001", "100000", "1", boundary + MINUTE_MS - 1]
    assert fast_reference([*raw, forming], boundary) == result
    assert result["direction"] == side and not result["shock"]


@pytest.mark.parametrize("problem", ["gap", "duplicate", "reordered", "nan", "bad_ohlc"])
def test_minute_reference_rejects_bad_data(problem):
    boundary = 200 * BAR_MS
    raw = minute_rows(boundary)
    if problem == "gap":
        raw.pop(50)
    elif problem == "duplicate":
        raw[50] = raw[49]
    elif problem == "reordered":
        raw[50], raw[51] = raw[51], raw[50]
    elif problem == "nan":
        raw[-1][4] = "nan"
    else:
        raw[-1][2] = "0.01"
    with pytest.raises(SafeError):
        fast_reference(raw, boundary)


@pytest.mark.parametrize(
    "problem", ["missing", "old", "future", "shock", "opposite", "different_config"]
)
def test_reference_gate_fails_closed(settings, store, problem):
    s, now_ms = btc_settings(settings), 200 * BAR_MS
    fields = context(store, s, now_ms)
    assert entry_gate(s, store, {**row(), **fields}, 1, now_ms) is None
    fast = store.get("simple_btc_fast")
    if problem == "missing":
        fast = None
    elif problem == "old":
        fast["timestamp_ms"] = now_ms - 2 * MINUTE_MS
    elif problem == "future":
        fast["timestamp_ms"] = now_ms + 1
    elif problem == "shock":
        fast["shock"] = True
    elif problem == "opposite":
        fast["direction"] = -1
    else:
        fast["config_hash"] = "different"
    store.put("simple_btc_fast", fast)
    assert entry_gate(s, store, {**row(), **fields}, 1, now_ms)


async def test_engine_forbids_btc_even_with_old_or_injected_selection(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    store.put("simple_selected", [BTC])
    await engine.quote(quote(BTC), 200 * BAR_MS)
    with pytest.raises(SafeError, match="reference-only"):
        await engine.enter(BTC, row(), 200 * BAR_MS)
    assert not store.rows("entries")


async def test_ai_cannot_override_btc_filter(settings, store, filters):
    s, asof = btc_settings(settings, simple_ai_enabled=True), 200 * BAR_MS
    engine = await engine_fixture(s, store, filters)
    permit(s, store, plan(asof))
    fields = context(store, s, asof, side=-1)
    await engine.enter("SOLUSDT", {**observation(asof), **fields}, asof)
    assert not engine.positions() and not store.rows("entries")
    assert "BTC" in store.get("last_signal")["reason"]


async def test_paper_btc_gate_has_no_openai_dependency_and_keeps_stops(settings, store, filters):
    s, asof = btc_settings(settings), 200 * BAR_MS
    engine = await engine_fixture(s, store, filters)
    fields = context(store, s, asof)
    await engine.enter("SOLUSDT", {**row(asof), **fields}, asof)
    assert engine.positions() and not s.simple_ai_enabled
    assert store.rows("entries")[0]["risk"] <= s.paper_equity * s.risk_per_trade
    store.put("simple_btc_fast", None)
    await engine.quote(quote(price=90, now_ms=asof + 1000), asof + 1000)
    assert not engine.positions() and await engine.coordinator.reconcile()


async def test_minute_task_refreshes_without_any_llm_call(settings):
    s = btc_settings(settings)
    service = SimpleService(s)
    boundary = 200 * BAR_MS
    service.now_ms = lambda: boundary

    async def request(method, path, params):
        assert method == "GET" and path == "/fapi/v1/klines"
        assert params["symbol"] == BTC and params["interval"] == "1m"
        service.stop.set()
        return minute_rows(boundary)

    service.exchange.request = request
    try:
        await service.bitcoin_reference()
        assert service.store.get("simple_btc_fast")["timestamp_ms"] == boundary
        assert service.advisor is None
    finally:
        await service.exchange.close()
        service.store.engine.dispose()


async def test_research_fetches_btc_separately_from_altcoin_candidates(settings, monkeypatch):
    import paperfrog.simple.service as service_module

    s = btc_settings(settings)
    service = SimpleService(s)
    alt, btc = histories(288)
    boundary = 288 * BAR_MS
    service.now_ms = lambda: boundary
    service.exchange.sync_time = AsyncMock()
    monkeypatch.setattr(service_module, "scan", AsyncMock(return_value={"symbols": ["SOLUSDT"]}))
    called = []

    async def candles(adapter, lake, symbol, start_ms, end_ms):
        called.append(symbol)
        return btc if symbol == BTC else alt

    monkeypatch.setattr(service_module, "candles", candles)
    pending = asyncio.create_task(service.research())
    try:
        async with asyncio.timeout(3):
            while service.store.get("simple_scan_at_ms") is None:
                await asyncio.sleep(0.01)
        assert BTC in called and "SOLUSDT" in called
        assert BTC not in service.store.get("simple_pool")
        assert BTC not in service.selected
        assert service.store.get("simple_btc_closed")["timestamp_ms"] == boundary
        assert service.store.get("simple_btc_pairs")["SOLUSDT"]["btc_pair_valid"]
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await service.exchange.close()
        service.store.engine.dispose()


async def test_live_gateway_checks_btc_independently_and_preserves_reductions(
    settings, store, filters
):
    engine, sim, adapter, obs, now_ms = await setup_engine(
        settings, store, filters, simple_btc_reference_enabled=True
    )
    fields = context(store, engine.s, obs["timestamp_ms"], now_ms=now_ms)
    await engine.enter("SOLUSDT", {**obs, **fields}, now_ms)
    assert sim.quantity > 0 and sim.stops
    store.put("simple_btc_fast", None)
    await engine.close("SOLUSDT", now_ms + 1000, "existing stop/exit rules remain independent")
    assert not sim.quantity and await engine.coordinator.reconcile()
    await adapter.close()


@pytest.mark.parametrize("policy", ["strict", "neutral_allowed"])
async def test_btc_reversal_before_dispatch_prevents_the_entry(settings, store, filters, policy):
    engine, sim, adapter, obs, now_ms = await setup_engine(
        settings,
        store,
        filters,
        simple_btc_reference_enabled=True,
        simple_btc_direction_policy=policy,
    )
    fields = context(store, engine.s, obs["timestamp_ms"], now_ms=now_ms)
    if policy == "neutral_allowed":
        fast = store.get("simple_btc_fast")
        fast["direction"] = 0
        store.put("simple_btc_fast", fast)
    store.put("consecutive_losses", 2)
    anchors_before = store.get("anchors")
    original = engine.broker.refresh

    async def reverse_during_refresh():
        await original()
        value = store.get("simple_btc_fast")
        value["direction"] = -1
        store.put("simple_btc_fast", value)

    engine.broker.refresh = reverse_during_refresh
    with pytest.raises(SafeError, match="BTC"):
        await engine.enter("SOLUSDT", {**obs, **fields}, now_ms)
    assert not sim.quantity
    assert not any(method == "POST" and path.endswith("/order") for method, path in sim.calls)
    assert store.get("state") == State.RUNNING
    assert store.get("reconciled") is True
    assert not store.rows("entries") and not store.unresolved_orders()
    assert not store.get("simple_pending")
    assert store.rows("orders")[0]["state"] == "REJECTED"
    assert store.get("consecutive_losses") == 2
    assert store.get("anchors") == anchors_before
    assert not store.get("risk_lock")
    assert await engine.coordinator.reconcile()
    await adapter.close()


async def test_active_ai_entry_can_trade_with_neutral_btc_and_keeps_protection(
    settings, store, filters
):
    engine, sim, adapter, obs, now_ms = await setup_engine(
        settings,
        store,
        filters,
        simple_ai_enabled=True,
        simple_entry_profile="active",
        simple_btc_reference_enabled=True,
        simple_btc_direction_policy="neutral_allowed",
    )
    try:
        fields = context(store, engine.s, obs["timestamp_ms"], now_ms=now_ms)
        for key in ("simple_btc_closed", "simple_btc_fast"):
            value = store.get(key)
            value["direction"] = 0
            store.put(key, value)
        permit(engine.s, store, plan(obs["timestamp_ms"]))
        await engine.enter("SOLUSDT", {**observation(obs["timestamp_ms"]), **obs, **fields}, now_ms)
        assert sim.quantity > 0 and sim.stops
        assert len(store.rows("entries")) == 1
        assert (
            store.rows("entries")[0]["risk"]
            <= float(engine.broker.equity()) * engine.s.risk_per_trade
        )
        store.put("simple_ai_active", None)
        store.put("simple_btc_fast", None)
        await engine.close("SOLUSDT", now_ms + 1000, "exit remains independent of entry policy")
        assert not sim.quantity and await engine.coordinator.reconcile()
    finally:
        await adapter.close()


async def test_direct_btc_entry_is_forbidden_at_the_live_gateway(settings, store, filters):
    engine, sim, adapter, _, _ = await setup_engine(settings, store, filters)
    with pytest.raises(SafeError, match="reference-only"):
        await engine.broker.submit(
            Order(
                client_id="pf-btc-test",
                symbol=BTC,
                side=1,
                quantity=Decimal(".01"),
                limit_price=Decimal("100"),
            )
        )
    assert not sim.quantity
    await adapter.close()


async def test_historical_replay_cannot_invent_minute_reference_evidence(settings, tmp_path):
    with pytest.raises(SafeError, match="recorded minute context"):
        await replay({}, {}, btc_settings(settings), BAR_MS, 2 * BAR_MS, tmp_path / "fake")
    assert not (tmp_path / "fake").exists()
