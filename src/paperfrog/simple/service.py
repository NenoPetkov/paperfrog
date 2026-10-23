from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from decimal import Decimal
from typing import Any

import uvicorn

from paperfrog.data.lake import Lake
from paperfrog.domain.types import Quote, SafeError, State
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.monitoring.health import health_app
from paperfrog.paper.broker import PaperBroker
from paperfrog.settings import Settings
from paperfrog.storage.database import Store
from paperfrog.storage.lock import InstanceLock
from paperfrog.telegram.control import Controls, Telegram
from paperfrog.telegram.events import EventNotifications
from paperfrog.telegram.formatting import positions_text, status_text

from . import confidence, rotation
from .adaptive import Advisor, enrich, request_limit
from .analysis import refresh_reports
from .authorization import directory, verify_simple_arm
from .bitcoin import BTC, MINUTE_BARS, MINUTE_MS, closed_reference, fast_reference, pair_features
from .blockers import explain
from .data import candles, funding, normalize, quote_snapshot, scan
from .engine import Engine
from .live_broker import LiveBroker
from .market_trend import day_context, market_context
from .market_trend import entry_gate as market_entry_gate
from .minute import HISTORY_BARS
from .minute import direction as minute_direction
from .minute import frame as minute_frame
from .strategy import BAR_MS, choose, execution_rules, indicators, require_execution, strategy_name


