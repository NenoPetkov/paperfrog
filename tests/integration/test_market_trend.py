from unittest.mock import AsyncMock

import numpy as np
import pytest
from pandas.testing import assert_frame_equal
from test_adaptive import Advisor, fake_client, permit, plan
from test_eight_minute import BOUNDARY, eight_settings, market
from test_simple import engine_fixture, source

from paperfrog.domain.types import Order, SafeError
from paperfrog.settings import Settings
from paperfrog.simple import rotation
from paperfrog.simple.market_trend import (
    DAY_FIELDS,
    day_context,
    entry_gate,
    market_context,
    require_order,
)
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS, indicators

POOL = [f"COIN{i:02}USDT" for i in range(20)]


def day(boundary=BOUNDARY, side=1):
    return {
        "day_context_ms": boundary // BAR_MS * BAR_MS,
        **{key: side * 0.02 for key in DAY_FIELDS},
    }


def breadth(store, s, boundary=BOUNDARY, side=1):
    at = boundary // BAR_MS * BAR_MS
    rows = {symbol: day(boundary, side) for symbol in [*POOL, "BTCUSDT", "ETHUSDT"]}
    record = {**market_context(rows, POOL, at), "config_hash": s.config_hash}
    store.put("simple_market_trend", record)
    return record


def mixed_breadth(store, s, boundary=BOUNDARY, side=1):
    record = breadth(store, s, boundary, side)
    record["eth_return_1h"] *= -1
    record["regime"] = "MIXED"
    store.put("simple_market_trend", record)
    return record


def guarded(settings, **changes):
    return eight_settings(settings, simple_market_trend_enabled=True, **changes)


def test_default_guard_is_on_and_paper_is_preserved():
    s = Settings(_env_file=None)
    assert s.simple_market_trend_enabled
    assert s.trading_mode == "paper" and not s.live_trading_enabled
    assert s.simple_parameters()["simple_market_trend_enabled"]


@pytest.mark.parametrize("side", [1, -1])
def test_exact_day_history_regression_mean_and_future_exclusion(side):
    raw = source(side=side)
    boundary = int(raw.iloc[250].timestamp_ms)
    original = raw.copy(deep=True)
    values = day_context(raw, boundary)
    assert values["day_context_ms"] == boundary
    assert values["day_return_4h"] == pytest.approx(np.exp(side * 16 * 0.0025) - 1)
    assert values["day_return_1h"] == pytest.approx(np.exp(side * 4 * 0.0025) - 1)
    assert all(side * values[key] > 0 for key in DAY_FIELDS)
    raw.loc[251:, "close"] *= 1e9
    assert day_context(raw, boundary) == values
    assert_frame_equal(original.iloc[:251], raw.iloc[:251])


def test_four_hour_features_ignore_an_older_falling_day():
    raw = source(150)
    raw.loc[raw.index[-49:], "close"] = np.r_[
        np.linspace(200, 80, 33)[:-1], np.linspace(80, 100, 17)
    ]
    at = int(raw.iloc[-1].timestamp_ms)
    assert raw.iloc[-1].close / raw.iloc[-49].close - 1 < 0
    features = day_context(raw, at)
    assert features["day_return_4h"] == pytest.approx(100 / raw.iloc[-17].close - 1)
    assert all(features[key] > 0 for key in DAY_FIELDS)
    raw.loc[raw.index[:-17], "close"] *= 3
    assert day_context(raw, at) == features
    assert "day_return_24h" not in features


def test_ai_windows_are_exact_and_old_schema_permissions_are_revoked(settings, store):
    from pydantic import ValidationError

    from paperfrog.simple.adaptive import Condition, active_plan, enrich

    raw = source(150)
    enriched = enrich(indicators(raw))
    assert enriched.iloc[-1].return_12h == pytest.approx(np.exp(48 * 0.0025) - 1)
    assert enriched.iloc[-1].return_2h == pytest.approx(np.exp(8 * 0.0025) - 1)
    for old in ["return_24h", "return_4h"]:
        with pytest.raises(ValidationError):
            Condition(feature=old, operator="ge", threshold=0)
    s = guarded(settings, simple_ai_enabled=True)
    permit(s, store, plan(BOUNDARY))
    assert active_plan(s, store, BOUNDARY, BOUNDARY + 1000) is not None
    record = store.get("simple_ai_active")
    record.pop("schema_version")
    store.put("simple_ai_active", record)
    assert active_plan(s, store, BOUNDARY, BOUNDARY + 1000) is None


