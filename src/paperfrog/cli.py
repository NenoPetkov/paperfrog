from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
from pydantic import ValidationError

from paperfrog.data.lake import Lake, backfill, collect_snapshot, validate
from paperfrog.data.universe import build_universe
from paperfrog.domain.types import Filters, SafeError
from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.models.training import Ensemble, evaluate, train_ensemble, walk_forward
from paperfrog.monitoring.logging import configure
from paperfrog.reporting.readiness import arm, readiness, write_report
from paperfrog.service import Service
from paperfrog.settings import Settings, load_settings
from paperfrog.storage.database import Store

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="paperfrog — funding feedback breakdown research and guarded execution.",
)
(
    config_app,
    db_app,
    data_app,
    universe_app,
    features_app,
    model_app,
    backtest_app,
    report_app,
    run_app,
    live_app,
    risk_app,
    emergency_app,
    account_app,
) = [typer.Typer(no_args_is_help=True) for _ in range(13)]
for name, sub in zip(
    (
        "config",
        "db",
        "data",
        "universe",
        "features",
        "model",
        "backtest",
        "report",
        "run",
        "live",
        "risk",
        "emergency",
        "account",
    ),
    (
        config_app,
        db_app,
        data_app,
        universe_app,
        features_app,
        model_app,
        backtest_app,
        report_app,
        run_app,
        live_app,
        risk_app,
        emergency_app,
        account_app,
    ),
    strict=True,
):
    app.add_typer(sub, name=name)
config_path: Path | None = None


@app.callback()
def main(
    config: Annotated[Path | None, typer.Option(help="Non-secret YAML configuration")] = None,
) -> None:
    global config_path
    config_path = config


def settings() -> Settings:
    try:
        s = load_settings(config_path)
        configure(s)
        return s
    except (ValidationError, ValueError):
        typer.echo(
            "Invalid configuration; inspect YAML/environment field names and safety bounds.",
            err=True,
        )
        raise typer.Exit(2) from None


def emit(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, default=str, allow_nan=False))


def execute(awaitable: object) -> object:
    try:
        return asyncio.run(awaitable)
    except SafeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


@config_app.command("validate")
def config_validate() -> None:
    s = settings()
    emit({"project": "paperfrog", "hash": s.config_hash, "config": s.public()})


@db_app.command("migrate")
def db_migrate() -> None:
    s = settings()
    store = Store(s.state_dir / "paperfrog.sqlite")
    emit({"schema_version": store.get("schema_version")})


def milliseconds(value: str) -> int:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return int(stamp.timestamp() * 1000)


@data_app.command("backfill")
def data_backfill(symbol: str, start: str, end: str, interval: str = "15m") -> None:
    s = settings()

    async def work() -> None:
        adapter = BinanceAdapter(s)
        try:
            emit(
                await backfill(
                    adapter,
                    Lake(s.data_dir),
                    symbol,
                    milliseconds(start),
                    milliseconds(end),
                    interval,
                )
            )
        finally:
            await adapter.close()

    execute(work())


@data_app.command("collect")
def data_collect(once: bool = False, streams: bool = False) -> None:
    s = settings()

    async def work() -> None:
        adapter = BinanceAdapter(s)
        try:
            if streams:
                from paperfrog.data.streams import collect_all

                await collect_all(
                    adapter, Lake(s.data_dir), Store(s.state_dir / "paperfrog.sqlite"), s.symbols
                )
                return
            while True:
                for symbol in s.symbols:
                    await collect_snapshot(adapter, Lake(s.data_dir), symbol)
                if once:
                    break
                await asyncio.sleep(15)
        finally:
            await adapter.close()

    execute(work())


@data_app.command("validate")
def data_validate(path: Path, interval_ms: int = 900000) -> None:
    report = validate(pd.read_parquet(path), interval_ms)
    emit(report)
    if not report["valid"]:
        raise typer.Exit(2)


@data_app.command("coverage")
def data_coverage(symbol: str) -> None:
    lake = Lake(settings().data_dir)
    emit(
        {
            kind: validate(lake.read(kind, symbol))
            for kind in (
                "spot_1m",
                "perp_1m",
                "spot_15m",
                "perp_15m",
                "funding",
                "open_interest",
                "snapshots",
            )
        }
    )


@universe_app.command("build")
def universe_build(input: Path, asof: str) -> None:
    s = settings()
    emit(
        build_universe(
            json.loads(input.read_text()),
            milliseconds(asof),
            s,
            Store(s.state_dir / "paperfrog.sqlite"),
        )
    )


