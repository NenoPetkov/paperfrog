from __future__ import annotations

import logging
import re
from typing import Any

import structlog

from paperfrog.settings import Settings


class Redactor:
    def __init__(self, values: list[str]):
        self.values = [v for v in values if v]

    def __call__(self, logger: Any, method: str, event: dict[str, Any]) -> dict[str, Any]:
        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    k: (
                        "[REDACTED]"
                        if re.search(r"secret|token|api.?key|signature|listenKey", k, re.I)
                        else clean(v)
                    )
                    for k, v in value.items()
                }
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            if isinstance(value, str):
                for secret in self.values:
                    value = value.replace(secret, "[REDACTED]")
                return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", value)
            return value

        return dict(clean(event))


def configure(s: Settings) -> None:
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "openai", "websockets"):
        logging.getLogger(name).disabled = True
    redactor = Redactor(
        [
            s.binance_api_key.get_secret_value(),
            s.binance_api_secret.get_secret_value(),
            s.telegram_bot_token.get_secret_value(),
            s.openai_api_key.get_secret_value(),
        ]
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            redactor,
            structlog.processors.JSONRenderer(),
        ]
    )
