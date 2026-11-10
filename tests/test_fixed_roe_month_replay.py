"""Research accounting and chronology checks; no profitability assertions."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def repository_import_path(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))


def test_offline_settings_cannot_inherit_live_environment(monkeypatch):
    from scripts.replay_fixed_roe_month import settings
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_API_KEY", "must-not-be-loaded")
    s = settings()
    assert s.trading_mode == "paper" and not s.live_trading_enabled
    assert s.binance_api_key.get_secret_value() == ""


def test_acquisition_bounds_retain_possible_entrant_and_prune_impossible_coin():
    from scripts.fetch_fixed_roe_month import DAY, HOUR, bound_candidates
    start, end = 10 * DAY, 11 * DAY
    hours = np.arange(start - DAY, end, HOUR)
    hourly = {f"COIN{i}": pd.DataFrame({"open_ms": hours, "quote_volume": 1_000_000.0})
              for i in range(100)}
    hourly["DUST"] = pd.DataFrame({"open_ms": hours, "quote_volume": 10.0})
    hourly["NEW_VOLUME"] = pd.DataFrame({"open_ms": hours, "quote_volume": 0.0})
    hourly["NEW_VOLUME"].loc[24, "quote_volume"] = 50_000_000.0
    metadata = {s: {"onboardDate": 0} for s in hourly}
    result = bound_candidates(hourly, metadata, start, end)
    assert "DUST" not in result
    assert "NEW_VOLUME" in result
    assert len(result) == 101


@pytest.mark.parametrize("side", [1, -1])
def test_mark_triggers_and_dual_touch_ordering(side):
    from scripts.replay_fixed_roe_month import bracket_hit
    p = {"side": side, "stop": 100 - 2.5 * side, "take": 100 + 2.5 * side}
    contract = {"open": 101, "high": 105, "low": 97}
    mark = {"open": 100, "high": 103, "low": 97}
    assert bracket_hit(p, contract, mark)[0] == "stop"
    assert bracket_hit(p, contract, mark, take_first=True)[0] == "take"
    assert bracket_hit(p, contract, mark)[2]
    # Contract touches alone must not trigger a mark-price hosted bracket.
    assert bracket_hit(p, contract, {"open": 100, "high": 101, "low": 99}) is None


def test_opening_gap_uses_contract_open_instead_of_trigger():
    from scripts.replay_fixed_roe_month import bracket_hit
    p = {"side": 1, "stop": 97.5, "take": 102.5}
    hit = bracket_hit(p, {"open": 94, "high": 96, "low": 93},
                      {"open": 95, "high": 96, "low": 94})
    assert hit == ("stop", 94, False)


def replay_fixture(folder, take_hit):
    from scripts.fetch_fixed_roe_month import DAY, FIVE
    start, end = 1791590400000, 1791590400000 + 3 * FIVE
    for name in ("candles", "marks", "signals", "funding"):
        (folder / name).mkdir()
    meta = {"symbol": "TESTUSDT", "quoteAsset": "USDT", "baseAsset": "TEST",
            "contractType": "PERPETUAL", "underlyingType": "COIN", "onboardDate": 0,
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
    (folder / "exchange-info.json").write_text(json.dumps({"symbols": [meta]}))
    (folder / "input-manifest.json").write_text(json.dumps({"start_ms": start, "end_ms": end,
                                                           "candidate_symbols": ["TESTUSDT"]}))
    times = np.arange(start - 3 * DAY, end, FIVE)
    raw = pd.DataFrame({"open_ms": times, "timestamp_ms": times + FIVE,
                        "open": 100., "high": 101., "low": 99., "close": 100.,
                        "quote_volume": 1_000_000., "taker_buy_quote": 500_000.})
    if take_hit:
        raw.loc[raw.open_ms == start, "high"] = 103
    raw.to_parquet(folder / "candles/TESTUSDT.parquet", index=False)
    raw.to_parquet(folder / "candles/BTCUSDT.parquet", index=False)
    raw[raw.open_ms >= start].to_parquet(folder / "marks/TESTUSDT.parquet", index=False)
    pd.DataFrame([{"timestamp_ms": start, "side": 1, "score": 1., "close": 100.,
                   "minute_atr": 1.}]).to_parquet(folder / "signals/TESTUSDT.parquet", index=False)
    (folder / "funding/TESTUSDT.json").write_text("[]")


@pytest.mark.parametrize("take_hit", [False, True])
def test_portfolio_balances_and_does_not_force_close_at_end(tmp_path, take_hit):
    from scripts.replay_fixed_roe_month import run
    replay_fixture(tmp_path, take_hit)
    r = run(tmp_path)
    assert abs(r["accounting_residual"]) < 1e-8
    assert r["closed_trades"] == int(take_hit)
    assert len(r["end_open_positions"]) == int(not take_hit)
    assert r["fees"] > 0


def test_funding_milliseconds_do_not_skip_payment_before_intrabar_exit(tmp_path):
    from scripts.fetch_fixed_roe_month import FIVE
    from scripts.replay_fixed_roe_month import run

    replay_fixture(tmp_path, False)
    start = json.loads((tmp_path / "input-manifest.json").read_text())["start_ms"]
    for path in (tmp_path / "candles/TESTUSDT.parquet", tmp_path / "marks/TESTUSDT.parquet"):
        raw = pd.read_parquet(path)
        raw.loc[raw.open_ms == start + FIVE, "high"] = 103
        raw.to_parquet(path, index=False)
    event = {"timestamp_ms": start + FIVE + 14, "rate": 0.001, "mark": 100}
    (tmp_path / "funding/TESTUSDT.json").write_text(json.dumps([event]))
    result = run(tmp_path)
    assert result["closed_trades"] == 1
    assert result["funding_net"] < 0
    trade = pd.read_csv(next(tmp_path.glob("*-trades.csv"))).iloc[0]
    assert result["funding_net"] == pytest.approx(-trade.quantity * .1)
    assert abs(result["accounting_residual"]) < 1e-8