@features_app.command("build")
def features_build(symbol: str) -> None:
    from paperfrog.features.pipeline import build_symbol

    result = build_symbol(settings(), symbol)
    emit(
        {
            "symbol": symbol,
            "rows": len(result),
            "path": str(settings().data_dir / "features" / f"{symbol}.parquet"),
        }
    )


@model_app.command("train")
def model_train(input: Path, test_start: str, name: str) -> None:
    if Path(name).name != name:
        raise typer.BadParameter("name must be one directory component")
    s = settings()
    m = train_ensemble(
        pd.read_parquet(input), pd.to_datetime(test_start, utc=True), s.artifacts_dir / name
    )
    emit({"model_hash": m.hash, "promoted": False})


@model_app.command("evaluate")
def model_evaluate(input: Path) -> None:
    emit(evaluate(pd.read_parquet(input)))


@model_app.command("walk-forward")
def model_walk_forward(input: Path, output: Path) -> None:
    result = walk_forward(pd.read_parquet(input), output)
    emit({"oos_samples": len(result), "output": str(output)})


@model_app.command("promote")
def model_promote(name: str, shadow_report: Path, confirm: Annotated[str, typer.Option()]) -> None:
    if confirm != "PROMOTE MODEL" or Path(name).name != name:
        raise typer.BadParameter(
            'local promotion requires --confirm "PROMOTE MODEL" and a simple artifact name'
        )
    s = settings()
    model = Ensemble(s.artifacts_dir / name, s.max_model_age_days)
    evidence = json.loads(shadow_report.read_text())
    required = ("backtest_reviewed", "shadow_reviewed", "baseline_compared", "leakage_reviewed")
    if evidence.get("model_hash") != model.hash or not all(
        evidence.get(k) is True for k in required
    ):
        raise typer.BadParameter("matching local backtest/shadow review required")
    store = Store(s.state_dir / "paperfrog.sqlite")
    record = {
        "directory": name,
        "hash": model.hash,
        "time": datetime.now(UTC).isoformat(),
        "evidence": evidence,
    }
    store.put("approved_model", record)
    store.audit("model_promotion", record)
    (s.state_dir / "live-arm.json").unlink(missing_ok=True)
    emit({"promoted": name, "live_disarmed": True})


@backtest_app.command("run")
def backtest_run(
    input: Path, filters: Path, output: Path, strategy: str = "ml", stresses: bool = False
) -> None:
    from paperfrog.backtest.engine import STRESSES, Stress, replay

    if strategy not in {"ml", "rules", "cash"}:
        raise typer.BadParameter("strategy must be ml, rules, or cash")
    s = settings()
    panel = pd.read_parquet(input)
    exchange_filters = {
        symbol: Filters(**{k: Decimal(str(v)) for k, v in fields.items()})
        for symbol, fields in json.loads(filters.read_text()).items()
    }

    async def work() -> None:
        results = []
        for stress in STRESSES if stresses else [Stress()]:
            results.append(
                await replay(panel, s, output / stress.name, exchange_filters, stress, strategy)
            )
        if stresses and results[0].get("trades", 0):
            from paperfrog.backtest.report import bootstrap

            trades = pd.read_parquet(output / "baseline" / "trades.parquet")
            best_symbol = str(trades.groupby("symbol").pnl.sum().idxmax())
            best_month = str(
                trades.groupby(
                    pd.to_datetime(trades.timestamp_ms, unit="ms", utc=True).dt.strftime("%Y-%m")
                )
                .pnl.sum()
                .idxmax()
            )
            for extra in (
                Stress("without_best_symbol", excluded_symbol=best_symbol),
                Stress("without_best_month", excluded_month=best_month),
            ):
                results.append(
                    await replay(panel, s, output / extra.name, exchange_filters, extra, strategy)
                )
            (output / "bootstrap.json").write_text(json.dumps(bootstrap(trades), indent=2))
        emit(results)

    execute(work())


@report_app.command("readiness")
def report_readiness(evidence: Path | None = None) -> None:
    s = settings()
    model_hash = ""
    approved = Store(s.state_dir / "paperfrog.sqlite").get("approved_model")
    if approved:
        model_hash = Ensemble(s.artifacts_dir / approved["directory"]).hash
    report = readiness(
        json.loads(evidence.read_text()) if evidence else {}, s.config_hash, model_hash
    )
    write_report(report, s.reports_dir)
    emit({"live_permitted": report["permitted"], "report": str(s.reports_dir / "readiness.md")})
    if not report["permitted"]:
        raise typer.Exit(2)


