from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="forbid", frozen=True, hide_input_in_errors=True
    )
    trading_mode: Literal["paper", "testnet", "live"] = "paper"
    binance_env: Literal["testnet", "live"] = "testnet"
    live_trading_enabled: bool = False
    i_understand_live_risk: str = ""
    binance_api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    binance_api_secret: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    openai_api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    telegram_allowed_user_ids: tuple[int, ...] = ()
    telegram_allowed_chat_ids: tuple[int, ...] = ()
    telegram_allow_groups: bool = False
    openai_primary_model: str = "gpt-6-luna"
    openai_escalation_model: str = "gpt-6.1-sol"
    allow_llm_unavailable: bool = False
    state_dir: Path = Path("state")
    data_dir: Path = Path("data")
    artifacts_dir: Path = Path("artifacts")
    reports_dir: Path = Path("reports")
    display_timezone: str = "Europe/Sofia"
    max_strategy_entries_per_day: int = Field(12, ge=0, le=16)
    max_entries_per_symbol_per_day: int = Field(2, ge=1, le=2)
    max_concurrent_strategy_trades: int = Field(2, ge=1, le=2)
    risk_per_trade: float = Field(0.002, gt=0, le=0.002)
    max_open_risk: float = Field(0.005, gt=0, le=0.005)
    max_target_notional: float = Field(0.08, gt=0, le=0.08)
    max_gross_notional: float = Field(0.5, gt=0, le=0.5)
    max_net_beta: float = Field(0.10, gt=0, le=0.10)
    daily_loss_limit: float = Field(0.0125, gt=0, le=0.0125)
    weekly_loss_limit: float = Field(0.035, gt=0, le=0.035)
    high_water_drawdown: float = Field(0.06, gt=0, le=0.06)
    exchange_leverage: Literal[2] = 2
    margin_type: Literal["ISOLATED"] = "ISOLATED"
    position_mode: Literal["ONE_WAY"] = "ONE_WAY"
    market_stale_seconds: int = Field(60, ge=1, le=120)
    account_stale_seconds: int = Field(60, ge=1, le=120)
    max_model_age_days: int = Field(30, ge=1, le=90)
    live_arm_hours: int = Field(4, ge=1, le=24)
    min_history_days: int = Field(180, ge=90)
    max_universe: int = Field(25, ge=1, le=25)
    max_spread: float = Field(0.002, gt=0, le=0.005)
    min_daily_quote_volume: float = Field(10_000_000, gt=0)
    max_participation: float = Field(0.01, gt=0, le=0.01)
    denylist: tuple[str, ...] = ()
    paper_equity: float = Field(10000, gt=0)
    paper_seed: int = 42
    fee_rate: float = Field(0.0005, ge=0, le=0.01)
    slippage_bps: float = Field(2, ge=0, le=100)
    latency_ms: int = Field(100, ge=0, le=5000)
    health_port: int = Field(8080, ge=1024, le=65535)
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        if self.trading_mode != "paper" and self.min_history_days < 180:
            raise ValueError("testnet and live require at least 180 history days")
        if self.trading_mode == "testnet" and self.binance_env != "testnet":
            raise ValueError("testnet requires BINANCE_ENV=testnet")
        if self.trading_mode == "live" and not (
            self.binance_env == "live"
            and self.live_trading_enabled
            and self.i_understand_live_risk == "YES"
        ):
            raise ValueError("live requires all four local environment conditions")
        if self.trading_mode != "live" and self.live_trading_enabled:
            raise ValueError("live flag conflicts with non-live mode")
        return self

    def public(self) -> dict[str, object]:
        return dict(self.model_dump(mode="json"))

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.public(), sort_keys=True).encode()).hexdigest()


def load_settings(path: Path | None = None) -> Settings:
    values = yaml.safe_load(path.read_text()) if path else {}
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError("configuration must be a mapping")
    if any("secret" in k.lower() or k.lower().endswith(("token", "api_key")) for k in values):
        raise ValueError("secrets must come from environment, never YAML")
    # Environment takes precedence over YAML; BaseSettings constructor normally wins.
    import os

    values = {k: v for k, v in values.items() if k.upper() not in os.environ}
    return Settings(**values)
