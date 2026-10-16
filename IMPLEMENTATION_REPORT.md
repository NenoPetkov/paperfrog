# paperfrog implementation report

## Equity margin range and removal of quote participation — 2026-10-16

The operator explicitly requested live removal of the top-price participation
rule and new 10–50% equity margin sizing, with an all-remaining-funds exception.
The opt-in `equity_range` policy targets up to 50% of total aggregate equity,
reserves estimated fees, and enforces an ordinary 10% minimum after lot rounding.
Only insufficient available funds permit a smaller entry; it must consume the
remaining fee-reserved capacity within one quantity increment. Other risk limits
cannot create undersized ordinary trades or be overridden to meet the floor.

The old participation cap is absent from this policy's sizing and risk gate.
It remains enforced in the original modes. Per-trade/open risk, portfolio margin,
gross/net exposure, leverage, position count, exchange minimums, loss locks and
protective exits remain. Current open-risk capacity reduces the new allocation
before reservation. A shared validator enforces range/fallback in planning, risk
reservation and the live gateway using independently refreshed available margin.
Short allocation uses the conservative ask-side price, and local pre-submission
sizing vetoes release only confirmed unsubmitted reservations. Every approved
allocation records all caps and its determining cap in the private audit.

The range applies to entry sizing snapshots; mark/equity changes after execution
can change displayed allocation percentages. Larger execution relative to the
best quote can increase partial/no fills or impact; existing IOC limits, hosted
protection, partial unwind and uncertain-order reconciliation remain enforced.
The policy is source/configuration bound and requires renewed local arming.
PAPER and standard risk sizing remain defaults. No profitability is established.

Verification: 485 tests passed, one credentialed testnet test skipped. Ruff lint
and formatting passed; strict mypy passed over 27 source files including risk,
execution, simple strategy, exchange and CLI. New cases cover both directions,
ordinary 10–50% allocations, fee-reserved remaining funds, disabled participation
limits, independent gateway bounds, existing risk/lock budgets, local veto release,
partial unwind and uncertain-order query-only recovery. Software checks do not
establish profitability.

## Exchange entry snapshot freshness — 2026-10-16

At the operator's request, qualifying exchange entries now refresh the selected
coin's book/mark and account margin after symbol setup and before sizing/pricing.
The existing signal, cost, spread, health and BTC gates are checked with current
inputs; exact AI/BTC intent binding and final broker checks remain. Cheap candidate
screening uses minimum costs only to decide whether to request fresh data, never
as order permission. It does not discard a candidate because an old quote has a
wide spread. Refresh failures and spread vetoes before order intent do not consume
the candle signal. Order uncertainty still preserves query-only recovery.

No additional entry threshold, price-distance veto, confirmation candle, shorter
signal lifetime or AI review is introduced. The 15-minute technical inputs and
permission boundary remain unchanged, as do sizing limits, leverage, loss locks,
cooldowns and exits. Current prices/liquidity/margin can still alter quantity or
an existing gate's outcome; identical realized trade counts cannot be guaranteed.
Historical PAPER replay remains deterministic using its supplied quote snapshots.

Verification: 450 tests passed, one credentialed testnet test skipped. Ruff lint
and formatting passed; strict mypy passed over 26 source files including risk,
execution, simple strategy, exchange and CLI. Tests cover both entry directions,
changed quotes/margin during setup, recovery from cached or unavailable quotes,
full candle eligibility, unchanged data-age limits, protective exits and duplicate
submission prevention. Live reductions use the current exchange clock so a newly
refreshed quote is not rejected using an earlier iteration timestamp. These checks
establish software behavior, not profitability or a guaranteed trade count.

Live activation exposed a pre-existing startup race: the initial `crisis=True`
flag could close an existing position before the first scan had returned. A
bounded initial-scan wait now distinguishes unknown context from an observed BTC
crisis. Stops, holding-time and financial-risk exits continue during this wait;
new entries remain paused. Missing monitoring data still exits after the existing
market-staleness timeout. Regression cases cover pending/expired initial scans,
actual BTC crisis, stop, holding-time and financial-risk exits. Deployment retained
the actual closure in trade/audit history and did not invoke risk resets.