def run_mode(mode: str, once: bool) -> None:
    s = settings()
    if s.trading_mode != mode:
        raise typer.BadParameter("command mode must match TRADING_MODE/YAML")
    execute(Service(s).run(once))


@run_app.command("paper")
def run_paper(once: bool = False) -> None:
    run_mode("paper", once)


@run_app.command("testnet")
def run_testnet(once: bool = False) -> None:
    run_mode("testnet", once)


@run_app.command("live")
def run_live(once: bool = False) -> None:
    run_mode("live", once)


@live_app.command("arm")
def live_arm(confirm: Annotated[str, typer.Option()]) -> None:
    s = settings()
    service = Service(s)
    service.load_model()

    async def work() -> None:
        # Authenticated reconciliation must succeed before the local record is created.
        await service.exchange.sync_time()
        await service.exchange.exchange_info()
        await service.exchange.verify_risk_settings(s.symbols)
        if not await service.coordinator.reconcile():
            raise SafeError("cannot arm with account mismatch")
        arm(s, service.model.hash, confirm)
        await service.exchange.close()

    execute(work())
    emit({"armed": True, "expires_in_hours": s.live_arm_hours})


@live_app.command("disarm")
def live_disarm() -> None:
    s = settings()
    (s.state_dir / "live-arm.json").unlink(missing_ok=True)
    Store(s.state_dir / "paperfrog.sqlite").put("manual_lock", True)
    emit({"armed": False})


@risk_app.command("status")
def risk_status() -> None:
    s = settings()
    store = Store(s.state_dir / "paperfrog.sqlite")
    emit(
        {
            "anchors": store.get("anchors"),
            "risk_lock": store.get("risk_lock"),
            "state": store.get("state"),
        }
    )


@risk_app.command("review-unlock")
def risk_review_unlock(confirm: Annotated[str, typer.Option()]) -> None:
    if confirm != "REVIEW RISK LOCK":
        raise typer.BadParameter("local review confirmation required")
    s = settings()
    store = Store(s.state_dir / "paperfrog.sqlite")
    # Never reset equity anchors. An unresolved drawdown re-locks at the next monitor tick.
    store.audit("local_risk_review", {"previous": store.get("risk_lock")})
    store.put("risk_lock", None)
    store.put("state", "PAUSED")
    store.put("manual_lock", True)
    emit({"state": "PAUSED", "anchors_preserved": True})


@account_app.command("reconcile")
def account_reconcile() -> None:
    s = settings()
    execute(Service(s).run(once=True))


@account_app.command("configure")
def account_configure(confirm: Annotated[str, typer.Option()]) -> None:
    if confirm != "CONFIGURE FLAT ACCOUNT":
        raise typer.BadParameter("explicit local flat-account configuration confirmation required")
    s = settings()

    async def work() -> None:
        adapter = BinanceAdapter(s)
        try:
            await adapter.sync_time()
            await adapter.configure_flat_account(s.symbols)
        finally:
            await adapter.close()

    execute(work())
    emit({"margin": "ISOLATED", "leverage": 2, "position_mode": "ONE_WAY"})


@emergency_app.command("flatten")
def emergency_flatten(confirm: Annotated[str, typer.Option()]) -> None:
    if confirm != "FLATTEN ALL POSITIONS":
        raise typer.BadParameter('use --confirm "FLATTEN ALL POSITIONS"')
    s = settings()
    # Queue the request to the single owning service to prevent two execution processes.
    store = Store(s.state_dir / "paperfrog.sqlite")
    operation = "local-" + os.urandom(12).hex()
    store.put("local_flatten_request", operation)
    emit(
        {
            "queued": operation,
            "note": "running service must acknowledge; inspect audit and positions",
        }
    )


@model_app.command("dataset")
def model_dataset(input: Path, output: Path) -> None:
    from paperfrog.models.dataset import build_samples

    emit({"episodes": len(build_samples(pd.read_parquet(input), output))})


def entrypoint() -> None:
    try:
        app()
    except SafeError as exc:
        typer.echo(str(exc), err=True)
        raise SystemExit(2) from None
    except (ValueError, OSError, KeyError):
        typer.echo(
            "Invalid or unavailable input; inspect configuration, data contracts and paths.",
            err=True,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    entrypoint()
