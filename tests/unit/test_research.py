import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from paperfrog.domain.types import SafeError
from paperfrog.features.engineering import build_features, funding_persistence
from paperfrog.models.labels import label_episode
from paperfrog.models.training import partitions
from paperfrog.strategy.engine import Episodes, entry_decision, exit_decision


def feature_input(n=300):
    rng = np.random.default_rng(42)
    mid = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    return pd.DataFrame(
        {
            "timestamp_ms": np.arange(n) * 900000,
            "perp_mid": mid,
            "spot_mid": mid * (1 + rng.normal(0, 0.001, n)),
            "funding_rate": rng.normal(0, 0.0001, n),
            "open_interest": 10000 + np.arange(n),
            "perp_quote_volume": 100000 + rng.random(n) * 10000,
            "spot_quote_volume": 100000,
            "perp_taker_buy_quote": 60000,
            "spot_taker_buy_quote": 50000,
            "bid": mid - 0.01,
            "ask": mid + 0.01,
        }
    )


def test_future_rows_cannot_change_features():
    frame = feature_input()
    full = build_features(frame)
    prefix = build_features(frame.iloc[:200])
    assert_frame_equal(full.iloc[:200], prefix)
    frame.loc[200:, "perp_mid"] *= 100
    assert_frame_equal(build_features(frame).iloc[:200], prefix)


def test_gap_not_forward_filled():
    with pytest.raises(SafeError, match="continuous"):
        build_features(feature_input().drop(50))


def test_persistence_only_available_twenty_minutes_after_funding():
    event = 3600000
    minutes = pd.DataFrame(
        {"timestamp_ms": np.arange(event - 600000, event + 1200001, 60000), "basis": 0.01}
    )
    result = funding_persistence(
        minutes, pd.DataFrame([{"timestamp_ms": event, "funding_rate": 0.001}])
    )
    assert result.iloc[0].timestamp_ms == event + 1200000
    assert result.iloc[0].funding_persistence == 1
    missing = funding_persistence(
        minutes.iloc[:-1], pd.DataFrame([{"timestamp_ms": event, "funding_rate": 0.001}])
    )
    assert missing.empty


def test_purged_temporal_splits():
    times = pd.date_range("2024-01-01", "2026-09-01", freq="12h", tz="UTC")
    data = pd.DataFrame({"timestamp_ms": times.as_unit("ms").astype("int64")})
    data["label_end_ms"] = data.timestamp_ms + 14400000
    train, val, test = partitions(data, pd.Timestamp("2026-07-01", tz="UTC"), 12)
    assert train.label_end_ms.max() + 14400000 < val.timestamp_ms.min()
    assert val.label_end_ms.max() + 14400000 < test.timestamp_ms.min()


def test_triple_barrier_respects_basis_and_liquidity():
    path = pd.DataFrame(
        {
            "timestamp_ms": np.arange(1, 17) * 900000,
            "perp_mid": 99,
            "btc_mid": 100,
            "eth_mid": 100,
            "basis": 0.006,
            "liquid": True,
        }
    )
    result = label_episode(
        path, 0.005, 0.01, 1, 0.5, 0.5, (100, 100, 100), fee_rate=0, slippage=0, spread=0
    )
    assert result["success"] == 1
    path["basis"] = 0.009
    assert (
        label_episode(
            path, 0.005, 0.01, 1, 0.5, 0.5, (100, 100, 100), fee_rate=0, slippage=0, spread=0
        )["success"]
        == 0
    )


def test_episode_not_repeated(store):
    data = pd.DataFrame(
        {
            "timestamp_ms": np.arange(4) * 900000,
            "basis": 0.01,
            "funding_rate": 0.001,
            "basis_z_30d": 3,
            "funding_z": 3,
            "oi_z_4h": 2,
            "volume_ratio_z": 2,
            "funding_persistence": 0.8,
            "spread": 0.001,
            "liquid": True,
            "oi_change_30m": -0.02,
            "perp_imbalance": -0.2,
            "perp_mid": 100,
        }
    )
    episodes = Episodes(store)
    assert episodes.update("SOLUSDT", data, {"SOLUSDT"}) is None
    row = data.iloc[-1].copy()
    row["timestamp_ms"] += 900000
    row["basis"] = 0.009
    data = pd.concat([data, pd.DataFrame([row])], ignore_index=True)
    assert episodes.update("SOLUSDT", data, {"SOLUSDT"}) is not None
    assert episodes.update("SOLUSDT", data, {"SOLUSDT"}) is None


@pytest.mark.parametrize(
    "p,r,baseline,allowed",
    [
        (0.74, 0.02, 0.8, False),
        (0.8, 0.001, 0.8, False),
        (0.8, 0.02, 0.2, False),
        (0.8, 0.02, 0.8, True),
    ],
)
def test_quantitative_decision(p, r, baseline, allowed):
    assert entry_decision([p], [r], baseline, 0, 0.001, 0.001, 0.001)[0] is allowed


def test_time_exit_and_partial_profit():
    base = dict(
        held_minutes=100,
        pnl_r=0,
        basis=0.01,
        initial_basis=0.01,
        basis_z=2,
        basis_change_sigma=0.001,
        partial_taken=False,
    )
    assert exit_decision(**(base | {"held_minutes": 240})).fraction == 1
    assert exit_decision(**(base | {"pnl_r": 1.5})).fraction == 0.5
    assert exit_decision(**(base | {"pnl_r": 1.5, "partial_taken": True})).fraction == 0