## Neutral BTC and active AI entry review — 2026-10-16

At the operator's request, optional `SIMPLE_BTC_DIRECTION_POLICY=neutral_allowed`
accepts neutral BTC on either or both reference timeframes. Opposing direction,
missing/stale context, minute shocks, measured-pair and supported-lag checks still
block entries. Neutral minute context ignores the ordinary one-minute counter-move
veto; directional context retains it. Strict alignment remains the default.
The same policy is checked in the engine and immediately before live submission,
and the configuration/source binding requires renewed local authorization.

AI reviews now follow `SIMPLE_ENTRY_PROFILE`: active seeks conditional setups per
coin without demanding watchlist consensus or proof of profitability. Mixed longer
returns, forecast errors and small samples alone do not mandate a blanket pause.
The review receives protected trend and volatility thresholds. Explicit pauses and
unavailable, malformed or expired responses still block new entries; there is no
forced quota or fallback. Stops, sizing, leverage, account limits, cooldowns and
live gates are retained. These changes can increase turnover and do not establish
improved returns or profitability.

Verification: 432 tests passed, one credentialed testnet test skipped. Ruff lint
and formatting passed; strict mypy passed over 26 source files including risk,
execution, simple strategy, exchange and CLI. The live service was restarted
through its existing local preflight/arming path with neutral BTC permitted and
the existing active profile. Risk baselines, loss locks and cooldown history were
preserved. Software checks do not establish profitability.

## Deterministic BTC reference — 2026-10-16

The operator requested BTC be excluded from the 100-coin candidate pool and used
as an independent algorithmic input for altcoin entries. BTC is now reference-only
through scanner, selector, entry engine and simple live broker; existing reductions
remain permitted. The default-on BTC gate requires a measured positive relationship,
15-minute BTC trend/momentum alignment, and separately refreshed closed one-minute
BTC momentum. Missing, stale, neutral, contrary and short-term shock context blocks
new entries. Ranking incorporates correlation, and a fixed lag forecast is used only
when its chronological diagnostic check supports it. The OpenAI implementation,
schema and prompts are unchanged. Stops, exit management, account risk ceilings,
leverage, cooldowns and existing risk ceilings are preserved. A typed local
pre-submission veto is journaled as a zero-fill rejection and releases only its
unsubmitted reservation; uncertain exchange outcomes retain the existing lock
and query-only reconciliation behavior.

Public read-only integration verified 100 altcoin candidates excluding BTC and 120
closed one-minute BTC candles, without account requests or orders. The checked
watchlist did not support assuming a dependable 15-minute lag. That observation
does not establish behavior at other horizons or a trading edge. Tests use an
explicitly synthetic BTC-led fixture to check causality and mechanics, not profit.
Historical replay rejects this new mode rather than fabricate one-minute context.
The prepared change requires human-controlled LIVE installation/restart/arming;
the already running process and its configuration were not changed by this work.
Verification: 384 tests passed, one credentialed testnet test skipped; Ruff lint
and formatting passed; strict mypy passed over the ten-file core and sixteen-file
simple/exchange/CLI scope. These checks establish software mechanics, not profit.

## Adaptive AI entry policy — 2026-10-16

At the operator's explicit request, optional `SIMPLE_AI_ENABLED=true` grants the
configured OpenAI model authority to compose new bounded entry rules and apply
them automatically to subsequent LIVE entries. The default remains disabled and
PAPER remains the default trading mode. Each review covers one 15-minute boundary.
New conditions are schema-validated and pass immutable trend, volatility/cost,
liquidity, sizing and risk gates; they are not performance-approved or paper-tested
before automatic application. They cannot edit source, stop/trailing/holding rules,
leverage, risk ceilings or locks, and cannot directly place or close orders.

