"""Explicitly validated higher-risk budget for isolated offline PAPER research only."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from paperfrog.settings import Settings


class OfflineResearchSettings(Settings):
    trading_mode: Literal["paper"] = "paper"
    live_trading_enabled: Literal[False] = False
    strategy_profile: Literal["simple"] = "simple"
    risk_per_trade: float = Field(0.02, gt=0, le=0.02)
    max_open_risk: float = Field(0.04, gt=0, le=0.04)
    max_target_notional: float = Field(1.0, gt=0, le=1.0)
    max_gross_notional: float = Field(1.5, gt=0, le=1.5)
    max_net_beta: float = Field(1.5, gt=0, le=1.5)
    daily_loss_limit: float = Field(0.05, gt=0, le=0.05)
    weekly_loss_limit: float = Field(0.10, gt=0, le=0.10)
    high_water_drawdown: float = Field(0.20, gt=0, le=0.20)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Offline budgets come only from validated public inputs, never credentials or env flags.
        return (init_settings,)

    @model_validator(mode="after")
    def isolated_budget(self) -> Self:
        if self.risk_per_trade > self.max_open_risk:
            raise ValueError("offline per-trade risk exceeds the total open-risk budget")
        return self


def offline_budget(base: Settings) -> OfflineResearchSettings:
    values = base.public()
    for key in (
        "risk_per_trade",
        "max_open_risk",
        "max_target_notional",
        "max_gross_notional",
        "max_net_beta",
        "daily_loss_limit",
        "weekly_loss_limit",
        "high_water_drawdown",
    ):
        values.pop(key)
    # Values are validated by the subclass; never bypass Settings constraints with model_copy.
    return OfflineResearchSettings.model_validate(values)
