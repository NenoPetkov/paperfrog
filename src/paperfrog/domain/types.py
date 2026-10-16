from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SafeError(Exception):
    """Only fixed, secret-free messages cross service boundaries."""


class UnknownOrder(SafeError):
    """The exchange may have accepted the request; never resubmit blindly."""


class OrderNotFound(SafeError):
    """A lookup found no order; this never permits submitting it again."""


class State(StrEnum):
    BOOTING = "BOOTING"
    SYNCING = "SYNCING"
    READY_PAPER = "READY_PAPER"
    READY_TESTNET = "READY_TESTNET"
    READY_LIVE_DISARMED = "READY_LIVE_DISARMED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DEGRADED = "DEGRADED"
    SOFT_RISK_LOCK = "SOFT_RISK_LOCK"
    DAILY_RISK_LOCK = "DAILY_RISK_LOCK"
    WEEKLY_RISK_LOCK = "WEEKLY_RISK_LOCK"
    HARD_RISK_LOCK = "HARD_RISK_LOCK"
    EMERGENCY_FLATTENING = "EMERGENCY_FLATTENING"
    ERROR_SAFE = "ERROR_SAFE"


class Order(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    client_id: str = Field(pattern=r"^pf-[a-zA-Z0-9-]{1,32}$")
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,24}USDT$")
    side: int = Field(ge=-1, le=1)
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
    reduce_only: bool = False

    @model_validator(mode="after")
    def nonzero(self) -> Order:
        if self.side == 0:
            raise ValueError("side must be -1 or 1")
        return self


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    client_id: str
    symbol: str
    side: int
    quantity: Decimal = Field(ge=0)
    price: Decimal = Field(ge=0)
    fee: Decimal = Field(ge=0)
    status: str
    exchange_id: str


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    mark: Decimal
    timestamp_ms: int

    def __post_init__(self) -> None:
        if not all(
            x.is_finite() for x in (self.bid, self.ask, self.bid_size, self.ask_size, self.mark)
        ):
            raise SafeError("nonfinite quote")
        if (
            self.bid <= 0
            or self.ask < self.bid
            or self.mark <= 0
            or min(self.bid_size, self.ask_size) < 0
        ):
            raise SafeError("invalid quote")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Filters:
    step: Decimal
    tick: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal

    def round_quantity(self, quantity: Decimal) -> Decimal:
        return (quantity / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step

    def round_price(self, price: Decimal) -> Decimal:
        return (price / self.tick).to_integral_value(rounding=ROUND_DOWN) * self.tick

    def validate(self, order: Order) -> None:
        if self.step <= 0 or self.tick <= 0:
            raise SafeError("invalid exchange filters")
        if order.quantity != self.round_quantity(order.quantity):
            raise SafeError("quantity violates lot size")
        if order.limit_price != self.round_price(order.limit_price):
            raise SafeError("price violates tick size")
        if not self.min_quantity <= order.quantity <= self.max_quantity:
            raise SafeError("quantity outside filters")
        if not order.reduce_only and order.quantity * order.limit_price < self.min_notional:
            raise SafeError("minimum notional not met")


class Broker(Protocol):
    async def submit(self, order: Order) -> Fill: ...
    async def query(self, symbol: str, client_id: str) -> Fill: ...
    async def positions(self) -> dict[str, Decimal]: ...
    async def cancel_entries(self) -> None: ...
    async def cancel_protection(self, symbol: str) -> None: ...
    async def protect(self, symbol: str, side: int, trigger: Decimal, key: str) -> None: ...
