from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from paperfrog.backtest.report import metrics
from paperfrog.data.lake import Lake
from paperfrog.domain.types import Filters, Quote, SafeError, State
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from .engine import Engine
from .strategy import (
    BAR_MS,
    RULES,
    Rules,
    choose,
    entry,
    execution_rules,
    indicators,
    require_paper,
    strategy_name,
)


def load_history(
    lake: Lake, symbols: list[str], start_ms: int, end_ms: int, rules: Rules = RULES
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    binding = lake.root / "market-environment.json"
    if (
        not binding.exists()
        or json.loads(binding.read_text()).get("environment") != "https://fapi.binance.com"
    ):
        raise SafeError("simple replay requires production public data")
    frames, funding = {}, {}
    for symbol in symbols:
        raw = lake.read("perp_15m", symbol)
        if raw.empty:
            raise SafeError("requested symbol history missing")
        raw = raw[raw.timestamp_ms.between(start_ms - rules.warmup_bars * BAR_MS + BAR_MS, end_ms)]
        x = indicators(raw, rules)
        if len(x) != (end_ms - start_ms) // BAR_MS + rules.warmup_bars:
            raise SafeError("requested replay needs complete candles plus the required warm-up")
        if (
            int(x.timestamp_ms.iloc[0]) != start_ms - rules.warmup_bars * BAR_MS + BAR_MS
            or int(x.timestamp_ms.iloc[-1]) != end_ms
        ):
            raise SafeError("requested replay boundaries incomplete")
        f = lake.read("funding", symbol)
        if f.empty or not {"timestamp_ms", "funding_rate", "mark"} <= set(f):
            raise SafeError("actual funding history missing")
        f = f[f.timestamp_ms.between(start_ms - 16 * 3600000, end_ms)]
        if (
            f.empty
            or not np.isfinite(f[["funding_rate", "mark"]].to_numpy()).all()
            or (f.mark <= 0).any()
            or f.timestamp_ms.diff().dropna().max() > 16 * 3600000 + 1000
            or f.timestamp_ms.min() > start_ms
            or f.timestamp_ms.max() < end_ms - 16 * 3600000
        ):
            raise SafeError("funding coverage incomplete; no synthetic settlements permitted")
        frames[symbol], funding[symbol] = x, f
    if "BTCUSDT" not in frames:
        raise SafeError("include BTCUSDT history for the market crisis veto")
    return frames, funding


async def replay(
    frames: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    s: Settings,
    start_ms: int,
    end_ms: int,
    output: Path,
    cost_multiplier: float = 1,
    delay_bars: int = 1,
    cash_only: bool = False,
    rules: Rules | None = None,
    memory_ledger: bool = False,
) -> dict[str, Any]:
    require_paper(s)
    if s.simple_ai_enabled:
        raise SafeError(
            "adaptive AI replay requires recorded forward decisions; historical AI is not fabricated"
        )
    if s.simple_btc_reference_enabled:
        raise SafeError(
            "BTC reference replay needs recorded minute context; legacy replay is not BTC-filter evidence"
        )
    rules = execution_rules(s) if rules is None else rules
    if output.exists() or cost_multiplier < 1 or delay_bars < 1:
        raise SafeError("use a new immutable replay directory and conservative stresses")
    if not frames or "BTCUSDT" not in frames or set(frames) != set(funding):
        raise SafeError("complete aligned frames and funding required")
    output.mkdir(parents=True)
    research = s.model_copy(
        update={
            "fee_rate": s.fee_rate * cost_multiplier,
            "slippage_bps": s.slippage_bps * cost_multiplier,
        }
    )
    # An isolated in-memory research ledger avoids repeated disk sync during a search.
    # The production Store and its durability settings remain unchanged.
    store = Store(Path(":memory:") if memory_ledger else output / "paper-risk.sqlite")
    # Idealized fractional historical filters, not current exchange metadata applied to the past.
    filters = {
        symbol: Filters(
            Decimal(".00000001"),
            Decimal(".00000001"),
            Decimal(".00000001"),
            Decimal("1000000000"),
            Decimal("5"),
        )
        for symbol in frames
    }
    engine = Engine(research, store, filters, rules)
    await engine.restore()
    store.put("state", State.RUNNING)
    records: dict[str, dict[int, dict[str, Any]]] = {
        symbol: {
            int(row["timestamp_ms"]): {str(k): v for k, v in row.items()}
            for row in frame.to_dict("records")
        }
        for symbol, frame in frames.items()
    }
    btc_returns = frames["BTCUSDT"].set_index("timestamp_ms").close.pct_change()
    btc_hour = frames["BTCUSDT"].set_index("timestamp_ms").close.pct_change(4)
    funding_records: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for symbol, settlements in funding.items():
        for funding_row in settlements.to_dict("records"):
            timestamp = int(funding_row["timestamp_ms"])
            if start_ms <= timestamp < end_ms:
                funding_records.setdefault(timestamp // BAR_MS * BAR_MS, []).append(
                    (symbol, {str(k): v for k, v in funding_row.items()})
                )
    skipped: Counter[str] = Counter()
    equity_rows = [{"timestamp_ms": start_ms, "equity": s.paper_equity}]
    selection_rows = []
    half_spread = rules.assumed_half_spread_bps / 10000 * cost_multiplier
    pending: list[tuple[int, str, dict[str, Any]]] = []
    selected: list[str] = []
    last_anchor_day = -1
    equity = float(s.paper_equity)
    active = False
    cached_lock: dict[str, Any] | None = None

    def snapshot(symbol: str, row: dict[str, Any], price: float, timestamp: int) -> Quote:
        visible = Decimal(str(max(float(row["quote_volume"]) / price, 1e-8)))
        mid = Decimal(str(price))
        half = Decimal(str(half_spread))
        return Quote(
            symbol,
            mid * (1 - half),
            mid * (1 + half),
            visible,
            visible,
            Decimal(str(price)),
            timestamp,
        )

    def enqueue(opened: int, current: dict[str, dict[str, Any]]) -> None:
        if not cash_only:
            pending.extend(
                (opened + delay_bars * BAR_MS, symbol, current[symbol])
                for symbol in choose(current, s, rules)
                if entry(
                    current[symbol],
                    research.fee_rate + research.slippage_bps / 10000 + half_spread,
                    s.simple_entry_mode,
                    rules,
                )[0]
            )

    try:
        for opened in range(start_ms, end_ms, BAR_MS):
            previous = {symbol: rows[opened] for symbol, rows in records.items()}
            current = {symbol: rows[opened + BAR_MS] for symbol, rows in records.items()}
            next_selected = choose(previous, s, rules)
            if next_selected != selected:
                selected = next_selected
                store.put("simple_selected", selected)
            selection_rows.append({"timestamp_ms": opened, "symbols": ",".join(selected)})
            due = [p for p in pending if p[0] <= opened]
            pending = [p for p in pending if p[0] > opened]
            locked = cached_lock is not None and (
                cached_lock.get("until") is None or cached_lock["until"] * 1000 > opened
            )
            # Flat cash needs no synthetic quotes, reconciliation or unchanged DB reads.
            # Keep every calendar anchor, selection and pending signal; never reset risk history.
            if not active and (not due or locked):
                if last_anchor_day != opened // 86400000:
                    engine.risk.anchors(equity, datetime.fromtimestamp(opened / 1000, UTC))
                    cached_lock = store.get("risk_lock")
                last_anchor_day = opened // 86400000
                if locked:
                    skipped["risk lock active"] += len(due)
                equity_rows.append({"timestamp_ms": opened + BAR_MS, "equity": equity})
                enqueue(opened, current)
                continue
            for symbol, settlement in funding_records.get(opened, []):
                if int(settlement["timestamp_ms"]) == opened:
                    engine.funding(
                        symbol,
                        opened,
                        Decimal(str(settlement["funding_rate"])),
                        Decimal(str(settlement["mark"])),
                    )
            active_before = active
            needed = (
                (set(selected) | set(engine.positions()) | {symbol for _, symbol, _ in due})
                if due or active_before
                else set()
            )
            for symbol in needed:
                await engine.quote(
                    snapshot(symbol, previous[symbol], float(current[symbol]["open"]), opened),
                    opened,
                )
            crisis = (
                abs(float(btc_returns.loc[opened])) >= 0.04
                or abs(float(btc_hour.loc[opened])) >= 0.07
            )
            if engine.positions():
                await engine.monitor(opened, previous, crisis)
            elif last_anchor_day != opened // 86400000:
                engine.risk.anchors(equity, datetime.fromtimestamp(opened / 1000, UTC))
            last_anchor_day = opened // 86400000
            lock = store.get("risk_lock")
            if (
                lock
                and lock.get("until") is not None
                and lock["until"] * 1000 <= opened
                and not engine.positions()
                and due
            ):
                try:
                    await engine.resume(opened)
                except SafeError:
                    pass
            if not cash_only and not crisis and store.get("state") == State.RUNNING:
                for _, symbol, signal_row in due:
                    try:
                        # Additional stress delays execution but never substitutes later indicators.
                        await engine.enter(symbol, signal_row, opened, delay_bars * BAR_MS)
                    except (SafeError, ValueError) as exc:
                        skipped[
                            str(exc)
                            if isinstance(exc, SafeError)
                            else "exchange filter or sizing rejection"
                        ] += 1
            for symbol, trade in list(engine.positions().items()):
                row = current[symbol]
                touched = (
                    float(row["low"]) <= trade["stop_price"]
                    if trade["side"] > 0
                    else float(row["high"]) >= trade["stop_price"]
                )
                for asset, settlement in funding_records.get(opened, []):
                    if asset == symbol and int(settlement["timestamp_ms"]) > opened:
                        credit = -trade["side"] * float(settlement["funding_rate"])
                        if not touched or credit <= 0:
                            engine.funding(
                                symbol,
                                int(settlement["timestamp_ms"]),
                                Decimal(str(settlement["funding_rate"])),
                                Decimal(str(settlement["mark"])),
                            )
                if touched:
                    adverse = float(row["low"] if trade["side"] > 0 else row["high"])
                    await engine.quote(
                        snapshot(symbol, previous[symbol], adverse, opened + BAR_MS),
                        opened + BAR_MS,
                    )
            for symbol in set(engine.positions()):
                await engine.quote(
                    snapshot(
                        symbol, current[symbol], float(current[symbol]["close"]), opened + BAR_MS
                    ),
                    opened + BAR_MS,
                )
            if due or active_before:
                equity = float(engine.broker.equity())
            if engine.positions():
                engine.risk.anchors(equity, datetime.fromtimestamp((opened + BAR_MS) / 1000, UTC))
            equity_rows.append({"timestamp_ms": opened + BAR_MS, "equity": equity})
            active = bool(engine.positions())
            cached_lock = store.get("risk_lock")
            enqueue(opened, current)
        for symbol in list(engine.positions()):
            await engine.close(symbol, end_ms, "end of historical evaluation")
        equity_rows[-1]["equity"] = float(engine.broker.equity())
        trades = pd.DataFrame(
            store.get("simple_closed", []),
            columns=[
                "timestamp_ms",
                "entry_ms",
                "symbol",
                "direction",
                "pnl",
                "reason",
                "fees",
                "funding",
                "turnover",
            ],
        )
        equity_frame = pd.DataFrame(equity_rows)
        result = metrics(equity_frame, trades)
        result.update(
            {
                "strategy": strategy_name(s.simple_entry_mode, rules),
                "entry_mode": s.simple_entry_mode,
                "entry_profile": s.simple_entry_profile,
                "ending_equity": float(engine.broker.equity()),
                "skipped_entries": dict(skipped),
                "risk_lock_at_end": store.get("risk_lock"),
                "selected_count": s.simple_selected_count,
                "input_symbols": list(frames),
                "execution_model": "idealized_15m_candles",
                "paper_only": True,
                "live_permitted": False,
            }
        )
        trades.to_parquet(output / "trades.parquet", index=False)
        equity_frame.to_parquet(output / "equity.parquet", index=False)
        pd.DataFrame(selection_rows).to_csv(output / "selections.csv", index=False)
        (output / "metrics.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        if memory_ledger:
            raw_connection = store.engine.raw_connection()
            try:
                connection = raw_connection.driver_connection
                if not isinstance(connection, sqlite3.Connection):
                    raise SafeError("research ledger unavailable for archival")
                with sqlite3.connect(output / "paper-risk.sqlite") as destination:
                    connection.backup(destination)
            finally:
                raw_connection.close()
        return result
    finally:
        store.engine.dispose()


async def evaluate(
    frames: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    s: Settings,
    start_ms: int,
    end_ms: int,
    output: Path,
    rules: Rules | None = None,
    memory_ledger: bool = False,
) -> dict[str, Any]:
    require_paper(s)
    if s.simple_btc_reference_enabled:
        raise SafeError(
            "BTC reference replay needs recorded minute context; legacy replay is not BTC-filter evidence"
        )
    rules = execution_rules(s) if rules is None else rules
    if output.exists():
        raise SafeError("use a new immutable simple research directory")
    output.mkdir(parents=True)
    source_paths = list(Path(__file__).parent.glob("*.py")) + [
        Path(__file__).parents[1] / "execution" / "coordinator.py",
        Path(__file__).parents[1] / "risk" / "engine.py",
        Path(__file__).parents[1] / "paper" / "broker.py",
    ]
    source_hashes = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths
    }
    manifest = {
        "source_hashes": source_hashes,
        "rules": rules.public(),
        "strategy": strategy_name(s.simple_entry_mode, rules),
        "entry_mode": s.simple_entry_mode,
        "entry_profile": s.simple_entry_profile,
        "configuration": s.public(),
        "created_at": datetime.now(UTC).isoformat(),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "input_hashes": {
            symbol: hashlib.sha256(
                pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes()
            ).hexdigest()
            for symbol, frame in frames.items()
        },
        "funding_hashes": {
            symbol: hashlib.sha256(
                pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes()
            ).hexdigest()
            for symbol, frame in funding.items()
        },
        "parameter_search": rules != execution_rules(s),
        "live_permitted": False,
        "limitations": [
            "Exploratory retrospective evaluation, not an untouched holdout or live-readiness approval.",
            "Historical input pool is fixed; delisted contracts and point-in-time membership are not proven.",
            "Selection uses only prior closed candles; entries/exits use later opens; stops use adverse candle extremes.",
            "Fractional historical filters and volume-derived depth are idealized, not historical order-book evidence.",
            "Assumed 5bp half-spread plus configured slippage and taker fees; actual historical funding rates/marks.",
            "Trailing/monitor updates occur every 15 minutes here; forward PAPER monitors every few seconds.",
            "Positive intrabar funding credits withheld if stop ordering is unknown.",
            "Risk counters and locks persist across every day and coin selection; no monthly resets.",
            "Frozen deterministic rules; no automatic learning or parameter changes after losses.",
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    cases = {}
    for name, multiplier, delay, cash in (
        ("baseline", 1, 1, False),
        ("2x_cost", 2, 1, False),
        ("extra_bar_delay", 1, 2, False),
        ("cash", 1, 1, True),
    ):
        cases[name] = await replay(
            frames,
            funding,
            s,
            start_ms,
            end_ms,
            output / name,
            multiplier,
            delay,
            cash,
            rules,
            memory_ledger,
        )
    if any(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
        for path, digest in source_hashes.items()
    ):
        raise SafeError("implementation changed during research; do not use partial results")
    result = {
        "evaluations": cases,
        "live_permitted": False,
        "validation": "exploratory_research_only",
    }
    (output / "evaluation.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    pd.DataFrame(
        [
            {
                "case": name,
                **{
                    key: metrics.get(key)
                    for key in (
                        "net_return",
                        "trades",
                        "profit_factor",
                        "max_drawdown",
                        "ending_equity",
                    )
                },
            }
            for name, metrics in cases.items()
        ]
    ).to_csv(output / "comparison.csv", index=False)
    return result