def test_live_authorization_includes_the_market_gate_source(settings, monkeypatch):
    from pathlib import Path

    from paperfrog.simple.authorization import strategy_hash

    previous = strategy_hash(settings)
    original = Path.read_bytes

    def changed(path):
        result = original(path)
        return result + b"\n# changed market veto\n" if path.name == "market_trend.py" else result

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert strategy_hash(settings) != previous


@pytest.mark.parametrize("damage", ["gap", "duplicate", "short", "nan", "future", "unaligned"])
def test_incomplete_or_invalid_day_fails_closed(damage):
    raw = source(120)
    boundary = int(raw.iloc[-1].timestamp_ms)
    if damage == "gap":
        raw = raw.drop(raw.index[-12])
    elif damage == "duplicate":
        raw.loc[raw.index[-12], "timestamp_ms"] = raw.iloc[-13].timestamp_ms
    elif damage == "short":
        raw = raw.tail(16)
    elif damage == "nan":
        raw.loc[raw.index[-12], "close"] = float("nan")
    elif damage == "future":
        boundary += BAR_MS
    else:
        boundary += 1
    with pytest.raises(SafeError):
        day_context(raw, boundary)


def test_breadth_uses_full_liquidity_pool_and_benchmarks_not_selected_gainers():
    at = BOUNDARY // BAR_MS * BAR_MS
    rows = {symbol: day(BOUNDARY, -1) for symbol in [*POOL, "BTCUSDT", "ETHUSDT"]}
    rows[POOL[0]] = day()
    result = market_context(rows, POOL, at)
    assert result["bull_index"] == 5
    assert result["regime"] == "BEAR"
    rows["BTCUSDT"] = day()
    assert market_context(rows, POOL, at)["regime"] == "MIXED"
    with pytest.raises(SafeError):
        market_context(rows, [POOL[0]], at)


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("component", ["breadth", "btc", "eth"])
def test_positive_day_with_falling_one_hour_market_is_mixed_in_both_directions(side, component):
    at = BOUNDARY // BAR_MS * BAR_MS
    rows = {symbol: day(BOUNDARY, side) for symbol in [*POOL, "BTCUSDT", "ETHUSDT"]}
    assert market_context(rows, POOL, at)["regime"] == ("BULL" if side > 0 else "BEAR")
    changed = POOL if component == "breadth" else [component.upper() + "USDT"]
    for symbol in changed:
        rows[symbol]["day_return_1h"] *= -1
    assert market_context(rows, POOL, at)["regime"] == "MIXED"


@pytest.mark.parametrize("damage", ["coverage", "btc", "eth", "stale", "nan"])
def test_missing_market_evidence_cannot_reuse_previous_permission(damage):
    at = BOUNDARY // BAR_MS * BAR_MS
    pool = [f"C{i:03}USDT" for i in range(100)]
    rows = {symbol: day() for symbol in [*pool, "BTCUSDT", "ETHUSDT"]}
    if damage == "coverage":
        for symbol in pool[:21]:
            rows.pop(symbol)
    elif damage in {"btc", "eth"}:
        rows.pop(damage.upper() + "USDT")
    elif damage == "stale":
        rows["BTCUSDT"]["day_context_ms"] -= BAR_MS
    else:
        rows["ETHUSDT"]["day_return_4h"] = float("nan")
    with pytest.raises(SafeError):
        market_context(rows, pool, at)


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("field", DAY_FIELDS)
def test_each_long_horizon_component_is_mandatory(settings, store, side, field):
    s = guarded(settings)
    breadth(store, s, side=side)
    row = {"timestamp_ms": BOUNDARY, **day(side=side)}
    assert entry_gate(s, store, row, side, BOUNDARY + 1000) is None
    row[field] *= -1
    assert "coin" in entry_gate(s, store, row, side, BOUNDARY + 1000)


