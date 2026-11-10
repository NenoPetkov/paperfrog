"""Indicative monthly fixed-ROE portfolio replay using public historical inputs.

Reuses live indicators, classification, bracket rounding and RiskEngine. It does
not instantiate an exchange adapter, read env credentials, or submit orders.
Historical depth, spread, latency and historical exchange-filter revisions are
not reconstructed; this is a candle-model estimate, not an exact live backtest.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Literal

import httpx
import numpy as np
import pandas as pd
from pyarrow import ArrowInvalid
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from paperfrog.domain.types import SafeError, State
from paperfrog.risk.engine import Health, Proposal, RiskEngine
from paperfrog.settings import Settings
from paperfrog.simple.fixed_roe import trigger
from paperfrog.simple.learning_replay import prepare
from paperfrog.simple.momentum import classify
from paperfrog.simple.strategy import execution_rules
from paperfrog.storage.database import Store
from scripts.fetch_fixed_roe_month import DAY, FIVE, PublicHistory, eligible_metadata

INITIAL = 10_000.0
SPREAD = 0.0003  # full 3bp spread, the active entry cap; historical quotes unavailable
HEALTH = Health(True, True, True, 0, 0)


class ResearchMemoryStore(Store):
    """Same transactional schema and RiskEngine, in an isolated ephemeral DB."""

    def __init__(self, path: Path):
        self.engine = create_engine("sqlite://", poolclass=StaticPool)
        self.migrate()


class PublicReplaySettings(Settings):
    trading_mode: Literal["paper"] = "paper"
    live_trading_enabled: Literal[False] = False

    @classmethod
    def settings_customise_sources(
        cls, settings_cls: type[BaseSettings], init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource, dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def settings() -> PublicReplaySettings:
    # Read-only production parameter snapshot 2026-11-09; no secrets/account state.
    return PublicReplaySettings(
        strategy_profile="simple", simple_btc_reference_enabled=False,
        simple_market_trend_enabled=False,
        simple_exit_policy="fixed_roe", simple_entry_profile="momentum",
        simple_entry_timeframe="5m", simple_selected_count=1, simple_scan_limit=100,
        simple_sizing_mode="margin", simple_position_margin_cap=0.1,
        simple_total_margin_cap=1, max_concurrent_strategy_trades=4,
        max_strategy_entries_per_day=12, max_entries_per_symbol_per_day=2,
        risk_per_trade=0.04, max_open_risk=0.05, max_target_notional=1.6,
        max_gross_notional=2, max_net_beta=2, daily_loss_limit=0.125,
        weekly_loss_limit=0.35, high_water_drawdown=0.6, profit_guard_scale=10,
        soft_loss_cooldown_minutes=30, max_spread=SPREAD, max_participation=0.01,
        fee_rate=0.0005, slippage_bps=2, simple_ai_enabled=False,
        simple_confidence_enabled=False, simple_carry_enabled=False,
    )


def source_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = [Path(__file__), root / "scripts/fetch_fixed_roe_month.py"]
    paths += [root / f"src/paperfrog/{v}.py" for v in (
        "simple/minute", "simple/strategy", "simple/momentum", "simple/learning_replay",
        "simple/fixed_roe", "risk/engine", "settings")]
    return hashlib.sha256(b"".join(p.read_bytes() for p in paths)).hexdigest()


def features(folder: Path, *, ready_only: bool = False) -> None:
    s, manifest = settings(), json.loads((folder / "input-manifest.json").read_text())
    rules = execution_rules(s)
    output = folder / "signals"
    output.mkdir(exist_ok=True)
    key = source_hash()
    for n, symbol in enumerate(manifest["candidate_symbols"], 1):
        if ready_only and not (folder / "candles" / f"{symbol}.parquet").exists():
            continue
        target = output / f"{symbol}.parquet"
        stamp = target.with_suffix(".sha256")
        if target.exists() and stamp.exists() and stamp.read_text() == key:
            continue
        try:
            raw = pd.read_parquet(folder / "candles" / f"{symbol}.parquet")
        except ArrowInvalid:
            if ready_only:
                continue
            raise
        frame = prepare(raw, s)["5m"]
        frame = frame[(frame.timestamp_ms >= manifest["start_ms"]) &
                      (frame.timestamp_ms < manifest["end_ms"]) &
                      (frame.daily_volume >= s.min_daily_quote_volume)]
        rows = []
        for row in frame.to_dict("records"):
            _, side, score = classify(row, s.fee_rate + s.slippage_bps / 10000 + SPREAD / 2, rules)
            if side:
                rows.append({k: row[k] for k in ("timestamp_ms", "close", "minute_atr")} |
                            {"side": side, "score": score * (1 - (2*s.fee_rate + 2*s.slippage_bps/10000 + SPREAD)/0.025)})
        temporary = target.with_suffix(".tmp")
        pd.DataFrame(rows, columns=["timestamp_ms", "close", "minute_atr", "side", "score"]).to_parquet(temporary, index=False)
        temporary.replace(target)
        stamp.write_text(key)
        print(f"signals {n}/{len(manifest['candidate_symbols'])} {symbol}: {len(rows)}", flush=True)


async def protection_inputs(folder: Path, *, ready_only: bool = False) -> None:
    manifest = json.loads((folder / "input-manifest.json").read_text())
    start, end = manifest["start_ms"], manifest["end_ms"]
    symbols = [s for s in manifest["candidate_symbols"]
               if (not ready_only or (folder / "signals" / f"{s}.parquet").exists())
               if not pd.read_parquet(folder / "signals" / f"{s}.parquet").empty]
    async with httpx.AsyncClient(base_url="https://fapi.binance.com", timeout=45) as client:
        api, sem = PublicHistory(client), asyncio.Semaphore(16)

        async def one(symbol):
            path = folder / "funding" / f"{symbol}.json"
            if path.exists() and (folder / "marks" / f"{symbol}.parquet").exists():
                return
            async with sem:
                await api.archive(symbol, start, end, folder / "marks" / f"{symbol}.parquet", mark=True)
                if not path.exists():
                    records, cursor = [], start
                    while cursor < end:
                        rows = await api.get("/fapi/v1/fundingRate", {
                            "symbol": symbol, "startTime": cursor,
                            "endTime": end, "limit": 1000,
                        })
                        if not rows:
                            break
                        records.extend({"timestamp_ms": int(r["fundingTime"]),
                                        "rate": float(r["fundingRate"]),
                                        "mark": float(r["markPrice"])} for r in rows)
                        advance = max(r["timestamp_ms"] for r in records) + 1
                        if advance <= cursor:
                            raise ValueError("funding pagination stalled")
                        cursor = advance
                    if not records or any(b["timestamp_ms"] - a["timestamp_ms"] > DAY
                                          for a, b in zip(records, records[1:], strict=False)):
                        raise ValueError(f"funding history incomplete for {symbol}")
                    path.parent.mkdir(exist_ok=True)
                    path.write_text(json.dumps(records))
                print(f"protection/funding {symbol}", flush=True)

        await asyncio.gather(*(one(s) for s in symbols))


def bracket_hit(p: dict, contract: dict, mark: dict, *, take_first: bool = False):
    """Mark triggers; contract price approximated by this bar's opening basis.

