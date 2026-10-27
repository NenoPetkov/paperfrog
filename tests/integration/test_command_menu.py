from unittest.mock import AsyncMock

import pytest
from test_controls import update

from paperfrog.domain.types import SafeError
from paperfrog.telegram.commands import COMMANDS, menu_commands
from paperfrog.telegram.control import Controls, Telegram


def test_menu_covers_all_commands_with_descriptions():
    menu = menu_commands()
    assert {"/" + item["command"] for item in menu} == COMMANDS
    assert len(menu) == len(COMMANDS) <= 100
    assert "/strategy" in COMMANDS
    assert all(1 <= len(item["description"]) <= 256 for item in menu)


async def test_register_commands_does_not_run_trading_controls(settings, store):
    controls = AsyncMock()
    telegram = Telegram(settings, store, controls)
    telegram.call = AsyncMock(return_value=True)
    try:
        await telegram.register_commands()
        telegram.call.assert_awaited_once_with("setMyCommands", {"commands": menu_commands()})
        controls.handle.assert_not_called()
    finally:
        await telegram.client.aclose()


async def test_confirmation_menu_click_explains_required_code_without_closing(settings, store):
    s = settings.model_copy(
        update={"telegram_allowed_user_ids": (1,), "telegram_allowed_chat_ids": (2,)}
    )
    controls = Controls(s, store, AsyncMock(), AsyncMock(), AsyncMock())
    reply = await controls.handle(update("/confirm_flatten"))
    assert "/flatten" in reply[1] and "CODE" in reply[1]
    controls.flatten.assert_not_called()


async def test_menu_registration_failure_still_allows_command_polling(settings, store):
    import asyncio

    telegram = Telegram(settings, store, AsyncMock())
    telegram.register_commands = AsyncMock(side_effect=SafeError("menu unavailable"))
    telegram.call = AsyncMock(side_effect=asyncio.CancelledError())
    try:
        with pytest.raises(asyncio.CancelledError):
            await telegram.poll()
        telegram.call.assert_awaited_once_with(
            "getUpdates", {"offset": 0, "timeout": 25, "allowed_updates": ["message"]}
        )
        assert any(row["kind"] == "telegram_menu_degraded" for row in store.rows("audit"))
    finally:
        await telegram.client.aclose()


async def test_help_explains_manual_and_historical_auto_strategies(settings):
    from test_controls import update

    from paperfrog.simple.service import SimpleService

    s = settings.model_copy(update={"telegram_allowed_user_ids": (1,), "telegram_allowed_chat_ids": (2,)})
    service = SimpleService(s)
    try:
        response = await service.controls.handle(update("/help"))
        assert "/strategy auto" in response[1] and "per-trade current/opposite routing" in response[1]
        assert "/strategy auto switches" not in response[1]
        assert "failed breakout or confirmed reversal" in response[1]
    finally:
        await service.exchange.close()