The service sends public candles/indicators, normalized closed-trade diagnostics,
and bounded persistent review history. Forecasts are scored against the next closed
candle, with missing results left unscored. No account balances, amounts, private
positions, order IDs or credentials are sent. Persisted context is not model
retraining. No improved prediction or profitability is established.

Missing/refused/malformed/expired reviews block entries without a static fallback;
exit monitoring continues independently. A live order is additionally bound to the
specific plan and exact order intent before dispatch. Durable attempt tracking limits
reviews to one per boundary and 96 per UTC day, with a 40-second request deadline,
no automatic retries and a bounded output size. Historical replay refuses AI mode
without recorded forward decisions. Local arming binds the adaptive implementation
and AI settings; Telegram cannot arm it or change risk.

Verification: 350 tests passed, one credentialed testnet test skipped. The configured
`gpt-6-luna` also produced a valid real API response for ten current symbols in about
eight seconds in a separate temporary ledger without any order tools or orders.
That response chose to pause. These checks establish software behavior and API
connectivity, not a trading edge. Earlier sections describe the original workflows.

## Active entry profile — 2026-10-14

At the operator's explicit request, `SIMPLE_ENTRY_PROFILE=active` adds a fixed,
moderately more permissive trend entry profile: eight completed 15-minute candles
for the breakout range instead of twelve, minimum EMA separation 0.4 ATR instead
of 0.5, and directional flow 0.03 instead of 0.05. Standard remains the default.
Position sizes, two-position maximum, leverage, protection, exposure budgets,
entry caps and daily/weekly/high-water loss locks are unchanged. Matching rules
are used by scanning, execution, PAPER replay, status and local authorization.
Changing a live profile requires fresh local authorization; Telegram cannot do it.
Profiles cannot be switched with open positions, and risk history is retained.

On the same already-viewed 30-day, 97-symbol pool, standard/active respectively
opened 85/96 baseline trades, 59/67 with doubled costs, and 75/82 with an extra
entry-candle delay. Net returns were -2.41%/-2.01%, -3.25%/-3.17% and
-2.73%/-2.73%. All six simulations lost money. No parameters were retuned after
the comparison. This measures retrospective frequency under idealized execution;
it is not an independent holdout, profitability evidence, or a trading quota.
The protocol and ledgers are retained in `reports/active-entry-comparison-v1`.

Verification: 215 tests passed, one credentialed testnet test skipped; Ruff lint
and format checks passed; strict mypy passed over 24 source files, including
risk, execution, simple strategy, exchange adapter and CLI. These are software
checks only. The operator requested live activation with the existing local
research waiver; the negative research results do not authorize deployment.

## Simple exchange execution update — 2026-10-14

At the operator's explicit request, the simple strategy now has an exchange execution
path and a locally recorded waiver of research approval, historical-performance gates,
paper/testnet waiting periods and legacy model promotion. PAPER remains the default.
The waiver states profitability is unproven; no passing research evidence is invented.
Legacy readiness gates and legacy arming remain unchanged.

BNFCR support uses the exchange's aggregate USD/credits margin balance without counting
underlying collateral twice. Cross Margin, Multi-Assets Mode, one-way positions and 2x
symbol leverage are verified. Credentials, configuration and strategy/execution source
bind the simple local arm. Simple PAPER, testnet and live ledgers are separate.
IOC entry, hosted initial stops, authoritative funding income, idempotent order queries,
partial-entry unwind and confirmed-fill restart recovery are implemented. ATR trailing
exits run in the process while the initial Binance stop remains hosted during outages.
Per-trade fee summaries remain estimates; account equity is read from Binance.

Verification: 207 tests passed, one credentialed testnet test skipped; Ruff lint/format
passed; strict mypy passed for the 10-file core and the 13-file simple/exchange/CLI scope.
Software checks establish mechanics only. Prolonged exchange-operation evidence and
positive strategy performance remain unproven. The original legacy verification and
limitations recorded below describe the earlier implementation, not this operator waiver.

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

## Simple profile added on 2026-10-14