Stop first on a bar that touches both levels, except in explicit sensitivity.
Opening gaps execute at the observed contract open before adding fill friction.
"""
    side = p["side"]
    stop, take = p["stop"], p["take"]
    if side * (mark["open"] - stop) <= 0:
        return "stop", float(contract["open"]), False
    if side * (mark["open"] - take) >= 0:
        return "take", float(contract["open"]), False
    stop_hit = mark["low"] <= stop if side > 0 else mark["high"] >= stop
    take_hit = mark["high"] >= take if side > 0 else mark["low"] <= take
    if not (stop_hit or take_hit):
        return None
    reason = "take" if take_hit and (not stop_hit or take_first) else "stop"
    level = take if reason == "take" else stop
    price = min(contract["high"], max(contract["low"], level * contract["open"] / mark["open"]))
    return reason, float(price), bool(stop_hit and take_hit)


def round_down(value: float, step: str) -> float:
    d = Decimal(step)
    return float((Decimal(str(value)) / d).to_integral_value(rounding=ROUND_DOWN) * d)


def load_inputs(folder: Path, *, context: bool = False) -> dict:
    s = settings()
    manifest = json.loads((folder / "input-manifest.json").read_text())
    start, end = manifest["start_ms"], manifest["end_ms"]
    metadata = eligible_metadata(json.loads((folder / "exchange-info.json").read_text()), end)
    symbols = manifest["candidate_symbols"]
    times = np.arange(start, end + FIVE, FIVE, dtype=np.int64)
    volumes = np.full((len(symbols), len(times)), -1.0)
    signals, tapes, marks, funding, filters = {}, {}, {}, {}, {}
    for i, symbol in enumerate(symbols):
        raw = pd.read_parquet(folder / "candles" / f"{symbol}.parquet")
        volume = pd.Series(raw.quote_volume.rolling(288).sum().to_numpy(), index=raw.timestamp_ms)
        volumes[i] = volume.reindex(times, fill_value=-1).fillna(-1).to_numpy()
        sig = pd.read_parquet(folder / ("signal-context" if context else "signals") / f"{symbol}.parquet")
        if sig.empty:
            continue
        for row in sig.to_dict("records"):
            signals.setdefault(int(row["timestamp_ms"]), []).append((symbol, row))
        # Indexed arrays keep memory bounded; histories are validated at collection.
        tapes[symbol] = raw.set_index("open_ms")[["open", "high", "low", "close"]]
        marks[symbol] = pd.read_parquet(folder / "marks" / f"{symbol}.parquet").set_index("open_ms")[["open", "high", "low", "close"]]
        for event in json.loads((folder / "funding" / f"{symbol}.json").read_text()):
            # Observed settlements are 0–14ms after a boundary, before modeled
            # next-open entries. Rounding up would miss funding on positions
            # stopped during the following candle. Fail on unsupported timing.
            if event["timestamp_ms"] % FIVE > 1000:
                raise ValueError("funding event is not at a supported settlement boundary")
            t = event["timestamp_ms"] // FIVE * FIVE
            funding.setdefault(t, []).append((symbol, event))
        filters[symbol] = {f["filterType"]: f for f in metadata[symbol]["filters"]}
    btc = pd.read_parquet(folder / "candles/BTCUSDT.parquet").set_index("timestamp_ms").close
    btc = btc[btc.index % 900_000 == 0]
    crisis = ((btc.pct_change().abs() >= 0.04) | (btc.pct_change(4).abs() >= 0.07)).reindex(times, method="ffill")
    return {"settings": s, "manifest": manifest, "metadata": metadata, "symbols": symbols,
            "times": times, "volumes": volumes, "signals": signals, "tapes": tapes,
            "marks": marks, "funding": funding, "filters": filters, "crisis": crisis}


def run(folder: Path, *, friction_multiplier: float = 1, take_first: bool = False,
        initial: float = INITIAL, inputs: dict | None = None,
        accept: Callable[[dict], bool] | None = None, start_ms: int | None = None,
        end_ms: int | None = None, label: str = "", output_folder: Path | None = None) -> dict:
    data = load_inputs(folder) if inputs is None else inputs
    s, manifest, metadata, symbols = (data[k] for k in ("settings", "manifest", "metadata", "symbols"))
    tapes, marks, funding, filters = (data[k] for k in ("tapes", "marks", "funding", "filters"))
    start, end = start_ms or manifest["start_ms"], end_ms or manifest["end_ms"]
    if not manifest["start_ms"] <= start < end <= manifest["end_ms"] or start % 900_000 or end % FIVE:
        raise ValueError("replay window outside inputs or not aligned")
    times = np.arange(start, end + FIVE, FIVE, dtype=np.int64)
    offset = (start - manifest["start_ms"]) // FIVE
    volumes = data["volumes"][:, offset:offset+len(times)]
    crisis = data["crisis"].iloc[offset:offset+len(times)].reset_index(drop=True)
    signals = data["signals"] if accept is None else {
        t: [(symbol, row) for symbol, row in rows if accept(row)]
        for t, rows in data["signals"].items()}
    cash, peak, max_drawdown = initial, initial, 0.0
    positions, trades, curve = {}, [], []
    rejected, lock_counts = Counter(), Counter()
    pool_sizes, pool_union, ambiguous, fees, funding_total = [], set(), 0, 0.0, 0.0
    slip = (s.slippage_bps / 10000 + SPREAD / 2) * friction_multiplier

    with tempfile.TemporaryDirectory(prefix="paperfrog-offline-fixed-roe-") as temp:
        store = ResearchMemoryStore(Path(temp) / "research.sqlite")
        risk = RiskEngine(s, store)
        store.put("state", State.RUNNING)

        def at(timestamp):
            return datetime.fromtimestamp(timestamp / 1000, UTC)

        def price(symbol, now, *, mark=True):
            frame = marks[symbol] if mark else tapes[symbol]
            if now == end:
                return float(frame.loc[now - FIVE, "close"])
            return float(frame.loc[now, "open"])

        def equity(now):
            return cash + sum(p["side"] * (price(symbol, now) - p["entry"]) * p["quantity"]
                              for symbol, p in positions.items())

        def close(symbol, raw_price, now, reason):
            nonlocal cash, fees
            p = positions.pop(symbol)
            fill = raw_price * (1 - p["side"] * slip)
            exit_fee = fill * p["quantity"] * s.fee_rate
            gross = p["side"] * (fill - p["entry"]) * p["quantity"]
            cash += gross - exit_fee
            fees += exit_fee
            pnl = gross - p["entry_fee"] - exit_fee + p["funding"]
            risk.closed(p["id"], pnl, at(now))
            trades.append({**p, "exit": fill, "exit_ms": now, "reason": reason,
                           "gross_pnl": gross, "pnl": pnl,
                           "net_roe": pnl / (p["entry"] * p["quantity"] / 2),
                           "exit_fee": exit_fee, "holding_minutes": (now-p["entry_ms"])/60000})

        pool = set()
        for idx, raw_now in enumerate(times):
            now = int(raw_now)
            if now > start:
                for symbol, p in list(positions.items()):
                    hit = bracket_hit(p, tapes[symbol].loc[now - FIVE].to_dict(),
                                      marks[symbol].loc[now - FIVE].to_dict(), take_first=take_first)
                    if hit:
                        reason, value, both = hit
                        ambiguous += int(both)
                        close(symbol, value, now - 1, reason)
            for symbol, event in funding.get(now, []):
                if symbol in positions and positions[symbol]["entry_ms"] < event["timestamp_ms"]:
                    p = positions[symbol]
                    amount = -p["side"] * p["quantity"] * event["mark"] * event["rate"]
                    cash += amount
                    p["funding"] += amount
                    funding_total += amount
            eq = equity(now)
            risk.anchors(eq, at(now))
            locked = store.get("risk_lock")
            if locked and locked.get("until") is not None and now / 1000 >= locked["until"] and not store.get("profit_entry_lock"):
                risk.resume(HEALTH, at(now))
                locked = None
            crisis_now = bool(crisis.iloc[idx])
            if locked or crisis_now:
                for symbol in list(positions):
                    close(symbol, price(symbol, now, mark=False), now, "risk_lock" if locked else "btc_crisis")
                eq = equity(now)
                if locked:
                    lock_counts[locked["reason"]] += 1
            peak = max(peak, eq)
            max_drawdown = max(max_drawdown, 1 - eq / peak)
            curve.append({"timestamp_ms": now, "equity": eq, "cash": cash,
                          "open_positions": len(positions)})
            if now == end:
                break
            if now % 900_000 == 0:
                eligible = [(float(volumes[i, idx]), symbol) for i, symbol in enumerate(symbols)
                            if volumes[i, idx] >= s.min_daily_quote_volume
                            and int(metadata[symbol]["onboardDate"]) <= now - 3*DAY
                            and int(metadata[symbol].get("deliveryDate", 4133404800000)) > now]
                pool = {v[1] for v in sorted(eligible, key=lambda v: (-v[0], v[1]))[:100]}
                pool_sizes.append(len(pool))
                pool_union |= pool
            if locked or crisis_now or store.get("profit_entry_lock"):
                continue
            candidates = [(row["score"], symbol, row) for symbol, row in signals.get(now, [])
                          if symbol in pool and symbol not in positions
                          and store.get("cooldown:"+symbol, 0) <= now/1000]
            # Per-coin daily caps are selection filters, before the single winner.
            day = at(now).date().isoformat()
            candidates = [c for c in candidates if sum(t["symbol"] == c[1] and at(t["entry_ms"]).date().isoformat() == day for t in trades) < s.max_entries_per_symbol_per_day]
            checked = []
            for score, symbol, row in candidates:
                current = price(symbol, now)
                if row["side"] * (current - row["close"]) >= -0.5*row["minute_atr"]:
                    checked.append((score, symbol, row))
            if not checked:
                continue
            _, symbol, row = sorted(checked, key=lambda v: (-v[0], v[1]))[0]
            f = filters[symbol]
            tick = f["PRICE_FILTER"]["tickSize"]
            side = int(row["side"])
            mid = price(symbol, now, mark=False)
            expected = mid * (1 + side * (SPREAD/2 + 0.001))
            allocation_price = round_down(expected, tick)
            costs = 0.025 + 2*s.fee_rate + 2*s.slippage_bps/10000 + SPREAD + 0.002 + 2*float(tick)/price(symbol, now)
            outstanding = sum(p["quantity"] * price(k, now) for k, p in positions.items())
            available = max(0, eq - outstanding / 2)
            budget = min(eq*s.risk_per_trade*store.get("risk_multiplier", 1.0)/costs,
                         eq*s.max_target_notional, max(0,eq*s.max_net_beta-outstanding),
                         eq*s.simple_position_margin_cap*2,
                         max(0,eq*s.simple_total_margin_cap*2-outstanding),
                         available / (0.5+s.fee_rate))
            quantity = round_down(budget/allocation_price, f["LOT_SIZE"]["stepSize"])
            fill = mid * (1 + side * slip)
            notional = quantity * fill
            if quantity < float(f["LOT_SIZE"]["minQty"]) or quantity*allocation_price < float(f["MIN_NOTIONAL"]["notional"]):
                rejected["below_exchange_minimum"] += 1
                continue
            trade_id = f"replay-{now}-{symbol}"
            try:
                risk.reserve(Proposal(trade_id, symbol, eq, quantity*allocation_price,
                                      quantity*allocation_price, quantity*allocation_price*costs,
                                      side*quantity*allocation_price/eq, .025, .5, 0), HEALTH, at(now))
            except SafeError as exc:
                rejected[str(exc)] += 1
                continue
            entry_fee = notional * s.fee_rate
            cash -= entry_fee
            fees += entry_fee
            positions[symbol] = {"id": trade_id, "symbol": symbol, "side": side,
                                 "entry_ms": now, "entry": fill, "quantity": quantity,
                                 "entry_fee": entry_fee, "funding": 0.0,
                                 "stop": float(trigger(Decimal(str(fill)), side, Decimal(tick), profit=False)),
                                 "take": float(trigger(Decimal(str(fill)), side, Decimal(tick), profit=True))}
        store.engine.dispose()
    # No forced close at the research end: open positions are marked separately.
    final = equity(end)
    gross = sum(t["gross_pnl"] for t in trades) + sum(p["side"]*(price(k,end)-p["entry"])*p["quantity"] for k,p in positions.items())
    winners, losers = [t for t in trades if t["pnl"] > 0], [t for t in trades if t["pnl"] <= 0]
    prefix = f"cost-{friction_multiplier:g}-{'take' if take_first else 'stop'}-first-{initial:g}"
    if label:
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in label):
            raise ValueError("invalid research output label")
        prefix = label + "-" + prefix
    output_folder = output_folder or folder
    output_folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(trades).to_csv(output_folder / f"{prefix}-trades.csv", index=False)
    pd.DataFrame(curve).to_csv(output_folder / f"{prefix}-equity.csv", index=False)
    summary = {"scenario": prefix, "initial_equity": initial, "final_equity": final,
               "return_pct": (final/initial-1)*100, "net_pnl": final-initial,
               "closed_trades": len(trades), "wins": len(winners), "losses": len(losers),
               "win_rate_pct": len(winners)/len(trades)*100 if trades else None,
               "profit_factor": sum(t["pnl"] for t in winners)/-sum(t["pnl"] for t in losers) if losers else None,
               "max_drawdown_pct_5m": max_drawdown*100,
               "gross_pnl_before_fees_funding": gross, "fees": fees, "funding_net": funding_total,
               "accounting_residual": final-initial-gross+fees-funding_total,
               "median_hold_minutes": float(np.median([t["holding_minutes"] for t in trades])) if trades else None,
               "exit_reasons": dict(Counter(t["reason"] for t in trades)),
               "long_trades": sum(t["side"] > 0 for t in trades),
               "short_trades": sum(t["side"] < 0 for t in trades),
               "ambiguous_dual_touch_5m_bars": ambiguous,
               "pool_min": min(pool_sizes), "pool_max": max(pool_sizes),
               "pool_unique_coins": len(pool_union), "lock_bars": dict(lock_counts),
               "entry_rejections": dict(rejected), "end_open_positions": list(positions.values()),
               "friction_multiplier": friction_multiplier, "fee_rate": s.fee_rate,
               "half_spread_plus_slippage_bps": slip*10000,
               "source_hash": source_hash(), "start_ms": start, "end_ms": end}
    if not math.isclose(summary["accounting_residual"], 0, abs_tol=1e-6):
        raise ValueError("replay ledger does not balance")
    (output_folder / f"{prefix}-summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False))
    print(json.dumps(summary, indent=2, allow_nan=False), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--stage", choices=["features", "inputs", "replay"], required=True)
    parser.add_argument("--friction-multiplier", type=float, default=1)
    parser.add_argument("--take-first", action="store_true")
    parser.add_argument("--initial", type=float, default=INITIAL)
    parser.add_argument("--ready-only", action="store_true")
    args = parser.parse_args()
    if args.stage == "features":
        features(args.folder, ready_only=args.ready_only)
    elif args.stage == "inputs":
        asyncio.run(protection_inputs(args.folder, ready_only=args.ready_only))
    else:
        if not 1 <= args.friction_multiplier <= 10 or not math.isfinite(args.initial) or args.initial <= 0:
            parser.error("invalid cost multiplier or starting equity")
        run(args.folder, friction_multiplier=args.friction_multiplier,
            take_first=args.take_first, initial=args.initial)