@pytest.mark.parametrize("strategy", ["current", "opposite"])
@pytest.mark.parametrize("side", [1, -1])
def test_verified_mixed_market_is_allowed_only_for_opposite(settings, store, strategy, side):
    s = guarded(settings)
    rotation.configure(store, strategy, 0)
    mixed_breadth(store, s, side=side)
    row = {"timestamp_ms": BOUNDARY, **day(side=side)}
    reason = entry_gate(s, store, row, side, BOUNDARY + 1000)
    if strategy == "opposite":
        assert reason is None
    else:
        assert "market MIXED" in reason


@pytest.mark.parametrize("damage", ["stale", "future", "coverage", "nan", "regime", "coin", "missing"])
def test_opposite_mixed_permission_still_requires_valid_market_and_coin_context(settings, store, damage):
    s = guarded(settings)
    rotation.configure(store, "opposite", 0)
    record = mixed_breadth(store, s)
    row = {"timestamp_ms": BOUNDARY, **day()}
    if damage == "stale":
        record["timestamp_ms"] -= BAR_MS
    elif damage == "future":
        record["timestamp_ms"] += BAR_MS
    elif damage == "coverage":
        record["pool_coins"] = 100
    elif damage == "nan":
        record["eth_return_1h"] = None
    elif damage == "regime":
        record["eth_return_1h"] = 0.02  # All components now say BULL, contradicting MIXED.
    elif damage == "coin":
        row["day_return_1h"] *= -1
    else:
        record = None
    store.put("simple_market_trend", record)
    assert entry_gate(s, store, row, 1, BOUNDARY + 1000)


@pytest.mark.parametrize("side", [1, -1])
def test_mixed_order_permission_is_revoked_after_switching_to_current(settings, store, side):
    from decimal import Decimal

    s = guarded(settings)
    rotation.configure(store, "opposite", 0)
    mixed_breadth(store, s, side=side)
    order = Order(client_id="pf-mixed", symbol="SOLUSDT", side=-side,
                  quantity=Decimal(1), limit_price=Decimal(100))
    store.put("simple_market_order:" + order.client_id, {
        "row": {"timestamp_ms": BOUNDARY, **day(side=side)},
        "config_hash": s.config_hash, "order": order.model_dump(mode="json"),
    })
    store.put("simple_strategy_order:" + order.client_id, {
        "order": order.model_dump(mode="json"), "active": "opposite",
        "epoch": rotation.selection(store)["epoch"], "decision_side": side,
    })
    from test_opposite_confirmation import confirmed_row

    from paperfrog.simple.strategy import RULES, opposite_entry

    permission = store.get("simple_strategy_order:" + order.client_id)
    evidence_row = confirmed_row(BOUNDARY, side)
    permission.update(opposite_level=opposite_entry(evidence_row, .0007)[2],
                      opposite_evidence={"row": evidence_row, "one_way_cost": .0007, "rules": RULES.public()})
    store.put("simple_strategy_order:" + order.client_id, permission)
    require_order(s, store, order, BOUNDARY + 1000)
    rotation.configure(store, "current", 1)
    with pytest.raises(SafeError, match="strategy selection changed"):
        require_order(s, store, order, BOUNDARY + 1000)


@pytest.mark.parametrize("side", [1, -1])
async def test_ai_cannot_override_coin_day_trend_or_market_direction(
    settings, store, filters, side
):
    s = guarded(settings, simple_ai_enabled=True)
    engine = await engine_fixture(s, store, filters)
    obs = {**market(side=side).iloc[-1].to_dict(), **day(side=side)}
    if side > 0:
        p = plan(BOUNDARY)
    else:
        p = plan(
            BOUNDARY,
            long_rules=[],
            short_rules=[
                {
                    "conditions": [
                        {"feature": "flow", "operator": "le", "threshold": -0.1},
                        {"feature": "rsi_14", "operator": "le", "threshold": 50},
                    ]
                }
            ],
        )
    permit(s, store, p)
    breadth(store, s, side=side)
    assert engine.entry_signal(obs, BOUNDARY + 1000, 0.001)[0] == side
    # Synthetic falling 4h history must veto an upward 8/24/40m bounce.
    obs["day_return_4h"] = -side * 0.021665642286416653
    result, reason = engine.entry_signal(obs, BOUNDARY + 1000, 0.001)
    assert result == 0 and "4-hour return" in reason
    obs.update(day(side=side))
    breadth(store, s, side=-side)
    result, reason = engine.entry_signal(obs, BOUNDARY + 1000, 0.001)
    assert result == 0 and "market" in reason


