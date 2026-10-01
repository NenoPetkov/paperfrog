from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import structlog
import uvicorn

from paperfrog import __version__
from paperfrog.data.lake import Lake, backfill, collect_snapshot
from paperfrog.domain.types import Order, Quote, SafeError, State
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.execution.coordinator import Coordinator, client_id
from paperfrog.llm.events import EventFilter
from paperfrog.models.training import Ensemble
from paperfrog.monitoring.health import health_app
from paperfrog.paper.broker import PaperBroker
from paperfrog.portfolio.sizing import estimate_hedge, size_pair
from paperfrog.reporting.readiness import verify_arm
from paperfrog.risk.crisis import CrisisMonitor, Observations
from paperfrog.risk.engine import Health, RiskEngine
from paperfrog.settings import Settings
from paperfrog.storage.database import Store
from paperfrog.storage.lock import InstanceLock
from paperfrog.strategy.engine import Episodes, entry_decision, exit_decision
from paperfrog.telegram.control import Controls, Telegram


class Service:
    def __init__(self, s: Settings):
        self.s = s
        self.store = Store(s.state_dir / "paperfrog.sqlite")
        self.exchange = BinanceAdapter(s)
        self.risk = RiskEngine(s, self.store)
        self.crisis = CrisisMonitor(self.store)
        self.market_history: list[tuple[float, float]] = []
        self.mark_deviation = 0.0
        self.lake = Lake(s.data_dir)
        self.episodes = Episodes(self.store)
        self.events = EventFilter(s, self.store)
        self.model: Ensemble | None = None
        self.quotes: dict[str, Quote] = {}
        self.stop = asyncio.Event()
        self.broker: PaperBroker | BinanceAdapter = self.exchange
        self.coordinator = Coordinator(self.broker, self.store, self.risk, self.exchange.filters)
        self.controls = Controls(s, self.store, self.resume, self.flatten, self.status)
        self.telegram = (
            Telegram(s, self.store, self.controls)
            if s.telegram_bot_token.get_secret_value()
            else None
        )
        self.log = structlog.get_logger()
        self.account_at = 0.0
        self.last_signal_boundary = -1
        self.arm_valid = False
        self.frames: dict[str, pd.DataFrame] = {}

    def load_model(self) -> None:
        approved = self.store.get("approved_model")
        if not approved:
            raise SafeError("no locally promoted model")
        model = Ensemble(self.s.artifacts_dir / approved["directory"], self.s.max_model_age_days)
        if model.hash != approved["hash"]:
            raise SafeError("promoted artifact changed")
        self.model = model

    def health(self) -> Health:
        market_at = self.store.get("market_healthy_at", 0.0)
        return Health(
            bool(self.store.get("reconciled")),
            self.model is not None,
            bool(self.exchange.filters),
            time.time() - market_at,
            time.time() - self.account_at,
            int(self.store.get("crisis_score", 0)),
        )

    async def resume(self) -> None:
        self.load_model()
        if self.s.trading_mode == "live":
            self.arm_valid = verify_arm(self.s, self.model.hash)
        await self.coordinator.reconcile()
        self.risk.resume(self.health(), datetime.now(UTC), self.arm_valid)
        self.exchange.entry_authorized = self.s.trading_mode != "paper"

    async def flatten(self, operation: str) -> bool:
        self.exchange.entry_authorized = False
        return await self.coordinator.flatten(self.quotes, operation)

    async def status(self, command: str) -> str:
        if command == "/positions":
            return json.dumps({k: str(v) for k, v in (await self.broker.positions()).items()})
        if command == "/orders":
            return json.dumps(
                [
                    {"id": r["id"], "symbol": r["symbol"], "state": r["state"]}
                    for r in self.store.rows("orders", 10)
                ]
            )
        if command in {"/risk", "/daily"}:
            return json.dumps(
                {
                    "anchors": self.store.get("anchors"),
                    "lock": self.store.get("risk_lock"),
                    "entries": len(
                        [
                            r
                            for r in self.store.rows("entries", 100)
                            if r["day"] == datetime.now(UTC).date().isoformat()
                        ]
                    ),
                }
            )
        if command == "/last_signal":
            return json.dumps(self.store.get("last_signal", {"reason": "no candidate"}))
        if command == "/version":
            return json.dumps(
                {
                    "version": __version__,
                    "schema": 1,
                    "model": self.model.hash if self.model else None,
                }
            )
        return json.dumps(
            {
                "state": self.store.get("state"),
                "mode": self.s.trading_mode,
                "environment": self.s.binance_env,
                "model": self.model.hash if self.model else None,
                "health": self.health().__dict__,
                "live_armed": self.arm_valid,
                "lock": self.store.get("risk_lock"),
            }
        )

    async def startup(self) -> None:
        self.store.put("state", State.SYNCING)
        self.store.put("reconciled", False)
        self.store.audit("configuration", {"hash": self.s.config_hash, "config": self.s.public()})
        await self.exchange.sync_time()
        await self.exchange.exchange_info()
        if self.s.trading_mode == "paper":
            self.broker = PaperBroker(
                self.store,
                self.exchange.filters,
                Decimal(str(self.s.paper_equity)),
                self.s.paper_seed,
                Decimal(str(self.s.fee_rate)),
                self.s.slippage_bps,
                self.s.latency_ms,
            )
            self.coordinator = Coordinator(
                self.broker, self.store, self.risk, self.exchange.filters
            )
        else:
            await self.exchange.account()
            await self.exchange.verify_risk_settings(self.s.symbols)
            known = {r["id"] for r in self.store.rows("orders", 100000)}
            if any(o["clientOrderId"] not in known for o in await self.exchange.open_orders()):
                raise SafeError("unknown exchange orders; manual reconciliation required")
        if not await self.coordinator.reconcile():
            raise SafeError("startup reconciliation mismatch")
        positions = await self.broker.positions()
        if positions:
            # Restore only locally journaled, verified protection. Unknown legs remain paused.
            for row in self.store.rows("entries", 100000):
                if row["closed"]:
                    continue
                trade = self.store.get("trade:" + row["id"])
                if not trade:
                    raise SafeError("open allocation missing; remain paused")
                stops = await self.exchange.open_stops() if self.s.trading_mode != "paper" else []
                for index, f in enumerate(trade["fills"]):
                    key = client_id(row["id"], f"stop-{index}")
                    if f["symbol"] in positions and not any(
                        x.get("clientAlgoId") == key for x in stops
                    ):
                        trigger = self.exchange.filters[f["symbol"]].round_price(
                            Decimal(f["price"])
                            * (1 - Decimal(str(trade["stop_fraction"])) * f["side"])
                        )
                        await self.broker.protect(f["symbol"], f["side"], trigger, key)
        self.store.put("state", State.PAUSED)
        try:
            self.load_model()
        except SafeError:
            self.store.audit(
                "readiness_blocked", {"reason": "promoted model unavailable; remain in cash"}
            )
        if self.s.trading_mode == "live":
            if self.model is None:
                raise SafeError("live model missing")
            self.arm_valid = verify_arm(self.s, self.model.hash)
        self.account_at = time.time()

    async def monitor(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.exchange.filters:
                    await self.startup()
                held = await self.broker.positions()
                self.mark_deviation = 0.0
                symbols = tuple(dict.fromkeys((*self.s.symbols, *held.keys())))
                for symbol in symbols:
                    snap = await collect_snapshot(self.exchange, self.lake, symbol)
                    quote = Quote(
                        symbol,
                        Decimal(str(snap["bid"])),
                        Decimal(str(snap["ask"])),
                        Decimal(str(snap["bid_size"])),
                        Decimal(str(snap["ask_size"])),
                        Decimal(str(snap["mark"])),
                        int(snap["timestamp_ms"]),
                    )
                    self.quotes[symbol] = quote
                    if symbol in held:
                        self.mark_deviation = max(
                            self.mark_deviation, abs(float(snap["mark"]) / float(snap["index"]) - 1)
                        )
                    if symbol == "BTCUSDT":
                        self.market_history.append((time.time(), float(quote.mid)))
                        self.market_history = [
                            v for v in self.market_history if v[0] >= time.time() - 90000
                        ]
                    if isinstance(self.broker, PaperBroker):
                        stops = await self.broker.quote(quote)
                        if stops:
                            self.store.put("state", State.DEGRADED)
                            await self.flatten("paper-stop-" + str(int(time.time())))
                if any(
                    time.time() * 1000 - q.timestamp_ms > self.s.market_stale_seconds * 1000
                    for q in self.quotes.values()
                ):
                    raise SafeError("stale exchange quotes")
                self.store.put("market_healthy_at", time.time())
                request = self.store.get("local_flatten_request")
                if request:
                    closed = await self.flatten(request)
                    if closed:
                        self.store.put("local_flatten_request", None)
                if isinstance(self.broker, PaperBroker):
                    equity = float(self.broker.equity())
                else:
                    account = await self.exchange.account()
                    equity = float(account["totalMarginBalance"])
                self.account_at = time.time()
                consistent = await self.coordinator.reconcile()
                self.risk.anchors(equity, datetime.now(UTC))
                await self.apply_crisis(not consistent, bool(held))
                actual = await self.broker.positions()
                gross = sum(
                    float(abs(qty) * self.quotes[symbol].mark) for symbol, qty in actual.items()
                )
                if gross > equity * self.s.max_gross_notional and actual:
                    await self.flatten("gross-breach-" + str(int(time.time()) // 60))
                if self.store.get("risk_lock") and held:
                    await self.flatten("risk-" + str(int(time.time()) // 60))
                await self.manage_exits()
                if not consistent and held:
                    await self.flatten("mismatch-" + str(int(time.time()) // 60))
                self.store.put("heartbeat", time.time())
            except (SafeError, ValueError, KeyError, OSError) as exc:
                self.exchange.entry_authorized = False
                self.store.put("state", State.DEGRADED)
                self.store.audit("monitor_degraded", {"reason": type(exc).__name__})
                self.log.warning("monitor_degraded", error_type=type(exc).__name__)
            self.store.put("heartbeat", time.time())
            await asyncio.sleep(10)

    async def signal_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if self.store.get("state") == State.RUNNING:
                    self.load_model()
                    self.risk.require_health(self.health())
                    if self.s.trading_mode == "live" and self.model:
                        self.arm_valid = verify_arm(self.s, self.model.hash)
                    anchors = self.store.get("anchors", {})
                    if anchors:
                        await self.evaluate_candidates(float(anchors["equity"]))
            except (SafeError, ValueError, KeyError, OSError) as exc:
                self.exchange.entry_authorized = False
                self.store.put("state", State.DEGRADED)
                self.store.audit("signal_degraded", {"reason": type(exc).__name__})
            await asyncio.sleep(5)

    async def evaluate_candidates(self, equity: float) -> None:
        boundary = int(time.time()) // 900 * 900000
        if boundary == self.last_signal_boundary:
            return
        self.last_signal_boundary = boundary
        universe = self.store.get("active_universe", {})
        if not universe.get("asof_ms", boundary + 1) <= boundary < universe.get("expires_ms", 0):
            return
        active = [r for r in self.store.rows("entries", 10000) if not r["closed"]]
        if active:  # Conservative v1 avoids shared hedge attribution between simultaneous pairs.
            return
        for symbol in universe["symbols"]:
            path = self.s.data_dir / "features" / f"{symbol}.parquet"
            if not path.exists() or self.model is None:
                continue
            bars = pd.read_parquet(path)
            bars = bars[bars.timestamp_ms <= boundary]
            if bars.empty or int(bars.iloc[-1].timestamp_ms) != boundary:
                continue
            candidate = self.episodes.update(symbol, bars, set(universe["symbols"]))
            if candidate is None:
                continue
            prediction = self.model.predict(bars.tail(1))
            if prediction["severe_drift"][0]:
                self.store.put("state", State.DEGRADED)
                continue
            allowed, edge, reason = entry_decision(
                list(prediction["probabilities"][0]),
                list(prediction["returns"][0]),
                float(prediction["baseline"][0]),
                0,
                0,
                0,
                0,
            )
            self.store.put(
                "last_signal",
                {
                    "symbol": symbol,
                    "probability": float(prediction["probabilities"][0].mean()),
                    "edge": edge,
                    "reason": reason,
                },
            )
            if not allowed:
                continue
            event = await self.events.assess(
                symbol.removesuffix("USDT"), float(bars.iloc[-1].basis_z_30d)
            )
            if not event.allowed:
                continue
            frames = {}
            for asset in (symbol, "BTCUSDT", "ETHUSDT"):
                f = pd.read_parquet(self.s.data_dir / "features" / f"{asset}.parquet")
                frames[asset] = (
                    f[f.timestamp_ms <= boundary].set_index("timestamp_ms").perp_mid.pct_change()
                )
            hedge = estimate_hedge(pd.DataFrame(frames).dropna().to_numpy())
            p, orders = size_pair(
                self.s,
                candidate.episode_id,
                symbol,
                -candidate.crowded_side,
                equity,
                0.01,
                hedge,
                self.quotes,
                self.exchange.filters,
                event.risk_multiplier * self.store.get("risk_multiplier", 1.0),
            )
            if self.s.trading_mode != "paper":
                await self.exchange.verify_risk_settings(tuple(o.symbol for o in orders))
                p = replace(p, liquidation_distance=await self.exchange.liquidation_buffer(orders))
            if self.s.trading_mode == "live":
                self.arm_valid = verify_arm(self.s, self.model.hash)
            self.exchange.entry_authorized = self.s.trading_mode != "paper"
            await self.coordinator.enter(p, orders, self.health(), datetime.now(UTC), 0.01)
            if self.s.trading_mode != "paper":
                try:
                    await self.exchange.verify_liquidation_buffers(0.25)
                except SafeError:
                    await self.flatten("liquidation-buffer-" + candidate.episode_id)
                    raise
            t = self.store.get("trade:" + candidate.episode_id)
            t.update(initial_basis=float(bars.iloc[-1].basis), equity_before=equity)
            self.store.put("trade:" + candidate.episode_id, t)
            break

    async def manage_exits(self) -> None:
        for row in self.store.rows("entries", 10000):
            if row["closed"]:
                continue
            trade = self.store.get("trade:" + row["id"])
            if not trade:
                continue
            positions = await self.broker.positions()
            symbols = {o["symbol"] for o in trade["orders"]}
            if not any(positions.get(symbol) for symbol in symbols):
                current_equity = float(
                    self.store.get("anchors", {}).get("equity", trade.get("equity_before", 0.0))
                )
                if isinstance(self.broker, PaperBroker):
                    current_equity = float(self.broker.equity())
                pnl = current_equity - float(trade.get("equity_before", current_equity))
                self.risk.closed(row["id"], pnl, datetime.now(UTC))
                for symbol in symbols:
                    await self.broker.cancel_protection(symbol)
                continue
            elapsed = (
                datetime.now(UTC) - datetime.fromisoformat(trade["opened_at"])
            ).total_seconds() / 60
            path = self.s.data_dir / "features" / f"{row['symbol']}.parquet"
            if path.exists():
                bars = pd.read_parquet(path)
                bars = bars[bars.timestamp_ms <= int(time.time() * 1000)]
                latest = bars.iloc[-1] if not bars.empty else None
            else:
                latest = None
            pnl = sum(
                float(
                    (self.quotes[f["symbol"]].mark - Decimal(f["price"]))
                    * Decimal(f["quantity"])
                    * f["side"]
                )
                for f in trade["fills"]
                if f["symbol"] in self.quotes
            )
            if isinstance(self.broker, PaperBroker) and "equity_before" in trade:
                pnl = float(self.broker.equity()) - float(trade["equity_before"])
            initial = trade.get("initial_basis", 0.0)
            decision = exit_decision(
                held_minutes=elapsed,
                pnl_r=pnl / trade["risk"],
                basis=float(latest.basis) if latest is not None else initial,
                initial_basis=initial,
                basis_z=float(latest.basis_z_30d) if latest is not None else 10.0,
                basis_change_sigma=0.001,
                partial_taken=trade["partial_taken"],
                portfolio_veto=not self.store.get("reconciled", False),
                event_veto=bool(self.store.get("held_event_veto:" + row["symbol"], False)),
            )
            if not decision.fraction:
                continue
            for f in trade["fills"]:
                positions = await self.broker.positions()
                quantity = min(abs(positions.get(f["symbol"], Decimal(0))), Decimal(f["quantity"]))
                quantity = self.exchange.filters[f["symbol"]].round_quantity(
                    quantity * Decimal(str(decision.fraction))
                )
                if not quantity:
                    continue
                q = self.quotes[f["symbol"]]
                limit = self.exchange.filters[f["symbol"]].round_price(
                    q.bid * Decimal(".98") if f["side"] > 0 else q.ask * Decimal("1.02")
                )
                await self.coordinator.submit_once(
                    Order(
                        client_id=client_id(
                            row["id"], f"exit-{int(time.time()) // 60}-{f['symbol']}"
                        ),
                        symbol=f["symbol"],
                        side=-f["side"],
                        quantity=quantity,
                        limit_price=limit,
                        reduce_only=True,
                    )
                )
            if decision.fraction < 1:
                trade["partial_taken"] = True
                self.store.put("trade:" + row["id"], trade)
            elif not await self.broker.positions():
                for f in trade["fills"]:
                    await self.broker.cancel_protection(f["symbol"])
                self.risk.closed(row["id"], pnl, datetime.now(UTC))

    async def apply_crisis(self, mismatch: bool, held: bool) -> None:
        now = time.time()

        def ret(seconds: int) -> float:
            previous = [v for v in self.market_history if v[0] <= now - seconds]
            return self.market_history[-1][1] / previous[-1][1] - 1 if previous else 0.0

        action = self.crisis.observe(
            now,
            Observations(
                btc_5m=ret(300),
                btc_15m=ret(900),
                btc_1h=ret(3600),
                mark_deviation=self.mark_deviation,
                mismatch=mismatch,
            ),
            emergency=mismatch and held,
        )
        if action.score:
            self.exchange.entry_authorized = False
            self.store.put("state", State.DEGRADED)
        if held and action.reduce_fraction == 1:
            await self.flatten("crisis-" + str(int(now) // 60))
            self.store.put(
                "risk_lock",
                {"state": State.HARD_RISK_LOCK, "until": None, "reason": "crisis_emergency"},
            )
            self.store.put("state", State.HARD_RISK_LOCK)
        elif held and action.reduce_fraction:
            await self.coordinator.reduce(
                self.quotes, action.reduce_fraction, "crisis-reduce-" + str(int(now) // 60)
            )

    async def research_loop(self) -> None:
        from paperfrog.features.pipeline import build_symbol

        last_boundary = -1
        while not self.stop.is_set():
            boundary = int(time.time()) // 900 * 900000
            if boundary != last_boundary:
                for symbol in self.s.symbols:
                    try:
                        await backfill(
                            self.exchange, self.lake, symbol, boundary - 3600000, boundary, "15m"
                        )
                        await asyncio.to_thread(build_symbol, self.s, symbol)
                    except (SafeError, ValueError, OSError, KeyError):
                        self.store.audit("feature_coverage_incomplete", {"symbol": symbol})
                last_boundary = boundary
            await asyncio.sleep(15)

    async def held_event_loop(self) -> None:
        while not self.stop.is_set():
            try:
                held = await self.broker.positions()
                for symbol in held:
                    result = await self.events.assess(symbol.removesuffix("USDT"), 0.0)
                    record = self.store.get("event:" + symbol.removesuffix("USDT"), {})
                    self.store.put(
                        "held_event_veto:" + symbol,
                        bool(record.get("event")) and not result.allowed,
                    )
            except (SafeError, ValueError):
                self.store.audit("held_event_unavailable", {"protective_stops": "retained"})
            await asyncio.sleep(60)

    async def user_stream(self) -> None:
        while not self.stop.is_set():
            try:
                key = await self.exchange.listen_key()

                async def renew() -> None:
                    while True:
                        await asyncio.sleep(25 * 60)
                        await self.exchange.renew_listen_key()

                async with asyncio.TaskGroup() as group:
                    group.create_task(renew())
                    async for event in self.exchange.stream(key, private=True):
                        kind = event.get("e")
                        if kind in {"MARGIN_CALL", "CONDITIONAL_ORDER_TRIGGER_REJECT"}:
                            self.exchange.entry_authorized = False
                            self.store.put(
                                "risk_lock",
                                {"state": State.HARD_RISK_LOCK, "until": None, "reason": kind},
                            )
                            self.store.put("state", State.HARD_RISK_LOCK)
                            self.store.audit("risk_lock", {"reason": kind})
                        if kind == "ACCOUNT_CONFIG_UPDATE":
                            self.exchange.entry_authorized = False
                            self.store.put("state", State.DEGRADED)
                            self.store.put("reconciled", False)
                        if kind == "ACCOUNT_UPDATE":
                            account_update = event.get("a", {})
                            reason = account_update.get("m")
                            if reason == "FUNDING_FEE":
                                self.store.audit(
                                    "funding",
                                    {
                                        "event_time": event["E"],
                                        "balances": account_update.get("B", []),
                                    },
                                )
                            elif reason in {
                                "DEPOSIT",
                                "WITHDRAW",
                                "ADJUSTMENT",
                                "MARGIN_TRANSFER",
                                "ASSET_TRANSFER",
                            }:
                                self.exchange.entry_authorized = False
                                self.store.put("state", State.PAUSED)
                                self.store.put("manual_lock", True)
                                self.store.audit("account_external_change", {"reason": reason})
                        if kind == "ORDER_TRADE_UPDATE":
                            order = event["o"]
                            unique = (
                                f"exchange:{order['s']}:{order['i']}:{order.get('t')}:{order['X']}"
                            )
                            if self.store.once(unique, {"kind": kind, "time": event["E"]}):
                                self.store.audit(
                                    "exchange_order_event",
                                    {
                                        "symbol": order["s"],
                                        "client_id": order["c"],
                                        "status": order["X"],
                                        "executed": order["z"],
                                    },
                                )
                        self.store.put("account_stream_at", time.time())
            except Exception:
                self.store.put("state", State.DEGRADED)
                self.store.put("reconciled", False)
                self.exchange.entry_authorized = False
                await asyncio.sleep(5)
                await self.coordinator.reconcile()

    async def audit_alerts(self) -> None:
        while not self.stop.is_set():
            if self.telegram:
                last = self.store.get("alert_cursor", 0)
                for row in reversed(self.store.rows("audit", 1000)):
                    if row["id"] <= last or row["kind"].startswith("telegram"):
                        continue
                    if row["kind"] in {
                        "risk_lock",
                        "unknown_order",
                        "flatten_result",
                        "order_terminal",
                        "stop_placed",
                        "funding",
                        "crowding",
                        "unwind",
                        "event_assessment",
                        "readiness_blocked",
                        "monitor_degraded",
                    }:
                        try:
                            await self.telegram.notify(
                                row["kind"],
                                row["kind"] + " " + row["payload"][:1800],
                                "URGENT"
                                if row["kind"] in {"risk_lock", "unknown_order"}
                                else "INFO",
                            )
                        except SafeError:
                            break
                    self.store.put("alert_cursor", row["id"])
            await asyncio.sleep(5)

    async def run(self, once: bool = False) -> None:
        with InstanceLock(self.s.state_dir):
            await self._run_owned(once)

    async def _run_owned(self, once: bool = False) -> None:
        try:
            await self.startup()
        except SafeError as exc:
            self.store.put("state", State.ERROR_SAFE)
            self.store.audit("startup_blocked", {"reason": str(exc)})
            if once or self.s.trading_mode == "live":
                await self.exchange.close()
                raise
        if once:
            print(await self.status("/status"))
            await self.exchange.close()
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        server = uvicorn.Server(
            uvicorn.Config(
                health_app(self.store),
                host="127.0.0.1",
                port=self.s.health_port,
                log_level="warning",
            )
        )
        tasks = [
            asyncio.create_task(self.monitor()),
            asyncio.create_task(self.signal_loop()),
            asyncio.create_task(self.held_event_loop()),
            asyncio.create_task(self.research_loop()),
            asyncio.create_task(server.serve()),
            asyncio.create_task(self.audit_alerts()),
        ]
        if self.telegram:
            tasks.append(asyncio.create_task(self.telegram.poll()))
        if self.s.trading_mode != "paper":
            tasks.append(asyncio.create_task(self.user_stream()))
        self.log.info("paperfrog_started", mode=self.s.trading_mode, state=self.store.get("state"))
        stop_task = asyncio.create_task(self.stop.wait())
        done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
        if stop_task not in done:
            self.store.audit(
                "critical_task_stopped", {"action": "safe shutdown; supervisor restart required"}
            )
            self.stop.set()
        stop_task.cancel()
        self.exchange.entry_authorized = False
        self.store.put("state", State.PAUSED)
        self.store.audit("shutdown", {"reason": "signal; protection retained"})
        server.should_exit = True
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.exchange.close()