The default flow is now a separate directional PAPER scanner and shared historical/forward engine. It scans up to 100 current liquid crypto futures, selects up to two from past candle liquidity/volatility/trend, trades confirmed breakouts long or short, monitors volatility stops/trailing/reversal/time/risk exits, and queues Telegram entry/close reasons. It has its own state and data directories. The losing standalone candle experiment was removed; its old research reports remain.

The new strategy is frozen deterministic research, not a trained or self-modifying AI. Its single-leg coordinator path accepts only the PAPER broker. Neither changing environment flags nor a legacy model/arm can enable its exchange execution. Profitability, genuine point-in-time universe reconstruction and real-money execution approval remain absent. It has not completed the required PAPER/testnet operating history. Historical simulations use explicitly assumed spreads/depth and fractional filters.

## Retained legacy operator reference

Set `STRATEGY_PROFILE=legacy` explicitly when using the following earlier strategy commands. The reference describes the old hedged model pipeline, not the current simple strategy.

## Legacy paper mode

```bash
uv run paperfrog run paper
```

The legacy profile (`STRATEGY_PROFILE=legacy`) uses production **public** futures and spot data, and never sends exchange orders. An empty installation collects observations and remains paused without a promoted model and point-in-time universe. It does not fabricate signals to fill a quota. Use `/resume` after all prerequisites are met. Existing exits and monitoring continue during `/stop` and risk locks. Startup with positions always reconciles and remains paused.

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

## Legacy collection and research

Examples below use real UTC ranges; adjust them to your required window. These are public API requests, not orders.

```bash
uv run paperfrog data backfill SOLUSDT 2026-08-01 2026-09-01 --interval 15m
uv run paperfrog data backfill SOLUSDT 2026-08-01 2026-09-01 --interval 1m
uv run paperfrog data collect
# Optional independent raw market stream recorder:
uv run paperfrog data collect --streams
uv run paperfrog data coverage SOLUSDT
uv run paperfrog data backfill-oi SOLUSDT 2026-08-01 2026-09-01
uv run paperfrog data assess 2026-08-01 2026-09-01 --output reports/data-readiness.json
uv run paperfrog data validate data/input-bars.parquet --interval-ms 900000
uv run paperfrog universe build data/universe-inputs.json 2026-09-01T00:00:00Z
uv run paperfrog features build SOLUSDT
```

Repeat backfills for BTCUSDT, ETHUSDT, and approved targets. Raw immutable batches are partitioned by kind/symbol/UTC date and queried through DuckDB. Spot and futures book observations are required for actual midpoint basis. Candle closes do not silently substitute for bid/ask midpoint history. Binance REST OI history is limited; it cannot supply an arbitrary 18-month OI archive. Existing valid archives or sustained forward collection are necessary. Missing core coverage blocks feature/model use. See [docs/DATA_CONTRACTS.md](docs/DATA_CONTRACTS.md).

`data backfill-oi` uses Binance's free daily USD-M metrics archives in PAPER mode,
verifies each ZIP against its published SHA-256 checksum, and retains positive finite
OI observations. Missing archives, rejected days and invalid observation counts are
reported; gaps are never filled with invented values. These production archives cannot
be imported into a demo data lake. Forward snapshot collection also retains separately
timestamped OI observations. The REST history limit does not apply to these archives.
`data assess` measures actual timestamps against the requested 15-minute grid and
checks backward OI/funding/quote availability. Exit 2 means incomplete coverage. A complete
raw coverage report does not replace feature, universe, model or live-readiness review.

The service refreshes recent closed bars/features in a separate research task. Risk monitoring runs independently of that task and LLM requests. Universe construction consumes a dated input manifest containing historical metadata and trailing liquidity measurements; selection is frozen for seven days. Rebuild using contemporaneous evidence, never current rankings applied retrospectively.

