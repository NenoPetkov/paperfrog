import pandas as pd
import pytest

from paperfrog.data.lake import Lake, validate
from paperfrog.data.universe import build_universe
from paperfrog.settings import Settings


def test_lake_idempotence_and_query(tmp_path):
    lake = Lake(tmp_path)
    frame = pd.DataFrame({"timestamp_ms": [900000, 1800000], "close": [1.0, 2.0]})
    assert lake.append("perp_15m", "SOLUSDT", frame, "test") == 2
    assert lake.append("perp_15m", "SOLUSDT", frame, "test") == 0
    assert len(lake.read("perp_15m", "SOLUSDT")) == 2
    assert validate(lake.read("perp_15m", "SOLUSDT"), 900000)["valid"]


def test_universe_pit_freeze(settings, store):
    row = dict(
        symbol="SOLUSDT",
        observed_ms=1000,
        base_asset="SOL",
        status="TRADING",
        contract_type="PERPETUAL",
        spot_match=True,
        valid_history_days=180,
        gap_fraction=0,
        perp_quote_median_30d=20e6,
        spot_quote_median_30d=20e6,
        spread_median=0.001,
        depth_usdt=1e6,
    )
    result = build_universe(
        [row, row | {"symbol": "FUTUREUSDT", "observed_ms": 1001}], 1000, settings, store
    )
    assert result["symbols"] == ["SOLUSDT"]
    assert result["reasons"]["FUTUREUSDT"] == ["future information"]
    assert build_universe([], 2000, settings, store) == result


def test_short_paper_universe_cannot_bypass_higher_history_requirement(store):
    row = dict(
        symbol="SOLUSDT",
        observed_ms=1000,
        base_asset="SOL",
        status="TRADING",
        contract_type="PERPETUAL",
        spot_match=True,
        valid_history_days=90,
        gap_fraction=0,
        perp_quote_median_30d=20e6,
        spot_quote_median_30d=20e6,
        spread_median=0.001,
        depth_usdt=1e6,
    )
    paper = Settings(_env_file=None, min_history_days=90)
    first = build_universe([row], 1000, paper, store)
    assert first["symbols"] == ["SOLUSDT"]
    assert build_universe([], 2000, paper, store) == first
    testnet = Settings(_env_file=None, trading_mode="testnet")
    higher = build_universe([row], 2000, testnet, store)
    assert higher["symbols"] == []
    assert higher["reasons"]["SOLUSDT"] == ["insufficient continuous history"]


def test_short_paper_history_still_rejects_missing_coverage(store):
    row = dict(
        symbol="SOLUSDT",
        observed_ms=1000,
        base_asset="SOL",
        status="TRADING",
        contract_type="PERPETUAL",
        spot_match=True,
        valid_history_days=89,
        gap_fraction=0,
        perp_quote_median_30d=20e6,
        spot_quote_median_30d=20e6,
        spread_median=0.001,
        depth_usdt=1e6,
    )
    s = Settings(_env_file=None, min_history_days=90)
    result = build_universe([row], 1000, s, store)
    assert result["symbols"] == []
    assert result["reasons"]["SOLUSDT"] == ["insufficient continuous history"]


@pytest.mark.asyncio
async def test_service_rejects_short_paper_universe_in_testnet(settings, monkeypatch):
    from unittest.mock import Mock

    from paperfrog.service import Service

    monkeypatch.setattr("paperfrog.service.time.time", lambda: 2000.0)
    service = Service(settings.model_copy(update={"trading_mode": "testnet"}))
    service.store.put(
        "active_universe",
        {"asof_ms": 0, "expires_ms": 86400000, "min_history_days": 90, "symbols": ["SOLUSDT"]},
    )
    inspect_entries = Mock(side_effect=AssertionError("must not inspect entries for this universe"))
    monkeypatch.setattr(service.store, "rows", inspect_entries)
    try:
        await service.evaluate_candidates(10000)
        inspect_entries.assert_not_called()
    finally:
        await service.exchange.close()
        service.store.engine.dispose()
