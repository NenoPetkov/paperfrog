"""Deployment operations; never reset ledgers, financial locks or live gates."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from paperfrog.monitoring.logging import configure
from paperfrog.reporting.readiness import credential_binding
from paperfrog.settings import Settings, load_settings
from paperfrog.simple.authorization import directory, verify_simple_arm


class DeploymentError(Exception):
    """An operator-facing message containing no configuration or credentials."""


def settings() -> Settings:
    s = load_settings()
    configure(s)
    if s.strategy_profile != "simple":
        raise DeploymentError("This deployment workflow supports the simple profile only.")
    for field, expected in (
        (s.state_dir, "state"),
        (s.data_dir, "data"),
        (s.artifacts_dir, "artifacts"),
        (s.reports_dir, "reports"),
    ):
        if field.resolve() != Path(expected).resolve():
            raise DeploymentError("Nonstandard runtime paths require an explicit migration.")
    return s


def context(s: Settings) -> dict[str, str]:
    return {
        "mode": s.trading_mode,
        "config_hash": s.config_hash,
        "account_binding": credential_binding(s) if s.trading_mode != "paper" else "paper",
    }


def compare(s: Settings, path: Path) -> None:
    previous = json.loads(path.read_text())
    if s.trading_mode != previous.get("mode") or context(s) != previous:
        raise DeploymentError(
            "Mode, account or configuration changed; deployment cannot authorize that change."
        )


def assert_idle(root: Path) -> None:
    # Check all profiles and the separate daily-review writer, including legacy state.
    for path in (root / "state").rglob("service.lock"):
        with path.open("rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError("A local bot or daily review is still running.") from None
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def strict_auto(root: Path, s: Settings) -> None:
    """Explicit local reduction of entry permission, preserving all financial state."""
    from paperfrog.simple import rotation
    from paperfrog.storage.database import Store

    assert_idle(root)
    path = directory(s) / "paperfrog.sqlite"
    if not path.exists():
        raise DeploymentError("Existing strategy ledger required.")
    store = Store(path)
    try:
        if not rotation.selection(store)["auto"]:
            raise DeploymentError("Strict reduction requires an existing automatic strategy.")
        rotation.configure(store, "auto", int(time.time() * 1000))
        store.audit("local_strict_auto_activation", {"reason": "unvalidated live probes disabled after holding/momentum review"})
    finally:
        store.engine.dispose()


def activate_momentum(root: Path, s: Settings, previous: Path, profile: str, hold: int) -> None:
    """Explicit operator-requested LIVE migration; no account, leverage or lock reset."""
    from paperfrog.simple.momentum import PROFILES
    from paperfrog.simple.strategy import require_execution
    from paperfrog.storage.database import Store

    assert_idle(root)
    compare(s, previous)
    require_execution(s)
    if (s.trading_mode != "live" or s.simple_sizing_mode not in {"equity_range", "margin"}
            or profile not in PROFILES or hold not in {1, 2, 4, 8}
            or hold > s.simple_max_hold_hours):
        raise DeploymentError("Momentum activation requires an existing authorized LIVE margin-sized account and bounded holding time.")
    path = directory(s) / "paperfrog.sqlite"
    if not path.exists():
        raise DeploymentError("Existing reconciled ledger required.")
    store = Store(path)
    try:
        if (store.get("reconciled") is not True or store.get("simple_positions")
                or store.get("simple_pending") or store.get("simple_closing")
                or store.unresolved_orders()
                or any(Decimal(str(q)) != 0 for q in store.get("expected_positions", {}).values())):
            raise DeploymentError("Momentum migration requires a flat reconciled account with no pending orders; existing positions are not altered.")
        updates = {
            "simple_entry_profile": profile, "simple_entry_mode": "trend",
            "simple_entry_timeframe": "5m", "simple_ai_review_minutes": 15,
            "simple_max_hold_hours": hold, "simple_selected_count": 10,
            "simple_btc_reference_enabled": False, "simple_market_trend_enabled": False,
            "simple_ai_enabled": False, "simple_confidence_enabled": False,
            # A 10% ceiling cannot satisfy the old exact 10% floor after fees
            # and lot rounding. Margin mode permits smaller allocations and
            # restores the existing quote-liquidity cap; never round risk up.
            "simple_sizing_mode": "margin",
            "simple_position_margin_cap": min(s.simple_position_margin_cap, 0.1),
            "max_concurrent_strategy_trades": min(s.max_concurrent_strategy_trades, 2),
            "max_strategy_entries_per_day": min(s.max_strategy_entries_per_day, 12),
            "max_entries_per_symbol_per_day": min(s.max_entries_per_symbol_per_day, 2),
            "max_spread": min(s.max_spread, 0.0003),
        }
        target = Settings(_env_file=root / ".env", **{**s.public(), **updates})
        if credential_binding(target) != credential_binding(s):
            raise DeploymentError("Account binding changed; momentum migration refused.")
        env = root / ".env"
        original = env.read_bytes()
        keys = {key.upper(): str(value).lower() if isinstance(value, bool) else str(value)
                for key, value in updates.items()}
        pattern = re.compile(r"^\s*(?:export\s+)?(" + "|".join(keys) + r")\s*=")
        lines = [line for line in original.decode().splitlines() if not pattern.match(line)]
        revised = "\n".join([*lines, *(f"{key}={value}" for key, value in keys.items())]) + "\n"

        def write_env(content: bytes) -> None:
            temporary = env.with_name(".env.momentum.tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(env)

        write_env(revised.encode())
        try:
            loaded = load_settings()
            if loaded.config_hash != target.config_hash or credential_binding(loaded) != credential_binding(s):
                raise DeploymentError("Reloaded configuration differs from the reviewed momentum settings.")
        except Exception:
            write_env(original)
            raise
        # Source/account/configuration binding is renewed by the existing local
        # arm command only after the exact reviewed change has been verified.
        previous.write_text(json.dumps(context(loaded)))
        store.audit("local_live_momentum_activation", {"parameters": updates, "profitability": "unproven",
                                                       "authorization": "explicit operator LIVE request"})
    finally:
        store.engine.dispose()


def check_databases(folder: Path) -> dict[str, dict[str, int]]:
    result = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix not in {".sqlite", ".db"}:
            continue
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise DeploymentError("SQLite integrity check failed; deployment stopped.")
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            result[str(path.relative_to(folder))] = {
                name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"')
                .fetchone()[0]
                for name in tables
            }
        finally:
            conn.close()
    return result


def ensure_space(root: Path) -> None:
    state_bytes = sum(p.stat().st_size for p in (root / "state").rglob("*") if p.is_file())
    if shutil.disk_usage(root).free < state_bytes * 2 + 2 * 1024**3:
        raise DeploymentError("Insufficient free space for a state backup and deployment reserve.")


def backup(root: Path, target: Path) -> None:
    assert_idle(root)
    ensure_space(root)
    before = check_databases(root / "state")
    target.mkdir(parents=True, mode=0o700, exist_ok=False)
    shutil.copytree(root / "state", target / "state", symlinks=True)
    for name in (".env", "pyproject.toml", "uv.lock"):
        shutil.copy2(root / name, target / name)
    (target / ".env").chmod(0o600)
    after = check_databases(target / "state")
    if before != after:
        raise DeploymentError("Database backup row counts differ; deployment stopped.")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    (target / "manifest.json").write_text(json.dumps({"revision": revision, "databases": after}))
    print(json.dumps({"backup": str(target), "verified_databases": len(after)}))


def read_status(s: Settings) -> dict[str, Any]:
    base = f"http://127.0.0.1:{s.health_port}"
    with urllib.request.urlopen(base + "/health", timeout=5) as response:
        health = json.load(response)
    heartbeat_at = time.time() - health["heartbeat_age"]
    with urllib.request.urlopen(base + "/simple", timeout=5) as response:
        status = json.load(response)
    process = json.loads((directory(s) / "process.json").read_text())
    started_at = datetime.fromisoformat(process["started_at"]).timestamp()
    conn = sqlite3.connect(
        (directory(s) / "paperfrog.sqlite").resolve().as_uri() + "?mode=ro", uri=True
    )
    try:
        runtime = {
            key: json.loads(value)
            for key, value in conn.execute(
                "SELECT key,value FROM kv WHERE key IN "
                "('reconciled','market_healthy_at','simple_scan_at_ms','manual_lock','risk_lock')"
            )
        }
        monitor_errors = conn.execute(
            "SELECT count(*) FROM audit WHERE ts >= ? AND kind IN "
            "('simple_monitor_degraded', 'simple_critical_task_stopped')",
            (int(started_at * 1000),),
        ).fetchone()[0]
    finally:
        conn.close()
    market_at = float(runtime.get("market_healthy_at", 0))
    scan_at = runtime.get("simple_scan_at_ms", 0) / 1000
    return {
        "mode": status.get("mode"),
        "state": status.get("state"),
        "heartbeat_age_seconds": round(health["heartbeat_age"], 2),
        "reconciled": runtime.get("reconciled") is True,
        "heartbeat_after_start": heartbeat_at >= started_at,
        "market_healthy_after_start": market_at >= started_at and 0 <= time.time() - market_at < 60,
        # An empty selection has no quotes to fetch. Require a completed new scan,
        # a subsequent monitor heartbeat and no monitor failures in this process.
        # Risk locks can mask DEGRADED state, so state/heartbeat alone cannot suffice.
        "idle_monitor_healthy_after_start": (
            status.get("selected") == []
            and status.get("open_positions") == []
            and market_at == 0
            and started_at <= scan_at <= heartbeat_at
            and monitor_errors == 0
        ),
        "scan_after_start": scan_at >= started_at,
        "manual_lock": bool(runtime.get("manual_lock")),
        "risk_locked": bool(runtime.get("risk_lock")),
        "open_positions": status.get("open_positions", []),
        "entry_timeframe": status.get("entry_timeframe"),
        "scan_pool_count": status.get("scan_pool_count"),
        "settings_match": status.get("loaded_settings") == s.simple_parameters(),
        "live_armed": verify_simple_arm(s) if s.trading_mode == "live" else False,
    }


def wait_online(s: Settings, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = read_status(s)
            if (
                status["mode"] == s.trading_mode
                and status["settings_match"]
                and status["reconciled"]
                and status["heartbeat_after_start"]
                and (
                    status["market_healthy_after_start"]
                    or status.get("idle_monitor_healthy_after_start", False)
                )
                and status["scan_after_start"]
                and 0 <= status["heartbeat_age_seconds"] < 30
                and status["state"] not in {"BOOTING", "ERROR_SAFE", "DEGRADED"}
            ):
                # PAUSED/cooldown is valid: a deployment must never clear a risk/manual lock.
                print(json.dumps(status))
                return
        except (OSError, urllib.error.URLError, ValueError, KeyError, sqlite3.Error):
            pass
        time.sleep(2)
    raise DeploymentError("Fresh heartbeat and reconciled startup were not confirmed in time.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("idle")
    commands.add_parser("space")
    commands.add_parser("run")
    commands.add_parser("status")
    commands.add_parser("databases")
    commands.add_parser("strategy-strict")
    momentum = commands.add_parser("activate-momentum")
    momentum.add_argument("context", type=Path)
    momentum.add_argument("--profile", required=True)
    momentum.add_argument("--hold-hours", type=int, required=True)
    momentum.add_argument("--confirm", required=True)
    commands.add_parser("context").add_argument("--require-arm", action="store_true")
    commands.add_parser("compare").add_argument("path", type=Path)
    commands.add_parser("rearm").add_argument("path", type=Path)
    commands.add_parser("backup").add_argument("path", type=Path)
    commands.add_parser("wait").add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    root = args.root.resolve()
    os.chdir(root)
    os.umask(0o077)
    if args.command == "idle":
        assert_idle(root)
        return
    if args.command == "space":
        ensure_space(root)
        return
    if args.command == "databases":
        print(json.dumps(check_databases(root / "state")))
        return
    if args.command == "backup":
        backup(root, args.path)
        return
    s = settings()
    if args.command == "strategy-strict":
        strict_auto(root, s)
    elif args.command == "activate-momentum":
        if args.confirm != "ACTIVATE LIVE MOMENTUM":
            raise DeploymentError("Explicit local momentum activation confirmation required.")
        activate_momentum(root, s, args.context, args.profile, args.hold_hours)
    elif args.command == "context":
        if args.require_arm and s.trading_mode == "live":
            verify_simple_arm(s)
        print(json.dumps(context(s)))
    elif args.command == "compare":
        compare(s, args.path)
    elif args.command == "rearm":
        compare(s, args.path)
        assert_idle(root)
        if s.trading_mode == "live":
            subprocess.run(
                [str(root / ".venv/bin/paperfrog"), "live", "arm", "--confirm", "ARM LIVE TRADING"],
                check=True,
            )
    elif args.command == "run":
        if (root / "state/MIGRATED_TO_OVH").exists():
            raise DeploymentError("This local runtime was migrated; start the bot on OVH only.")
        folder = directory(s)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / "process.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "supervisor_pid": os.getppid(),
                    "started_at": datetime.now(UTC).isoformat(),
                    "mode": s.trading_mode,
                    "log": "journalctl -u paperfrog.service",
                }
            )
        )
        temporary.replace(folder / "process.json")
        binary = str(root / ".venv/bin/paperfrog")
        os.execv(binary, [binary, "run", s.trading_mode])
    elif args.command == "status":
        print(json.dumps(read_status(s)))
    elif args.command == "wait":
        wait_online(s, args.timeout)


if __name__ == "__main__":
    try:
        main()
    except DeploymentError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        # Validation/library exceptions may embed configuration, URLs or credentials.
        print(f"Deployment operation failed ({type(exc).__name__}); inspect locally.", file=sys.stderr)
        raise SystemExit(1) from None
