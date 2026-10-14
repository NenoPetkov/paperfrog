from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paperfrog.domain.types import SafeError
from paperfrog.settings import Settings

# Missing evidence always fails. Report provenance is locally reviewed, never LLM generated.
GATES: dict[str, tuple[str, float | bool]] = {
    "oos_months": ("min", 9),
    "oos_trades": ("min", 250),
    "sharpe": ("min", 1.0),
    "profit_factor": ("min", 1.25),
    "max_drawdown": ("max", 0.08),
    "return_2x_cost": ("positive", 0.0),
    "symbol_pnl_concentration": ("max", 0.25),
    "month_pnl_concentration": ("max", 0.25),
    "positive_month_fraction": ("min", 0.6),
    "risk_violations": ("max", 0),
    "calibration_documented": ("true", True),
    "return_without_best_symbol": ("positive", 0.0),
    "return_without_best_month": ("positive", 0.0),
    "paper_days": ("min", 30),
    "paper_trades": ("min", 100),
    "testnet_days": ("min", 7),
    "reconciliation_failures": ("max", 0),
    "duplicate_orders": ("max", 0),
    "risk_breaches": ("max", 0),
    "slippage_verified": ("true", True),
    "telegram_verified": ("true", True),
    "testnet_flatten_verified": ("true", True),
    "restart_verified": ("true", True),
    "point_in_time_history_verified": ("true", True),
    "leakage_reviewed": ("true", True),
    "implementation_reviewed": ("true", True),
}


def readiness(evidence: dict[str, Any], config_hash: str, model_hash: str) -> dict[str, Any]:
    import math

    checks = {}
    for name, (op, limit) in GATES.items():
        value = evidence.get(name)
        valid = isinstance(value, (int, float)) and math.isfinite(value)
        passed = (
            value is True
            if op == "true"
            else valid
            and isinstance(value, (int, float))
            and (
                value >= limit if op == "min" else value <= limit if op == "max" else value > limit
            )
        )
        checks[name] = {
            "passed": bool(passed),
            "observed": value,
            "required": limit,
            "comparison": op,
        }
    return {
        "permitted": all(v["passed"] for v in checks.values()),
        "checks": checks,
        "created_at": datetime.now(UTC).isoformat(),
        "config_hash": config_hash,
        "model_hash": model_hash,
        "evidence_hash": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
        "validation": "passed"
        if all(v["passed"] for v in checks.values())
        else "unproven_or_failed",
    }


def write_report(report: dict[str, Any], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "readiness.json").write_text(json.dumps(report, indent=2))
    lines = [
        "# paperfrog live readiness",
        "",
        f"Live permitted: **{report['permitted']}**",
        "",
        "Missing evidence fails closed. Software tests do not demonstrate a trading edge.",
        "",
        "| Gate | Result | Observed | Required |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {k} | {'PASS' if v['passed'] else 'BLOCKED'} | {v['observed']} | {v['comparison']} {v['required']} |"
        for k, v in report["checks"].items()
    ]
    (folder / "readiness.md").write_text("\n".join(lines) + "\n")


def credential_binding(s: Settings) -> str:
    key = s.binance_api_key.get_secret_value()
    if not key:
        raise SafeError("exchange credential identity missing")
    return hashlib.sha256(("paperfrog-account-key:" + key).encode()).hexdigest()


def check_report(s: Settings, model_hash: str) -> dict[str, Any]:
    path = s.reports_dir / "readiness.json"
    if not path.exists():
        raise SafeError("live readiness report missing")
    r = json.loads(path.read_text())
    if (
        not r.get("permitted")
        or r.get("config_hash") != s.config_hash
        or r.get("model_hash") != model_hash
    ):
        raise SafeError("readiness failed or not bound to current configuration/model")
    if set(r.get("checks", {})) != set(GATES) or not all(
        v.get("passed") is True for v in r["checks"].values()
    ):
        raise SafeError("incomplete readiness checks")
    age = (datetime.now(UTC) - datetime.fromisoformat(r["created_at"])).total_seconds()
    if not 0 <= age < 86400:
        raise SafeError("readiness report stale")
    return dict(r)


def arm(s: Settings, model_hash: str, confirmation: str) -> None:
    if s.strategy_profile != "legacy":
        raise SafeError("simple strategy has no exchange execution approval")
    if s.trading_mode != "live" or confirmation != "ARM LIVE TRADING":
        raise SafeError("local live arming requirements not satisfied")
    report = check_report(s, model_hash)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    path = s.state_dir / "live-arm.json"
    record = {
        "config_hash": s.config_hash,
        "model_hash": model_hash,
        "account_binding": credential_binding(s),
        "expires": datetime.now(UTC).timestamp() + s.live_arm_hours * 3600,
        "evidence_hash": report["evidence_hash"],
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(record, file)


def verify_arm(s: Settings, model_hash: str) -> bool:
    if s.trading_mode != "live":
        return False
    if s.strategy_profile != "legacy":
        raise SafeError("simple strategy has no exchange execution approval")
    report = check_report(s, model_hash)
    path = s.state_dir / "live-arm.json"
    if not path.exists() or path.stat().st_mode & 0o077:
        raise SafeError("private local arm record missing")
    r = json.loads(path.read_text())
    if not (
        r["config_hash"] == s.config_hash
        and r["model_hash"] == model_hash
        and r["account_binding"] == credential_binding(s)
        and r["expires"] > datetime.now(UTC).timestamp()
        and r["evidence_hash"] == report["evidence_hash"]
    ):
        raise SafeError("live arm expired or invalidated")
    return True
