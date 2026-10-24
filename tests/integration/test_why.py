import copy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from test_controls import update
from test_market_trend import breadth, guarded

from paperfrog.simple import rotation
from paperfrog.simple.blockers import explain
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import BAR_MS

NOW = 1_792_419_360_000 + 1000
BOUNDARY = NOW // BAR_MS * BAR_MS
ENTRY = NOW // 480_000 * 480_000


def ready(settings, store, **changes):
    s = guarded(settings, **changes)
    store.put("manual_lock", False)
    store.put("heartbeat", NOW / 1000)
    store.put("reconciled", True)
    store.put("simple_selected", ["SOLUSDT"])
    record = breadth(store, s, BOUNDARY)
    store.audit(
        "simple_selection",
        {
            "asof_ms": ENTRY,
            "entry_timeframe": "8m",
            "trend_hours": 4,
            "confirmation_hours": 1,
            "pool_size": 100,
            "usable_size": 100,
        },
    )
    return s, record


def test_mixed_explains_exact_conflicting_readings_and_checks(settings, store):
    s, record = ready(settings, store)
    record.update(
        {
            "regime": "MIXED",
            "median_return_1h": -0.012,
            "btc_return_1h": -0.005,
            "eth_return_1h": -0.013,
        }
    )
    store.put("simple_market_trend", record)
    output = explain(s, store, NOW, False, False)
    assert "Market: MIXED" in output
    assert "Market median: 4h +2.00% | 1h -1.20%" in output
    assert "LONG market check: blocked" in output
    assert "Market median 1h is negative (-1.20%)" in output
    assert "SHORT market check: blocked" in output
    assert "4h advancing coins must be below 50%" in output
    assert "MIXED permits neither LONG nor SHORT" in output
    assert "EEST" in output and "17:15:00" in output
    assert "config_hash" not in output and '{"' not in output


def test_opposite_mixed_reports_permission_and_preserves_risk_blockers(settings, store):
    s, record = ready(settings, store)
    rotation.configure(store, "opposite", 0)
    record.update(regime="MIXED", eth_return_1h=-0.02)
    store.put("simple_market_trend", record)
    store.put("risk_lock", {"reason": "three_losses", "until": NOW / 1000 + 60})
    output = explain(s, store, NOW, False, False)
    assert "Market: MIXED" in output
    assert "LONG market check: passed — MIXED allowed in opposite strategy" in output
    assert "SHORT market check: passed — MIXED allowed in opposite strategy" in output
    assert "MIXED permits neither" not in output
    assert "coin trend, BTC and risk checks still apply" in output
    assert "loss streak cooldown, until" in output
    assert "a trend signal alone is insufficient" in output


@pytest.mark.parametrize("side,label", [(1, "LONG"), (-1, "SHORT")])
def test_aligned_market_reports_pass_for_the_correct_direction(settings, store, side, label):
    s, _ = ready(settings, store)
    breadth(store, s, BOUNDARY, side)
    output = explain(s, store, NOW, False, False)
    assert f"{label} market check: passed" in output
    assert "No general blocker found" in output


@pytest.mark.parametrize(
    "damage", ["old_windows", "stale", "future", "coverage", "missing", "nan", "regime"]
)
def test_invalid_or_old_snapshot_is_never_relabelled_as_current(settings, store, damage):
    s, record = ready(settings, store)
    if damage == "old_windows":
        record["median_return_24h"] = record.pop("median_return_4h")
        record["trend_hours"] = 24
    elif damage == "stale":
        record["timestamp_ms"] -= BAR_MS
    elif damage == "future":
        record["timestamp_ms"] += BAR_MS
    elif damage == "coverage":
        record["pool_coins"] = 100
    elif damage == "missing":
        record = None
    elif damage == "nan":
        # SQLite JSON can contain null even though Store rejects nonfinite floats.
        record["median_return_4h"] = None
    else:
        record["regime"] = "BEAR"
    store.put("simple_market_trend", record)
    output = explain(s, store, NOW, False, False)
    assert "waiting for fresh 4h/1h data" in output
    assert "Bitcoin: 4h" not in output
    assert "LONG market check: passed" not in output


