"""Run the isolated A/B/C PAPER experiment with no account or messaging credentials."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from paperfrog.settings import Settings
from paperfrog.simple.service import SimpleService


def paper_settings(root: Path) -> Settings:
    # Explicit defaults block ambient environment and the production .env from
    # injecting credentials, live mode, larger risk allowances or shared paths.
    values = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }
    values.update(yaml.safe_load((root / "config/momentum-paper.yaml").read_text()))
    values.update(
        state_dir=root / "state-momentum-paper",
        data_dir=root / "data/momentum-paper",
        reports_dir=root / "reports/momentum-paper",
        artifacts_dir=root / "artifacts/momentum-paper",
        binance_env="live",
        health_port=8082,
        binance_api_key="",
        binance_api_secret="",
        openai_api_key="",
        telegram_bot_token="",
    )
    settings = Settings(_env_file=None, **values)
    if settings.trading_mode != "paper" or settings.simple_entry_profile != "momentum":
        raise ValueError("isolated momentum runner requires its PAPER profile")
    return settings


if __name__ == "__main__":
    try:
        asyncio.run(SimpleService(paper_settings(Path(__file__).resolve().parents[1])).run())
    except Exception as exc:
        print(f"Momentum PAPER startup failed ({type(exc).__name__}).", flush=True)
        raise SystemExit(1) from None
