"""Deployment operations; never reset ledgers, financial locks or live gates."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
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
    if args.command == "context":
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
