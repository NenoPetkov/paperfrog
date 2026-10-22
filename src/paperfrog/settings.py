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
    max_strategy_entries_per_day: int = Field(12, ge=0, le=40)
    max_entries_per_symbol_per_day: int = Field(2, ge=1, le=4)
    max_concurrent_strategy_trades: int = Field(2, ge=1, le=4)
    risk_per_trade: float = Field(0.002, gt=0, le=0.04)
    max_open_risk: float = Field(0.005, gt=0, le=0.05)
    max_target_notional: float = Field(0.08, gt=0, le=1.6)
    max_gross_notional: float = Field(0.5, gt=0, le=2.0)
    max_net_beta: float = Field(0.10, gt=0, le=2.0)
    daily_loss_limit: float = Field(0.0125, gt=0, le=0.125)
    weekly_loss_limit: float = Field(0.035, gt=0, le=0.35)
    high_water_drawdown: float = Field(0.06, gt=0, le=0.60)
    profit_guard_scale: float = Field(1.0, ge=1, le=10)
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
    simple_entry_timeframe: Literal["1m", "5m", "8m", "15m"] = "15m"
    simple_entry_mode: SimpleEntryMode = "trend"
    simple_entry_profile: Literal["standard", "active"] = "standard"
    simple_max_hold_hours: int = Field(4, ge=1, le=24)
    simple_sizing_mode: Literal["risk", "margin", "equity_range"] = "risk"
    simple_position_margin_cap: float = Field(0.8, gt=0, le=0.8)
    simple_total_margin_cap: float = Field(1.0, gt=0, le=1.0)
    futures_account_mode: Literal["isolated", "credits"] = "isolated"
    simple_live_research_waiver: bool = False
    simple_confidence_enabled: bool = False
    simple_ai_enabled: bool = False
    simple_ai_review_minutes: int = Field(15, ge=1, le=15)
    simple_ai_output_limit: int = Field(4000, ge=512, le=8192)
    simple_btc_reference_enabled: bool = True
    simple_btc_direction_policy: Literal["strict", "neutral_allowed"] = "strict"
    simple_market_trend_enabled: bool = True

    @model_validator(mode="after")
    def validate_sizing_budget(self) -> Self:
        if self.simple_position_margin_cap > self.simple_total_margin_cap:
            raise ValueError("position margin cap exceeds total margin cap")
        if self.simple_sizing_mode in {"margin", "equity_range"}:
            if self.strategy_profile != "simple":
                raise ValueError("margin sizing requires the simple profile")
            if self.simple_sizing_mode == "equity_range" and not (
                0.1 <= self.simple_position_margin_cap <= 0.5
            ):
                raise ValueError("equity range sizing requires a 10% to 50% position margin cap")
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
        if self.simple_ai_review_minutes not in {1, 5, 8, 15}:
            raise ValueError("AI review interval must be 1, 5, 8 or 15 minutes")
        if (self.simple_entry_timeframe == "8m") != (self.simple_ai_review_minutes == 8):
            raise ValueError("eight-minute entries require eight-minute AI review cadence")
        if self.simple_ai_review_minutes == 1 and self.simple_entry_timeframe != "1m":
            raise ValueError("one-minute AI reviews require one-minute entry candles")
        if self.simple_ai_review_minutes == 5 and self.simple_entry_timeframe == "15m":
            raise ValueError("five-minute AI reviews require one- or five-minute entry candles")
        if self.simple_entry_timeframe in {"1m", "5m", "8m"} and (
            self.strategy_profile != "simple" or self.simple_entry_mode != "trend"
        ):
            raise ValueError("short-interval entries require the simple trend profile")
        if self.simple_ai_enabled and (
            self.strategy_profile != "simple" or self.simple_entry_mode != "trend"
        ):
            raise ValueError("adaptive AI requires the simple trend profile")
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
        parameters = dict(
            self.model_dump(
                mode="json",
                include={
                    "simple_selected_count",
                    "simple_entry_mode",
                    "simple_entry_profile",
                    "simple_entry_timeframe",
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
                    "daily_loss_limit",
                    "weekly_loss_limit",
                    "high_water_drawdown",
                    "profit_guard_scale",
                    "simple_confidence_enabled",
                    "simple_ai_enabled",
                    "simple_ai_review_minutes",
                    "simple_ai_output_limit",
                    "simple_btc_reference_enabled",
                    "simple_btc_direction_policy",
                    "simple_market_trend_enabled",
                },
            )
        )
        if self.simple_sizing_mode == "equity_range":
            parameters.update(
                simple_position_margin_min=0.1,
                simple_remaining_margin_fallback=True,
                simple_liquidity_cap_enabled=False,
            )
        return parameters

    @property
    def simple_entry_bar_ms(self) -> int:
        return {"1m": 60_000, "5m": 300_000, "8m": 480_000, "15m": 900_000}[
            self.simple_entry_timeframe
        ]

    @property
    def simple_ai_review_bar_ms(self) -> int:
        return self.simple_ai_review_minutes * 60_000

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