class SimpleService:
    def __init__(self, s: Settings):
        require_execution(s)
        if s.simple_ai_enabled and not s.openai_api_key.get_secret_value():
            raise SafeError("adaptive AI requires a locally configured OpenAI API key")
        self.s = s
        self.rules = execution_rules(s)
        self.directory = directory(s)
        self.store = Store(self.directory / "paperfrog.sqlite")
        self.advisor = Advisor(s, self.store, clock=self.now_ms) if s.simple_ai_enabled else None
        self.lake = Lake(
            s.data_dir / ("simple-testnet" if s.trading_mode == "testnet" else "simple")
        )
        self.exchange = BinanceAdapter(s)
        self.engine: Engine | None = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.held_rows: dict[str, dict[str, Any]] = {}
        self.context_rows: dict[str, dict[str, Any]] = {}
        self.minute_frames: dict[str, Any] = {}
        self.selected: list[str] = []
        self.crisis = True
        self.scan_complete = False
        self.initial_scan_deadline = time.monotonic() + s.market_stale_seconds
        self.stop = asyncio.Event()
        self.mutex = asyncio.Lock()
        self.controls = Controls(
            s,
            self.store,
            self.resume,
            self.flatten,
            self.status,
            force_resume=self.refuse_force,
            strategy=self.strategy_control,
        )
        self.telegram = (
            Telegram(s, self.store, self.controls)
            if s.telegram_bot_token.get_secret_value()
            else None
        )
        self.auto_start = True

    def now_ms(self) -> int:
        """Use Binance's synchronized UTC clock when checking exchange timestamps."""
        return int(time.time() * 1000) + self.exchange.offset_ms

    def require_resume_market(self) -> None:
        if self.engine is None:
            raise SafeError("execution engine is not ready")
        if self.crisis:
            raise SafeError("BTC crisis gate active or reference scan unavailable")
        if not self.selected:
            raise SafeError("no eligible coins in the latest scan; waiting for a qualifying setup")
        now = self.now_ms()
        if any(
            not 0
            <= now - self.rows.get(symbol, {}).get("timestamp_ms", 0)
            < self.s.simple_entry_bar_ms
            for symbol in self.selected
        ):
            raise SafeError("closed-candle scan is missing, expired or future-dated")

    async def resume(self) -> None:
        await self.resume_operator(force=False)

    async def refuse_force(self) -> None:
        await self.resume_operator(force=True)

    async def resume_operator(self, *, force: bool) -> None:
        async with self.mutex:
            if self.engine is None:
                raise SafeError("execution engine is not ready")
            if not force:
                self.require_resume_market()
            await self.engine.resume(
                self.now_ms(),
                override_locks=True,
                override_resume_market_checks=force,
                before_reset=None if force else self.require_resume_market,
            )
            self.auto_start = False

    async def flatten(self, operation: str) -> bool:
        async with self.mutex:
            if self.engine is None:
                return False
            self.auto_start = False
            self.store.put("manual_lock", True)
            self.store.transition(State.PAUSED, "operator requested closing all positions")
            for symbol in list(self.engine.positions()):
                await self.engine.close(symbol, self.now_ms(), "operator requested flatten")
            return not await self.engine.broker.positions()

    async def strategy_control(self, choice: str | None) -> str:
        async with self.mutex:
            if choice is not None:
                if self.engine is None:
                    raise SafeError("execution engine is not ready")
                rotation.configure(self.store, choice, self.now_ms())
            description = rotation.describe(self.store, self.s.display_timezone)
            if self.s.simple_confidence_enabled:
                result = confidence.state(self.store)
                description += (
                    f"\nConfidence: {result['wins']} wins / {result['losses']} losses / "
                    f"{result['breakevens']} breakevens; win streak {result['streak']}."
                    f"\nNet target: {confidence.target(self.store):.2%} of entry notional. "
                    f"Awaiting verified accounting: {len(result['pending'])}."
                )
            return description

    async def status(self, command: str) -> str:
        positions = (
            self.engine.positions() if self.engine else self.store.get("simple_positions", {})
        )
        if command == "/status":
            return status_text(self.store.get("state", State.BOOTING), positions)
        if command == "/positions":
            return positions_text(positions)
        return await self.diagnostics(command)

    async def diagnostics(self, command: str) -> str:
        armed = False
        if self.s.trading_mode == "live":
            with contextlib.suppress(SafeError):
                armed = verify_simple_arm(self.s)
        if command == "/why":
            return await asyncio.to_thread(
                explain, self.s, self.store, self.now_ms(), self.crisis, armed
            )
        if command == "/positions":
            return json.dumps(self.engine.positions() if self.engine else {})
        if command == "/last_signal":
            return json.dumps(self.store.get("last_signal", {"reason": "no qualifying candidate"}))
        if command == "/orders":
            return json.dumps(
                [
                    {"symbol": r["symbol"], "state": r["state"]}
                    for r in self.store.rows("orders", 10)
                ]
            )
        return json.dumps(
            {
                "mode": self.s.trading_mode,
                "account_mode": self.s.futures_account_mode,
                "strategy": ("adaptive-ai-v1+" if self.s.simple_ai_enabled else "")
                + strategy_name(self.s.simple_entry_mode, self.rules)
                + (
                    rotation.VERSION
                    if rotation.selection(self.store)["active"] == "opposite"
                    else ""
                ),
                "strategy_rotation": rotation.selection(self.store),
                "confidence": {
                    **confidence.state(self.store),
                    "enabled": self.s.simple_confidence_enabled,
                    "target_fraction": confidence.target(self.store),
                },
                "entry_mode": self.s.simple_entry_mode,
                "entry_profile": self.s.simple_entry_profile,
                "entry_timeframe": self.s.simple_entry_timeframe,
                "ai_review_minutes": self.s.simple_ai_review_minutes,
                "risk_context_timeframe": "15m",
                "loaded_settings": self.s.simple_parameters(),
                "breakout_minutes": 5 * self.s.simple_entry_bar_ms // MINUTE_MS
                if self.s.simple_entry_timeframe in {"1m", "5m", "8m"}
                else self.rules.breakout_bars * BAR_MS // 60000,
                "state": self.store.get("state"),
                "scan_pool_count": len(self.store.get("simple_pool", [])),
                "selected": self.selected,
                "open_positions": list(self.engine.positions()) if self.engine else [],
                "equity": self.store.get("anchors", {}).get("equity"),
                "loss_lock": self.store.get("risk_lock"),
                "daily_entry_cap": self.s.max_strategy_entries_per_day,
                "per_coin_daily_cap": self.s.max_entries_per_symbol_per_day,
                "live_permitted": armed,
                "live_armed": armed,
                "research_requirements": "waived_by_operator"
                if self.s.simple_live_research_waiver
                else "required",
                "profitability": "unproven",
                "market_trend": {
                    "enabled": self.s.simple_market_trend_enabled,
                    "context": self.store.get("simple_market_trend"),
                    "policy": "coin 4h return, regression, mean and 1h trend must align; current requires aligned market breadth and BTC/ETH; opposite also accepts a verified MIXED market",
                    "mixed_entries_allowed": rotation.selection(self.store)["active"] == "opposite",
                },
                "daily_analysis": {
                    "directory": str(
                        self.s.reports_dir / ("simple-" + self.s.trading_mode) / "daily"
                    ),
                    "timezone": "UTC",
                    "status": self.store.get("simple_daily_analysis_status", "pending"),
                },
                "adaptive_ai": {
                    "enabled": self.s.simple_ai_enabled,
                    "review_minutes": self.s.simple_ai_review_minutes,
                    "max_requests_per_utc_day": request_limit(self.s),
                    "model": self.s.openai_primary_model if self.s.simple_ai_enabled else None,
                    "status": self.store.get("simple_ai_status"),
                    "last_review": self.store.get("simple_ai_active"),
                    "last_usage": self.store.get("simple_ai_last_usage"),
                    "learning": "persisted context and scored forecasts; no model retraining",
                },
                "btc_reference": {
                    "enabled": self.s.simple_btc_reference_enabled,
                    "direction_policy": self.s.simple_btc_direction_policy,
                    "tradable": False,
                    "closed": self.store.get("simple_btc_closed"),
                    "fast": self.store.get("simple_btc_fast"),
                    "eligible_pairs": sum(
                        p.get("btc_pair_valid") is True
                        for p in self.store.get("simple_btc_pairs", {}).values()
                    ),
                    "role": "deterministic direction policy, shock checks and measured lag; independent of OpenAI",
                },
            }
        )

    async def research(self) -> None:
        last_boundary = -1
        while not self.stop.is_set():
            boundary = self.now_ms() // BAR_MS * BAR_MS
            if boundary == last_boundary:
                await asyncio.sleep(2)
                continue
            try:
                await self.exchange.sync_time()
                snapshot = await scan(self.exchange, self.s)
                semaphore = asyncio.Semaphore(3)
                held = list(self.engine.positions()) if self.engine else []
                pool = list(dict.fromkeys([*snapshot["symbols"], *held, "BTCUSDT", "ETHUSDT"]))

                async def one(
                    symbol: str, boundary: int = boundary, semaphore: asyncio.Semaphore = semaphore
                ) -> tuple[str, dict[str, Any] | None, bool, Any]:
                    async with semaphore:
                        try:
                            frame = await asyncio.to_thread(
                                indicators,
                                await candles(
                                    self.exchange,
                                    self.lake,
                                    symbol,
                                    boundary - (self.rules.warmup_bars + 96) * BAR_MS,
                                    boundary,
                                ),
                                self.rules,
                            )
                            if self.advisor is not None or self.s.simple_entry_timeframe in {
                                "1m",
                                "5m",
                                "8m",
                            }:
                                frame = await asyncio.to_thread(enrich, frame)
                            row = {str(k): v for k, v in frame.iloc[-1].to_dict().items()}
                            if self.s.simple_market_trend_enabled:
                                row.update(await asyncio.to_thread(day_context, frame, boundary))
                                for key, value in row.items():
                                    if key.startswith("day_"):
                                        frame.loc[frame.index[-1], key] = value
                            blocked = False
                            if symbol == "BTCUSDT":
                                blocked = (
                                    abs(float(frame.close.pct_change().iloc[-1])) >= 0.04
                                    or abs(float(frame.close.pct_change(4).iloc[-1])) >= 0.07
                                )
                            return symbol, row, blocked, frame
                        except (SafeError, ValueError, KeyError):
                            return symbol, None, symbol == "BTCUSDT", None

                observations = await asyncio.gather(*(one(symbol) for symbol in pool))
                rows = {symbol: row for symbol, row, _, _ in observations if row is not None}
                market = None
                if self.s.simple_market_trend_enabled:
                    try:
                        market = market_context(rows, snapshot["symbols"], boundary)
                        market["config_hash"] = self.s.config_hash
                    except SafeError:
                        # Publish unavailability rather than retaining an old permission.
                        market = None
                reference: dict[str, Any] | None = None
                pairs: dict[str, dict[str, Any]] = {}
                if self.s.simple_btc_reference_enabled:
                    frames = {
                        symbol: frame for symbol, _, _, frame in observations if frame is not None
                    }

                    def context(
                        data: dict[str, Any], asof_ms: int
                    ) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
                        btc = data.get(BTC)
                        if btc is None:
                            return None, {}
                        return closed_reference(btc, self.rules), {
                            symbol: pair_features(frame, btc, asof_ms)
                            for symbol, frame in data.items()
                            if symbol != BTC
                        }

                    reference, pairs = await asyncio.to_thread(context, frames, boundary)
                    for symbol, row in rows.items():
                        if symbol != BTC:
                            row.update(pairs.get(symbol, {"btc_pair_valid": False}))
                async with self.mutex:
                    if self.s.simple_market_trend_enabled:
                        self.store.put("simple_market_trend", market)
                        self.store.audit(
                            "simple_market_trend",
                            market or {"status": "unavailable", "asof_ms": boundary},
                        )
                    if self.s.simple_btc_reference_enabled:
                        self.store.put(
                            "simple_btc_closed",
                            {
                                **reference,
                                "config_hash": self.s.config_hash,
                            }
                            if reference
                            else None,
                        )
                        self.store.put("simple_btc_pairs", pairs)
                    self.context_rows = rows
                    if self.s.simple_entry_timeframe == "15m":
                        self.rows = rows
                    self.crisis = "BTCUSDT" not in rows or any(
                        blocked for _, _, blocked, _ in observations
                    )
                    context_selected = choose(
                        {
                            symbol: rows[symbol]
                            for symbol in snapshot["symbols"]
                            if symbol in rows
                            and (
                                not self.s.simple_market_trend_enabled
                                or any(
                                    market_entry_gate(
                                        self.s, self.store, rows[symbol], side, self.now_ms()
                                    )
                                    is None
                                    for side in (-1, 1)
                                )
                            )
                        },
                        self.s,
                        self.rules,
                    )
                    if self.s.simple_entry_timeframe == "15m":
                        self.selected = context_selected
                        self.store.put("simple_selected", self.selected)
                    self.store.put("simple_pool", snapshot["symbols"])
                    self.store.put("simple_scan_at_ms", int(time.time() * 1000))
                    self.store.audit(
                        "simple_selection"
                        if self.s.simple_entry_timeframe == "15m"
                        else "simple_risk_context",
                        {
                            "asof_ms": boundary,
                            "entry_timeframe": "15m",
                            "trend_hours": 4,
                            "confirmation_hours": 1,
                            "pool_size": len(snapshot["symbols"]),
                            "usable_size": len(rows),
                            "selected": context_selected,
                            "selection": "recent trailing liquidity, stable volatility and trend strength",
                            "execution_mode": self.s.trading_mode,
                        },
                    )
                    if self.s.simple_entry_timeframe == "15m":
                        self.scan_complete = True
                last_boundary = boundary
                if self.advisor is not None and self.s.simple_entry_timeframe == "15m":
                    await self.advisor.review(
                        {
                            symbol: frame
                            for symbol, _, _, frame in observations
                            if frame is not None
                        },
                        list(self.selected),
                        boundary,
                    )
            except (SafeError, ValueError, OSError, KeyError) as exc:
                self.store.audit("simple_scan_degraded", {"reason": type(exc).__name__})
                await asyncio.sleep(10)

    async def entry_frame(
        self, symbol: str, timeframe: str, boundary: int, context: dict[str, Any]
    ) -> Any:
        interval_ms = {"1m": MINUTE_MS, "5m": 5 * MINUTE_MS, "8m": 8 * MINUTE_MS}[timeframe]
        async with asyncio.timeout(12):
            raw = await self.exchange.request(
                "GET",
                "/fapi/v1/klines",
                {
                    "symbol": symbol,
                    "interval": "1m" if timeframe == "8m" else timeframe,
                    "limit": HISTORY_BARS * 8 if timeframe == "8m" else HISTORY_BARS,
                    "startTime": boundary - HISTORY_BARS * interval_ms,
                    "endTime": boundary - 1,
                },
            )
        data = await asyncio.to_thread(minute_frame, raw, context, boundary, timeframe)
        columns = [
            "timestamp_ms",
            "open_ms",
            "open",
            "high",
            "low",
            "close",
            "quote_volume",
            "taker_buy_quote",
        ]
        if timeframe == "8m":
            # Archive the exact minute evidence, appending only newly available rows.
            source = normalize(raw, boundary, MINUTE_MS)
            key = "simple_8m_source_saved:" + symbol
            source = source[source.timestamp_ms > self.store.get(key, 0)]
            await asyncio.to_thread(
                self.lake.append, "perp_1m", symbol, source, "binance_simple_closed_minute_klines"
            )
            self.store.put(key, boundary)
        archived = data[columns].copy()
        archived.attrs = {}
        await asyncio.to_thread(
            self.lake.append,
            "perp_" + timeframe,
            symbol,
            archived,
            "binance_simple_aggregated_1m_klines"
            if timeframe == "8m"
            else "binance_simple_closed_minute_klines",
        )
        return data

    async def refresh_held_minutes(self) -> None:
        """Keep existing positions on their recorded candle timeframe after a switch."""
        positions = self.engine.positions() if self.engine else {}
        held = {
            symbol: trade
            for symbol, trade in positions.items()
            if trade.get("entry_timeframe") in {"1m", "5m", "8m"}
            and trade["entry_timeframe"] != self.s.simple_entry_timeframe
        }
        self.held_rows = {symbol: row for symbol, row in self.held_rows.items() if symbol in held}

        async def one(symbol: str, trade: dict[str, Any]) -> None:
            timeframe = trade["entry_timeframe"]
            interval = {"1m": MINUTE_MS, "5m": 5 * MINUTE_MS, "8m": 8 * MINUTE_MS}[timeframe]
            boundary = self.now_ms() // interval * interval
            if self.held_rows.get(symbol, {}).get("timestamp_ms") == boundary:
                return
            context = self.context_rows.get(symbol)
            if context is None or not 0 <= boundary - int(context.get("timestamp_ms", 0)) < BAR_MS:
                return
            try:
                data = await self.entry_frame(symbol, timeframe, boundary, context)
                if not 0 <= self.now_ms() - boundary < interval:
                    raise SafeError("original-timeframe monitoring scan expired")
                self.held_rows[symbol] = data.iloc[-1].to_dict()
            except (
                SafeError,
                ValueError,
                OSError,
                KeyError,
                TypeError,
                IndexError,
                TimeoutError,
            ) as exc:
                self.store.audit(
                    "simple_held_data_degraded",
                    {"symbol": symbol, "error_type": type(exc).__name__},
                )

        await asyncio.gather(*(one(symbol, trade) for symbol, trade in held.items()))

    async def held_research(self) -> None:
        while not self.stop.is_set():
            try:
                await self.refresh_held_minutes()
            except (
                SafeError,
                ValueError,
                OSError,
                KeyError,
                TypeError,
                IndexError,
                TimeoutError,
            ) as exc:
                self.store.audit("simple_held_data_degraded", {"error_type": type(exc).__name__})
            await asyncio.sleep(1)

    async def refresh_minutes(self, boundary: int) -> None:
        """Rank the liquid pool on each configured entry-candle boundary."""
        interval_ms = self.s.simple_entry_bar_ms
        if not self.context_rows:
            return
        pool = list(
            dict.fromkeys(
                [
                    *self.store.get("simple_pool", []),
                    *(self.engine.positions() if self.engine else {}),
                ]
            )
        )
        contexts = self.context_rows
        context_at = int(contexts.get(BTC, {}).get("timestamp_ms", 0))
        if self.s.simple_entry_timeframe == "8m" and boundary < context_at <= self.now_ms():
            # On a late start, the newest completed 15m context can be newer
            # than the last 8m bar. Wait for the next bar without using future
            # context or reporting ordinary readiness as an operational error.
            return
        if context_at != boundary // BAR_MS * BAR_MS:
            raise SafeError("waiting for current closed 15-minute risk context")
        semaphore = asyncio.Semaphore(5)

        async def one(symbol: str) -> tuple[str, Any]:
            context = contexts.get(symbol)
            if context is None or symbol == BTC:
                return symbol, None
            async with semaphore:
                try:
                    data = await self.entry_frame(
                        symbol, self.s.simple_entry_timeframe, boundary, context
                    )
                    return symbol, data
                except (SafeError, ValueError, TypeError, KeyError, IndexError, TimeoutError):
                    return symbol, None

        observations = await asyncio.gather(*(one(symbol) for symbol in pool))
        if not 0 <= self.now_ms() - boundary < interval_ms:
            raise SafeError("entry-candle scan expired; new entries blocked")
        frames = {symbol: data for symbol, data in observations if data is not None}
        rows = {symbol: data.iloc[-1].to_dict() for symbol, data in frames.items()}
        eligible = {}
        trend_blocks = {}
        for symbol, row in rows.items():
            side, _ = minute_direction(row, self.rules)
            if not side:
                continue
            reason = market_entry_gate(self.s, self.store, row, side, self.now_ms())
            if reason:
                trend_blocks[symbol] = reason
            else:
                eligible[symbol] = row
        async with self.mutex:
            strategy = rotation.selection(self.store)
            selected = choose(eligible, self.s, self.rules)
            self.rows, self.minute_frames, self.selected = rows, frames, selected
            # Missing observations immediately revoke dispatch permission for that symbol.
            for symbol in pool:
                self.store.put("simple_minute:" + symbol, rows.get(symbol))
            self.store.put("simple_selected", selected)
            self.store.put("simple_scan_at_ms", self.now_ms())
            self.store.audit(
                "simple_selection",
                {
                    "asof_ms": boundary,
                    "entry_timeframe": self.s.simple_entry_timeframe,
                    "trend_hours": 4,
                    "confirmation_hours": 1,
                    "pool_size": len(pool),
                    "usable_size": len(rows),
                    "selected": selected,
                    "selection": f"closed {self.s.simple_entry_timeframe} EMA 5/20, aligned 1/3/5-bar slope and flow",
                    "execution_mode": self.s.trading_mode,
                    "market_trend": self.store.get("simple_market_trend")
                    if self.s.simple_market_trend_enabled
                    else None,
                    "day_trend_blocks": trend_blocks,
                    "strategy": strategy["active"],
                    "strategy_blocks": {},
                    "signals": {
                        symbol: {
                            key: rows[symbol][key]
                            for key in (
                                "close",
                                "trend_atr",
                                "flow",
                                *(
                                    f"return_{bars * interval_ms // MINUTE_MS}m"
                                    for bars in (1, 3, 5)
                                ),
                            )
                        }
                        for symbol in selected
                    },
                },
            )
            self.scan_complete = True

    async def minute_research(self) -> None:
        last_boundary = -1
        interval_ms = self.s.simple_entry_bar_ms
        while not self.stop.is_set():
            boundary = self.now_ms() // interval_ms * interval_ms
            if boundary == last_boundary or not self.context_rows:
                await asyncio.sleep(1)
                continue
            try:
                await self.refresh_minutes(boundary)
                last_boundary = boundary
            except (SafeError, ValueError, OSError, KeyError) as exc:
                self.store.audit("simple_minute_degraded", {"error_type": type(exc).__name__})
                await asyncio.sleep(5)

    async def minute_ai_reviews(self) -> None:
        """Review fresh minute observations on the locally configured AI cadence."""
        last_boundary = -1
        while not self.stop.is_set():
            review_ms = self.s.simple_ai_review_bar_ms
            boundary = self.now_ms() // review_ms * review_ms
            frames = {
                symbol: data[data.timestamp_ms <= boundary]
                for symbol, data in self.minute_frames.items()
            }
            selected = [
                symbol
                for symbol in self.selected
                if (
                    symbol in frames
                    and len(frames[symbol]) >= 61
                    and int(frames[symbol].iloc[-1].timestamp_ms) == boundary
                )
            ]
            if boundary == last_boundary or not selected or self.advisor is None:
                await asyncio.sleep(1)
                continue
            await self.advisor.review(frames, selected, boundary)
            last_boundary = boundary

    async def bitcoin_reference(self) -> None:
        """Refresh the independent short-term BTC gate every closed minute."""
        last_boundary = -1
        while not self.stop.is_set():
            boundary = self.now_ms() // MINUTE_MS * MINUTE_MS
            if boundary == last_boundary:
                await asyncio.sleep(1)
                continue
            try:
                async with asyncio.timeout(12):
                    raw = await self.exchange.request(
                        "GET",
                        "/fapi/v1/klines",
                        {
                            "symbol": BTC,
                            "interval": "1m",
                            "limit": MINUTE_BARS,
                            "startTime": boundary - MINUTE_BARS * MINUTE_MS,
                            "endTime": boundary - 1,
                        },
                    )
                reference = await asyncio.to_thread(fast_reference, raw, boundary)
                if not 0 <= self.now_ms() - boundary < 2 * MINUTE_MS:
                    raise SafeError("BTC minute response expired")
                reference["config_hash"] = self.s.config_hash
                self.store.put("simple_btc_fast", reference)
                self.store.audit("simple_btc_fast", reference)
                last_boundary = boundary
            except (SafeError, ValueError, KeyError, TypeError, IndexError, TimeoutError) as exc:
                self.store.put("simple_btc_fast", None)
                self.store.audit("simple_btc_unavailable", {"error_type": type(exc).__name__})
                await asyncio.sleep(5)

    async def fetch_quote(self, symbol: str) -> Quote:
        return await quote_snapshot(self.exchange, symbol)

    async def monitor(self) -> None:
        while not self.stop.is_set():
            try:
                engine = self.engine
                if engine is None:
                    raise SafeError("simple engine unavailable")
                if isinstance(engine.broker, LiveBroker):
                    await engine.broker.refresh()
                    await engine.broker.refresh_funding(self.now_ms())
                    if self.store.get("state") == State.ERROR_SAFE:
                        async with self.mutex:
                            await engine.recover_execution()
                symbols = list(dict.fromkeys([*engine.positions(), *self.selected]))
                semaphore = asyncio.Semaphore(3)

                async def one(symbol: str, semaphore: asyncio.Semaphore = semaphore) -> Quote:
                    async with semaphore:
                        return await self.fetch_quote(symbol)

                fetched = await asyncio.gather(*(one(symbol) for symbol in symbols))
                # Funding after downtime is resolved from actual settlements before fresh trading.
                pending_funding = []
                for symbol, trade in engine.positions().items():
                    if not isinstance(engine.broker, PaperBroker):
                        continue
                    now = self.now_ms()
                    last = int(
                        self.store.get("simple_funding_checked:" + symbol, trade["opened_ms"])
                    )
                    if now - last >= 60000:
                        await funding(
                            self.exchange,
                            self.lake,
                            symbol,
                            max(0, last - 60000),
                            now + 1,
                            allow_empty=True,
                        )
                        history = self.lake.read("funding", symbol)
                        if not history.empty:
                            for row in history[
                                history.timestamp_ms.between(trade["opened_ms"] + 1, now)
                            ].to_dict("records"):
                                pending_funding.append((symbol, row))
                        self.store.put("simple_funding_checked:" + symbol, now)
                async with self.mutex:
                    now = self.now_ms()
                    # Apply funding to holdings before an observed stop can close them.
                    for symbol, row in pending_funding:
                        engine.funding(
                            symbol,
                            int(row["timestamp_ms"]),
                            Decimal(str(row["funding_rate"])),
                            Decimal(str(row["mark"])),
                        )
                    for quote in fetched:
                        await engine.quote(quote, self.now_ms())
                    operation = self.store.get("local_flatten_request")
                    if operation:
                        self.auto_start = False
                        self.store.put("manual_lock", True)
                        self.store.transition(State.PAUSED, "local emergency close requested")
                        for symbol in list(engine.positions()):
                            await engine.close(symbol, now, "local emergency flatten requested")
                        if not await engine.broker.positions():
                            self.store.put("local_flatten_request", None)
                            self.store.audit("simple_flatten_verified", {"operation": operation})
                    if engine.positions() or self.selected:
                        observed_scan = (
                            self.scan_complete or bool(self.rows) or bool(self.held_rows)
                        )
                        pending_original = any(
                            trade.get("entry_timeframe") in {"1m", "5m", "8m"}
                            and trade["entry_timeframe"] != self.s.simple_entry_timeframe
                            and self.held_rows.get(symbol, {}).get("entry_timeframe")
                            != trade["entry_timeframe"]
                            for symbol, trade in engine.positions().items()
                        )
                        awaiting_initial_scan = (
                            not observed_scan or pending_original
                        ) and time.monotonic() < self.initial_scan_deadline
                        await engine.monitor(
                            self.now_ms(),
                            {
                                **self.context_rows,
                                **{
                                    symbol: {**row, "risk_context": self.context_rows.get(symbol)}
                                    for symbol, row in self.rows.items()
                                },
                                **{
                                    symbol: row
                                    for symbol, row in self.held_rows.items()
                                    if engine.positions().get(symbol, {}).get("entry_timeframe")
                                    == row.get("entry_timeframe")
                                },
                            },
                            self.crisis if observed_scan else False,
                            awaiting_initial_scan=awaiting_initial_scan,
                        )
                    self.store.put("market_healthy_at", time.time() if fetched else 0)
                    now = self.now_ms()
                    fresh = (
                        bool(self.selected)
                        and not self.crisis
                        and all(
                            0
                            <= now - self.rows.get(symbol, {}).get("timestamp_ms", 0)
                            < self.s.simple_entry_bar_ms
                            for symbol in self.selected
                        )
                        and all(
                            symbol in engine.broker.quotes
                            and 0
                            <= now - engine.broker.quotes[symbol].timestamp_ms
                            <= self.s.market_stale_seconds * 1000
                            for symbol in self.selected
                        )
                    )
                    if (
                        (self.auto_start or self.store.get("state") == State.DEGRADED)
                        and self.store.get("state") not in {State.ERROR_SAFE, State.RUNNING}
                        and fresh
                        and not self.store.get("manual_lock")
                        and not self.store.get("risk_lock")
                    ):
                        await engine.resume(now)
                        self.auto_start = False
                    lock = self.store.get("risk_lock")
                    if (
                        lock
                        and lock.get("until") is not None
                        and lock["until"] * 1000 <= now
                        and fresh
                        and not self.store.get("manual_lock")
                    ):
                        await engine.resume(now)
                    if (
                        self.store.get("state") == State.RUNNING
                        and fresh
                        and not self.store.get("manual_lock")
                        and not self.store.get("local_flatten_request")
                    ):
                        for symbol in self.selected:
                            if self.store.get("state") != State.RUNNING:
                                break
                            try:
                                if isinstance(engine.broker, LiveBroker):
                                    await engine.broker.refresh()
                                await engine.enter(symbol, self.rows[symbol], self.now_ms())
                            except (SafeError, ValueError) as exc:
                                self.store.audit(
                                    "simple_entry_blocked",
                                    {
                                        "symbol": symbol,
                                        "reason": str(exc)
                                        if isinstance(exc, SafeError)
                                        else "sizing or filter validation",
                                    },
                                )
                    self.store.put("heartbeat", time.time())
                if self.s.simple_confidence_enabled and isinstance(engine.broker, LiveBroker):
                    await confidence.reconcile(self.s, self.store, self.exchange, self.now_ms())
            except (SafeError, ValueError, KeyError, OSError) as exc:
                self.exchange.entry_authorized = False
                reason = str(exc) if isinstance(exc, SafeError) else type(exc).__name__
                if (
                    not self.store.get("risk_lock")
                    and not self.store.get("manual_lock")
                    and self.store.get("state") != State.ERROR_SAFE
                ):
                    self.store.transition(State.DEGRADED, reason)
                self.store.put("market_healthy_at", 0)
                self.store.audit(
                    "simple_monitor_degraded",
                    {"reason": reason},
                )
                self.store.put("heartbeat", time.time())
            await asyncio.sleep(self.s.simple_poll_seconds)

    async def notifications(self) -> None:
        while True:
            if self.telegram:
                await EventNotifications(self.s, self.store, self.telegram).deliver()
            await asyncio.sleep(5)

    async def daily_analysis(self) -> None:
        folder = self.s.reports_dir / ("simple-" + self.s.trading_mode) / "daily"
        while True:
            try:
                # Analysis runs off the execution loop and writes only local reports.
                trades = self.store.get("simple_closed", [])
                await asyncio.to_thread(refresh_reports, trades, folder, self.now_ms())
                self.store.put("simple_daily_analysis_status", "ready")
            except Exception as exc:
                # Optional reporting must not interrupt monitoring or hosted protection.
                # Exception text can contain private data; retain only its class.
                self.store.put("simple_daily_analysis_status", "unavailable")
                self.store.audit(
                    "simple_daily_analysis_unavailable", {"error_type": type(exc).__name__}
                )
            await asyncio.sleep(60)

    async def run(self, once: bool = False) -> None:
        with InstanceLock(self.directory):
            tasks: list[asyncio.Task[Any]] = []
            server: uvicorn.Server | None = None
            try:
                await self.exchange.sync_time()
                await self.exchange.exchange_info()
                live_broker = None
                if self.s.trading_mode != "paper":
                    if self.s.trading_mode == "live":
                        verify_simple_arm(self.s)
                    live_broker = LiveBroker(self.s, self.store, self.exchange)
                    await live_broker.preflight()
                self.engine = Engine(
                    self.s, self.store, self.exchange.filters, self.rules, broker=live_broker
                )
                await self.engine.restore()
                rotation.initialize(self.store, self.engine.strategy, self.now_ms())
                self.store.put("operating_mode", self.s.trading_mode)
                self.store.put("simple_loaded_settings", self.s.simple_parameters())
                if not once:
                    self.store.audit("simple_process_started", {"mode": self.s.trading_mode})
                if live_broker is not None:
                    from datetime import UTC, datetime

                    self.engine.risk.anchors(float(live_broker.equity()), datetime.now(UTC))
                    self.store.audit(
                        "research_waiver", {"operator_waived": True, "profitability": "unproven"}
                    )
                if self.engine.positions():
                    self.auto_start = self.s.trading_mode != "paper"
                    if self.s.trading_mode == "paper":
                        self.store.put("manual_lock", True)
                if once:
                    print(await self.diagnostics("/status"))
                    return
                # Bind synchronously before tasks can place simulated entries or poll Telegram.
                import socket

                sock = socket.socket()
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", self.s.health_port))
                except OSError:
                    sock.close()
                    raise SafeError(
                        "health port occupied; stop the old service before starting simple"
                    ) from None
                sock.listen(128)
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, self.stop.set)
                app = health_app(self.store)

                @app.get("/simple")
                async def simple_status() -> dict[str, Any]:
                    return dict(json.loads(await self.diagnostics("/status")))

                server = uvicorn.Server(
                    uvicorn.Config(
                        app, host="127.0.0.1", port=self.s.health_port, log_level="warning"
                    )
                )
                tasks = [
                    asyncio.create_task(self.research(), name="market research"),
                    asyncio.create_task(self.monitor(), name="trade monitoring"),
                    asyncio.create_task(self.notifications(), name="Telegram notifications"),
                    asyncio.create_task(self.daily_analysis(), name="daily trade analysis"),
                    asyncio.create_task(server.serve(sockets=[sock]), name="health server"),
                ]
                if self.s.simple_btc_reference_enabled:
                    tasks.append(
                        asyncio.create_task(self.bitcoin_reference(), name="BTC minute reference")
                    )
                if self.s.simple_entry_timeframe in {"1m", "5m", "8m"}:
                    tasks.append(
                        asyncio.create_task(self.minute_research(), name="short-interval entries")
                    )
                    if self.advisor is not None:
                        tasks.append(
                            asyncio.create_task(self.minute_ai_reviews(), name="AI entry policy")
                        )
                if any(
                    trade.get("entry_timeframe") in {"1m", "5m", "8m"}
                    and trade["entry_timeframe"] != self.s.simple_entry_timeframe
                    for trade in self.engine.positions().values()
                ):
                    tasks.append(
                        asyncio.create_task(
                            self.held_research(), name="original-timeframe monitoring"
                        )
                    )
                if self.telegram:
                    tasks.append(
                        asyncio.create_task(self.telegram.poll(), name="Telegram commands")
                    )
                stop_task = asyncio.create_task(self.stop.wait())
                tasks.append(stop_task)
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if stop_task not in done:
                    failed = next(task for task in tasks if task in done)
                    error = None if failed.cancelled() else failed.exception()
                    detail = type(error).__name__ if error else "task exited"
                    self.store.transition(
                        State.ERROR_SAFE,
                        f"{failed.get_name()} stopped ({detail}); restart required",
                    )
                    self.store.put("manual_lock", True)
                    self.store.audit(
                        "simple_critical_task_stopped",
                        {
                            "action": "stop entries; supervisor restart required",
                            "task": failed.get_name(),
                            "error_type": detail,
                        },
                    )
            finally:
                self.store.transition(
                    State.PAUSED, "bot process stopped; hosted protection retained"
                )
                self.store.audit(
                    "simple_shutdown",
                    {"action": "entries paused; positions and hosted protection retained"},
                )
                if server:
                    server.should_exit = True
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                await self.exchange.close()
                if self.advisor is not None:
                    await self.advisor.close()
                if self.telegram:
                    if tasks:
                        with contextlib.suppress(SafeError, TimeoutError):
                            async with asyncio.timeout(3):
                                await EventNotifications(
                                    self.s, self.store, self.telegram
                                ).deliver()
                    await self.telegram.client.aclose()
                self.store.engine.dispose()
