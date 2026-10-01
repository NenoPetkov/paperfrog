# paperfrog implementation report

Project, Python package, CLI, service, Telegram display text, and Docker service are named **paperfrog**. The repository contains an implemented research/paper system and guarded exchange integration. It is **not yet a validated production trading system**, and this report does not claim every research/operational requirement in the original brief is fully satisfied.

## Delivered

- Python 3.12 package with exactly pinned dependencies, a resolved `uv.lock`, immutable settings and a redacted configuration hash.
- SQLite WAL state, append-oriented audit records, persisted risk anchors, transactional entry reservations, deterministic order IDs, unique fill summaries, unknown-order reconciliation and exclusive service ownership.
- Native signed Binance REST adapter; public backfill/snapshots; routed WebSocket recorder and account stream; ordinary IOC orders; current Algo API mark-price protection; one-way/isolated/2x verification; maintenance-bracket and liquidation-buffer checks.
- Immutable Parquet/DuckDB lake, timestamp/coverage checks, explicit historical-data gaps, dated universe snapshots, causal features, crowding/unwind episodes, paired labels, calibrated XGBoost classifier/return regressor, logistic baseline, temporal purging/embargoes, walk-forward artifacts and explicit promotion.
- Event replay sharing production strategy, sizing, risk, paper broker and execution code. Baseline/cost/delay/missed-entry/spread/slippage stress specifications, best-symbol/month removals and bootstrap summaries; cash and deterministic-rule comparisons.
- Paper execution with bid/ask prices, depth-limited partial fills, IOC expiry, fees, funding, deterministic rejection seeds, latency, account PnL, hosted-stop simulation and outages.
- Fixed risk caps, daily/weekly/high-water locks, profit protection, cooldowns, crisis scoring, reduction/flatten paths and independent monitoring tasks.
- OpenAI Responses `web_search` plus strict factual Structured Outputs, configurable primary/escalation models, cached deterministic event policy, no private account input and no order tools.
- Allowlisted Telegram long polling, exact commands, rate limits, update deduplication, expiring user/chat-bound flatten confirmation, alerts and protected resume semantics.
- CLI, local-only health/readiness, Docker/Compose definitions, Makefile, operational/security/threat-model documentation and test fixtures.

## Verification performed

| Check | Result |
|---|---|
| `uv sync --frozen` | Passed on CPython 3.12.14 |
| `uv run pytest -q` | 103 passed, 1 skipped |
| Real XGBoost classifier/regressor training + artifact reload/integrity test | Passed; synthetic data, no profitability inference |
| Event-driven paired-trade replay + cash comparator | Passed; synthetic mechanics fixture |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check .` | Passed |
| `uv run mypy` | Passed, strict core scope: 10 source files |
| `uv run pip-audit --skip-editable` | 85 dependencies inspected, no known vulnerabilities reported |
| `uv run paperfrog config validate` | Passed; PAPER, live disabled, secrets absent |
| `uv run paperfrog run paper --once` | Passed against public Binance time/exchange information; PAUSED, reconciled, no model, no live arm |
| `uv run paperfrog report readiness` | Expected exit 2; `live_permitted=false` |
| Credentialed Binance testnet | Skipped; explicit credentials/opt-in not supplied |
| Real OpenAI and Telegram calls | Not exercised; mocked offline tests used |
| Docker image / Compose runtime | Not exercised; Docker is unavailable in this environment |

No live orders were sent, no live arm was created, and no production model was approved. The public startup smoke check did not submit orders.

## Material limitations and remaining acceptance work

1. **Research evidence is absent.** No genuine nine-month OOS report, 250-trade evaluation, 30-day paper record or seven-day testnet record exists. The strategy is unproven, not passed. Synthetic fixture PnL is not evidence.
2. **Historical inputs must be obtained honestly.** Binance's recent OI API cannot produce arbitrary long history. Actual spot/perpetual midpoint observations, historical symbol/status changes, filter validity, liquidity and funding events need complete provenance. The universe builder consumes a dated evidence manifest; it does not reconstruct historical listings automatically.
3. **Research feature scope is explicit.** The current model has 15 core inputs and additional computed diagnostics. Not every exploratory feature in the brief is implemented; the omitted families are listed in docs/FEATURES.md. Funding z is sampled from last-known rates. No historical LLM/news labels are invented.
4. **Replay remains an approximation.** Quotes determine execution and entries wait for a later event, but labels use fifteen-minute paths and cannot prove intrabar barrier-touch ordering. One filter set is accepted per replay run. Full venue queue behavior, exchange maintenance events and liquidation mechanics are not replicated. Threshold-only/funding-fade comparators and every requested reporting diagnostic are not all implemented. Use the supplied cash/rules/ML comparisons; do not claim missing ones ran.
5. **One active pair is deliberate.** The configured hard ceiling is two, but service/replay enforce one until shared BTC/ETH hedge attribution across simultaneous pairs is accepted. IOC partial pairs unwind confirmed fills rather than attempting a discretionary hedge completion.
6. **Crisis inputs need expanded acceptance.** Deterministic scoring supports the specified conditions; runtime currently wires absolute BTC moves, mark/index deviation and reconciliation mismatch. Full cross-sectional robust baselines, liquidation coverage and collateral stability feeds remain an implementation/readiness limitation. Incomplete signals must not be attested as verified live protection.
7. **Exchange integration needs actual testnet acceptance.** Read-only REST smoke is opt-in. Trading, partial fills, stop rejection/cancellation, private-stream recovery and restart behavior are mocked, not proven on an account. Fee values in REST terminal summaries are conservative estimates; actual venue fee/funding/event accounting must be reconciled during operational acceptance. Protection is a hosted mark-price close-position stop per filled leg; an independently configured second catastrophic stop is not implemented.
8. **Account binding is credential scoped.** Arms hash the dedicated API-key identity, configuration and model. They do not independently verify an exchange UID. Verify withdrawal restrictions, IP allowlisting, account eligibility and ownership in Binance management.
9. **Local state and attestations are trusted.** Readiness evidence and model shadow reviews are local operator declarations. No cryptographic claim is made that these files prove performance. An operator with write access can change source/state; host security remains essential.
10. **Long-running operational acceptance remains.** Stream sequencing, alert delivery, slippage realism, sustained no-duplicate behavior, disk growth, backup/restore and all crisis scenarios need prolonged observation. Raw stream batching can lose the final unflushed buffer on process termination. Do not treat generated deployment files as a tested deployment.

These limitations are why the explicit `implementation_reviewed`, data/leakage, research and operational gates must remain false until substantiated. The repository implements fail-closed startup/arming rather than claiming incomplete evidence permits live deployment.

## Commands

Setup: `uv sync --frozen`, `cp .env.example .env`, `chmod 600 .env`.

Paper: `uv run paperfrog run paper`.

Testnet: `TRADING_MODE=testnet BINANCE_ENV=testnet uv run paperfrog --config config/testnet.example.yaml run testnet`.

Tests: `uv run pytest`; lint: `uv run ruff check .`; formatting: `uv run ruff format --check .`; strict core types: `uv run mypy`.

Telegram: create a bot with official BotFather, set its token locally, discover numeric private-chat IDs with `scripts/telegram_ids.py`, then configure both JSON allowlists. OpenAI uses `gpt-6-luna` for candidate-specific factual checks and `gpt-6.1-sol` for specified escalations. Neither can trade.

See [README.md](README.md) for complete research, promotion, readiness, testnet, local arming and Docker commands. [docs/API_VERIFICATION.md](docs/API_VERIFICATION.md) links the official references and documents the Algo-stop and routed-WebSocket adaptations.
