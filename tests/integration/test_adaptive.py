import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from openai import AsyncOpenAI, LengthFinishReasonError
from pandas.testing import assert_frame_equal
from pydantic import ValidationError
from test_simple import engine_fixture, quote, row, source
from test_simple_live import setup_engine

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings
from paperfrog.simple.adaptive import (
    MAX_REQUESTS_PER_UTC_DAY,
    AdaptivePlan,
    Advisor,
    Condition,
    RuleGroup,
    active_plan,
    enrich,
    signal,
)
from paperfrog.simple.strategy import BAR_MS, execution_rules, indicators


def ai_settings(settings, **changes):
    return Settings(_env_file=None, **{**settings.public(), "simple_ai_enabled": True, **changes})


def plan(asof_ms=200 * BAR_MS, action="trade", **changes):
    return AdaptivePlan.model_validate(
        {
            "asof_ms": asof_ms,
            "action": action,
            "long_rules": [
                {
                    "conditions": [
                        {"feature": "flow", "operator": "ge", "threshold": 0.1},
                        {"feature": "rsi_14", "operator": "ge", "threshold": 50},
                    ]
                }
            ],
            "short_rules": [],
            "forecasts": [{"symbol": "SOLUSDT", "expected_return_15m": 0.001}],
            "explanation": "Ограничен пример; няма доказано предимство.",
            "lessons": ["Една загуба не е доказателство за грешка."],
            **changes,
        }
    )


def permit(s, store, p):
    store.put(
        "simple_ai_active",
        {"config_hash": s.config_hash, "digest": p.digest, "plan": p.model_dump(mode="json")},
    )


def observation(now_ms=200 * BAR_MS):
    return {
        **row(now_ms),
        "return_1h": 0.01,
        "return_4h": 0.02,
        "return_24h": 0.03,
        "rsi_14": 65,
        "volume_ratio": 1.2,
        "breakout_up_atr": 1,
        "breakout_down_atr": -2,
        "range_position": 0.9,
    }


def frames(asof_ms=200 * BAR_MS):
    raw = source(288)
    raw["timestamp_ms"] += asof_ms - int(raw.iloc[-1].timestamp_ms)
    return {"SOLUSDT": enrich(indicators(raw))}


def fake_client(p=None):
    client = AsyncMock(spec=AsyncOpenAI)
    client.responses = SimpleNamespace(
        parse=AsyncMock(return_value=SimpleNamespace(output_parsed=p))
    )
    return client


def test_ai_is_opt_in_and_cannot_select_legacy_or_reverse(settings):
    assert not settings.simple_ai_enabled and settings.trading_mode == "paper"
    for change in ({"strategy_profile": "legacy"}, {"simple_entry_mode": "reverse"}):
        with pytest.raises(ValidationError):
            ai_settings(settings, **change)


async def test_historical_replay_cannot_fabricate_adaptive_ai_evidence(settings, tmp_path):
    from paperfrog.simple.backtest import replay

    with pytest.raises(SafeError, match="recorded forward decisions"):
        await replay({}, {}, ai_settings(settings), BAR_MS, 2 * BAR_MS, tmp_path / "fake")
    assert not (tmp_path / "fake").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("risk_per_trade", 1),
        ("exchange_leverage", 20),
        ("stop_atr", 100),
        ("code", "print('bad')"),
        ("tools", ["place_order"]),
    ],
)
def test_model_cannot_write_risk_or_code(field, value):
    with pytest.raises(ValidationError):
        plan(**{field: value})


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -101, 101])
def test_conditions_reject_nonfinite_and_unbounded_thresholds(threshold):
    with pytest.raises(ValidationError):
        Condition(feature="flow", operator="ge", threshold=threshold)


def test_conditions_cannot_read_attributes_or_execute_python():
    with pytest.raises(ValidationError):
        Condition(feature="__import__('os').environ", operator="ge", threshold=1)
    with pytest.raises(ValidationError):
        RuleGroup(conditions=[Condition(feature="flow", operator="ge", threshold=0)])


def test_new_features_are_causal_and_have_expected_units():
    raw = source(360)
    original = enrich(indicators(raw.iloc[:250]))
    raw.loc[250:, ["open", "high", "low", "close"]] *= 50
    assert_frame_equal(original, enrich(indicators(raw)).iloc[:250])
    assert original.iloc[-1].rsi_14 == 100
    assert original.iloc[-1].return_1h == pytest.approx(np.exp(0.0025 * 4) - 1)


