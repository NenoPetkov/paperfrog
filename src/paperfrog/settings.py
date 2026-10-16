from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SimpleEntryMode = Literal["trend", "reverse", "failed_breakout"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="forbid", frozen=True, hide_input_in_errors=True
    )
    trading_mode: Literal["paper", "testnet", "live"] = "paper"
    strategy_profile: Literal["simple", "legacy"] = "simple"
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
    soft_loss_cooldown_minutes: int = Field(720, ge=1, le=720)
    max_strategy_entries_per_day: int = Field(12, ge=0, le=16)
    max_entries_per_symbol_per_day: int = Field(2, ge=1, le=2)
    max_concurrent_strategy_trades: int = Field(2, ge=1, le=2)
    risk_per_trade: float = Field(0.002, gt=0, le=0.04)
    max_open_risk: float = Field(0.005, gt=0, le=0.05)
    max_target_notional: float = Field(0.08, gt=0, le=1.6)
    max_gross_notional: float = Field(0.5, gt=0, le=2.0)
    max_net_beta: float = Field(0.10, gt=0, le=2.0)
    daily_loss_limit: float = Field(0.0125, gt=0, le=0.0125)
    weekly_loss_limit: float = Field(0.035, gt=0, le=0.035)
    high_water_drawdown: float = Field(0.06, gt=0, le=0.06)
    exchange_leverage: Literal[2] = 2
    margin_type: Literal["ISOLATED", "CROSSED"] = "ISOLATED"
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
    simple_scan_limit: int = Field(100, ge=2, le=100)
    simple_selected_count: int = Field(2, ge=1, le=10)
    simple_poll_seconds: int = Field(5, ge=2, le=30)
    simple_entry_mode: SimpleEntryMode = "trend"
    simple_entry_profile: Literal["standard", "active"] = "standard"
    simple_max_hold_hours: int = Field(4, ge=1, le=24)
    simple_sizing_mode: Literal["risk", "margin"] = "risk"
    simple_position_margin_cap: float = Field(0.8, gt=0, le=0.8)
    simple_total_margin_cap: float = Field(1.0, gt=0, le=1.0)
    futures_account_mode: Literal["isolated", "credits"] = "isolated"
    simple_live_research_waiver: bool = False

    @model_validator(mode="after")
    def validate_sizing_budget(self) -> Self:
        if self.simple_position_margin_cap > self.simple_total_margin_cap:
            raise ValueError("position margin cap exceeds total margin cap")
        if self.simple_sizing_mode == "margin":
            if self.strategy_profile != "simple":
                raise ValueError("margin sizing requires the simple profile")
        elif (
            self.risk_per_trade > 0.002
            or self.max_open_risk > 0.005
            or self.max_target_notional > 0.08
            or self.max_gross_notional > 0.5
            or self.max_net_beta > 0.10
        ):
            raise ValueError("larger limits require explicit simple margin sizing")
        return self

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        if self.simple_entry_profile == "active" and self.simple_entry_mode != "trend":
            raise ValueError("active entry profile requires trend mode")
        if self.futures_account_mode == "credits" and self.strategy_profile != "simple":
            raise ValueError("credits accounts require the simple execution profile")
        if (self.futures_account_mode == "credits") != (self.margin_type == "CROSSED"):
            raise ValueError(
                "credits accounts require CROSSED margin; isolated accounts require ISOLATED"
            )
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

    def simple_parameters(self) -> dict[str, object]:
        return dict(
            self.model_dump(
                mode="json",
                include={
                    "simple_selected_count",
                    "simple_entry_mode",
                    "simple_entry_profile",
                    "simple_max_hold_hours",
                    "simple_sizing_mode",
                    "simple_position_margin_cap",
                    "simple_total_margin_cap",
                    "soft_loss_cooldown_minutes",
                    "risk_per_trade",
                    "max_open_risk",
                    "max_target_notional",
                    "max_gross_notional",
                    "max_net_beta",
                    "max_concurrent_strategy_trades",
                },
            )
        )

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