@pytest.mark.parametrize("damage", ["missing", "stale", "future", "config", "mixed", "nan"])
def test_stale_and_malformed_gate_context_cannot_allow_orders(settings, store, damage):
    s = guarded(settings)
    row = {"timestamp_ms": BOUNDARY, **day()}
    record = breadth(store, s)
    if damage == "missing":
        record = None
    elif damage == "stale":
        record["timestamp_ms"] -= BAR_MS
    elif damage == "future":
        record["timestamp_ms"] += BAR_MS
    elif damage == "config":
        record["config_hash"] = "other"
    elif damage == "mixed":
        record["regime"] = "MIXED"
    else:
        row["day_slope_4h"] = float("nan")
    store.put("simple_market_trend", record)
    assert entry_gate(s, store, row, 1, BOUNDARY + 1000)


async def test_guard_filters_eight_minute_pool_before_ai_selection(settings, store):
    s = guarded(settings)
    service = SimpleService(s)
    service.store.engine.dispose()
    service.store = store
    obs = market()
    risk = {**obs.attrs["risk_context"], **day()}
    service.context_rows = {"BTCUSDT": risk, "ELSAUSDT": risk}
    service.now_ms = lambda: BOUNDARY + 1000
    service.store.put("simple_pool", ["ELSAUSDT"])
    data = obs.copy()
    for key, value in day().items():
        data[key] = value
    data["day_return_4h"] = -0.021665642286416653
    service.entry_frame = AsyncMock(return_value=data)
    breadth(store, s)
    try:
        await service.refresh_minutes(BOUNDARY)
        assert service.selected == []
        assert "ELSAUSDT" in service.rows  # Exit observations are retained.
        audit = store.rows("audit", 1)[0]
        assert "4-hour return" in audit["payload"]
    finally:
        await service.exchange.close()


@pytest.mark.parametrize("missing_btc", [False, True])
async def test_research_builds_market_before_selection_and_revokes_missing_coverage(
    settings, monkeypatch, missing_btc
):
    import paperfrog.simple.service as module

    s = guarded(settings)
    service = SimpleService(s)
    boundary = BOUNDARY // BAR_MS * BAR_MS
    service.now_ms = lambda: BOUNDARY + 1000
    service.exchange.sync_time = AsyncMock()
    monkeypatch.setattr(module, "scan", AsyncMock(return_value={"symbols": POOL}))

    async def candles(_exchange, _lake, symbol, *_):
        if symbol == "BTCUSDT" and missing_btc:
            raise SafeError("fixture missing benchmark")
        raw = source(288)
        raw["timestamp_ms"] += boundary - int(raw.iloc[-1].timestamp_ms)
        return raw

    monkeypatch.setattr(module, "candles", candles)
    audit = service.store.audit

    def finish(kind, payload, *args):
        audit(kind, payload, *args)
        if kind == "simple_risk_context":
            service.stop.set()

    service.store.audit = finish
    service.store.put("simple_market_trend", {"old_permission": True})
    try:
        await service.research()
        actual = service.store.get("simple_market_trend")
        if missing_btc:
            assert actual is None
        else:
            assert actual["regime"] == "BULL"
            assert actual["covered_coins"] == 20
            assert actual["timestamp_ms"] == boundary
            assert "ETHUSDT" in service.context_rows
            assert service.context_rows[POOL[0]]["day_context_ms"] == boundary
        assert all("day_return_4h" in row for row in service.context_rows.values())
    finally:
        await service.exchange.close()
        service.store.engine.dispose()


