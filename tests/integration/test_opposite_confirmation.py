from copy import deepcopy
from dataclasses import replace

import pytest
from pandas.testing import assert_frame_equal
from test_simple import row, source

from paperfrog.domain.types import SafeError
from paperfrog.simple.strategy import BAR_MS, RULES, entry, exit_reason, indicators, opposite_entry


def confirmed_row(now=200 * BAR_MS, direction=1, kind="failed"):
    """Old direction breaks out, then a later candle rejects it with reversed flow."""
    obs = row(now, direction)
    obs.update(
        open=100 + direction * .8, close=100, fast=100 + direction * .5,
        prior_high=100 + (2 if direction > 0 else -.2),
        prior_low=100 + (.2 if direction > 0 else -2),
        flow=-direction * .4,
        previous_trend_atr=direction * 2, previous_close=100 + direction,
        previous_fast=100 + direction * .5, previous_flow=direction * .4,
        previous_atr_fraction=.008,
        previous_prior_high=100 + (.4 if direction > 0 else 2),
        previous_prior_low=100 + (-2 if direction > 0 else -.4),
    )
    if kind == "structure":
        obs["previous_prior_high" if direction > 0 else "previous_prior_low"] = 100 + direction * 2
    return obs


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("kind", ["failed", "structure"])
def test_later_rejection_or_structure_break_confirms_opposite(side, kind):
    obs = confirmed_row(direction=side, kind=kind)
    assert entry(obs, .0007)[0] == 0  # Not a current trend continuation signal.
    actual, reason, level = opposite_entry(obs, .0007)
    assert actual == -side and level is not None
    assert ("breakout failed" if kind == "failed" else "structure reversal") in reason


@pytest.mark.parametrize("side", [1, -1])
def test_ordinary_trend_breakout_cannot_be_blindly_inverted(side):
    obs = row(direction=side)
    assert entry(obs, .0007)[0] == side
    assert opposite_entry(obs, .0007)[0] == 0


@pytest.mark.parametrize("damage", ["flow", "body", "no_reclaim", "no_prior_trend", "nan", "no_history", "cost", "short_candles"])
@pytest.mark.parametrize("side", [1, -1])
def test_unconfirmed_or_invalid_rejections_cannot_trade(side, damage):
    obs = confirmed_row(direction=side)
    cost = .0007
    if damage == "flow":
        obs["flow"] = side * .4
    elif damage == "body":
        obs["open"] = 100 - side
    elif damage == "no_reclaim":
        obs["close"] = 100 + side * 2
    elif damage == "no_prior_trend":
        obs["previous_trend_atr"] = 0
    elif damage == "nan":
        obs["previous_close"] = float("nan")
    elif damage == "no_history":
        obs.pop("previous_flow")
    elif damage == "cost":
        cost = .01
    else:
        obs["entry_timeframe"] = "5m"
    assert opposite_entry(obs, cost)[0] == 0


@pytest.mark.parametrize("side", [1, -1])
def test_confirmation_is_causal_and_requires_a_later_completed_bar(side):
    raw = source(240, side)
    before = indicators(raw)
    last = raw.iloc[-1]
    next_close = float(before.iloc[-1]["prior_high" if side > 0 else "prior_low"]) - side * .3
    next_bar = dict(last)
    next_bar.update(
        timestamp_ms=int(last.timestamp_ms) + BAR_MS, open=float(last.close), close=next_close,
        high=max(float(last.close), next_close) * 1.001,
        low=min(float(last.close), next_close) * .999,
        taker_buy_quote=300_000 if side > 0 else 700_000,
    )
    import pandas as pd

    after = indicators(pd.concat([raw, pd.DataFrame([next_bar])], ignore_index=True))
    assert_frame_equal(before, after.iloc[:-1].reset_index(drop=True))
    assert opposite_entry(before.iloc[-1].to_dict(), .0007)[0] == 0
    assert opposite_entry(after.iloc[-1].to_dict(), .0007)[0] == -side
    broken = raw.copy()
    broken.loc[239, "timestamp_ms"] += BAR_MS
    with pytest.raises(SafeError, match="continuous"):
        indicators(broken)


@pytest.mark.parametrize("side", [1, -1])
def test_confirmed_inverse_tracks_reversal_invalidation_instead_of_old_trend(side):
    actual = -side
    obs = confirmed_row(direction=side)
    _, _, level = opposite_entry(obs, .0007)
    position = {
        "side": actual, "decision_side": side, "entry_mode": "trend", "opened_ms": 0,
        "stop_price": 100 - actual * 2, "opposite_confirmation_version": 1, "opposite_level": level,
    }
    assert exit_reason(position, 100, BAR_MS, obs) is None
    continuing = {**obs, "trend_atr": actual * 2, "close": 100 + actual}
    assert exit_reason(position, 100, 2 * BAR_MS, continuing) is None
    invalid = {**continuing, "close": level + side * .01}
    assert "reclaimed" in exit_reason(position, 100, 3 * BAR_MS, invalid)
    legacy = deepcopy(position)
    legacy.pop("opposite_confirmation_version")
    assert "original entry decision" in exit_reason(legacy, 100, 2 * BAR_MS, continuing)
    assert "stop" in exit_reason(position, position["stop_price"], BAR_MS, obs)
    assert "holding limit" in exit_reason(position, 100, RULES.maximum_hold_bars * BAR_MS, obs)
    assert opposite_entry(obs, .0007, replace(RULES, allowed_side="long" if actual < 0 else "short"))[0] == 0