def test_permission_is_bound_to_config_digest_clock_and_boundary(settings, store):
    s, p = ai_settings(settings), plan()
    permit(s, store, p)
    assert active_plan(s, store, p.asof_ms, p.asof_ms) == p
    assert active_plan(s, store, p.asof_ms, p.asof_ms - 1) is None
    assert active_plan(s, store, p.asof_ms, p.asof_ms + BAR_MS) is None
    assert active_plan(s, store, p.asof_ms + BAR_MS, p.asof_ms + BAR_MS) is None
    assert (
        active_plan(ai_settings(settings, simple_selected_count=3), store, p.asof_ms, p.asof_ms)
        is None
    )
    record = store.get("simple_ai_active")
    record["plan"]["long_rules"][0]["conditions"][0]["threshold"] = 0
    store.put("simple_ai_active", record)
    assert active_plan(s, store, p.asof_ms, p.asof_ms) is None


def test_synthesized_rules_preserve_trend_cost_and_volatility_gates(settings, store):
    s, p = ai_settings(settings), plan()
    permit(s, store, p)
    rules, obs = execution_rules(s), observation()
    # This is a NEW entry: the original breakout is not needed, but immutable gates are.
    obs["close"] = 98.0
    assert signal(s, store, obs, p.asof_ms, rules, 0.001)[0] == 1
    for change in ({"trend_atr": -2}, {"atr_fraction": 0.1}, {"rsi_14": float("nan")}):
        assert signal(s, store, {**obs, **change}, p.asof_ms, rules, 0.001)[0] == 0
    assert signal(s, store, obs, p.asof_ms, rules, 0.1)[0] == 0
    permit(s, store, plan(short_rules=p.model_dump()["long_rules"]))
    assert signal(s, store, obs, p.asof_ms, rules, 0.001)[0] == 0


async def test_review_is_one_request_per_boundary_and_restores_context(settings, store):
    s, p = ai_settings(settings), plan()
    client = fake_client(p)
    advisor = Advisor(s, store, client, lambda: p.asof_ms + 1000)
    f = frames()
    await advisor.review(f, ["SOLUSDT"], p.asof_ms)
    await advisor.review(f, ["SOLUSDT"], p.asof_ms)
    assert client.responses.parse.await_count == 1
    assert active_plan(s, store, p.asof_ms, p.asof_ms + 1000) == p
    sent = client.responses.parse.call_args.kwargs
    assert sent["store"] is False and "tools" not in sent
    assert sent["max_output_tokens"] == s.simple_ai_output_limit
    assert sent["model"] == s.openai_primary_model
    next_ms = p.asof_ms + BAR_MS
    client.responses.parse.return_value.output_parsed = plan(next_ms)
    restarted = Advisor(s, store, client, lambda: next_ms + 1000)
    await restarted.review(frames(next_ms), ["SOLUSDT"], next_ms)
    snapshot = json.loads(client.responses.parse.call_args.kwargs["input"][1]["content"])
    assert snapshot["prior_reviews"][0]["forecast_results"] is not None
    assert len(store.get("simple_ai_history")) == 2


@pytest.mark.parametrize("profile", ["standard", "active"])
async def test_review_uses_selected_entry_profile_and_keeps_explicit_pause(
    settings, store, profile
):
    s = ai_settings(settings, simple_entry_profile=profile)
    p = plan(action="pause", long_rules=[], short_rules=[])
    client = fake_client(p)
    await Advisor(s, store, client, lambda: p.asof_ms + 1000).review(
        frames(), ["SOLUSDT"], p.asof_ms
    )
    messages = client.responses.parse.call_args.kwargs["input"]
    snapshot = json.loads(messages[1]["content"])
    assert snapshot["entry_policy"]["profile"] == profile
    assert snapshot["entry_policy"]["minimum_trend_atr"] == execution_rules(s).minimum_trend_atr
    assert ("ACTIVE entry profile" in messages[0]["content"][0]["text"]) == (profile == "active")
    assert signal(s, store, observation(), p.asof_ms, execution_rules(s), 0.001)[0] == 0
    assert store.get("simple_ai_status") == "pause"


