import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from test_simple import engine_fixture, quote, row
from test_simple_live import setup_engine
from typer.testing import CliRunner

from paperfrog.cli import app
from paperfrog.simple.analysis import daily_report, refresh_reports, render_report
from paperfrog.simple.engine import Engine
from paperfrog.simple.journal import indicators
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS
from paperfrog.storage.database import Store


@pytest.mark.parametrize("side", [1, -1])
async def test_rich_journal_roundtrip_costs_and_excursions(settings, store, filters, side):
    engine = await engine_fixture(settings, store, filters)
    start = 200 * BAR_MS
    signal = {**row(direction=side), "volume_ratio": 0.75, "rsi_14": float("nan")}
    await engine.enter("SOLUSDT", signal, start)
    opened = engine.positions()["SOLUSDT"]
    assert opened["entry_context"]["indicators"]["volume_ratio"] == 0.75
    assert "rsi_14" not in opened["entry_context"]["indicators"]
    engine.funding("SOLUSDT", start + 1, Decimal(".0001"), Decimal(100))
    favorable = 100 + side * 0.6
    adverse = 100 - side * 0.3
    await engine.quote(quote(price=favorable, now_ms=start + 1000), start + 1000)
    await engine.quote(quote(price=adverse, now_ms=start + 2000), start + 2000)
    # Recovery retains the original entry context and observed path.
    restored = Engine(settings, store, filters)
    await restored.restore()
    await restored.quote(quote(price=100 + side * 0.4, now_ms=start + 3000), start + 3000)
    await restored.close("SOLUSDT", start + 3000, "test close")
    closed = store.get("simple_closed")[0]
    assert closed["entry_context"] == opened["entry_context"]
    assert closed["pnl"] == pytest.approx(closed["gross_pnl"] - closed["fees"] + closed["funding"])
    assert closed["fees"] == pytest.approx(closed["entry_fee"] + closed["exit_fee"])
    assert closed["net_return"] == pytest.approx(
        closed["pnl"] / (closed["entry_price"] * closed["quantity"])
    )
    assert closed["mfe_fraction"] == pytest.approx(side * (favorable / closed["entry_price"] - 1))
    assert closed["mae_fraction"] == pytest.approx(side * (adverse / closed["entry_price"] - 1))
    assert closed["holding_seconds"] == 3
    assert closed["entry_execution_cost"]["slippage_cost"] > 0
    assert closed["exit_execution_cost"]["slippage_cost"] > 0
    # Execution shortfalls reconcile the mid-price move to fill-price gross PnL.
    mid_pnl = 0.4 * closed["quantity"]
    shortfall = sum(
        closed[k][name]
        for k in ("entry_execution_cost", "exit_execution_cost")
        for name in ("spread_cost", "slippage_cost")
    )
    assert closed["gross_pnl"] == pytest.approx(mid_pnl - shortfall)


