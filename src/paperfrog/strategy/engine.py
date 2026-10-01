from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from paperfrog.storage.database import Store


@dataclass(frozen=True)
class Candidate:
    episode_id: str
    symbol: str
    timestamp_ms: int
    crowded_side: int
    confirmations: int


class Episodes:
    def __init__(self, store: Store):
        self.store = store

    def update(
        self,
        symbol: str,
        bars: pd.DataFrame,
        active_universe: set[str],
        event_veto: bool = False,
        crisis: bool = False,
    ) -> Candidate | None:
        if len(bars) < 4:
            return None
        r = bars.iloc[-1]
        ts = int(r.timestamp_ms)
        if ts % 900000 != 0:
            return None
        key = "episode:" + symbol
        episode = self.store.get(key)
        if episode and (
            abs(r.basis_z_30d) < 1
            or ts - episode["start"] > 86400000
            or symbol not in active_universe
            or event_veto
        ):
            self.store.audit("episode_ended", episode)
            self.store.put(key, None)
            episode = None
        if symbol not in active_universe or crisis or event_veto:
            return None
        if episode is None:
            mandatory = [
                "basis_z_30d",
                "funding_z",
                "basis",
                "funding_rate",
                "oi_z_4h",
                "volume_ratio_z",
                "funding_persistence",
                "spread",
            ]
            if not all(math.isfinite(float(r[k])) for k in mandatory):
                return None
            if not (
                abs(r.basis_z_30d) >= 2.5
                and abs(r.funding_z) >= 2
                and r.basis * r.funding_rate > 0
                and (r.oi_z_4h >= 1.25 or r.volume_ratio_z >= 1.5)
                and r.funding_persistence >= 0.65
                and r.spread <= 0.002
                and bool(r.liquid)
            ):
                return None
            episode = {
                "id": hashlib.sha256(f"{symbol}:{ts}".encode()).hexdigest()[:20],
                "symbol": symbol,
                "start": ts,
                "side": 1 if r.basis > 0 else -1,
                "evaluated": False,
            }
            self.store.put(key, episode)
            self.store.audit("crowding", episode)
            return None
        if episode["evaluated"]:
            return None
        side = episode["side"]
        basis = bars.basis.iloc[-4:].abs()
        contracted = int((basis.diff().iloc[-3:] < 0).sum()) >= 2
        oi_fell = bool(r.oi_change_30m < 0)
        flow = bool((bars.perp_imbalance.iloc[-2:] * side < 0).all())
        failed_extreme = bool(
            r.perp_mid <= bars.perp_mid.iloc[-3:-1].max()
            if side > 0
            else r.perp_mid >= bars.perp_mid.iloc[-3:-1].min()
        )
        confirmations = sum(
            (contracted, oi_fell, flow, failed_extreme and basis.iloc[-1] < basis.iloc[-2])
        )
        if confirmations < 3:
            return None
        episode["evaluated"] = True
        self.store.put(key, episode)
        candidate = Candidate(episode["id"], symbol, ts, side, int(confirmations))
        self.store.audit("unwind", candidate.__dict__)
        return candidate


def entry_decision(
    probabilities: list[float],
    returns: list[float],
    baseline: float,
    funding: float,
    fees: float,
    spread: float,
    slippage: float,
    safety_margin: float = 0.0005,
) -> tuple[bool, float, str]:
    values = probabilities + returns + [baseline, funding, fees, spread, slippage, safety_margin]
    if not probabilities or not returns or not all(math.isfinite(x) for x in values):
        return False, 0.0, "invalid prediction"
    edge = float(
        np.median(returns)
        + funding
        - fees
        - spread
        - slippage
        - 0.5 * np.std(returns)
        - safety_margin
    )
    if np.median(probabilities) < 0.75:
        return False, edge, "probability below threshold"
    if np.median(returns) <= 0 or edge < 0.0035:
        return False, edge, "insufficient cost-adjusted edge"
    if baseline < 0.40:
        return False, edge, "baseline strongly disagrees"
    return True, edge, "quantitative gates passed"


@dataclass(frozen=True)
class ExitDecision:
    fraction: float
    reason: str


def exit_decision(
    *,
    held_minutes: float,
    pnl_r: float,
    basis: float,
    initial_basis: float,
    basis_z: float,
    basis_change_sigma: float,
    partial_taken: bool,
    event_veto: bool = False,
    portfolio_veto: bool = False,
    trailing_hit: bool = False,
) -> ExitDecision:
    if portfolio_veto or event_veto:
        return ExitDecision(1.0, "portfolio" if portfolio_veto else "material_event")
    if pnl_r <= -1:
        return ExitDecision(1.0, "hard_risk_stop")
    if (basis - initial_basis) * np.sign(initial_basis) >= max(0.0035, 1.5 * basis_change_sigma):
        return ExitDecision(1.0, "basis_invalidation")
    if held_minutes >= 240 or abs(basis_z) <= 0.5 or trailing_hit:
        return ExitDecision(1.0, "time" if held_minutes >= 240 else "convergence_or_trailing")
    if not partial_taken and (abs(basis) <= abs(initial_basis) * 0.5 or pnl_r >= 1.5):
        return ExitDecision(0.5, "partial_profit")
    return ExitDecision(0.0, "hold")
