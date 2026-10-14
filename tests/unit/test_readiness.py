import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from paperfrog.domain.types import SafeError
from paperfrog.reporting.readiness import GATES, arm, readiness, verify_arm, write_report
from paperfrog.settings import Settings
from paperfrog.storage.lock import InstanceLock


def test_live_requires_every_environment_condition():
    for changes in (
        {},
        {"binance_env": "live"},
        {"binance_env": "live", "live_trading_enabled": True},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, trading_mode="live", **changes)


def test_short_history_is_explicit_and_paper_only():
    assert Settings(_env_file=None).min_history_days == 180
    assert Settings(_env_file=None, min_history_days=90).min_history_days == 90
    with pytest.raises(ValidationError):
        Settings(_env_file=None, min_history_days=89)
    with pytest.raises(ValidationError, match="180 history days"):
        Settings(_env_file=None, trading_mode="testnet", min_history_days=90)
    with pytest.raises(ValidationError, match="180 history days"):
        Settings(
            _env_file=None,
            trading_mode="live",
            binance_env="live",
            live_trading_enabled=True,
            i_understand_live_risk="YES",
            min_history_days=90,
        )


def test_empty_readiness_blocks(settings):
    r = readiness({}, settings.config_hash, "")
    assert not r["permitted"] and all(not c["passed"] for c in r["checks"].values())


def test_arming_is_bound_and_expires(tmp_path):
    s = Settings(
        _env_file=None,
        trading_mode="live",
        strategy_profile="legacy",
        binance_env="live",
        live_trading_enabled=True,
        i_understand_live_risk="YES",
        binance_api_key="dedicated-account-key",
        state_dir=tmp_path / "state",
        reports_dir=tmp_path / "reports",
    )
    evidence = {
        name: True if op == "true" else limit + 1 if op in {"min", "positive"} else 0
        for name, (op, limit) in GATES.items()
    }
    report = readiness(evidence, s.config_hash, "model")
    write_report(report, s.reports_dir)
    with pytest.raises(SafeError):
        arm(s, "model", "YES")
    arm(s, "model", "ARM LIVE TRADING")
    assert verify_arm(s, "model")
    with pytest.raises(SafeError):
        verify_arm(s, "changed-model")
    path = s.state_dir / "live-arm.json"
    r = json.loads(path.read_text())
    r["expires"] = datetime.now(UTC).timestamp() - 1
    path.write_text(json.dumps(r))
    with pytest.raises(SafeError, match="expired"):
        verify_arm(s, "model")


def test_exclusive_process_lock(tmp_path):
    with InstanceLock(tmp_path):
        with pytest.raises(SafeError):
            with InstanceLock(tmp_path):
                raise AssertionError("second lock must never succeed")