def test_lock_capacity_limits_cooldowns_and_expired_locks_are_distinct(settings, store):
    s, _ = ready(settings, store)
    store.put("manual_lock", True)
    store.put("risk_lock", {"reason": "three_losses", "until": NOW / 1000 + 60})
    store.put("profit_entry_lock", True)
    store.put("simple_positions", {"SOLUSDT": {}, "ETHUSDT": {}})
    store.put("cooldown:SOLUSDT", NOW / 1000 + 120)
    store.put("cooldown:ETHUSDT", NOW / 1000 - 1)
    day = datetime.fromtimestamp(NOW / 1000, UTC).date().isoformat()
    with store.engine.begin() as conn:
        for i in range(s.max_strategy_entries_per_day):
            conn.execute(
                text(
                    "INSERT INTO entries(id,day,symbol,ts,closed,risk,gross) VALUES (:id,:day,'SOLUSDT',:ts,1,1,1)"
                ),
                {"id": str(i), "day": day, "ts": NOW},
            )
    output = explain(s, store, NOW, False, False)
    for part in [
        "Manual pause",
        "loss streak cooldown, until",
        "profit protection",
        "maximum number",
        "Daily entry limit reached",
        "SOLUSDT: daily per-coin",
        "SOLUSDT: coin cooldown",
        "Coin cooldowns: 1 active",
    ]:
        assert part in output
    store.put("risk_lock", {"reason": "three_losses", "until": NOW / 1000 - 1})
    output = explain(s, store, NOW, False, False)
    assert "cooldown has expired" in output
    assert "Risk lock: loss streak cooldown, until" not in output


def test_fresh_rejections_explain_coins_and_old_scan_is_marked_stale(settings, store):
    s, _ = ready(settings, store)
    store.put("simple_selected", [])
    audit = {
        "asof_ms": ENTRY,
        "entry_timeframe": "8m",
        "trend_hours": 4,
        "confirmation_hours": 1,
        "pool_size": 100,
        "usable_size": 100,
        "day_trend_blocks": {"ELSAUSDT": "coin 4-hour return opposes LONG (-2.00%)"},
    }
    store.audit("simple_selection", audit)
    output = explain(s, store, NOW, False, False)
    assert "ELSAUSDT: coin 4-hour return opposes LONG (-2.00%)" in output
    assert "No coin passed" in output
    audit["asof_ms"] -= 480_000
    store.audit("simple_selection", audit)
    output = explain(s, store, NOW, False, False)
    assert "Waiting for the next fresh entry scan" in output
    assert "ELSAUSDT" not in output


async def test_why_is_authenticated_read_only_and_in_the_menu(settings):
    from paperfrog.telegram.commands import menu_commands

    s = guarded(settings, telegram_allowed_user_ids=(1,), telegram_allowed_chat_ids=(2,))
    service = SimpleService(s)
    service.now_ms = lambda: NOW
    ready(s, service.store)
    service.crisis = False
    service.exchange.request = AsyncMock()
    keys = ["manual_lock", "risk_lock", "anchors", "consecutive_losses", "simple_positions"]
    before = {key: copy.deepcopy(service.store.get(key)) for key in keys}
    try:
        assert await service.controls.handle(update("/why", uid=99)) is None
        response = await service.controls.handle(update("/why"))
        assert response and response[0] == 2 and "Trading check" in response[1]
        assert "Market: BULL" in response[1]
        assert {key: service.store.get(key) for key in keys} == before
        service.exchange.request.assert_not_called()
        assert "why" in {item["command"] for item in menu_commands()}
    finally:
        await service.exchange.close()
        service.store.engine.dispose()


def test_ai_pending_and_paused_permissions_are_explained(settings, store):
    from test_adaptive import permit, plan

    s, _ = ready(settings, store, simple_ai_enabled=True)
    output = explain(s, store, NOW, False, False)
    assert "AI has no current entry permission" in output
    permit(s, store, plan(ENTRY, action="pause"))
    assert "AI requested a pause" in explain(s, store, NOW, False, False)
    permit(s, store, plan(ENTRY))
    assert "AI: current entry permission is active" in explain(s, store, NOW, False, False)