def test_order_binding_recheck_and_reduce_only_exemption(settings, store):
    from decimal import Decimal

    s = guarded(settings)
    row = {"timestamp_ms": BOUNDARY, **day()}
    order = Order(
        client_id="pf-test",
        symbol="SOLUSDT",
        side=1,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
    )
    breadth(store, s)
    with pytest.raises(SafeError, match="bound"):
        require_order(s, store, order, BOUNDARY + 1000)
    permission = {"row": row, "config_hash": s.config_hash, "order": order.model_dump(mode="json")}
    store.put("simple_market_order:" + order.client_id, permission)
    require_order(s, store, order, BOUNDARY + 1000)
    breadth(store, s, side=-1)
    with pytest.raises(SafeError, match="market BEAR"):
        require_order(s, store, order, BOUNDARY + 1000)
    store.put("simple_market_trend", None)
    require_order(s, store, order.model_copy(update={"reduce_only": True}), BOUNDARY + 1000)


async def test_ai_receives_day_and_market_evidence_without_old_active_advice(settings, store):
    from paperfrog.simple.adaptive import review_prompt

    s = guarded(settings, simple_ai_enabled=True, simple_entry_profile="active")
    frame = market()
    for key, value in day().items():
        frame[key] = value
    record = breadth(store, s)
    advisor = Advisor(s, store, fake_client(plan(BOUNDARY)), lambda: BOUNDARY + 1000)
    payload = advisor.snapshot({"SOLUSDT": frame}, ["SOLUSDT"], BOUNDARY)
    assert payload["markets"][0]["day_trend"]["day_return_4h"] == 0.02
    assert payload["market_trend"]["bull_index"] == record["bull_index"]
    assert "config_hash" not in payload["market_trend"]
    assert payload["entry_policy"]["day_market_trend_required"]
    prompt = review_prompt(s)
    assert "Mixed one/four/4-hour returns are context" not in prompt
    assert "cannot be overridden" in prompt


@pytest.mark.parametrize("strategy", ["current", "opposite"])
def test_ai_mixed_context_follows_selected_strategy(settings, store, strategy):
    s = guarded(settings, simple_ai_enabled=True)
    rotation.configure(store, strategy, 0)
    frame = market()
    for key, value in day().items():
        frame[key] = value
    mixed_breadth(store, s)
    advisor = Advisor(s, store, fake_client(plan(BOUNDARY)), lambda: BOUNDARY + 1000)
    if strategy == "current":
        with pytest.raises(SafeError, match="selected strategy"):
            advisor.snapshot({"SOLUSDT": frame}, ["SOLUSDT"], BOUNDARY)
    else:
        payload = advisor.snapshot({"SOLUSDT": frame}, ["SOLUSDT"], BOUNDARY)
        assert payload["market_trend"]["regime"] == "MIXED"
        assert payload["entry_policy"]["strategy"] == "opposite"


async def test_live_dispatch_rechecks_market_after_final_quote_and_keeps_exits(
    settings, store, filters, monkeypatch
):
    import time

    from test_simple_live import setup_engine

    fixed_now = int(time.time() * 1000) // 7_200_000 * 7_200_000 + 1000
    monkeypatch.setattr(time, "time", lambda: fixed_now / 1000)
    engine, sim, adapter, _, now = await setup_engine(
        settings,
        store,
        filters,
        simple_entry_timeframe="8m",
        simple_ai_review_minutes=8,
        simple_market_trend_enabled=True,
    )
    boundary = now // 480_000 * 480_000
    obs = {**market(boundary).iloc[-1].to_dict(), **day(boundary)}
    breadth(store, engine.s, boundary)
    store.put("simple_minute:SOLUSDT", obs)
    request = adapter.request
    calls = 0

    async def flip_at_final_quote(method, path, *args, **kwargs):
        nonlocal calls
        if path.endswith("/bookTicker"):
            calls += 1
            if calls == 2:
                breadth(store, engine.s, boundary, -1)
        return await request(method, path, *args, **kwargs)

    adapter.request = flip_at_final_quote
    try:
        with pytest.raises(SafeError, match="market BEAR"):
            await engine.enter("SOLUSDT", obs, now)
        assert calls == 2 and sim.quantity == 0
        assert not any(method == "POST" and path.endswith("/order") for method, path in sim.calls)
    finally:
        await adapter.close()


