import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.responses import ResponseUsage
from test_adaptive import ai_settings, fake_client, frames, permit, plan

from paperfrog.simple.adaptive import Advisor, active_plan, forecast_feedback, token_usage
from paperfrog.simple.strategy import BAR_MS


async def test_sdk_serializes_explicit_caching_and_parses_usage_without_network(settings, store):
    p = plan()

    def respond(request):
        body = json.loads(request.content)
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert body["input"][0]["role"] == "developer"
        assert body["input"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert isinstance(body["input"][1]["content"], str)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-6-luna",
                "parallel_tool_calls": False,
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": p.model_dump_json(), "annotations": []}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 2000,
                    "input_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 0},
                    "output_tokens": 400,
                    "output_tokens_details": {"reasoning_tokens": 100},
                    "total_tokens": 2400,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        async with AsyncOpenAI(api_key="mock-local-api-key", http_client=http) as client:
            s = ai_settings(settings)
            await Advisor(s, store, client, lambda: p.asof_ms).review(
                frames(), ["SOLUSDT"], p.asof_ms
            )
    assert active_plan(s, store, p.asof_ms, p.asof_ms) == p
    assert store.get("simple_ai_last_usage")["usage"]["cached_tokens"] == 1024


@pytest.mark.parametrize("minutes", [1, 5, 15])
@pytest.mark.parametrize("blocker", ["manual_lock", "risk_lock", "position_capacity"])
async def test_useless_reviews_skip_without_budget_or_permission_and_recover_next_boundary(
    settings, store, minutes, blocker
):
    s = ai_settings(settings, simple_entry_timeframe="1m", simple_ai_review_minutes=minutes)
    boundary = 200 * BAR_MS
    step = s.simple_ai_review_bar_ms
    permit(s, store, plan(boundary - step))
    if blocker == "position_capacity":
        store.put("simple_positions", {"SOLUSDT": {}, "ETHUSDT": {}})
    else:
        store.put(blocker, True)
    client = fake_client(plan(boundary))
    await Advisor(s, store, client, lambda: boundary).review(frames(), ["SOLUSDT"], boundary)
    client.responses.parse.assert_not_called()
    assert store.get("simple_ai_budget") is None
    assert store.get("simple_ai_status") == "skipped_" + blocker
    assert active_plan(s, store, boundary, boundary) is None
    assert store.get("simple_ai_active") is None
    # A freed slot or removed lock cannot resurrect an old plan or retry this boundary.
    store.put("simple_positions", {})
    store.put("manual_lock", False)
    store.put("risk_lock", None)
    await Advisor(s, store, client, lambda: boundary).review(frames(), ["SOLUSDT"], boundary)
    client.responses.parse.assert_not_called()
    later = boundary + step
    client.responses.parse.return_value.output_parsed = plan(later)
    await Advisor(s, store, client, lambda: later).review(frames(later), ["SOLUSDT"], later)
    assert client.responses.parse.await_count == 1
    assert store.get("simple_ai_budget")["count"] == 1
    assert active_plan(s, store, later, later) is not None


@pytest.mark.parametrize("model", ["gpt-6-luna", "other-model"])
async def test_only_stable_prefix_is_cached_and_usage_is_recorded(settings, store, model):
    s, p = ai_settings(settings, openai_primary_model=model), plan()
    client = fake_client(p)
    usage = ResponseUsage(
        input_tokens=1000,
        input_tokens_details={"cached_tokens": 200, "cache_write_tokens": 100},
        output_tokens=400,
        output_tokens_details={"reasoning_tokens": 150},
        total_tokens=1400,
    )
    client.responses.parse.return_value.usage = usage
    await Advisor(s, store, client, lambda: p.asof_ms).review(frames(), ["SOLUSDT"], p.asof_ms)
    sent = client.responses.parse.call_args.kwargs
    if model == "gpt-6-luna":
        assert sent["prompt_cache_options"] == {"mode": "explicit"}
        assert sent["input"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    else:
        assert "prompt_cache_options" not in sent
        assert isinstance(sent["input"][0]["content"], str)
    assert "prompt_cache_breakpoint" not in sent["input"][1]
    record = store.get("simple_ai_last_usage")
    assert record["usage"] == {
        "input_tokens": 1000,
        "cached_tokens": 200,
        "cache_write_tokens": 100,
        "output_tokens": 400,
        "reasoning_tokens": 150,
        "total_tokens": 1400,
    }
    assert record["request_characters"] == len(sent["input"][1]["content"])
    assert any(r["kind"] == "simple_ai_usage" for r in store.rows("audit"))


def test_usage_never_copies_unknown_text_or_guesses_missing_counts():
    assert token_usage(SimpleNamespace()) == {}
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=True,
            output_tokens="private-sentinel",
            total_tokens=-1,
            input_tokens_details=SimpleNamespace(cached_tokens=120, cache_write_tokens=None),
            output_tokens_details=SimpleNamespace(reasoning_tokens=20, secret="private-sentinel"),
        )
    )
    assert token_usage(response) == {"cached_tokens": 120, "reasoning_tokens": 20}


def test_feedback_uses_only_matured_scores_and_compares_no_change():
    def record(asof, results):
        return {"plan": {"asof_ms": asof}, "forecast_results": results}

    history = [
        record(0, [{"expected": 0.01, "actual": -0.02}]),
        record(BAR_MS, [{"expected": 0.03, "actual": 0.01}]),
        record(2 * BAR_MS, [{"expected": 99, "actual": 99}]),
        record(0, None),
    ]
    feedback = forecast_feedback(history, 2 * BAR_MS)
    assert feedback["observations"] == 2
    assert feedback["mean_absolute_error"] == pytest.approx(0.025)
    assert feedback["no_change_mean_absolute_error"] == pytest.approx(0.015)
    assert forecast_feedback([], 0)["mean_absolute_error"] is None


async def test_compact_memory_retains_latest_rules_and_full_scored_feedback(settings, store):
    s, boundary = ai_settings(settings), 200 * BAR_MS
    history = [
        {
            "plan": plan(boundary - (8 - i) * BAR_MS).model_dump(mode="json"),
            "forecast_results": [{"symbol": "SOLUSDT", "expected": 0.001, "actual": 0.002}],
        }
        for i in range(8)
    ]
    store.put("simple_ai_history", history)
    client = fake_client(plan())
    await Advisor(s, store, client, lambda: boundary).review(frames(), ["SOLUSDT"], boundary)
    body = client.responses.parse.call_args.kwargs["input"][1]["content"]
    snapshot = json.loads(body)
    assert len(snapshot["prior_reviews"]) == 2
    assert snapshot["prior_reviews"][-1]["plan"] == history[-1]["plan"]
    assert snapshot["forecast_feedback"]["observations"] == 8
    assert len(snapshot["markets"][0]["recent_bars"]) == 32
    assert len(body) < len(json.dumps({**snapshot, "prior_reviews": history}, ensure_ascii=False))