@pytest.mark.parametrize(
    "failure", [None, TimeoutError("private-example"), ValueError("private-example")]
)
async def test_refusal_or_failure_blocks_entries_without_replaying_or_logging_body(
    settings, store, failure
):
    s = ai_settings(settings)
    client = fake_client()
    client.responses.parse.side_effect = failure
    advisor = Advisor(s, store, client, lambda: 200 * BAR_MS)
    await advisor.review(frames(), ["SOLUSDT"], 200 * BAR_MS)
    await Advisor(s, store, client, lambda: 200 * BAR_MS).review(
        frames(), ["SOLUSDT"], 200 * BAR_MS
    )
    assert client.responses.parse.await_count == 1
    assert store.get("simple_ai_active") is None
    assert "private-example" not in str(store.rows("audit", 100))


async def test_malformed_sdk_output_preserves_monitoring(settings, store):
    client = fake_client({"action": "trade"})
    await Advisor(ai_settings(settings), store, client, lambda: 200 * BAR_MS).review(
        frames(), ["SOLUSDT"], 200 * BAR_MS
    )
    assert store.get("simple_ai_active") is None
    assert store.get("simple_ai_status") == "unavailable"


async def test_sdk_output_limit_error_fails_closed(settings, store):
    from openai.types.chat import ChatCompletion

    response = ChatCompletion(
        id="example", model="example", created=1, object="chat.completion", choices=[]
    )
    client = fake_client()
    client.responses.parse.side_effect = LengthFinishReasonError(completion=response)
    await Advisor(ai_settings(settings), store, client, lambda: 200 * BAR_MS).review(
        frames(), ["SOLUSDT"], 200 * BAR_MS
    )
    assert store.get("simple_ai_active") is None


@pytest.mark.parametrize("kind", ["future", "wrong_symbol", "late"])
async def test_mismatched_or_expired_reviews_cannot_grant_entries(settings, store, kind):
    s, boundary = ai_settings(settings), 200 * BAR_MS
    result = plan(boundary + BAR_MS) if kind == "future" else plan()
    if kind == "wrong_symbol":
        result = plan(forecasts=[{"symbol": "ETHUSDT", "expected_return_15m": 0.001}])
    clock = iter([boundary, boundary + BAR_MS] if kind == "late" else [boundary, boundary])
    await Advisor(s, store, fake_client(result), lambda: next(clock)).review(
        frames(), ["SOLUSDT"], boundary
    )
    assert store.get("simple_ai_active") is None


