from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

from paperfrog.domain.types import SafeError, State
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from . import activity
from .sizing import require_equity_margin_allocation


@dataclass(frozen=True)
class Proposal:
    trade_id: str
    symbol: str
    equity: float
    target: float
    gross: float
    risk: float
    net_beta: float
    stop_fraction: float
    liquidation_distance: float
    participation: float
    available_margin: float | None = None
    quantity_step_notional: float = 0.0
    activity_probe: bool = False


@dataclass(frozen=True)
class Health:
    reconciled: bool
    model_valid: bool
    filters_loaded: bool
    market_age: float
    account_age: float
    crisis_score: int = 0
    event_allowed: bool = True


class RiskEngine:
    def __init__(self, settings: Settings, store: Store, profit_guard_scale: float = 1.0):
        if not math.isfinite(profit_guard_scale) or not 1 <= profit_guard_scale <= 10:
            raise SafeError("invalid profit protection scale")
        if profit_guard_scale != 1 and (
            settings.trading_mode != "paper" or settings.live_trading_enabled
        ):
            raise SafeError("scaled profit protection is offline PAPER-only")
        self.s = settings
        self.store = store
        self.profit_guard_scale = max(settings.profit_guard_scale, profit_guard_scale)

    def anchors(self, equity: float, now: datetime) -> dict[str, object]:
        if not math.isfinite(equity) or equity <= 0 or now.tzinfo is None:
            raise SafeError("invalid equity or clock")
        now = now.astimezone(UTC)
        day, week = now.date().isoformat(), (now.date() - timedelta(days=now.weekday())).isoformat()
        with self.store.transaction() as conn:
            a = self.store.get("anchors", {}, conn)
            if a.get("day") != day:
                a.update(day=day, day_equity=equity, peak=equity)
            if a.get("week") != week:
                a.update(week=week, week_equity=equity)
            a["peak"] = max(a["peak"], equity)
            a["high_water"] = max(a.get("high_water", equity), equity)
            a["equity"] = equity
            self.store.put("anchors", a, conn)
            lock: dict[str, Any] | None = None
            if equity <= a["high_water"] * (1 - self.s.high_water_drawdown) + 1e-9:
                lock = {"state": State.HARD_RISK_LOCK, "until": None, "reason": "high_water"}
            elif equity <= a["week_equity"] * (1 - self.s.weekly_loss_limit) + 1e-9:
                lock = {
                    "state": State.WEEKLY_RISK_LOCK,
                    "until": None,
                    "reason": "weekly_local_review",
                }
            elif equity <= a["day_equity"] * (1 - self.s.daily_loss_limit) + 1e-9:
                lock = {
                    "state": State.DAILY_RISK_LOCK,
                    "until": self._tomorrow(now),
                    "reason": "daily_loss",
                }
            peak_profit = a["peak"] - a["day_equity"]
            protected_profit = a["day_equity"] * 0.0075 * self.profit_guard_scale
            if lock is None and peak_profit >= protected_profit - 1e-9:
                if (
                    a["peak"] - equity
                    >= max(a["day_equity"] * 0.0035 * self.profit_guard_scale, 0.35 * peak_profit)
                    - 1e-9
                ):
                    lock = {
                        "state": State.DAILY_RISK_LOCK,
                        "until": self._tomorrow(now),
                        "reason": "profit_giveback",
                    }
            old = self.store.get("risk_lock", None, conn)
            rank = {
                State.SOFT_RISK_LOCK: 1,
                State.DAILY_RISK_LOCK: 2,
                State.WEEKLY_RISK_LOCK: 3,
                State.HARD_RISK_LOCK: 4,
            }
            if lock and (not old or rank[State(lock["state"])] >= rank[State(old["state"])]):
                self.store.put("risk_lock", lock, conn)
                self.store.transition(lock["state"], str(lock["reason"]), conn)
                if lock != old:
                    self.store.audit("risk_lock", lock, conn)
            self.store.put(
                "risk_multiplier",
                0.5 if peak_profit >= protected_profit - 1e-9 else 1.0,
                conn,
            )
            self.store.put(
                "profit_entry_lock",
                peak_profit >= a["day_equity"] * 0.0125 * self.profit_guard_scale - 1e-9,
                conn,
            )
            return dict(a)

    @staticmethod
    def _tomorrow(now: datetime) -> float:
        return (
            now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ).timestamp()

    def require_health(self, h: Health, *, market_checks: bool = True) -> None:
        if not all((h.reconciled, h.model_valid, h.filters_loaded, h.event_allowed)):
            raise SafeError("entry health gates failed")
        if not 0 <= h.account_age <= self.s.account_stale_seconds:
            raise SafeError("stale or invalid data: exchange account snapshot")
        if market_checks and not 0 <= h.market_age <= self.s.market_stale_seconds:
            raise SafeError("stale or invalid data: missing, expired or future-dated market quote")
        if market_checks and h.crisis_score:
            raise SafeError("crisis entry veto")

    def reserve(self, p: Proposal, h: Health, now: datetime) -> None:
        self.require_health(h)
        values = (
            p.equity,
            p.target,
            p.gross,
            p.risk,
            p.net_beta,
            p.stop_fraction,
            p.liquidation_distance,
            p.participation,
        )
        if (
            not all(math.isfinite(v) for v in values)
            or min(p.equity, p.target, p.gross, p.risk, p.stop_fraction) <= 0
        ):
            raise SafeError("invalid proposal")
        if now.tzinfo is None:
            raise SafeError("UTC clock required")
        now = now.astimezone(UTC)
        day = now.date().isoformat()
        scale = activity.SIZE_SCALE if p.activity_probe else 1.0
        if p.target > p.equity * self.s.max_target_notional * scale + 1e-9 or p.gross < p.target:
            raise SafeError("target cap exceeded")
        if self.s.simple_sizing_mode in {"margin", "equity_range"} and (
            p.target
            > p.equity * self.s.simple_position_margin_cap * self.s.exchange_leverage * scale + 1e-9
        ):
            raise SafeError("position margin cap exceeded")
        if abs(p.net_beta) > self.s.max_net_beta or (
            self.s.simple_sizing_mode != "equity_range"
            and not 0 <= p.participation <= self.s.max_participation
        ):
            raise SafeError("beta or liquidity cap exceeded")
        if self.s.simple_sizing_mode == "equity_range":
            if p.available_margin is None:
                raise SafeError("fresh available margin required for equity range sizing")
            require_equity_margin_allocation(
                self.s, p.equity, p.target, p.available_margin, p.quantity_step_notional,
                activity.SIZE_SCALE if p.activity_probe else 1.0,
            )
        if p.liquidation_distance < max(0.25, 5 * p.stop_fraction):
            raise SafeError("liquidation buffer insufficient")
        with self.store.transaction() as conn:
            if self.store.get("state", State.PAUSED, conn) != State.RUNNING:
                raise SafeError("new entries require RUNNING")
            if self.store.get("risk_lock", None, conn) or self.store.get(
                "manual_lock", False, conn
            ):
                raise SafeError("risk lock active")
            if self.store.get("profit_entry_lock", False, conn):
                raise SafeError("daily profit entry lock")
            if (
                p.risk
                > p.equity * self.s.risk_per_trade * self.store.get("risk_multiplier", 1.0, conn) * scale
                + 1e-9
            ):
                raise SafeError("per trade risk exceeded")
            count: int = conn.execute(
                text("SELECT count(*) FROM entries WHERE day=:d"), {"d": day}
            ).scalar_one()
            scount: int = conn.execute(
                text("SELECT count(*) FROM entries WHERE day=:d AND symbol=:s"),
                {"d": day, "s": p.symbol},
            ).scalar_one()
            if (
                count >= self.s.max_strategy_entries_per_day
                or scount >= self.s.max_entries_per_symbol_per_day
            ):
                raise SafeError("entry frequency cap")
            active = conn.execute(
                text(
                    "SELECT count(*),coalesce(sum(risk),0),coalesce(sum(gross),0) FROM entries WHERE closed=0"
                )
            ).one()
            if active[0] >= self.s.max_concurrent_strategy_trades:
                raise SafeError("concurrent trade cap")
            if active[1] + p.risk > p.equity * self.s.max_open_risk + 1e-9:
                raise SafeError("open risk cap")
            if active[2] + p.gross > p.equity * self.s.max_gross_notional + 1e-9:
                raise SafeError("gross exposure cap")
            if self.s.simple_sizing_mode in {"margin", "equity_range"} and (
                active[2] + p.gross
                > p.equity * self.s.simple_total_margin_cap * self.s.exchange_leverage + 1e-9
            ):
                raise SafeError("total margin cap exceeded")
            if self.store.get("cooldown:" + p.symbol, 0, conn) > now.timestamp():
                raise SafeError("symbol cooldown")
            if p.activity_probe:
                activity.reserve(self.store, p.trade_id, int(now.timestamp() * 1000), conn)
            elif activity.has_open_probe(self.store, conn):
                raise SafeError("active auto probe must close before another entry")
            conn.execute(
                text("INSERT INTO entries(id,day,symbol,ts,risk,gross) VALUES (:i,:d,:s,:t,:r,:g)"),
                {
                    "i": p.trade_id,
                    "d": day,
                    "s": p.symbol,
                    "t": int(now.timestamp() * 1000),
                    "r": p.risk,
                    "g": p.gross,
                },
            )
            self.store.audit("entry_reserved", {"id": p.trade_id, "symbol": p.symbol}, conn)

    def closed(
        self, trade_id: str, pnl: float, now: datetime, conn: Connection | None = None
    ) -> None:
        if conn is None:
            with self.store.transaction() as connection:
                self.closed(trade_id, pnl, now, connection)
            return
        self._closed(trade_id, pnl, now, conn)

    def _closed(self, trade_id: str, pnl: float, now: datetime, conn: Connection) -> None:
        row = conn.execute(
            text("SELECT symbol,closed FROM entries WHERE id=:i"), {"i": trade_id}
        ).one()
        if row.closed:
            return
        conn.execute(text("UPDATE entries SET closed=1 WHERE id=:i"), {"i": trade_id})
        self.store.put(
            "cooldown:" + row.symbol, now.timestamp() + (45 if pnl > 0 else 180) * 60, conn
        )
        losses = (
            0 if pnl > 0 else self.store.get("consecutive_losses", 0, conn) + (1 if pnl < 0 else 0)
        )
        self.store.put("consecutive_losses", losses, conn)
        if pnl > 0:
            self.store.put("loss_cooldown_step", 0, conn)
        if losses >= 3 and not self.store.get("risk_lock", None, conn):
            # Expiry preserves the losing streak. Repeated losses therefore need
            # longer recovery pauses, rather than another immediate trading cycle.
            # Persist the step atomically with the exit and never shorten an active lock.
            step = min(10, max(0, int(self.store.get("loss_cooldown_step", 0, conn))))
            minutes = min(720, self.s.soft_loss_cooldown_minutes * 2**step)
            self.store.put("loss_cooldown_step", min(10, step + 1), conn)
            self.store.put(
                "risk_lock",
                {
                    "state": State.SOFT_RISK_LOCK,
                    "until": now.timestamp() + minutes * 60,
                    "reason": "three_losses",
                    "cooldown_minutes": minutes,
                },
                conn,
            )
            self.store.transition(State.SOFT_RISK_LOCK, "three_losses", conn)
            self.store.audit("risk_lock", self.store.get("risk_lock", None, conn), conn)
        self.store.audit(
            "trade_closed",
            {"id": trade_id, "symbol": row.symbol, "pnl": pnl, "consecutive_losses": losses},
            conn,
        )

    def resume(
        self,
        h: Health,
        now: datetime,
        live_armed: bool = False,
        *,
        override_locks: bool = False,
        equity: float | None = None,
        override_resume_market_checks: bool = False,
    ) -> None:
        if override_resume_market_checks and not override_locks:
            raise SafeError("market-check override requires an explicit operator reset")
        # This override affects the resume acknowledgement only. Reservations and
        # order dispatch still call require_health with all checks enabled.
        self.require_health(h, market_checks=not override_resume_market_checks)
        if now.tzinfo is None:
            raise SafeError("UTC clock required")
        now = now.astimezone(UTC)
        if self.s.trading_mode == "live" and not live_armed:
            raise SafeError("local live arm missing")
        if override_locks and (equity is None or not math.isfinite(equity) or equity <= 0):
            raise SafeError("fresh positive equity required to reset risk locks")
        with self.store.transaction() as conn:
            if self.store.get("local_flatten_request", None, conn):
                raise SafeError("emergency close pending; verify closure before resuming")
            lock = self.store.get("risk_lock", None, conn)
            if override_locks:
                previous = self.store.get("anchors", {}, conn)
                cooldown_keys: list[str] = list(
                    conn.execute(
                        text("SELECT key FROM kv WHERE substr(key,1,9)='cooldown:'")
                    ).scalars()
                )
                cooldowns = {key: self.store.get(key, 0, conn) for key in cooldown_keys}
                anchors = {
                    **previous,
                    "day": now.date().isoformat(),
                    "week": (now.date() - timedelta(days=now.weekday())).isoformat(),
                    "day_equity": equity,
                    "week_equity": equity,
                    "peak": equity,
                    "high_water": equity,
                    "equity": equity,
                }
                self.store.audit(
                    "operator_risk_reset",
                    {
                        "previous_state": self.store.get("state", None, conn),
                        "previous_lock": lock,
                        "previous_anchors": previous,
                        "previous_profit_entry_lock": self.store.get(
                            "profit_entry_lock", False, conn
                        ),
                        "previous_risk_multiplier": self.store.get("risk_multiplier", 1.0, conn),
                        "previous_manual_lock": self.store.get("manual_lock", False, conn),
                        "previous_consecutive_losses": self.store.get(
                            "consecutive_losses", 0, conn
                        ),
                        "previous_loss_cooldown_step": self.store.get(
                            "loss_cooldown_step", 0, conn
                        ),
                        "previous_cooldowns": cooldowns,
                        "equity": equity,
                        "resume_market_checks_overridden": override_resume_market_checks,
                    },
                    conn,
                )
                self.store.put("anchors", anchors, conn)
                self.store.put("profit_entry_lock", False, conn)
                self.store.put("risk_multiplier", 1.0, conn)
                self.store.put("consecutive_losses", 0, conn)
                self.store.put("loss_cooldown_step", 0, conn)
                for key in cooldowns:
                    self.store.put(key, 0, conn)
            else:
                if lock and (lock["until"] is None or now.timestamp() < lock["until"]):
                    raise SafeError("risk lock cannot be overridden automatically; use /resume")
                if self.store.get("profit_entry_lock", False, conn):
                    raise SafeError("profit lock active; use /resume")
            self.store.put("risk_lock", None, conn)
            self.store.put("manual_lock", False, conn)
            self.store.transition(
                State.RUNNING,
                "operator reset risk locks; resume market checks overridden; order gates remain active"
                if override_resume_market_checks
                else "operator reset risk locks; health and execution checks passed"
                if override_locks
                else "health and execution checks passed",
                conn,
            )


def crisis_score(
    *,
    btc_5m: float,
    sigma_5m: float,
    btc_15m: float,
    sigma_15m: float,
    btc_1h: float,
    spread_ratio: float,
    dispersion_z: float,
    mark_deviation: float,
    liquidation_z: float = 0,
    stale_held: bool = False,
    mismatch: bool = False,
    collateral_abnormal: bool = False,
) -> int:
    return sum(
        (
            abs(btc_5m) >= max(0.025, 5 * sigma_5m),
            abs(btc_15m) >= max(0.04, 6 * sigma_15m),
            abs(btc_1h) >= 0.07,
            spread_ratio >= 4,
            dispersion_z >= 5,
            abs(mark_deviation) >= 0.03,
            liquidation_z >= 6,
            stale_held,
            mismatch,
            collateral_abnormal,
        )
    )