async def test_paper_stop_journals_trigger_quote(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    start = 200 * BAR_MS
    await engine.enter("SOLUSDT", row(), start)
    await engine.quote(quote(price=95, now_ms=start + 1000), start + 1000)
    closed = store.get("simple_closed")[0]
    assert closed["exit_quote_reference"]["mid"] == 95
    assert closed["exit_execution_cost"] is not None
    assert closed["mae_fraction"] < -0.04


async def test_live_recovered_stop_has_no_fabricated_exit_slippage(settings, store, filters):
    engine, sim, adapter, signal, now = await setup_engine(settings, store, filters)
    try:
        await engine.enter("SOLUSDT", signal, now)
        sim.fire_stop()
        restored = Engine(engine.s, store, filters, broker=engine.broker)
        await restored.restore()
        await restored.restore()
        closed = store.get("simple_closed")
        assert len(closed) == 1
        assert closed[0]["entry_context"]["fee_basis"] == "estimated"
        assert closed[0]["entry_execution_cost"] is not None
        assert closed[0]["exit_execution_cost"] is None
    finally:
        await adapter.close()


async def test_legacy_open_trade_stays_unknown(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    start = 200 * BAR_MS
    await engine.enter("SOLUSDT", row(), start)
    positions = engine.positions()
    for key in (
        "journal_version",
        "entry_context",
        "entry_execution_cost",
        "mfe_fraction",
        "mae_fraction",
        "last_observed_ms",
    ):
        positions["SOLUSDT"].pop(key, None)
    store.put("simple_positions", positions)
    await engine.quote(quote(price=101, now_ms=start + 1000), start + 1000)
    await engine.close("SOLUSDT", start + 1000, "legacy close")
    trade = store.get("simple_closed")[0]
    assert trade["journal_version"] == 0
    assert trade["entry_context"] is None
    assert trade["mfe_fraction"] is None
    assert trade["mae_fraction"] is None


def test_context_allowlist_drops_unknown_text_and_nonfinite_values():
    assert indicators(
        {"flow": 0.4, "rsi_14": float("inf"), "api_key": "discard-me", "extra": "discard-me"}
    ) == {"flow": 0.4}


def report_trades():
    midnight = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
    base = {
        "timestamp_ms": midnight,
        "entry_ms": midnight - 1000,
        "symbol": "SOLUSDT",
        "direction": 1,
        "reason": "stop",
        "fees": 2,
        "funding": -1,
    }
    rich = {
        "journal_version": 1,
        "entry_context": {
            "strategy": "test",
            "currency": "USDT",
            "fee_basis": "simulated",
            "indicators": {"volume_ratio": 0.5, "flow": 0.4},
        },
    }
    return midnight, [
        {**base, **rich, "pnl": 7, "net_return": 0.07},
        {**base, **rich, "pnl": -2, "net_return": -0.02},  # gross +1, costs turn it negative
        {**base, "pnl": 0, "funding": 2},
        {**base, "timestamp_ms": midnight - 1, "pnl": 999},
        {**base, "timestamp_ms": midnight + 86400000, "pnl": 999},
    ]


def test_report_comparison_costs_coverage_and_utc_boundaries():
    midnight, trades = report_trades()
    report = daily_report(trades, date(2026, 9, 1), midnight + 86400000)
    summary = report["summary"]
    assert (summary["trades"], summary["wins"], summary["losses"], summary["breakeven"]) == (
        3,
        1,
        1,
        1,
    )
    assert summary["win_rate"] == pytest.approx(1 / 3)
    assert summary["profit_factor"] == 3.5
    assert summary["cost_flipped_trades"] == 1
    assert summary["metrics"]["net_pnl"]["total"] == 5
    assert summary["metrics"]["gross_pnl"]["total"] == 11
    assert summary["metrics"]["fees"]["total"] == 6
    assert summary["metrics"]["funding"]["total"] == 0
    assert summary["metrics"]["net_return"]["known_trades"] == 2
    assert summary["metrics"]["slippage_cost"]["known_trades"] == 0
    assert report["outcomes"]["winners"]["entry_indicators"]["flow"] == {
        "known_trades": 1,
        "mean": 0.4,
    }
    assert report["groups"]["volume_regime"]["unknown"]["trades"] == 1
    assert report["coverage"] == {"rich_journal_trades": 2, "legacy_trades": 1}
    assert report["period_complete"]
    assert "Winners versus losers" in render_report(report)
    json.dumps(report, allow_nan=False)


def test_reports_handle_empty_partial_unknown_and_no_losses():
    midnight, trades = report_trades()
    empty = daily_report([], date(2026, 9, 1), midnight)
    assert empty["summary"]["win_rate"] is None
    assert empty["summary"]["profit_factor"] is None
    assert not empty["period_complete"]
    only_win = daily_report(trades[:1], date(2026, 9, 1), midnight)
    assert only_win["summary"]["profit_factor"] is None
    unknown = {**trades[0], "timestamp_ms": midnight + 1, "fees": None, "funding": None}
    partial = daily_report([unknown], date(2026, 9, 1), midnight)
    assert partial["summary"]["trades"] == 0
    known = daily_report([unknown], date(2026, 9, 1), midnight + 1)
    assert known["summary"]["metrics"]["gross_pnl"]["total"] is None


def test_refresh_backfills_and_is_idempotent(tmp_path):
    midnight, trades = report_trades()
    refresh_reports(trades, tmp_path, midnight + 86400000)
    path = tmp_path / "2026-09-01.json"
    original = path.stat().st_mtime_ns
    refresh_reports(trades, tmp_path, midnight + 86400000)
    assert path.stat().st_mtime_ns == original
    assert (tmp_path / "2026-08-31.txt").exists()
    assert (tmp_path / "2026-09-02.txt").exists()


def test_cli_reads_existing_journal_without_trading(settings, monkeypatch, tmp_path):
    _, trades = report_trades()
    store = Store(settings.state_dir / "simple" / "paperfrog.sqlite")
    store.put("simple_closed", trades)
    store.engine.dispose()
    monkeypatch.setattr("paperfrog.cli.settings", lambda: settings)
    result = CliRunner().invoke(
        app,
        [
            "simple",
            "analysis",
            "--day",
            "2026-09-01",
            "--json-output",
            "--output",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["summary"]["trades"] == 3
    assert (tmp_path / "out" / "2026-09-01.txt").exists()
    result = CliRunner().invoke(app, ["simple", "analysis", "--day", "invalid"])
    assert result.exit_code != 0


async def test_daily_report_failure_does_not_change_trading_state(settings, store, monkeypatch):
    service = SimpleService(settings)
    service.store.engine.dispose()
    service.store = store
    previous = store.get("state")

    def fail(*args):
        raise OSError("private exception text must not be journaled")

    async def stop_loop(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr("paperfrog.simple.service.refresh_reports", fail)
    monkeypatch.setattr("paperfrog.simple.service.asyncio.sleep", stop_loop)
    try:
        with pytest.raises(asyncio.CancelledError):
            await service.daily_analysis()
        assert store.get("state") == previous
        assert store.get("simple_daily_analysis_status") == "unavailable"
        assert "private exception" not in json.dumps(store.rows("audit"))
    finally:
        await service.exchange.close()
