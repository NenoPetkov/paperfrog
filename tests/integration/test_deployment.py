import fcntl
import importlib.util
import io
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from paperfrog.settings import Settings

spec = importlib.util.spec_from_file_location(
    "server_ops", Path(__file__).resolve().parents[2] / "scripts/server_ops.py"
)
assert spec and spec.loader
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def test_deploy_refuses_a_running_bot_or_review(tmp_path):
    folder = tmp_path / "state/simple-live/daily-review"
    folder.mkdir(parents=True)
    with (folder / "service.lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ops.DeploymentError, match="still running"):
            ops.assert_idle(tmp_path)
    ops.assert_idle(tmp_path)


def test_backup_preserves_wal_records_and_financial_locks(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    folder = root / "state/simple-live"
    folder.mkdir(parents=True)
    (folder / "environment").symlink_to(tmp_path / "external-venv", target_is_directory=True)
    for name in (".env", "pyproject.toml", "uv.lock"):
        (root / name).write_text("")
    connection = sqlite3.connect(folder / "paperfrog.sqlite")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO kv VALUES ('risk_lock', ?)", ('{"state":"COOLDOWN"}',))
    connection.execute("INSERT INTO kv VALUES ('manual_lock', 'true')")
    connection.commit()
    # Keep the connection open so committed records remain in the WAL during copying.
    target = tmp_path / "backup"
    monkeypatch.setattr(ops.subprocess, "check_output", lambda *a, **kw: "a" * 40)
    monkeypatch.setattr(ops, "ensure_space", lambda _: None)
    try:
        ops.backup(root, target)
        copied = sqlite3.connect(target / "state/simple-live/paperfrog.sqlite")
        try:
            assert copied.execute("SELECT * FROM kv ORDER BY key").fetchall() == [
                ("manual_lock", "true"),
                ("risk_lock", '{"state":"COOLDOWN"}'),
            ]
        finally:
            copied.close()
        assert target.stat().st_mode & 0o077 == 0
        assert (target / ".env").stat().st_mode & 0o077 == 0
        assert (target / "state/simple-live/environment").is_symlink()
        assert json.loads((target / "manifest.json").read_text())["databases"] == {
            "simple-live/paperfrog.sqlite": {"kv": 2}
        }
    finally:
        connection.close()


def test_invalid_database_aborts_backup_before_copy(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "paperfrog.sqlite").write_bytes(b"invalid sqlite")
    target = tmp_path / "backup"
    monkeypatch.setattr(ops, "ensure_space", lambda _: None)
    with pytest.raises(sqlite3.DatabaseError):
        ops.backup(tmp_path, target)
    assert not target.exists()


def test_insufficient_space_blocks_deployment(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024))
    with pytest.raises(ops.DeploymentError, match="Insufficient free space"):
        ops.ensure_space(tmp_path)


def test_deployment_rejects_changed_risk_or_mode(tmp_path):
    settings = Settings(_env_file=None)
    previous = tmp_path / "context.json"
    previous.write_text(json.dumps(ops.context(settings)))
    ops.compare(settings, previous)
    with pytest.raises(ops.DeploymentError, match="configuration changed"):
        ops.compare(settings.model_copy(update={"risk_per_trade": 0.004}), previous)
    with pytest.raises(ops.DeploymentError, match="configuration changed"):
        ops.compare(settings.model_copy(update={"trading_mode": "live"}), previous)


def test_startup_wait_accepts_preserved_cooldown_without_reset(monkeypatch, capsys):
    settings = Settings(_env_file=None)
    status = {
        "mode": "paper",
        "settings_match": True,
        "reconciled": True,
        "heartbeat_after_start": True,
        "market_healthy_after_start": True,
        "scan_after_start": True,
        "heartbeat_age_seconds": 1,
        "state": "COOLDOWN",
    }
    monkeypatch.setattr(ops, "read_status", lambda _: status)
    ops.wait_online(settings, 1)
    assert json.loads(capsys.readouterr().out)["state"] == "COOLDOWN"


def test_startup_rejects_unreconciled_runtime(monkeypatch):
    settings = Settings(_env_file=None)
    clock = iter([0, 0, 2])
    monkeypatch.setattr(ops.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        ops,
        "read_status",
        lambda _: {"mode": "paper", "settings_match": True, "reconciled": False},
    )
    with pytest.raises(ops.DeploymentError, match="not confirmed"):
        ops.wait_online(settings, 1)


@pytest.mark.parametrize(
    "change",
    [
        {"state": "DEGRADED"},
        {"heartbeat_after_start": False},
        {"market_healthy_after_start": False},
        {"scan_after_start": False},
    ],
)
def test_startup_rejects_degraded_or_previous_process_health(monkeypatch, change):
    settings = Settings(_env_file=None)
    status = {
        "mode": "paper",
        "settings_match": True,
        "reconciled": True,
        "heartbeat_after_start": True,
        "market_healthy_after_start": True,
        "scan_after_start": True,
        "heartbeat_age_seconds": 1,
        "state": "RUNNING",
        **change,
    }
    clock = iter([0, 0, 2])
    monkeypatch.setattr(ops.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    monkeypatch.setattr(ops, "read_status", lambda _: status)
    with pytest.raises(ops.DeploymentError, match="not confirmed"):
        ops.wait_online(settings, 1)


@pytest.mark.parametrize(
    ("selected", "positions", "scan_at", "error_kind", "expected"),
    [
        ([], [], 998_000, None, True),
        (["ETHUSDT"], [], 998_000, None, False),
        ([], ["ETHUSDT"], 998_000, None, False),
        (None, [], 998_000, None, False),
        ([], [], 980_000, None, False),
        ([], [], 999_500, None, False),
        ([], [], 998_000, "simple_monitor_degraded", False),
        ([], [], 998_000, "simple_critical_task_stopped", False),
    ],
)
def test_idle_startup_requires_new_scan_and_successful_monitor(
    tmp_path, monkeypatch, selected, positions, scan_at, error_kind, expected
):
    settings = Settings(_env_file=None).model_copy(update={"state_dir": tmp_path})
    folder = ops.directory(settings)
    folder.mkdir()
    (folder / "process.json").write_text(
        json.dumps({"started_at": datetime.fromtimestamp(990, UTC).isoformat()})
    )
    with sqlite3.connect(folder / "paperfrog.sqlite") as conn:
        conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE audit (ts INTEGER, kind TEXT)")
        conn.executemany(
            "INSERT INTO kv VALUES (?, ?)",
            [
                ("reconciled", "true"),
                ("market_healthy_at", "0"),
                ("simple_scan_at_ms", str(scan_at)),
                ("risk_lock", '{"state":"DAILY_RISK_LOCK"}'),
            ],
        )
        # Failures from a previous process must not disqualify a healthy restart.
        conn.execute("INSERT INTO audit VALUES (980000, 'simple_monitor_degraded')")
        if error_kind:
            conn.execute("INSERT INTO audit VALUES (995000, ?)", (error_kind,))
    status = {
        "mode": "paper",
        "state": "DAILY_RISK_LOCK",
        "selected": selected,
        "open_positions": positions,
        "loaded_settings": settings.simple_parameters(),
    }
    monkeypatch.setattr(ops.time, "time", lambda: 1000)
    monkeypatch.setattr(
        ops.urllib.request,
        "urlopen",
        lambda url, **kw: io.StringIO(
            json.dumps({"heartbeat_age": 1} if url.endswith("/health") else status)
        ),
    )
    observed = ops.read_status(settings)
    assert observed["idle_monitor_healthy_after_start"] is expected
    assert observed["market_healthy_after_start"] is False
    assert observed["risk_locked"] is True
    if expected:
        ops.wait_online(settings, 1)
    else:
        clock = iter([0, 0, 2])
        monkeypatch.setattr(ops.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(ops.time, "sleep", lambda _: None)
        with pytest.raises(ops.DeploymentError, match="not confirmed"):
            ops.wait_online(settings, 1)
