"""Cost-aware, chronological strategy selection. Never changes financial limits."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, stdev
from typing import Any

from paperfrog.risk import activity
from paperfrog.settings import Settings
from paperfrog.storage.database import Store

from . import rotation
from .strategy import BAR_MS

VERSION = 1
POLICY_VERSION = 2
KEY = "simple_learning"
EVIDENCE = "simple_learning_outcomes"
TIMEFRAMES = ("5m", "15m")
HOLD_HOURS = (1, 2, 4, 8)
DAY = 86_400_000
WINDOW = 7 * DAY
MIN_TRADES = 24
MIN_BLOCKS = 8
MIN_EDGE = 0.0002
DWELL_MS = 4 * BAR_MS


def pattern(market: dict[str, Any] | None) -> str:
    """Fixed, interpretable bins, evaluated only from closed market observations."""
    if not market:
        return "unknown"
    try:
        regime = market["regime"]
        four, one, breadth = (float(market[k]) for k in (
            "median_return_4h", "median_return_1h", "bull_index"
        ))
        if not all(math.isfinite(v) for v in (four, one, breadth)):
            return "unknown"
        if regime == "MIXED":
            return "MIXED:reversal" if four * one < 0 else "MIXED:disagreement"
        if regime not in {"BULL", "BEAR"}:
            return "unknown"
        strong = (breadth >= 65 if regime == "BULL" else breadth <= 35)
        accelerating = abs(one) >= abs(four) / 4 and four * one > 0
        return regime + (":persistent" if strong and accelerating else ":fading")
    except (KeyError, ValueError, TypeError, OverflowError):
        return "unknown"


def score(records: list[dict[str, Any]], now_ms: int, context: str) -> list[dict[str, Any]]:
    """No open/future labels; purge overlapping training labels at the holdout cut.

    Correlated coins count as one four-hour block for uncertainty. Simulated
    outcomes are screening evidence, never an account PnL or proof of profit.
    """
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    for r in records:
        try:
            valid = (
                r["version"] == VERSION and r["pattern"] == context
                and now_ms - WINDOW <= r["opened_ms"] < r["closed_ms"] < now_ms
                and r["strategy"] in rotation.CHOICES and r["timeframe"] in TIMEFRAMES
                and r["hold_hours"] in HOLD_HOURS
                and math.isfinite(float(r["net_return"]))
                and math.isfinite(float(r["cost"])) and r["cost"] >= 0
            )
            if valid and r["id"] not in seen:
                seen.add(r["id"])
                groups[r["strategy"], r["timeframe"], r["hold_hours"]].append(r)
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    results = []
    for (strategy, timeframe, hold_hours), rows in sorted(groups.items()):
        rows.sort(key=lambda r: (r["opened_ms"], r["id"]))
        blocks: dict[int, list[float]] = defaultdict(list)
        for r in rows:
            blocks[r["opened_ms"] // (4 * 3_600_000)].append(r["net_return"])
        values = [mean(v) for _, v in sorted(blocks.items())]
        cutoff = rows[int(len(rows) * 0.7)]["opened_ms"]
        train = [r for r in rows if r["closed_ms"] < cutoff]
        validation = [r for r in rows if r["opened_ms"] >= cutoff]
        lower = mean(values) - 2.5 * stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None
        train_mean = mean(r["net_return"] for r in train) if train else None
        validation_mean = mean(r["net_return"] for r in validation) if validation else None
        stress = mean(r["net_return"] - r["cost"] for r in rows)
        days = len({r["opened_ms"] // DAY for r in rows})
        validation_blocks = len({r["opened_ms"] // (4 * 3_600_000) for r in validation})
        eligible = (
            len(rows) >= MIN_TRADES and len(values) >= MIN_BLOCKS and days >= 3
            and len(train) >= 12 and len(validation) >= 6 and validation_blocks >= 3
            and lower is not None and lower > MIN_EDGE
            and train_mean is not None and train_mean > 0
            and validation_mean is not None and validation_mean > MIN_EDGE and stress > 0
        )
        results.append({
            "strategy": strategy, "timeframe": timeframe, "hold_hours": hold_hours, "trades": len(rows),
            "blocks": len(values), "days": days, "mean_net_return": mean(values),
            "lower_bound": lower, "train_trades": len(train), "validation_trades": len(validation),
            "train_mean": train_mean, "validation_mean": validation_mean, "validation_blocks": validation_blocks,
            "double_cost_mean": stress, "eligible": eligible,
        })
    return sorted(results, key=lambda r: (r["eligible"], r["lower_bound"] or -1), reverse=True)


def review(store: Store, s: Settings, market: dict[str, Any] | None, now_ms: int) -> dict[str, Any]:
    """Review once per closed 15m context, with confirmation and minimum dwell."""
    boundary = now_ms // BAR_MS * BAR_MS
    old = store.get(KEY, {})
    if (old.get("asof_ms") == boundary and old.get("config_hash") == s.config_hash
            and old.get("policy_version") == POLICY_VERSION):
        return old
    context = pattern(market)
    fresh = (
        market is not None and market.get("timestamp_ms") == boundary
        and market.get("config_hash") == s.config_hash
    )
    research = store.get("simple_learning_research", {})
    evidence_fresh = (
        research.get("config_hash") == s.config_hash and not research.get("errors", [True])
        and 0 <= now_ms - research.get("asof_ms", 0) < 4 * 3_600_000
    )
    ranking = score(store.get(EVIDENCE, []), boundary, context) if fresh and evidence_fresh else []
    candidates = [r for r in ranking if r["eligible"] and r["hold_hours"] <= s.simple_max_hold_hours]
    winner = candidates[0] if candidates else None
    selection = rotation.selection(store)
    probe = (
        winner is None and selection.get("activity_mode") == "active"
        and fresh and evidence_fresh and s.simple_market_trend_enabled
        and not s.simple_ai_enabled and s.simple_entry_mode == "trend"
        and context in {"BULL:persistent", "BEAR:persistent"}
        and s.simple_max_hold_hours >= 1
    )
    candidate_policy = "probe" if probe else "validated"
    wanted = [winner["strategy"], winner["timeframe"], winner["hold_hours"]] if winner else None
    if probe:
        wanted = ["current", "15m", 1]
    consecutive = (
        old.get("confirmations", 0) + 1
        if (wanted and old.get("candidate") == wanted
            and old.get("candidate_policy", "validated") == candidate_policy
            and old.get("pattern") == context and old.get("asof_ms") == boundary - BAR_MS)
        else 1 if wanted else 0
    )
    reason = "No validated net edge for this market pattern; collecting shadow evidence."
    allowed = False
    with store.transaction() as conn:
        state = rotation.selection(store, conn)
        current = [state["active"], state.get("timeframe", s.simple_entry_timeframe), state.get("hold_hours", s.simple_max_hold_hours)]
        current_score = next((r for r in candidates if [r["strategy"], r["timeframe"], r["hold_hours"]] == current), None)
        if state["auto"] and wanted:
            if (wanted == current and state.get("entry_policy", "validated") == candidate_policy
                    and (not probe or consecutive >= 2)):
                allowed = True
                reason = "Selected variant retains positive purged holdout and cost-stressed evidence."
            elif consecutive >= 2 and now_ms - state.get("changed_ms", 0) >= DWELL_MS:
                improvement = winner["lower_bound"] - current_score["lower_bound"] if winner and current_score else math.inf
                if improvement >= 0.0005:
                    switch_reason = (f"Limited trend probe in {context}; historical edge unvalidated"
                                     if probe else f"Historical {context} edge; {wanted[1]}")
                    rotation.switch(store, state, wanted[0], now_ms, switch_reason, conn)
                    state["timeframe"] = wanted[1]
                    state["hold_hours"] = wanted[2]
                    state["entry_policy"] = candidate_policy
                    store.put(rotation.KEY, state, conn)
                    allowed = True
                    reason = "Changed after two agreeing reviews and at least one hour dwell."
            if not allowed and current_score and state.get("entry_policy", "validated") == "validated":
                allowed = True
                reason = "Retaining validated incumbent while challenger awaits confirmation/dwell."
        if probe:
            reason = ("Limited trend probe; historical edge unvalidated. Half size; one position; one per hour; six per rolling 24h."
                      if allowed else "Limited trend probe waiting for two same-pattern reviews and one-hour dwell.")
        result = {
            "version": VERSION, "policy_version": POLICY_VERSION,
            "asof_ms": boundary, "expires_ms": boundary + BAR_MS,
            "config_hash": s.config_hash, "pattern": context, "candidate": wanted,
            "confirmations": consecutive, "allowed": allowed, "reason": reason,
            "candidate_policy": candidate_policy,
            "entry_policy": state.get("entry_policy", "validated"),
            "activity_mode": state.get("activity_mode", "strict"),
            "ranking": ranking, "active": state["active"], "epoch": state["epoch"],
            "timeframe": state.get("timeframe", s.simple_entry_timeframe),
            "hold_hours": state.get("hold_hours", s.simple_max_hold_hours),
            "evidence": "simulated closed trades; profitability unproven",
        }
        store.put(KEY, result, conn)
        store.audit("simple_learning_review", result, conn)
    return result


def is_probe(store: Store) -> bool:
    state = rotation.selection(store)
    return bool(state["auto"] and state.get("entry_policy") == "probe")


def veto(store: Store, s: Settings, now_ms: int, trade_id: str | None = None) -> str | None:
    state = rotation.selection(store)
    if not state["auto"]:
        return None
    r = store.get(KEY, {})
    research = store.get("simple_learning_research", {})
    if not (
        research.get("config_hash") == s.config_hash
        and not research.get("errors", [True])
        and 0 <= now_ms - research.get("asof_ms", 0) < 4 * 3_600_000
        and r.get("hold_hours") == rotation.hold_hours(store, s)
        and
        r.get("allowed") is True and r.get("config_hash") == s.config_hash
        and r.get("epoch") == state["epoch"] and r.get("active") == state["active"]
        and r.get("timeframe") == rotation.timeframe(store, s)
        and r.get("asof_ms", 0) <= now_ms < r.get("expires_ms", 0)
    ):
        return "automatic strategy waiting for fresh validated historical net edge"
    if is_probe(store):
        if not (
            state.get("activity_mode") == "active" and state["active"] == "current"
            and r.get("entry_policy") == "probe" and r.get("policy_version") == POLICY_VERSION
            and r.get("pattern") in {"BULL:persistent", "BEAR:persistent"}
            and r.get("timeframe") == "15m" and r.get("hold_hours") == 1
            and s.simple_market_trend_enabled and not s.simple_ai_enabled
            and s.simple_entry_mode == "trend"
        ):
            return "active auto probe policy missing or incompatible"
        return activity.blocker(store, now_ms, trade_id)
    return None


def position_budget(position: dict[str, Any], market: dict[str, Any] | None,
                    now_ms: int, default_hours: float) -> dict[str, Any]:
    """Shorten a learned holding budget as follow-through fails; never extend its cap."""
    if not position.get("adaptive_holding") or not market:
        return position
    if not 0 <= now_ms - market.get("timestamp_ms", 0) < BAR_MS:
        return position
    try:
        one = float(market["median_return_1h"])
        four = float(market["median_return_4h"])
        if not math.isfinite(one) or not math.isfinite(four):
            return position
        side = position["side"]
        cap = min(float(position.get("max_hold_hours", default_hours)), default_hours)
        if side * one <= 0:
            cap = min(cap, 1.0)
        elif side * four <= 0 or abs(one) < abs(four) / 4:
            cap = min(cap, 2.0)
        return {**position, "max_hold_hours": cap}
    except (KeyError, TypeError, ValueError, OverflowError):
        return position