```bash
# A validated panel joins features, actual funding settlements, and PIT universe membership.
uv run paperfrog model panel data/universe-history.json data/panel.parquet
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

`model panel` consumes an array of retained dated universe snapshots (the complete
`universe build` outputs, including input evidence). It refuses missing/future membership
evidence, overlapping snapshots and unusable target features. Current membership is
never applied to older rows. The feature files for all configured symbols, including
BTC/ETH hedges, must already be prepared. Universe history and promotion review cannot
be generated by marking incomplete historical coverage healthy.

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

Telegram cannot arm live mode, change leverage or configured risk thresholds, promote a model, or terminate the process. Authenticated `/resume` and `/start_trading` explicitly reset financial locks and equity baselines after execution, health and existing local authorization checks; resets retain the previous values in the audit. Edited, forwarded, old and unapproved group commands are rejected. Poll offsets and deduplication survive restart.

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

## Reversal follow-up completed on 2026-10-14

Two fixed candidates were implemented and tested against the same retained inputs: opposite breakout signals and confirmed failed breakouts. The original 30-day baseline was reproduced before comparison. Reversed signals returned -2.40% over the recent 97-coin month and -5.89% over the two-year three-coin pool. Confirmed failed breakouts returned -0.57% and -5.66%, respectively. Both remained negative with doubled costs and delayed entries; neither qualified. Cash returned 0%. These are exploratory tests of already-viewed history, not independent out-of-sample profitability evidence.

165 software tests passed and one optional credentialed testnet test was skipped; Ruff and strict mypy checks passed. Software checks do not establish profitability. New PAPER entries are manually paused while collection/monitoring continue; existing positions are flat. Risk anchors/counters/locks are preserved. No real-money start or live arming occurred. New strategy modes were not activated in the running process. The user intends to explain the goal in a new conversation.

Retained results: `reports/simple-reversal-study-v1/summary.json` and `comparison.csv`. Handoff: `reports/reversal-handoff.json`.
# One-minute entry timing

`SIMPLE_ENTRY_TIMEFRAME=1m` adds a separately scheduled closed-minute scan and
watchlist ranking across the entire liquid pool. Entry timing requires EMA 5/20,
same-direction one/three/five-minute moves, fast-average slope, latest candle body
and taker flow. Counter-moves after the close are checked against half the minute
ATR with a freshly read quote at dispatch. Minute permissions expire at the next
boundary and bind the source/configuration, exact order and most recent minute
observation. Invalid/gapped/future/stale data and changed observations fail closed.

AI permission is reviewed every minute when `SIMPLE_AI_REVIEW_MINUTES=1`, with a
1,440-attempt UTC daily cap. The default remains 15-minute reviews and 96 attempts.
Decisions expire at the next configured review boundary, including at actual order
dispatch. Requests use at most 40 seconds and cannot outlive that permission window.
The single-attempt journal and existing usage survive restarts or a cadence change;
failed, refused, stale or late responses do not permit a static fallback. Monitoring
and the minute scan continue independently while the request is pending. Diagnostic
forecasts retain their separate 15-minute horizon. Rules evaluate minute features in the
permission window and cannot bypass the deterministic slope gate. Inputs identify
the differing feature horizons and define the sign of downward breakout distance.
The previous generated `breakout_down_atr <= 0` condition is not treated as proof
of a fresh downward breakout. AI may still veto an otherwise eligible minute entry.

Risk ATR, stop/trailing distances, BTC correlation/lag context and market-crisis
checks retain 15-minute data. Independent minute BTC direction/shock checks remain.
No size, leverage, entry-count, holding-time or loss limit increases are introduced.
Pre-existing positions retain their original trend exit, hosted protection and
holding-time checks; new entries record their own timeframe. Entry permission
failure cannot veto reduce-only protection or exits. Historical 15-minute replay
refuses this mode because it cannot reproduce these minute decisions.

Tests cover aligned slopes in both directions, rebounds despite a persistent EMA
trend, missing/duplicate/invalid/unclosed candles, minute expiry, order binding,
mid-submission price turns, fresh watchlist ranking, AI permission/forecast timing,
BTC gates, preserved stop sizing and legacy-position/reduce-only exits. Software
validation does not establish profitability. Restart and renewed local LIVE arming
are required to load a changed source/configuration.
