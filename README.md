# paperfrog

paperfrog is a Python 3.12 implementation of funding feedback breakdown research, paper execution, and guarded Binance USDⓈ-M integration. It looks for persistent crowding **followed by an unwind**, then constructs an alt/BTC/ETH relative-value pair. Cash and zero trades are valid outcomes.

**Default: PAPER. Live readiness: BLOCKED. Trading edge: unproven.** There is no trained production model or historical out-of-sample performance claim in this repository. Tests verify software mechanics, not profitability. Read [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) for delivered scope and material limitations before operating it.

## Install and verify

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv sync --frozen
cp .env.example .env
chmod 600 .env
uv run paperfrog config validate
uv run paperfrog db migrate
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pip-audit --skip-editable
```

Dependencies are exactly pinned in `pyproject.toml` and resolved, with hashes, in `uv.lock`. The test suite, synthetic replay, and static checks require no credentials. No npm commands are used. Secrets load from environment or the local ignored `.env`; non-secret YAML is selected with `--config`. Environment variables override YAML. ID lists use JSON, for example `[123456789]`.

## Paper mode

```bash
uv run paperfrog run paper
```

Paper uses production **public** futures and spot data, and never sends exchange orders. An empty installation collects observations and remains paused without a promoted model and point-in-time universe. It does not fabricate signals to fill a quota. Use `/resume` after all prerequisites are met. Existing exits and monitoring continue during `/stop` and risk locks. Startup with positions always reconciles and remains paused.

For shorter PAPER research, set `MIN_HISTORY_DAYS=90` in `.env` and restart the service.
The default remains 180 days; testnet and live reject values below 180. A universe
created using a shorter requirement cannot satisfy a later higher requirement through
the seven-day cache. This setting only changes universe eligibility: complete quote/OI
coverage, feature windows, model training history, promotion and entry gates still apply.

To inspect startup once rather than launch the service:

```bash
uv run paperfrog run paper --once
```

A self-contained synthetic execution demonstration:

```bash
uv run python scripts/demo_replay.py
```

This fixture deliberately constructs an episode to exercise execution, costs, exits, and reports. **Its PnL is not research evidence.**

## Collect and research

Examples below use real UTC ranges; adjust them to your required window. These are public API requests, not orders.

```bash
uv run paperfrog data backfill SOLUSDT 2026-08-01 2026-09-01 --interval 15m
uv run paperfrog data backfill SOLUSDT 2026-08-01 2026-09-01 --interval 1m
uv run paperfrog data collect
# Optional independent raw market stream recorder:
uv run paperfrog data collect --streams
uv run paperfrog data coverage SOLUSDT
uv run paperfrog data validate data/input-bars.parquet --interval-ms 900000
uv run paperfrog universe build data/universe-inputs.json 2026-09-01T00:00:00Z
uv run paperfrog features build SOLUSDT
```

Repeat backfills for BTCUSDT, ETHUSDT, and approved targets. Raw immutable batches are partitioned by kind/symbol/UTC date and queried through DuckDB. Spot and futures book observations are required for actual midpoint basis. Candle closes do not silently substitute for bid/ask midpoint history. Binance REST OI history is limited; it cannot supply an arbitrary 18-month OI archive. Existing valid archives or sustained forward collection are necessary. Missing core coverage blocks feature/model use. See [docs/DATA_CONTRACTS.md](docs/DATA_CONTRACTS.md).

The service refreshes recent closed bars/features in a separate research task. Risk monitoring runs independently of that task and LLM requests. Universe construction consumes a dated input manifest containing historical metadata and trailing liquidity measurements; selection is frozen for seven days. Rebuild using contemporaneous evidence, never current rankings applied retrospectively.

```bash
# A validated panel joins features, actual funding settlements, and PIT universe membership.
uv run paperfrog model dataset data/panel.parquet artifacts/dataset-001
uv run paperfrog model train artifacts/dataset-001/episodes.parquet 2026-09-01 candidate-001
uv run paperfrog model walk-forward artifacts/dataset-001/episodes.parquet artifacts/walkforward-001
uv run paperfrog model evaluate artifacts/walkforward-001/out_of_sample.parquet
# Join OOS predictions back to the panel by timestamp and symbol before ML replay.
uv run paperfrog backtest run data/oos-panel.parquet data/filters.json reports/replay-001 --stresses
uv run paperfrog backtest run data/panel.parquet data/filters.json reports/rules-001 --strategy rules
uv run paperfrog backtest run data/panel.parquet data/filters.json reports/cash-001 --strategy cash
```

Models use up to 6/12/18-month training windows where enough history exists, two validation months, one test month, four-hour purging, and embargoes. Validation is split again for early stopping and Platt calibration. JSON XGBoost models and numeric preprocessing avoid loading pickle code. Promotion requires a local review record, and invalidates a prior live arm:

```bash
uv run paperfrog model promote candidate-001 reports/shadow-review.json --confirm "PROMOTE MODEL"
uv run paperfrog report readiness
# Once genuine, reviewed research/operational evidence exists:
uv run paperfrog report readiness --evidence reports/evidence.json
```

Readiness exits **2** when evidence is missing or fails. An operator-authored review/evidence file is a trusted local attestation, not an automatically proven fact. See [LIVE_READINESS.md](LIVE_READINESS.md). Never copy synthetic fixture metrics into it.

## Binance testnet

Create demo/testnet credentials in the official [Binance Demo Trading environment](https://demo.binance.com/). Use a dedicated account and separate keys from live. The adapter uses the documented `https://demo-fapi.binance.com` endpoint. Configure one-way mode, single-asset USDT, isolated margin, and 2x leverage on the exact symbols. The explicit configuration command refuses an account with positions or orders:

