from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from paperfrog.research.redesign import (
    DAY,
    FRICTION,
    FUTURE_FEE,
    HOUR,
    Candidate,
    History,
    replay,
)


def tapes(days=90, symbols=("A", "B", "C", "D")):
    base = pd.DataFrame(
        {
            "open_ms": np.arange(days * 24) * HOUR,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 2_000_000.0,
        }
    )
    return {s: base.copy() for s in symbols}


@pytest.mark.parametrize(
    "candidate", [Candidate("relative_momentum", 7, 7), Candidate("channel", 55, 20)]
)
def test_future_prices_and_unpublished_funding_cannot_change_current_signal(candidate):
    raw = tapes()
    for j, frame in enumerate(raw.values()):
        path = np.exp(np.linspace(0, j * 0.1, len(frame))) * 100
        frame[["open", "high", "low", "close"]] = np.tile(path, (4, 1)).T
    funding = {s: [] for s in raw}
    h = History(raw, deepcopy(raw), funding)
    i = 70 * 24
    c = candidate
    original = h.targets(c, i)
    assert original
    for frame in raw.values():
        frame.loc[frame.open_ms >= i * HOUR, ["open", "high", "low", "close"]] *= 30
    for symbol in funding:
        funding[symbol] = [{"timestamp_ms": i * HOUR, "funding_rate": 1, "mark": 100}]
    changed = History(raw, deepcopy(raw), funding)
    assert changed.targets(c, i) == original
    np.testing.assert_array_equal(changed.past_funding(i, 7), h.past_funding(i, 7))
    assert changed.past_funding(i + 1, 7).sum() > 0


@pytest.mark.parametrize("side", [1, -1])
def test_fixed_price_roundtrip_reconciles_costs_and_funding_boundaries(monkeypatch, side):
    raw = tapes(symbols=("A",))
    start, end = 60 * DAY, 63 * DAY
    events = [
        {"timestamp_ms": when, "funding_rate": 0.001, "mark": 100}
        for when in (start, start + 8 * HOUR, end)
    ]
    h = History(raw, deepcopy(raw), {"A": events})
    monkeypatch.setattr(h, "targets", lambda c, i: [(0, side, 1.0)] if i == 60 * 24 else [])
    c = Candidate("relative_momentum", 7, 7)
    result = replay(h, c, start, end)
    quantity = 2000 / (100 * (1 + side * FRICTION))
    entry, exit_ = 100 * (1 + side * FRICTION), 100 * (1 - side * FRICTION)
    fees = quantity * (entry + exit_) * FUTURE_FEE
    funding = -side * quantity * 100 * 0.001 * 2
    assert result["trades"] == 1
    assert result["funding"] == pytest.approx(funding)
    assert result["fees"] == pytest.approx(fees)
    assert result["net"] == pytest.approx(side * quantity * (exit_ - entry) - fees + funding)
    assert result["gross_price_pnl_before_friction"] == pytest.approx(0, abs=1e-8)
    stressed = replay(h, c, start, end, cost_multiplier=2)
    stressed_q = 2000 / (100 * (1 + side * FRICTION * 2))
    assert stressed["funding"] == pytest.approx(-side * stressed_q * 0.2 * (2 if side > 0 else 1))


def test_carry_counts_both_legs_and_funding_without_free_price_profit():
    raw = tapes()
    events = [
        {"timestamp_ms": when, "funding_rate": 0.001, "mark": 100}
        for when in range(0, 90 * DAY, 8 * HOUR)
    ]
    h = History(raw, deepcopy(raw), {s: events for s in raw})
    r = replay(h, Candidate("carry", 7, 7), 60 * DAY, 80 * DAY)
    assert r["trades"] > 0
    assert r["net"] > 0
    assert r["fees"] > 0 and r["estimated_friction"] > 0
    assert r["net"] == pytest.approx(r["funding"] - r["fees"] - r["estimated_friction"])
    assert r["gross_price_pnl_before_friction"] == pytest.approx(0, abs=1e-8)