async def test_live_allowed_entry_retains_stop_and_exit_when_market_disappears(
    settings, store, filters, monkeypatch
):
    import time

    from test_simple_live import setup_engine

    fixed_now = int(time.time() * 1000) // 7_200_000 * 7_200_000 + 1000
    monkeypatch.setattr(time, "time", lambda: fixed_now / 1000)
    engine, sim, adapter, _, now = await setup_engine(
        settings,
        store,
        filters,
        simple_entry_timeframe="8m",
        simple_ai_review_minutes=8,
        simple_market_trend_enabled=True,
    )
    boundary = now // 480_000 * 480_000
    obs = {**market(boundary).iloc[-1].to_dict(), **day(boundary)}
    breadth(store, engine.s, boundary)
    store.put("simple_minute:SOLUSDT", obs)
    try:
        await engine.enter("SOLUSDT", obs, now)
        assert sim.stops and sim.quantity != 0
        store.put("simple_market_trend", None)
        await engine.close("SOLUSDT", engine.broker.now_ms(), "operator requested flatten")
        assert sim.quantity == 0 and not engine.positions()
    finally:
        await adapter.close()


@pytest.mark.parametrize("strategy", ["current", "opposite"])
async def test_fifteen_minute_candidate_scan_respects_selected_mixed_policy(settings, monkeypatch, strategy):
    import paperfrog.simple.service as module

    s = settings.model_copy(update={"simple_market_trend_enabled": True})
    service = SimpleService(s)
    boundary = BOUNDARY // BAR_MS * BAR_MS
    service.now_ms = lambda: BOUNDARY + 1000
    rotation.configure(service.store, strategy, 0)
    service.exchange.sync_time = AsyncMock()
    monkeypatch.setattr(module, "scan", AsyncMock(return_value={"symbols": POOL}))

    async def candles(_exchange, _lake, symbol, *_):
        raw = source(288, side=-1 if symbol == "ETHUSDT" else 1)
        raw["timestamp_ms"] += boundary - int(raw.iloc[-1].timestamp_ms)
        return raw

    monkeypatch.setattr(module, "candles", candles)
    audit = service.store.audit

    def finish(kind, payload, *args):
        audit(kind, payload, *args)
        if kind == "simple_selection":
            service.stop.set()

    service.store.audit = finish
    try:
        await service.research()
        assert service.store.get("simple_market_trend")["regime"] == "MIXED"
        assert bool(service.selected) is (strategy == "opposite")
    finally:
        await service.exchange.close()
        service.store.engine.dispose()


@pytest.mark.parametrize("side", [1, -1])
async def test_opposite_fifteen_minute_live_entry_trades_mixed_with_stop_and_exit(
    settings, store, filters, monkeypatch, side
):
    import time

    from test_opposite_confirmation import confirmed_row
    from test_simple_live import setup_engine

    fixed_now = int(time.time() * 1000) // 7_200_000 * 7_200_000 + 1000
    monkeypatch.setattr(time, "time", lambda: fixed_now / 1000)
    engine, sim, adapter, obs, now = await setup_engine(
        settings, store, filters, simple_market_trend_enabled=True,
    )
    rotation.configure(store, "opposite", now)
    obs.update(confirmed_row(now, side))
    obs.update(day(now, side))
    mixed_breadth(store, engine.s, now, side)
    try:
        await engine.enter("SOLUSDT", obs, now)
        trade = engine.positions()["SOLUSDT"]
        assert trade["side"] == -side and trade["decision_side"] == side
        assert sim.quantity * side < 0
        assert store.get("live_stops")["SOLUSDT"]["state"] == "CONFIRMED"
        assert store.get("live_stops")["SOLUSDT"]["side"] == -side
        assert sum(method == "POST" and path.endswith("/order") for method, path in sim.calls) == 1
        store.put("simple_market_trend", None)
        await engine.close("SOLUSDT", engine.broker.now_ms(), "test protective close")
        assert sim.quantity == 0 and not engine.positions()
    finally:
        await adapter.close()
