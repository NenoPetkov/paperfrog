# File index

| Area | Files |
|---|---|
| Package/config | `pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `config/*.yaml` |
| Entry points | `src/paperfrog/cli.py`, `service.py`, `settings.py` |
| Contracts and storage | `domain/types.py`, `storage/database.py`, `storage/lock.py`, `migrations/001.sql` |
| Data/research | `data/lake.py`, `data/universe.py`, `data/streams.py`, `features/engineering.py`, `features/pipeline.py` |
| Models | `models/dataset.py`, `models/labels.py`, `models/training.py` |
| Strategy/portfolio/risk | `strategy/engine.py`, `portfolio/sizing.py`, `risk/engine.py`, `risk/crisis.py` |
| Brokers/execution | `exchange/binance.py`, `paper/broker.py`, `execution/coordinator.py` |
| Replay/reports | `backtest/engine.py`, `backtest/report.py`, `reporting/readiness.py` |
| Controls/monitoring | `llm/events.py`, `telegram/control.py`, `monitoring/health.py`, `monitoring/logging.py` |
| Tests | `tests/unit/`, `tests/integration/`, `tests/property/`, `tests/fixtures/replay.py` |
| Utilities | `scripts/demo_replay.py`, `scripts/telegram_ids.py` |
| Deployment | `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `Makefile` |
| Documentation | `README.md`, `ARCHITECTURE.md`, `STRATEGY.md`, `RISK.md`, `SECURITY.md`, `THREAT_MODEL.md`, `LIVE_READINESS.md`, `IMPLEMENTATION_REPORT.md`, `CHANGELOG.md`, `docs/`, `LICENSE` |

Generated raw data, state databases, model artifacts and research reports are ignored by Git. Only directory placeholders are included. Generated readiness artifacts are local; the checked-in LIVE_READINESS.md records the initial blocked status.