async def test_daily_budget_is_durable_and_bounded(settings, store):
    s, boundary = ai_settings(settings), 200 * BAR_MS
    store.put(
        "simple_ai_budget", {"day": boundary // 86_400_000, "count": MAX_REQUESTS_PER_UTC_DAY}
    )
    client = fake_client(plan())
    await Advisor(s, store, client, lambda: boundary).review(frames(), ["SOLUSDT"], boundary)
    client.responses.parse.assert_not_called()
    assert store.get("simple_ai_active") is None


async def test_no_private_amounts_or_credentials_are_sent_and_free_text_is_redacted(
    settings, store
):
    s = ai_settings(settings, binance_api_secret="private-sentinel-value")
    store.put(
        "simple_closed",
        [
            {
                "symbol": "SOLUSDT",
                "direction": 1,
                "entry_ms": 1,
                "timestamp_ms": 2,
                "pnl": -17.123456,
                "turnover": 234.5678,
            }
        ],
    )
    client = fake_client(plan(explanation="private-sentinel-value"))
    await Advisor(s, store, client, lambda: 200 * BAR_MS).review(
        frames(), ["SOLUSDT"], 200 * BAR_MS
    )
    sent = client.responses.parse.call_args.kwargs["input"][1]["content"]
    for private in ("private-sentinel-value", "-17.123456", "234.5678", "equity", "api_key"):
        assert private not in sent
    assert "private-sentinel-value" not in str(store.get("simple_ai_history"))


async def test_ai_entries_keep_risk_locks_sizing_stops_and_rule_attribution(
    settings, store, filters
):
    s, p = ai_settings(settings), plan()
    engine = await engine_fixture(s, store, filters)
    obs = observation()
    await engine.enter("SOLUSDT", obs, p.asof_ms)
    assert not engine.positions()  # No AI permission means no fallback to the base signal.
    permit(s, store, p)
    store.put("manual_lock", True)
    with pytest.raises(SafeError):
        await engine.enter("SOLUSDT", obs, p.asof_ms)
    assert not engine.positions()
    store.put("manual_lock", False)
    # A blocked candidate consumes that candle's signal; use the next boundary.
    next_ms = p.asof_ms + BAR_MS
    permit(s, store, plan(next_ms))
    await engine.quote(quote(now_ms=next_ms), next_ms)
    await engine.enter("SOLUSDT", observation(next_ms), next_ms)
    trade = engine.positions()["SOLUSDT"]
    assert trade["ai_rule_id"] == plan(next_ms).digest
    assert store.rows("entries")[0]["risk"] <= s.paper_equity * s.risk_per_trade
    assert 0.006 <= (100 - trade["stop_price"]) / 100 <= 0.02
    store.put("simple_ai_active", None)
    await engine.quote(quote(price=90, now_ms=next_ms + 1000), next_ms + 1000)
    assert not engine.positions()  # Exits keep working while AI is unavailable.
    assert store.get("simple_closed")[-1]["ai_rule_id"] == trade["ai_rule_id"]
    assert await engine.coordinator.reconcile()


async def test_slow_ai_does_not_hold_monitoring_mutex(settings, store, filters):
    s, boundary = ai_settings(settings), 200 * BAR_MS
    started, release = asyncio.Event(), asyncio.Event()
    client = fake_client()

    async def delayed(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(output_parsed=plan())

    client.responses.parse.side_effect = delayed
    advisor = Advisor(s, store, client, lambda: boundary)
    pending = asyncio.create_task(advisor.review(frames(), ["SOLUSDT"], boundary))
    await asyncio.wait_for(started.wait(), timeout=3)
    # This is the same engine monitor used in the independent service task.
    engine = await engine_fixture(s, store, filters)
    await asyncio.wait_for(engine.monitor(boundary, {"SOLUSDT": observation()}), timeout=1)
    assert not pending.done()
    release.set()
    await pending


async def test_service_scan_releases_monitoring_mutex_before_awaiting_ai(settings, monkeypatch):
    import paperfrog.simple.service as service_module

    s = ai_settings(settings, openai_api_key="mock-local-api-key")
    service = service_module.SimpleService(s)
    boundary = 288 * BAR_MS
    service.now_ms = lambda: boundary
    service.exchange.sync_time = AsyncMock()
    monkeypatch.setattr(service_module, "scan", AsyncMock(return_value={"symbols": ["SOLUSDT"]}))
    monkeypatch.setattr(service_module, "candles", AsyncMock(return_value=source(288)))
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_review(*args):
        started.set()
        await release.wait()

    service.advisor.review = delayed_review
    pending = asyncio.create_task(service.research())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.wait_for(service.mutex.acquire(), timeout=1)
        service.mutex.release()
        assert not pending.done()
        assert service.selected == ["SOLUSDT"]
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await service.advisor.close()
        await service.exchange.close()
        service.store.engine.dispose()


async def test_ai_live_execution_keeps_local_arm_protection_and_actual_risk_caps(
    settings, store, filters
):
    engine, sim, adapter, obs, now_ms = await setup_engine(
        settings, store, filters, simple_ai_enabled=True
    )
    obs = {**observation(obs["timestamp_ms"]), **obs}
    permit(engine.s, store, plan(obs["timestamp_ms"]))
    await engine.enter("SOLUSDT", obs, now_ms)
    assert sim.quantity > 0 and sim.stops
    assert (
        store.rows("entries")[0]["risk"] <= float(engine.broker.equity()) * engine.s.risk_per_trade
    )
    assert engine.positions()["SOLUSDT"]["ai_rule_id"]
    store.put("simple_ai_active", None)
    await engine.close("SOLUSDT", now_ms + 1000, "test exit with unavailable AI")
    assert not sim.quantity and await engine.coordinator.reconcile()
    await adapter.close()


async def test_live_broker_rejects_unbound_order_even_with_valid_ai_review(
    settings, store, filters
):
    from paperfrog.domain.types import Order

    engine, sim, adapter, obs, now_ms = await setup_engine(
        settings, store, filters, simple_ai_enabled=True
    )
    permit(engine.s, store, plan(obs["timestamp_ms"]))
    with pytest.raises(SafeError, match="AI order permission"):
        await engine.broker.submit(
            Order(
                client_id="pf-unbound", symbol="SOLUSDT", side=1, quantity=".01", limit_price="100"
            )
        )
    assert not sim.quantity and not any(method == "POST" for method, _ in sim.calls)
    await adapter.close()