```bash
# Put testnet credentials in the local .env, never in command arguments.
TRADING_MODE=testnet BINANCE_ENV=testnet uv run paperfrog --config config/testnet.example.yaml account configure --confirm "CONFIGURE FLAT ACCOUNT"
TRADING_MODE=testnet BINANCE_ENV=testnet uv run paperfrog --config config/testnet.example.yaml run testnet
# Explicit read-only credentialed smoke test:
PAPERFROG_TESTNET_SMOKE=1 TRADING_MODE=testnet BINANCE_ENV=testnet uv run pytest -m testnet
```

Paper, testnet and live example configurations use separate state/data paths. A data lake is bound to its originating futures API environment. Testnet prices are not evidence of a real-market edge. Testnet operation remains paused until model, data, universe, reconciliation, and risk conditions permit resumption.

## Telegram

1. Open the official verified `@BotFather`, send `/newbot`, and choose a display name such as **paperfrog** and an available bot username.
2. Put the resulting token in `TELEGRAM_BOT_TOKEN` in `.env`.
3. Stop other pollers. Send a private message to your bot, then obtain **only your numeric IDs** locally:

```bash
# Export the token securely into your shell environment first.
uv run python scripts/telegram_ids.py
```

4. Set `TELEGRAM_ALLOWED_USER_IDS=[your_user_id]` and `TELEGRAM_ALLOWED_CHAT_IDS=[your_chat_id]`. Both must match. Restart paperfrog.

Use `/help`, `/status`, `/positions`, `/orders`, `/risk`, `/last_signal`, `/daily`, `/health`, `/version`. `/stop` and `/pause` stop new entries. `/resume` and `/start_trading` can resume only the already configured mode with all gates satisfied. `/lock` creates a manual pause. `/flatten` returns a random code; submit `/confirm_flatten CODE` within 60 seconds in the same allowed chat as the same user. Codes are single-use and are not audited in plaintext.

Telegram cannot arm live mode, change leverage, raise risk, promote a model, override a hard lock, or terminate the process. Edited, forwarded, old and unapproved group commands are rejected. Poll offsets and deduplication survive restart.

## OpenAI event risk

`OPENAI_PRIMARY_MODEL=gpt-6-luna` and `OPENAI_ESCALATION_MODEL=gpt-6.1-sol` are configurable. The Responses API uses `web_search` and a strict Pydantic Structured Output schema. Escalation is limited to low confidence, conflicting/unknown reports, severe/security events, or an extreme move. Results are cached approximately 25 minutes; material vetoes persist six hours. No model substitution occurs when a configured model is unavailable.

Only public asset identifiers and an observation time leave the application. No balances, credentials, order IDs, private positions, or trading functions are sent to OpenAI. Deterministic Python applies a veto or size reduction; an LLM cannot improve a failed quantitative signal. The default unavailable policy blocks entries. Optional non-extreme fallback is capped at half size. Historical news is never invented.

## Live arming

Live is disabled by default and must remain blocked until research, operational, and implementation review gates are genuinely satisfied. The live example intentionally does not start without explicit local environment settings:

```bash
export BINANCE_ENV=live TRADING_MODE=live LIVE_TRADING_ENABLED=true I_UNDERSTAND_LIVE_RISK=YES
uv run paperfrog --config config/live.example.yaml live arm --confirm "ARM LIVE TRADING"
uv run paperfrog --config config/live.example.yaml run live
uv run paperfrog --config config/live.example.yaml live disarm
```

Arming checks a matching recent readiness report, approved model, risk settings, reconciliation, configuration/model hashes and account-key identity binding. It expires after four hours by default. No arm was created during repository implementation. A dedicated account/subaccount, futures permission only, disabled withdrawals, IP allowlisting and very small initial capital are required operator practices. The USD-M account API does not itself prove API-key withdrawal permission; verify it in Binance API management.

## Docker and operations

Docker/Compose configuration is supplied; Docker is not available in the implementation environment, so the image has not been built here.

```bash
mkdir -p state data artifacts reports logs
sudo chown -R 10001:10001 state data reports logs
# Ensure artifacts are readable by UID 10001; never place secrets there.
docker compose up -d --build
docker compose logs --tail=100 -f
# Health binds to loopback INSIDE the container; there is no published port.
docker compose exec paperfrog python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
```

For a local process, visit `http://127.0.0.1:8080/health` or `/ready` (503 unless entry-ready). SIGTERM pauses entries and retains hosted protection. Docker restarts the service; an OS advisory lock prevents two execution processes sharing state. Do not copy a running SQLite database without its WAL; use SQLite online backup.

```bash
uv run paperfrog risk status
# Queue a reduction request to the currently running owning service:
uv run paperfrog emergency flatten --confirm "FLATTEN ALL POSITIONS"
# Verify completion via /positions, /orders and the audit ledger.
uv run paperfrog account reconcile --help
```

A queued local flatten is not confirmation of closure. If the service is down, use Binance's own interface for emergency closure and then restart/reconcile. Never blindly retry an unknown exchange result. HTTP 503/timeouts can mean an order executed: the ledger queries the existing client ID and locks entries rather than creating another order.

The strategy may lose money. Leverage does not create an edge; past performance does not establish future performance. Gaps and outages may defeat intended stops. Event classification may be wrong. Jurisdiction, tax, and exchange eligibility remain the operator's responsibility.
