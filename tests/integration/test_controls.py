import hashlib
import json
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from paperfrog.llm.events import EventFilter, EventRiskAssessment, Source, policy
from paperfrog.monitoring.logging import Redactor
from paperfrog.settings import Settings
from paperfrog.telegram.control import Controls


def update(command, uid=1, cid=2, **kwargs):
    return {
        "update_id": int(time.time_ns()),
        "message": {
            "from": {"id": uid},
            "chat": {"id": cid, "type": "private"},
            "date": time.time(),
            "text": command,
            **kwargs,
        },
    }


@pytest.fixture
def controls(store):
    s = Settings(_env_file=None, telegram_allowed_user_ids=(1,), telegram_allowed_chat_ids=(2,))
    return Controls(
        s, store, AsyncMock(), AsyncMock(return_value=True), AsyncMock(return_value="status")
    )


@pytest.mark.parametrize("uid,cid", [(99, 2), (1, 99), (99, 99)])
async def test_allowlist(controls, uid, cid):
    assert await controls.handle(update("/flatten", uid, cid)) is None
    controls.flatten.assert_not_called()


async def test_edited_forwarded_and_group_rejected(controls):
    assert await controls.handle(update("/resume", forward_origin={"type": "user"})) is None
    u = update("/resume")
    u["edited_message"] = u["message"]
    assert await controls.handle(u) is None
    u = update("/resume")
    u["message"]["chat"]["type"] = "group"
    assert await controls.handle(u) is None
    controls.resume.assert_not_called()


@pytest.mark.parametrize(
    "command", ["/live", "/arm", "/set_leverage 20", "/resume extra", "/override_lock"]
)
async def test_no_dangerous_telegram_commands(controls, command):
    response = await controls.handle(update(command))
    assert "Unknown" in response[1]
    controls.resume.assert_not_called()


async def test_flatten_requires_one_time_bound_code(controls):
    reply = await controls.handle(update("/flatten"))
    code = reply[1].split()[-1]
    controls.flatten.assert_not_called()
    denied = await controls.handle(update("/confirm_flatten " + code, cid=99))
    assert denied is None
    await controls.handle(update("/confirm_flatten " + code))
    assert controls.flatten.await_count == 1
    reply = await controls.handle(update("/confirm_flatten " + code))
    assert "invalid" in reply[1]
    assert controls.flatten.await_count == 1


async def test_expired_confirmation(controls):
    controls.challenges[(1, 2)] = (hashlib.sha256(b"code").hexdigest(), time.monotonic() - 1)
    response = await controls.handle(update("/confirm_flatten code"))
    assert "expired" in response[1]
    controls.flatten.assert_not_called()


def event(kind="none", severity=0, confidence=0.9):
    return EventRiskAssessment(
        asset="SOL",
        checked_at_utc=datetime.now(UTC),
        material_event_found=kind != "none",
        event_type=kind,
        severity=severity,
        confidence=confidence,
        likely_direction="unknown",
        fundamental_change=False,
        security_or_operational_event=False,
        rumor_only=False,
        source_count=0,
        sources=[],
        concise_factual_summary="test fixture",
        uncertainty_reason="",
    )


@pytest.mark.parametrize(
    "kind",
    ["exploit", "hack", "chain_halt", "exchange_delisting", "exchange_failure", "stablecoin_event"],
)
def test_severe_events_veto(kind):
    assert not policy(event(kind, 0.6, 0.7), 0, datetime.now(UTC)).allowed


def test_llm_unavailable_extreme_fails_closed():
    assert not policy(None, 4.01, datetime.now(UTC), True).allowed
    assert policy(None, 1, datetime.now(UTC), True).risk_multiplier == 0.5


def test_event_schema_uses_supported_string_formats():
    supported = {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "uuid",
    }

    def check(node):
        if isinstance(node, dict):
            if "format" in node:
                assert node["format"] in supported
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(EventRiskAssessment.model_json_schema())


@pytest.mark.parametrize("url", ["not-a-url", "file:///tmp/event", "javascript:alert(1)"])
def test_event_source_still_rejects_invalid_http_urls(url):
    with pytest.raises(ValidationError):
        Source(
            title="Public event",
            publisher="Example",
            published_at=datetime.now(UTC),
            url=url,
        )


async def test_openai_refusal_fails_closed(settings, store):
    client = AsyncMock(spec=AsyncOpenAI)
    client.responses = AsyncMock()
    client.responses.parse.return_value.output_parsed = None
    result = await EventFilter(settings, store, client).assess("SOL", 1)
    assert not result.allowed
    sent = client.responses.parse.call_args.kwargs
    assert sent["model"] == "gpt-6-luna" and sent["tools"] == [{"type": "web_search"}]
    assert "equity" not in json.dumps(sent["input"])


async def test_openai_escalation(settings, store):
    filt = EventFilter(settings, store, AsyncMock(spec=AsyncOpenAI))
    filt._request = AsyncMock(
        side_effect=[(event("unknown", 0.2, 0.5), "one"), (event("hack", 0.8, 0.9), "two")]
    )
    result = await filt.assess("SOL", 5)
    assert not result.allowed
    assert filt._request.call_args_list[1].args[1] == "gpt-6.1-sol"


def test_secret_redaction_and_serialization():
    s = Settings(_env_file=None, binance_api_secret="sensitive", telegram_bot_token="bot-token")
    assert "sensitive" not in s.model_dump_json() and "bot-token" not in repr(s)
    r = Redactor(["sensitive", "bot-token"])
    value = r(
        None,
        "info",
        {"message": "sensitive bot-token", "api_key": "other", "nested": {"signature": "abcd"}},
    )
    assert "sensitive" not in str(value) and "abcd" not in str(value)
