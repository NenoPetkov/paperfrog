import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_risk import proposal

from paperfrog.domain.types import SafeError, State
from paperfrog.risk.engine import RiskEngine
from paperfrog.settings import Settings, load_settings
from paperfrog.storage.database import Store


def close_trade(risk, store, index, pnl, now, health):
    trade = proposal(str(index), f"ALT{index}USDT")
    risk.reserve(trade, health, now)
    risk.closed(str(index), pnl, now)


def test_recovery_preset_changes_only_cooldown_and_concurrent_cap():
    preset = Path(__file__).resolve().parents[2] / "config/loss-protection.yaml"
    s = load_settings(preset)
    assert s.soft_loss_cooldown_minutes == 30
    assert s.max_concurrent_strategy_trades == 2
    assert s.trading_mode == "paper" and not s.live_trading_enabled


def test_repeated_losses_double_pause_durably_and_cap_at_twelve_hours(
    settings, tmp_path, health, now
):
    s = Settings(_env_file=None, **{**settings.public(), "soft_loss_cooldown_minutes": 30})
    path = tmp_path / "risk.sqlite"
    store = Store(path)
    store.put("state", State.RUNNING)
    store.put("reconciled", True)
    risk = RiskEngine(s, store)
    risk.anchors(10000, now)
    anchors = store.get("anchors")
    for index in range(3):
        close_trade(risk, store, index, -1, now, health)
    assert store.get("risk_lock")["cooldown_minutes"] == 30
    for index, duration in enumerate([60, 120, 240, 480, 720, 720], start=3):
        expiry = store.get("risk_lock")["until"]
        later = now + timedelta(seconds=expiry - now.timestamp())
        with pytest.raises(SafeError, match="cannot be overridden"):
            risk.resume(health, later - timedelta(milliseconds=1))
        store.engine.dispose()
        store = Store(path)
        risk = RiskEngine(s, store)
        risk.resume(health, later)
        close_trade(risk, store, index, -1, later, health)
        assert store.get("risk_lock")["until"] == later.timestamp() + duration * 60
        assert store.get("risk_lock")["cooldown_minutes"] == duration
        assert store.get("consecutive_losses") == index + 1
        assert store.get("anchors") == anchors
    store.engine.dispose()


def test_winning_exit_resets_future_backoff_without_unlocking_current_pause(
    settings, store, health, now
):
    s = Settings(_env_file=None, **{**settings.public(), "soft_loss_cooldown_minutes": 30})
    risk = RiskEngine(s, store)
    risk.reserve(proposal("winner", "WINUSDT"), health, now)
    store.put("loss_cooldown_step", 4)
    store.put("consecutive_losses", 8)
    lock = {
        "state": State.SOFT_RISK_LOCK,
        "until": now.timestamp() + 7200,
        "reason": "three_losses",
    }
    store.put("risk_lock", lock)
    risk.closed("winner", 1, now)
    assert store.get("loss_cooldown_step") == 0
    assert store.get("consecutive_losses") == 0
    assert store.get("risk_lock") == lock
    risk.resume(health, now + timedelta(hours=2))
    for index in range(3):
        close_trade(risk, store, index, -1, now + timedelta(hours=2), health)
    assert store.get("risk_lock")["cooldown_minutes"] == 30


def test_flat_exit_and_duplicate_exit_do_not_reset_or_advance_backoff(settings, store, health, now):
    risk = RiskEngine(settings, store)
    store.put("loss_cooldown_step", 3)
    store.put("consecutive_losses", 1)
    close_trade(risk, store, 0, 0, now, health)
    risk.closed("0", 0, now)
    assert store.get("loss_cooldown_step") == 3
    assert store.get("consecutive_losses") == 1


def test_operator_reset_audits_backoff_while_automatic_resume_retains_it(
    settings, store, health, now
):
    risk = RiskEngine(settings, store)
    store.put("loss_cooldown_step", 3)
    risk.resume(health, now)
    assert store.get("loss_cooldown_step") == 3
    risk.resume(health, now, override_locks=True, equity=10000)
    assert store.get("loss_cooldown_step") == 0
    reset = next(row for row in store.rows("audit") if row["kind"] == "operator_risk_reset")
    assert json.loads(reset["payload"])["previous_loss_cooldown_step"] == 3


def test_active_higher_priority_lock_is_not_replaced_or_shortened(settings, store, health, now):
    risk = RiskEngine(settings, store)
    risk.reserve(proposal("last"), health, now)
    store.put("consecutive_losses", 4)
    store.put("loss_cooldown_step", 3)
    lock = {"state": State.HARD_RISK_LOCK, "until": None, "reason": "high_water"}
    store.put("risk_lock", lock)
    risk.closed("last", -1, now)
    assert store.get("risk_lock") == lock
    assert store.get("loss_cooldown_step") == 3