def test_static_carry_does_not_rotate_or_close_on_brief_negative_funding():
    raw = tapes(symbols=("BTCUSDT", "ETHUSDT", "OTHERUSDT"))
    events = [
        {
            "timestamp_ms": when,
            "funding_rate": -0.0001 if 61 * DAY <= when < 64 * DAY else 0.0001,
            "mark": 100,
        }
        for when in range(0, 90 * DAY, 8 * HOUR)
    ]
    h = History(raw, deepcopy(raw), {s: events for s in raw})
    r = replay(h, Candidate("static_carry", 30, 360), 60 * DAY, 84 * DAY)
    assert r["trades"] == 2
    assert {t["symbol"] for t in r["trade_records"]} == {"BTCUSDT", "ETHUSDT"}
    assert all(
        t["closed_ms"] == 84 * DAY and t["reason"] == "period liquidation"
        for t in r["trade_records"]
    )
    assert r["net"] == pytest.approx(r["funding"] - r["fees"] - r["estimated_friction"])


def test_stop_gap_fills_at_adverse_open_and_cannot_reopen_immediately(monkeypatch):
    raw = tapes(symbols=("A",))
    raw["A"].loc[raw["A"].open_ms >= 60 * DAY + HOUR, ["open", "high", "low", "close"]] = 95
    h = History(raw, deepcopy(raw), {"A": []})
    monkeypatch.setattr(h, "targets", lambda c, i: [(0, 1, 1.0)] if i == 60 * 24 else [])
    r = replay(h, Candidate("relative_momentum", 7, 7), 60 * DAY, 63 * DAY)
    assert r["trades"] == 1
    trade = r["trade_records"][0]
    assert trade["reason"] == "gap stop"
    assert trade["closed_ms"] == 60 * DAY + HOUR
    assert trade["net"] < -100


def test_misaligned_or_invalid_venue_data_fails_closed():
    raw = tapes()
    spot = deepcopy(raw)
    spot["A"].loc[50, "open_ms"] += HOUR
    with pytest.raises(ValueError, match="identical"):
        History(raw, spot, {s: [] for s in raw})


def test_pair_formation_uses_only_completed_past_training_days():
    from paperfrog.research.pairs import formation

    raw = tapes(symbols=("A", "B"))
    rng = np.random.default_rng(42)
    common = np.repeat(np.cumsum(rng.normal(0, 0.03, 90)), 24)
    ratio = np.repeat(0.01 * np.sin(np.arange(90) * 2 * np.pi / 9), 24)
    for symbol, offset in (("A", ratio), ("B", np.zeros_like(ratio))):
        price = 100 * np.exp(common + offset)
        raw[symbol][["open", "high", "low", "close"]] = np.tile(price, (4, 1)).T
    h = History(raw, deepcopy(raw), {s: [] for s in raw})
    before = formation(h, 60 * 24, 60)
    assert before
    for frame in raw.values():
        frame.loc[frame.open_ms >= 60 * DAY, ["open", "high", "low", "close"]] *= 10
    changed = History(raw, deepcopy(raw), {s: [] for s in raw})
    assert formation(changed, 60 * 24, 60) == before
    assert all(m["formed_before_ms"] <= 60 * DAY for m in before)


def test_pair_execution_charges_both_legs_and_closes_together(monkeypatch):
    from paperfrog.research import pairs

    raw = tapes(symbols=("A", "B"))
    h = History(raw, deepcopy(raw), {s: [] for s in raw})
    model = {"a": 0, "b": 1, "mean": -0.03, "std": 0.01}
    monkeypatch.setattr(pairs, "formation", lambda *_: [model])
    r = pairs.replay_pairs(h, pairs.PairSpec(60, 2), 60 * DAY, 63 * DAY)
    assert r["trades"] == 1
    assert r["trade_records"][0]["symbols"] == ["A", "B"]
    assert r["trade_records"][0]["closed_ms"] == 63 * DAY
    assert r["net"] == pytest.approx(-r["fees"] - r["estimated_friction"])
    assert r["gross_price_pnl_before_friction"] == pytest.approx(0, abs=1e-8)
    assert r["fees"] > 3.9
