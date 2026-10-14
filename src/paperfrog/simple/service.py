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
from paperfrog.settings import Settings
from paperfrog.storage.database import Store
from paperfrog.storage.lock import InstanceLock
from paperfrog.telegram.control import Controls, Telegram

from .data import candles, funding, scan
from .engine import Engine
from .strategy import BAR_MS, WARMUP, choose, indicators, require_paper, strategy_name


class SimpleService:
    def __init__(self, s: Settings):
        require_paper(s)
        self.s = s
        self.directory = s.state_dir / "simple"
        self.store = Store(self.directory / "paperfrog.sqlite")
        self.lake = Lake(s.data_dir / "simple")
        self.exchange = BinanceAdapter(s)
        self.engine: Engine | None = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.selected: list[str] = []
        self.crisis = True
        self.stop = asyncio.Event()
        self.mutex = asyncio.Lock()
        self.controls = Controls(s, self.store, self.resume, self.flatten, self.status)
        self.telegram = (
            Telegram(s, self.store, self.controls)
            if s.telegram_bot_token.get_secret_value()
            else None
        )
        self.auto_start = True

    def now_ms(self) -> int:
        """Use Binance's synchronized UTC clock when checking exchange timestamps."""
        return int(time.time() * 1000) + self.exchange.offset_ms

    async def resume(self) -> None:
        async with self.mutex:
            now = self.now_ms()
            if (
                self.engine is None
                or not self.selected
                or self.crisis
                or any(
                    now - self.rows.get(symbol, {}).get("timestamp_ms", 0) >= 2 * BAR_MS
                    for symbol in self.selected
                )
            ):
                raise SafeError("fresh scan and non-crisis market required")
            await self.engine.resume(now)

    async def flatten(self, operation: str) -> bool:
        async with self.mutex:
            if self.engine is None:
                return False
            self.auto_start = False
            self.store.put("manual_lock", True)
            self.store.put("state", State.PAUSED)
            for symbol in list(self.engine.positions()):
                await self.engine.close(symbol, self.now_ms(), "operator requested flatten")
            return not await self.engine.broker.positions()

    async def status(self, command: str) -> str:
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
                "mode": "paper",
                "strategy": strategy_name(self.s.simple_entry_mode),
                "entry_mode": self.s.simple_entry_mode,
                "state": self.store.get("state"),
                "scan_pool_count": len(self.store.get("simple_pool", [])),
                "selected": self.selected,
                "open_positions": list(self.engine.positions()) if self.engine else [],
                "equity": self.store.get("anchors", {}).get("equity"),
                "loss_lock": self.store.get("risk_lock"),
                "daily_entry_cap": self.s.max_strategy_entries_per_day,
                "per_coin_daily_cap": self.s.max_entries_per_symbol_per_day,
                "live_permitted": False,
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
                pool = list(dict.fromkeys([*snapshot["symbols"], *held, "BTCUSDT"]))

                async def one(
                    symbol: str, boundary: int = boundary, semaphore: asyncio.Semaphore = semaphore
                ) -> tuple[str, dict[str, Any] | None, bool]:
                    async with semaphore:
                        try:
                            frame = indicators(
                                await candles(
                                    self.exchange,
                                    self.lake,
                                    symbol,
                                    boundary - (WARMUP + 96) * BAR_MS,
                                    boundary,
                                )
                            )
                            row = {str(k): v for k, v in frame.iloc[-1].to_dict().items()}
                            blocked = False
                            if symbol == "BTCUSDT":
                                blocked = (
                                    abs(float(frame.close.pct_change().iloc[-1])) >= 0.04
                                    or abs(float(frame.close.pct_change(4).iloc[-1])) >= 0.07
                                )
                            return symbol, row, blocked
                        except (SafeError, ValueError, KeyError):
                            return symbol, None, symbol == "BTCUSDT"

                observations = await asyncio.gather(*(one(symbol) for symbol in pool))
                rows = {symbol: row for symbol, row, _ in observations if row is not None}
                async with self.mutex:
                    self.rows = rows
                    self.crisis = "BTCUSDT" not in rows or any(
                        blocked for _, _, blocked in observations
                    )
                    self.selected = choose(
                        {symbol: rows[symbol] for symbol in snapshot["symbols"] if symbol in rows},
                        self.s,
                    )
                    self.store.put("simple_selected", self.selected)
                    self.store.put("simple_pool", snapshot["symbols"])
                    self.store.put("simple_scan_at_ms", int(time.time() * 1000))
                    self.store.audit(
                        "simple_selection",
                        {
                            "asof_ms": boundary,
                            "pool_size": len(snapshot["symbols"]),
                            "usable_size": len(rows),
                            "selected": self.selected,
                            "selection": "recent trailing liquidity, stable volatility and trend strength",
                            "live_permitted": False,
                        },
                    )
                last_boundary = boundary
            except (SafeError, ValueError, OSError, KeyError) as exc:
                self.store.audit("simple_scan_degraded", {"reason": type(exc).__name__})
                await asyncio.sleep(10)

    async def monitor(self) -> None:
        while not self.stop.is_set():
            try:
                engine = self.engine
                if engine is None:
                    raise SafeError("simple engine unavailable")
                symbols = list(dict.fromkeys([*engine.positions(), *self.selected]))
                fetched = []
                for symbol in symbols:
                    book, premium = await asyncio.gather(
                        self.exchange.request(
                            "GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}
                        ),
                        self.exchange.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}),
                    )
                    now = self.now_ms()
                    if not 0 <= now - int(premium["time"]) <= self.s.market_stale_seconds * 1000:
                        raise SafeError("stale futures mark data")
                    fetched.append(
                        Quote(
                            symbol,
                            Decimal(book["bidPrice"]),
                            Decimal(book["askPrice"]),
                            Decimal(book["bidQty"]),
                            Decimal(book["askQty"]),
                            Decimal(premium["markPrice"]),
                            int(book["time"]),
                        )
                    )
                # Funding after downtime is resolved from actual settlements before fresh trading.
                pending_funding = []
                for symbol, trade in engine.positions().items():
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
                        await engine.quote(quote, now)
                    operation = self.store.get("local_flatten_request")
                    if operation:
                        self.auto_start = False
                        self.store.put("manual_lock", True)
                        self.store.put("state", State.PAUSED)
                        for symbol in list(engine.positions()):
                            await engine.close(symbol, now, "local emergency flatten requested")
                        if not await engine.broker.positions():
                            self.store.put("local_flatten_request", None)
                            self.store.audit("simple_flatten_verified", {"operation": operation})
                    if engine.positions() or self.selected:
                        await engine.monitor(now, self.rows, self.crisis)
                    self.store.put("market_healthy_at", time.time() if fetched else 0)
                    fresh = (
                        bool(self.selected)
                        and not self.crisis
                        and all(
                            0 <= now - self.rows.get(symbol, {}).get("timestamp_ms", 0) < BAR_MS
                            for symbol in self.selected
                        )
                    )
                    if (
                        (self.auto_start or self.store.get("state") == State.DEGRADED)
                        and fresh
                        and not self.store.get("manual_lock")
                        and not self.store.get("risk_lock")
                    ):
                        await engine.resume(now)
                        self.auto_start = False
                        engine.notice(
                            "started",
                            f"Simple futures PAPER started: scanning up to {self.s.simple_scan_limit} coins and selecting up to {self.s.simple_selected_count}. No real-money orders. Entries and closes include reasons.",
                        )
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
                            try:
                                await engine.enter(symbol, self.rows[symbol], now)
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
            except (SafeError, ValueError, KeyError, OSError) as exc:
                self.store.put("state", State.DEGRADED)
                self.store.put("market_healthy_at", 0)
                self.store.audit(
                    "simple_monitor_degraded",
                    {"reason": str(exc) if isinstance(exc, SafeError) else type(exc).__name__},
                )
                self.store.put("heartbeat", time.time())
            await asyncio.sleep(self.s.simple_poll_seconds)

    async def notifications(self) -> None:
        while True:
            if self.telegram:
                notices = self.store.get("simple_notices", {})
                for key, message in notices.items():
                    if self.store.get("simple_notice_sent:" + key):
                        continue
                    try:
                        await self.telegram.notify("simple-" + key, message)
                        self.store.put("simple_notice_sent:" + key, True)
                    except SafeError:
                        self.store.audit(
                            "simple_telegram_degraded",
                            {"reason": "notification unavailable; retry pending"},
                        )
                        break
            await asyncio.sleep(5)

    async def run(self, once: bool = False) -> None:
        with InstanceLock(self.directory):
            tasks: list[asyncio.Task[Any]] = []
            server: uvicorn.Server | None = None
            try:
                await self.exchange.sync_time()
                await self.exchange.exchange_info()
                self.engine = Engine(self.s, self.store, self.exchange.filters)
                await self.engine.restore()
                if self.engine.positions():
                    self.auto_start = False
                    self.store.put("manual_lock", True)
                if once:
                    print(await self.status("/status"))
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
                    return dict(json.loads(await self.status("/status")))

                server = uvicorn.Server(
                    uvicorn.Config(
                        app, host="127.0.0.1", port=self.s.health_port, log_level="warning"
                    )
                )
                tasks = [
                    asyncio.create_task(self.research()),
                    asyncio.create_task(self.monitor()),
                    asyncio.create_task(self.notifications()),
                    asyncio.create_task(server.serve(sockets=[sock])),
                ]
                if self.telegram:
                    tasks.append(asyncio.create_task(self.telegram.poll()))
                stop_task = asyncio.create_task(self.stop.wait())
                tasks.append(stop_task)
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if stop_task not in done:
                    self.store.put("state", State.ERROR_SAFE)
                    self.store.put("manual_lock", True)
                    self.store.audit(
                        "simple_critical_task_stopped",
                        {"action": "stop entries; supervisor restart required"},
                    )
            finally:
                self.store.put("state", State.PAUSED)
                self.store.audit(
                    "simple_shutdown",
                    {"action": "entries paused; PAPER positions/protection journal retained"},
                )
                if server:
                    server.should_exit = True
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                await self.exchange.close()
                if self.telegram:
                    await self.telegram.client.aclose()
                self.store.engine.dispose()
