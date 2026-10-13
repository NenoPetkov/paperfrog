import numpy as np
import pandas as pd
import pytest

from paperfrog.data.lake import Lake
from paperfrog.data.preparation import assemble_panel, assess_history
from paperfrog.domain.types import SafeError
from paperfrog.features.engineering import CORE_FEATURES


def test_coverage_counts_holes_instead_of_claiming_span(settings):
    lake = Lake(settings.data_dir)
    lake.append("perp_15m", "SOLUSDT", pd.DataFrame({"timestamp_ms": [900000, 2700000]}), "fixture")
    result = assess_history(settings, 0, 2700000)
    coverage = result["symbols"]["SOLUSDT"]["perp_15m"]
    assert coverage["expected_bars"] == 3 and coverage["missing_bars"] == 1
    assert not result["raw_coverage_complete"] and result["live_permitted"] is False


def test_quote_coverage_cannot_use_future_or_crossed_books(settings):
    lake = Lake(settings.data_dir)
    lake.append(
        "snapshots",
        "SOLUSDT",
        pd.DataFrame(
            {
                "timestamp_ms": [900001, 1800000],
                "bid": [99, 101],
                "ask": [101, 99],
                "spot_bid": [99, 99],
                "spot_ask": [101, 101],
                "bid_size": [1, 1],
                "ask_size": [1, 1],
                "mark": [100, 100],
            }
        ),
        "fixture",
    )
    result = assess_history(settings, 0, 1800000)
    assert result["symbols"]["SOLUSDT"]["snapshots"]["matched_bars"] == 0


def save_features(settings, missing_core=False):
    folder = settings.data_dir / "features"
    folder.mkdir(parents=True)
    for symbol in settings.symbols:
        frame = pd.DataFrame({name: [0.1, 0.1, 0.1] for name in CORE_FEATURES})
        frame["timestamp_ms"] = [900000, 1800000, 2700000]
        frame["perp_mid"] = 100
        for name, value in {
            "bid": 99,
            "ask": 101,
            "bid_size": 1,
            "ask_size": 1,
            "mark": 100,
            "funding_settlement": False,
            "liquid": True,
        }.items():
            frame[name] = value
        if missing_core:
            frame["funding_persistence"] = np.nan
        frame.to_parquet(folder / f"{symbol}.parquet", index=False)


def universe():
    return {
        "asof_ms": 1800000,
        "expires_ms": 3600000,
        "min_history_days": 180,
        "symbols": ["SOLUSDT"],
        "inputs": [{"symbol": "SOLUSDT", "observed_ms": 1700000, "valid_history_days": 180}],
    }


def test_panel_does_not_backdate_current_universe(settings, tmp_path):
    save_features(settings)
    panel = assemble_panel(settings, [universe()], tmp_path / "panel.parquet")
    target = panel[panel.symbol == "SOLUSDT"]
    assert target.in_universe.tolist() == [False, True, True]
    assert not panel[panel.symbol == "BTCUSDT"].in_universe.any()


def test_panel_rejects_future_membership_evidence(settings, tmp_path):
    row = universe()
    row["inputs"][0]["observed_ms"] = row["asof_ms"] + 1
    with pytest.raises(SafeError, match="point-in-time"):
        assemble_panel(settings, [row], tmp_path / "panel.parquet")
    assert not (tmp_path / "panel.parquet").exists()


def test_panel_rejects_missing_core_features(settings, tmp_path):
    save_features(settings, missing_core=True)
    with pytest.raises(SafeError, match="complete features"):
        assemble_panel(settings, [universe()], tmp_path / "panel.parquet")
    assert not (tmp_path / "panel.parquet").exists()


def test_panel_requires_historical_snapshots(settings, tmp_path):
    with pytest.raises(SafeError, match="historical universe snapshots"):
        assemble_panel(settings, [], tmp_path / "panel.parquet")
