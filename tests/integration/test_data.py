import pandas as pd

from paperfrog.data.lake import Lake, validate
from paperfrog.data.universe import build_universe


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
