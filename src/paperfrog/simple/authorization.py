from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from paperfrog.domain.types import SafeError
from paperfrog.reporting.readiness import credential_binding
from paperfrog.settings import Settings

from .strategy import execution_rules, require_execution, strategy_name


def directory(s: Settings) -> Path:
    return s.state_dir / ("simple" if s.trading_mode == "paper" else "simple-" + s.trading_mode)


def strategy_hash(s: Settings) -> str:
    rules = execution_rules(s)
    digest = hashlib.sha256(
        json.dumps(
            {"rules": rules.public(), "strategy": strategy_name(s.simple_entry_mode, rules)},
            sort_keys=True,
        ).encode()
    )
    for name in (
        "strategy.py",
        "rotation.py",
        "confidence.py",
        "engine.py",
        "live_broker.py",
        "service.py",
        "data.py",
        "adaptive.py",
        "bitcoin.py",
        "minute.py",
        "market_trend.py",
    ):
        digest.update(Path(__file__).with_name(name).read_bytes())
    root = Path(__file__).parent.parent
    for name in (
        "exchange/binance.py",
        "execution/coordinator.py",
        "risk/engine.py",
        "risk/sizing.py",
    ):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def arm_simple(s: Settings, confirmation: str) -> None:
    require_execution(s)
    if s.trading_mode != "live" or confirmation != "ARM LIVE TRADING":
        raise SafeError("local live arming requirements not satisfied")
    folder = directory(s)
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "config_hash": s.config_hash,
        "strategy_hash": strategy_hash(s),
        "account_binding": credential_binding(s),
        "research_requirements": "waived_by_operator",
        "profitability": "unproven",
        "created_at": datetime.now(UTC).isoformat(),
    }
    temporary = folder / "live-arm.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(folder / "live-arm.json")


def verify_simple_arm(s: Settings) -> bool:
    if s.trading_mode != "live":
        return False
    require_execution(s)
    path = directory(s) / "live-arm.json"
    try:
        if path.stat().st_mode & 0o077:
            raise SafeError("private local simple arm record required")
        record = json.loads(path.read_text())
        if not (
            record.get("config_hash") == s.config_hash
            and record.get("strategy_hash") == strategy_hash(s)
            and record.get("account_binding") == credential_binding(s)
            and record.get("research_requirements") == "waived_by_operator"
        ):
            raise SafeError("simple live authorization changed; local rearming required")
    except (OSError, ValueError, AttributeError):
        raise SafeError("local simple live arm missing or invalid") from None
    return True
