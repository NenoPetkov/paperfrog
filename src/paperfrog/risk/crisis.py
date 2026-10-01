from __future__ import annotations

from dataclasses import dataclass

from paperfrog.risk.engine import crisis_score
from paperfrog.storage.database import Store


@dataclass(frozen=True)
class Observations:
    btc_5m: float = 0.0
    sigma_5m: float = 0.0
    btc_15m: float = 0.0
    sigma_15m: float = 0.0
    btc_1h: float = 0.0
    spread_ratio: float = 1.0
    dispersion_z: float = 0.0
    mark_deviation: float = 0.0
    liquidation_z: float = 0.0
    stale_held: bool = False
    mismatch: bool = False
    collateral_abnormal: bool = False


@dataclass(frozen=True)
class CrisisAction:
    score: int
    state: str
    reduce_fraction: float
    block_until: float


class CrisisMonitor:
    def __init__(self, store: Store):
        self.store = store

    def observe(
        self, timestamp: float, o: Observations, *, emergency: bool = False
    ) -> CrisisAction:
        score = crisis_score(
            btc_5m=o.btc_5m,
            sigma_5m=o.sigma_5m,
            btc_15m=o.btc_15m,
            sigma_15m=o.sigma_15m,
            btc_1h=o.btc_1h,
            spread_ratio=o.spread_ratio,
            dispersion_z=o.dispersion_z,
            mark_deviation=o.mark_deviation,
            liquidation_z=o.liquidation_z,
            stale_held=o.stale_held,
            mismatch=o.mismatch,
            collateral_abnormal=o.collateral_abnormal,
        )
        prior = self.store.get("crisis", {"score": 0, "block_until": 0.0, "reduced": False})
        until = max(prior["block_until"], timestamp + 1800 if score else 0.0)
        state = (
            "EMERGENCY"
            if emergency or score >= 3
            else "CRISIS"
            if score >= 2
            else "CAUTION"
            if score or until > timestamp
            else "NORMAL"
        )
        reduction = (
            1.0
            if state == "EMERGENCY"
            else 0.5
            if state == "CRISIS" and not prior["reduced"]
            else 0.0
        )
        record = {
            "score": score,
            "state": state,
            "block_until": until,
            "reduced": state == "CRISIS" or prior["reduced"] and state != "NORMAL",
        }
        self.store.put("crisis", record)
        self.store.put("crisis_score", max(score, 1 if until > timestamp else 0))
        self.store.put("crisis_risk_multiplier", 1.0 if state == "NORMAL" else 0.5)
        if state != prior.get("state"):
            self.store.audit("crisis_transition", record)
        return CrisisAction(score, state, reduction, until)
