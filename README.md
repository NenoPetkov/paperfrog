# paperfrog

## One- and five-minute slope entries

Set `SIMPLE_ENTRY_TIMEFRAME=5m` and `SIMPLE_AI_REVIEW_MINUTES=5` for five-minute
entry candles and AI reviews. The simple trend profile fetches 120 completed
five-minute candles and reranks the pool at each five-minute boundary. EMA 5/20
uses five-minute bars, with aligned 5/15/25-minute returns and trading flow.
The hourly return uses 12 bars; breakout and range windows cover 25 and 120 minutes.
Signals expire at the next five-minute boundary, and the execution quote must
still pass the fresh-price and rebound checks. Fifteen-minute risk context and
the independent BTC minute gate remain in place.

Five-minute AI reviews allow at most 288 attempts per UTC day and require `1m`
or `5m` entry candles. Five-minute entries also support 15-minute AI reviews;
one-minute reviews require `1m` entries. AI reviews run only when AI is enabled.

Set `SIMPLE_ENTRY_TIMEFRAME=1m` for closed one-minute entry signals. The simple
trend profile reranks the entire liquid altcoin pool every minute, using EMA 5/20,
aligned one/three/five-minute returns, the latest candle body and trading flow.
Shorts are blocked during an upward turn; longs are blocked during a downward
turn. A fresh execution quote also blocks a counter-move greater than half the
one-minute ATR after the signal close. Signals expire at the next minute boundary.
These checks run again independently in the live broker before submission and
are bound to the exact order. Missing, incomplete, stale or contrary inputs block entries.

`SIMPLE_AI_REVIEW_MINUTES=1` selects an AI review for each completed minute, with
at most 1,440 attempts per UTC day. This requires one-minute entry candles. The
default review interval remains 15 minutes with 96 attempts per day. Each decision
expires at the next review boundary; attempts and usage survive restarts. Reviews
compose generic conditions over the refreshed minute
features; a pause or unavailable review still blocks new entries. The minute loop
continues while an AI request is pending. Forecasts cover the reviewed sample;
validated generic rules apply to the changing minute watchlist. AI input explicitly
labels minute trend/flow features and longer-horizon context. Diagnostic forecasts
retain their separate 15-minute horizon and are not entry permission.

Fifteen-minute candles still supply the existing ATR stop sizing, trailing distance,
24-hour liquidity and BTC correlation/lag context. The independent BTC minute gate,
position sizes, leverage, holding-time limits, daily entry caps and loss limits remain.
Existing positions keep their original trend exit; new minute positions use minute
trend exits. Stops only tighten, and reduce-only exits do not need minute entry permission.
The legacy default setting is `15m`; `.env.example` selects `1m` for new installations.
`simple status` exposes configured and loaded entry timeframes. A running process
needs a restart, and LIVE source/configuration changes need renewed local authorization.
The old 15-minute replay refuses minute mode instead of inventing minute signals.
Faster signals can produce more noise and do not establish a profitable strategy.

## Equity margin allocation: 10–50%

`SIMPLE_SIZING_MODE=equity_range` with `SIMPLE_POSITION_MARGIN_CAP=0.5`
selects the largest permitted entry allocation up to 50% of aggregate account
equity as margin at the existing 2x leverage. Ordinary allocations must be at
least 10% of equity after quantity rounding. If available margin cannot fund
10% after estimated entry fees, the sole exception uses all remaining available
margin after fees and exchange lot rounding. If another risk/exposure limit
would force an ordinary allocation below 10%, or prevent full use of the small
remaining balance, the entry is skipped. Exchange minimums still apply.

This policy removes the top-quote participation cap from both sizing and risk
reservation. Fresh spread/data, order-price limits, per-trade/open-risk budgets,
portfolio exposure, loss locks, position count and protective exits remain.
Sizing also considers remaining open-risk capacity before reserving a new entry.
The live gateway independently checks the allocation against fresh account data.
Short quantities use a conservative ask-side allocation price. Entry fees and
lot rounding normally leave allocations slightly below the 50% target.

Percentages describe entry allocations using current equity; market value and
equity changes can change the displayed percentage after a fill. Each approved
`simple_equity_allocation` audit records equity, available margin, planned margin
fraction, all sizing caps, the limiting cap and any remaining-balance exception.
PAPER and the original risk sizing remain defaults; other sizing policies retain
their existing participation checks. Live policy changes require fresh local
authorization and restart; Telegram cannot select this policy or arm it.

## Optional larger-position configuration

All conversation changes are integrated into the main `src/paperfrog` package,
including execution-price validation/requery, restart journal checks, the ten-coin
watchlist option, margin sizing, configurable loss cooldown and Telegram events.
The older copies under `state/prepared` are historical artifacts, not the active
source or required patches.

`larger-positions.yaml` combines the requested ten-coin active watchlist,
five-minute consecutive-loss cooldown and larger sizing in one opt-in PAPER preset.
It targets margin of up to 80%
of current aggregate equity per position, with a 100% portfolio margin ceiling at
2x leverage (160% and 200% notional respectively). For credits accounts the base
is the exchange's USD/BNFCR aggregate equity, including unrealized PnL, not a sum
of collateral balances or 80% of the remaining free margin on each entry.

Live sizing additionally checks available margin and reserves estimated entry fees.
Stops, liquidity, remaining portfolio capacity and planned-risk caps can reduce the
size. This preset explicitly raises planned risk to at most 4% per trade and 5%
across open trades. It also opts into 10x account thresholds: 12.5% daily loss,
35% weekly loss and 60% high-water drawdown, with `profit_guard_scale: 10`.
These are much larger loss allowances; they do not guarantee a maximum realized
loss or prevent liquidation. Default account limits remain 1.25%, 3.5% and 6%.

The default two-position limit and LONG/SHORT signals remain. The combined preset
sets an eight-hour maximum hold; `SIMPLE_MAX_HOLD_HOURS` defaults to four hours and
accepts 1–24. Stops, trend reversal and risk exits can close positions earlier.
The default sizing mode and default limits remain unchanged. No existing
live environment configuration or authorization is updated by this preset.

`soft_loss_cooldown_minutes` controls future consecutive-loss locks. The default
is 720 minutes; both `five-minute-cooldown.yaml` and the combined preset select five minutes. Existing lock
expiry timestamps and loss history are preserved until an explicit `/resume` reset.

No build is required. A running Python process keeps its loaded code and settings.
`simple status` reports `configured_settings` and `loaded_settings` separately;
the latter is the last startup snapshot (null for older processes), not proof that
the process is still alive. A restart is required to load changes, and LIVE source
or configuration changes require renewed local authorization. The combined YAML
is PAPER-only by default and does not modify `.env` or existing lock expiries.

Telegram sends friendly startup/running messages, successful buys and sells, closed
PnL, and actual status changes with their reasons. Routine skipped candidates
(including sizes below the exchange minimum) stay in the local audit instead of
generating per-coin Telegram alerts. `/status` shows only status and open positions.
Loss cooldowns explain when automatic recovery is possible; `/resume` can reset them early.
The first subscription sends a current-state snapshot instead of replaying old
history. Each chat has a durable delivery cursor; failed sends retry.
A crash after Telegram accepts a message but before the cursor is saved can cause
a duplicate. Graceful shutdown attempts a bounded final delivery; queued messages
survive a restart, but an offline process cannot send immediate alerts.

paperfrog is a Python 3.12 futures research bot. Its default simple profile scans recent Binance USDT perpetual data, ranks up to 100 liquid crypto coins, selects up to two, chooses long/short from trend breakouts and trading flow, and monitors exits. Cash and zero trades are valid outcomes.

**Default: PAPER. Trading edge: unproven.** The simple profile supports locally authorized exchange execution with an explicit operator waiver of research approval and waiting periods. Legacy model readiness remains separate. Tests verify software mechanics, not profitability. Read [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) for delivered scope and material limitations before operating it.

## BTC reference and altcoin-only selection

The simple profile excludes `BTCUSDT` before filling its pool of up to 100
altcoins. BTC is still fetched separately, including when it is in the denylist,
and retained for the existing market-crisis checks. Selection, the entry engine
and the simple exchange broker all refuse new BTC positions. Existing BTC
positions can still be protected and reduced.

`SIMPLE_BTC_REFERENCE_ENABLED` defaults to true. The deterministic entry algorithm
requires agreement between BTC's 15-minute trend/15-minute and one-hour momentum
and a separately refreshed one-minute BTC reference. The latter reads 120 closed
one-minute futures candles every minute, compares EMA 5/20, and checks one/five/
15-minute moves. The default `SIMPLE_BTC_DIRECTION_POLICY=strict` blocks entries
on neutral, contrary, missing, stale or shock context. The optional
`SIMPLE_BTC_DIRECTION_POLICY=neutral_allowed` accepts neutral BTC on either or
both timeframes. An opposing direction on either timeframe still blocks, as do
missing/stale context and short-term shocks. For a neutral minute reference,
ordinary one-minute counter-moves do not add a direction veto; shock limits remain.
Correlation and supported lag checks are unchanged.
Checks run on the existing monitoring loop and again immediately
before dispatch. Existing exits, sizing, leverage, risk limits and locks are unchanged.

Each altcoin's contemporaneous correlation and beta to BTC are estimated from 192
past 15-minute return pairs. A positive correlation of at least 0.25 and beta
between zero and three are required. Candidate scores additionally weight this
measured BTC relationship. A separate, fixed one-bar lag model estimates the next
altcoin 15-minute return from the most recent BTC return. Its first 128 samples
are checked against the following 64: it needs positive bounded coefficients,
lag correlation >= 0.10, and at least 1% lower squared error than a historical-mean
baseline before its estimate can influence ranking or veto an opposing entry.
Otherwise there is no claimed predictive lag; the mandatory BTC direction checks
and the altcoin's own entry rules still apply. No search over delays is performed.

This logic has no OpenAI dependency and adds no BTC features to the AI schema or
prompt. AI-generated entries cannot bypass it. One-minute candles do not establish
or target a seconds-long lead, and statistical fit is not evidence of profitability.
`/simple` and `simple status` expose BTC context separately from AI status. Minute
reference snapshots are audited; closed-bar pair diagnostics are retained locally.

The current historical replay refuses reference-enabled settings because it does
not reproduce the one-minute context. Explicitly disabling the reference gate can
evaluate the legacy entry baseline, with BTC still excluded from trading; that
result is not evidence for this new policy. No historical BTC minute decisions are
fabricated. Applying the change to LIVE requires a restart and fresh local arming;
the AI cannot disable this gate or create that authorization.

## Optional adaptive AI entry rules

`SIMPLE_AI_ENABLED=true` enables one OpenAI review per completed review boundary
(15 minutes by default, or every minute with `SIMPLE_AI_REVIEW_MINUTES=1`),
using the configured `OPENAI_PRIMARY_MODEL` and a private local `OPENAI_API_KEY`.
It defaults to false; PAPER remains the default execution mode. LIVE still requires
all existing environment conditions and a local arm bound to the account, source
and configuration. Changing the AI model or enabling AI requires fresh local arming.

Each review receives public market summaries for up to ten selected coins, their
latest 32 closed candles and eleven numeric indicators, up to two prior reviews
with scored forecasts, and up to 50 closed trades expressed as approximate net
returns. No balances, quantities, private open positions, order IDs or credentials
are sent. Price history used to calculate indicators remains three days. Forecasts
are scored only at their specified 15-minute target; missing observations are left
unscored. Close-to-close forecast scores are diagnostics, not executable PnL.
Matured scores from up to 128 stored reviews also supply mean absolute error
against a no-price-change baseline, with an explicit warning about overlapping
forecasts and small samples. The prompt asks for current conflicting evidence,
invalidation conditions and concise explanations; it discourages needless rule changes.

The model can compose new AND/OR entry conditions over trend, flow, volatility,
one/four/24-hour returns, RSI, volume ratio, breakout distance and range position,
or pause entries. With `SIMPLE_ENTRY_PROFILE=active`, the review seeks conditional
setups per coin instead of requiring agreement across the watchlist. Mixed longer
returns, forecast errors, a small trade sample or lack of proven profitability
alone are not reasons to pause every coin. The model receives the configured
trend/volatility entry thresholds and may still pause when no defensible setup
remains. Standard retains its cautious review policy. Neither profile forces an
order or substitutes a static signal for an AI pause or failure.
These are validated data, never Python or shell code. Long/short
trend alignment, volatility/cost gates, position sizing, stops, trailing, holding
limit, leverage, liquidity, daily entry caps, cooldowns and loss limits remain
independent and unchanged. The AI cannot modify risk settings or manage existing
positions. Its rules can replace the original breakout/flow entry trigger, so the
adaptive strategy has different, unproven trading behavior even with fixed limits.

Permissions expire at the next configured review boundary. Missing, refused, malformed,
out-of-scope or late output blocks new entries; there is no static-strategy fallback.
Monitoring and exits run independently while the request is pending. A live order
also needs permission bound to its exact intent immediately before submission.
There is at most one request attempt per boundary and 96 attempts per UTC day
for 15-minute reviews, or 1,440 for explicitly selected one-minute reviews,
durable across restarts, with no automatic request retries. Each request has a
deadline of 40 seconds or the remaining permission window, whichever is shorter,
and `SIMPLE_AI_OUTPUT_LIMIT` (default 4000, maximum 8192 tokens).
These bounds constrain API usage, not a fixed monetary bill.

Reviews skip API calls when a manual/risk lock is present or all configured
position slots are occupied. A skipped boundary clears prior entry permission,
uses no API request budget and waits for the next boundary before reconsidering;
exits continue independently. Freeing a slot does not revive an expired review.
GPT-6 requests use explicit-only caching at the stable instruction boundary,
avoiding cache writes for the changing market snapshot. Other models retain
their existing cache behavior. Input JSON is compact; candle values use eight
significant digits for presentation while execution indicators retain full precision.

The `simple_ai_usage` audit event and `/simple` diagnostics `adaptive_ai.last_usage`
record numeric input, cache-read, cache-write, output and reasoning token usage
when returned by the SDK, plus request character count. Missing usage stays missing;
SDK errors before a parsed response may still incur charges that are not captured.
Reasoning tokens are part of output usage, and cached/written tokens are subsets
of input usage; do not add them again. These counters are not a billing estimate.
Actual savings and any change in decision quality require forward measurement.

During restart, trend exits use only candles matching the position's original
entry timeframe. A completed 15-minute risk scan cannot substitute for missing
one- or five-minute trend data. Existing startup grace and unavailable-data exits
still apply, and stops, holding limits, portfolio limits and crisis exits remain active.

`/simple` and `simple status` expose AI status and the most recent review. Reviews,
forecasts and trade-to-rule attribution are journaled. This is persistent feedback
in the model's context, **not automatic model training**, and neither it nor passing
software tests establishes improved prediction or profitability. New live entry
rules can lose money; a few trades cannot establish an edge.
Historical backtests refuse AI-enabled settings without recorded forward decisions;
the bot does not fabricate historical AI advice or present an empty replay as evidence.

## Trade journal and daily analysis

New simple-profile entries retain a versioned journal with the entry signal reason,
strategy/rules and configuration hash, closed-candle indicators, quote/spread,
execution mode and timeframe. Closed records add entry/exit fill prices, holding
time, gross and net PnL, both fees, signed funding, and observed favorable/adverse
excursions. Entry context and quote references survive execution recovery. Older
records remain usable; unavailable historical context is reported as unknown.

The service refreshes local daily reports every minute, including historical days
with closed trades, yesterday, and the current partial day:

```text
reports/simple-paper/daily/YYYY-MM-DD.json
reports/simple-paper/daily/YYYY-MM-DD.txt
```

The directory uses `simple-live` or `simple-testnet` for those execution modes,
under the configured `reports_dir`. Days use **UTC exit time**, consistent with the
bot's ledger. All lifetime costs belong to the day a trade closes; open positions
are excluded. Reports compare winners, losers and breakeven trades, including
average entry indicators, holding times, excursions and net returns. Breakdowns
cover symbol, direction, strategy, exit reason, timeframe, volume ratio, AI rule,
fee basis and currency. Every metric includes its available sample count.
`/simple` diagnostics expose the report directory and generation status.

Generate a report locally without exchange access (using the same configuration
as the intended bot instance):

```bash
uv run --python 3.12 paperfrog simple analysis
uv run --python 3.12 paperfrog simple analysis --day 2026-10-15
uv run --python 3.12 paperfrog simple analysis --day 2026-10-15 --json-output --output reports/trade-review
```

The default is yesterday UTC. `--output` writes both JSON and readable text;
without it the command only prints the report. Missing journals and future dates
are rejected. Automatic reports are local only and do not send Telegram messages,
alter the AI's inputs, change entry rules, or mutate risk controls. Optional report
generation failures are recorded by exception class and retried next minute.

Net PnL is fill-price gross PnL minus fees plus signed funding. Spread and slippage
diagnostics are already embedded in fill prices and **must not be deducted again**.
Slippage uses the pre-order bid/ask as its reference; a recovered exchange-hosted
stop has no reliable contemporaneous reference, so its slippage stays unknown.
Live/testnet fees remain estimates from the configured fee rate, not reconciled
commissions. Funding includes recorded income; late exchange postings can be
missing. Excursions use sampled marks and fills, so downtime and between-poll
extremes are not captured. Monetary comparisons require consistent account units.
Small samples, feature differences and passing software tests do not establish
causation, improved prediction or profitability.

### Scheduled AI review and actionable proposals

The separate daily reviewer sends Telegram around **10:00 Europe/Sofia**, starting
18 October 2026 on this host. It reviews the previous complete local calendar day
and compares it with the seven days preceding that day. Daylight-saving changes
are handled by the timezone. Only closed trades are included. This schedule is
independent of the entry-review interval and the UTC reports above.

`scripts/daily_review.cron` is installed as `/etc/cron.d/paperfrog-daily-review`.
Cron checks every minute; Python gates the local delivery time and persists report
and delivery state. It makes at most one AI attempt per report day, with a 4,000
output-token ceiling, and skips AI on days with no closed trades. Normalized
metrics go to the model; exact monetary totals are calculated locally. An AI
failure produces a deterministic summary without proposals. A restart catches up
the latest due day without sending a backlog of historical reports.

Reports and up to two evidence-backed proposals are saved under
`reports/simple-<mode>/daily-ai/`, with proposals in `proposals/DR-YYYYMMDD-NN.json`.
Each proposal contains the observed pattern, evidence references, an exact change
specification, target files and source/configuration hashes, acceptance criteria,
validation steps and rollback. Ask the coding assistant **Apply DR-YYYYMMDD-NN**
to inspect and implement a saved proposal. This is not a Telegram trading command;
proposals never execute automatically. Current code and evidence must be reviewed
before implementation, with existing PAPER defaults, risk limits and live gates
preserved. Small samples can justify collecting evidence rather than changing rules.

```bash
uv run --no-sync python -m paperfrog.simple.daily_review --show-proposal DR-20261017-01
uv run --no-sync python -m paperfrog.simple.daily_review --preview-day 2026-10-16 --timezone Europe/Sofia
```

Preview mode writes to a separate `daily-ai-preview` folder and never sends Telegram.
The worker opens the trading ledger read-only and keeps its own state in
`state/simple-<mode>/daily-review/reviews.sqlite` (`state/simple/` for PAPER).
Telegram delivery uses the configured allowed chats, retains per-message
acknowledgements, and retries failed parts at most three times, five minutes apart.
An acknowledgement lost after Telegram accepts a message can cause a duplicate.
Local reports remain available if delivery fails. Logs are in `logs/daily-review.log`.

Running services need to load the updated code to collect the richer journal.
Existing source-bound live authorization must be renewed locally after an update;
the reporting command itself does not authorize trading.

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

## Simple bot (default)

```bash
uv run paperfrog run paper
uv run paperfrog simple status
curl http://127.0.0.1:8081/simple
```

Set `STRATEGY_PROFILE=simple`, `SIMPLE_SCAN_LIMIT=100`,
`SIMPLE_SELECTED_COUNT=2` and `SIMPLE_POLL_SECONDS=5` in `.env`.
The PAPER profile uses `state/simple` and `data/simple`; its health server uses the
configured `HEALTH_PORT` (8081 in this installation). Stop the old service before
starting it: only one process may poll the same Telegram bot.

Every 15 minutes, scan current liquid USDT perpetual crypto contracts and read
three days of closed futures candles. Exclude stale, incomplete, illiquid or extreme
volatility inputs. Rank recent trend strength and liquidity; a selection is a watchlist,
not an instruction to trade. Enter only after a three-hour breakout, aligned EMA trend
and confirming taker flow. Stop distance uses recent ATR, bounded to 0.6–2%.
Monitor every five seconds; exit on protective/trailing stop, trend reversal, four-hour
expiry, unavailable candle monitoring, BTC crisis, portfolio risk lock or manual flatten.
Stops only tighten. Existing per-trade, exposure, daily/weekly/drawdown, cooldown and
entry-count limits apply. Total direct exposure is additionally bounded by the existing
net-beta notional budget, without assuming two opposite coin positions offset risk.

For an eligible exchange entry, finish symbol setup first, then refresh that coin's
book/mark and account margin together before calculating quantity and limit price.
The actual spread and costs are checked using that new quote. Cached wide or stale
quotes do not discard a potential setup before refresh. A refresh failure before
submission leaves the signal available for a later monitoring pass; uncertain order
outcomes still use query-only reconciliation. Only eligible, unconsumed signals
perform the extra refresh. The 15-minute indicators, AI cadence, signal lifetime,
entry thresholds, liquidity/risk budgets and exits are unchanged. No extra
confirmation candle or price-distance filter is added. Fresher prices can still
change sizing or the result of an existing execution check. Offline PAPER replay
continues to use its supplied historical quotes.

After restart, existing positions retain stop, holding-time and financial-risk
monitoring while the first candle/BTC scan loads. New entries stay paused. A
pending first scan is not a confirmed BTC crisis. This initial wait is bounded by
`MARKET_STALE_SECONDS`; unavailable monitoring data then uses the existing exit.

Telegram reports filled buys/sells with direction, price and size, plus closed
PnL with estimated fees and recorded funding. `/status`, `/positions`, `/last_signal`, `/pause`, `/resume`
and confirmed `/flatten` work with the simple profile. Notification retries run separately
from monitoring. `/pause` pauses entries while exits continue. Restart with open PAPER
positions restores protection and stays paused until explicit `/resume`.

`/resume` (also `/start_trading`) explicitly resets financial locks, daily and weekly
equity baselines, the high-water mark, profit protection, consecutive-loss counts,
symbol cooldowns and manual pauses using fresh current equity. Previous baselines,
locks and counters are retained in an audit record; trade and order history remains
intact. This permits further losses after a reset. Automatic recovery does not reset
these baselines or override an unexpired lock. Reconciliation, protective stops,
fresh data, crisis checks, pending emergency closure and existing local live
authorization still have to pass. Position, entry-count and exposure caps still apply.

Operator resume refreshes the selected/held contracts' book and mark quotes after
execution recovery and then refreshes account equity. A cold quote cache or quotes
that expired during reconciliation no longer require waiting for the polling loop.
Normal resume rechecks candle readiness after those network calls; its refusal now
distinguishes missing candidates, a stale candle scan, crisis/reference readiness,
market quotes and account data. Genuine stale or invalid exchange responses still
block ordinary resume.

`/refuse_force` is an explicit, audited **resume-time** market-check override for the
simple profile. It resets the same financial locks as `/resume` even when the scan
is empty/expired, the crisis flag is active, or market quotes cannot be refreshed.
It does not arm LIVE, change risk limits, fabricate a quote or submit an order.
Reconciliation, hosted protection, fresh positive account equity and the pending
emergency-close gate remain mandatory. This is a one-time resume acknowledgement,
not a persistent override of trading rules: entry/exit logic, fresh executable
quotes, AI permission, BTC gates and crisis handling continue normally. The bot
may therefore be RUNNING while waiting for a valid trading opportunity. `/pause`
or `/stop` pauses entries as usual. The command accepts no arguments and uses the
same Telegram allowlist, replay protection and rate limit as other commands.

The optional account settings accept `DAILY_LOSS_LIMIT=0.125`,
`WEEKLY_LOSS_LIMIT=0.35`, `HIGH_WATER_DRAWDOWN=0.60` and
`PROFIT_GUARD_SCALE=10`. At scale 10, profit protection starts at a 7.5% daily peak
gain, the minimum giveback is 3.5% of starting equity and the profit entry cap is
12.5%; the relative giveback remains 35% of peak profit. Settings changes require
a restart and renewed local LIVE authorization. Telegram cannot change thresholds
or create that authorization.

Default limits are 12 entries total and two per coin per UTC day, with at most two
concurrent positions (configurable up to four). Selection may change during the day. The bot does not force a 5–20-trade
quota. With adaptive AI disabled, these are frozen deterministic rules: no automatic
ML retraining, no changes triggered by individual losses, and no promised profitability.

`MAX_STRATEGY_ENTRIES_PER_DAY` defaults to 12 and accepts 0–40. Set it to 40 for
up to 40 entries per UTC day across all symbols. Reserved entries count even if
aborted, and the configured per-symbol limit still applies. Zero blocks all new
entries. Changing this setting requires a restart and renewed local LIVE authorization.

`MAX_ENTRIES_PER_SYMBOL_PER_DAY` defaults to 2 and accepts 1–4. Set it to 4 to
permit up to four entries per coin per UTC day. Counts survive restarts and reset
for the next UTC day. The total daily entry cap, concurrent-position cap, cooldowns
and other risk limits still apply. Changing this setting also requires a restart
and renewed local LIVE authorization.

`SIMPLE_ENTRY_PROFILE=standard` preserves the original three-hour breakout rules.
The optional `active` profile uses a two-hour closed-candle breakout, minimum EMA
separation of 0.4 ATR (standard: 0.5), and minimum directional flow of 0.03
(standard: 0.05). It requires `SIMPLE_ENTRY_MODE=trend`. Both profiles use the same
15-minute signals, five-second monitoring, configurable watchlist and position limit,
position sizing, 2x leverage, stops, exposure caps, entry caps and loss locks.
Active can produce more eligible setups; additional trades and profitability are
not guaranteed. Higher turnover can increase fees and losses.

`SIMPLE_SELECTED_COUNT` controls the watchlist independently of the position limit.
It defaults to 2 and accepts 1–10. Set it to 10 to evaluate up to ten eligible coins
from the scanned pool each cycle. Fewer may qualify for the watchlist.
`MAX_CONCURRENT_STRATEGY_TRADES` defaults to 2 and accepts 1–4. Set it to 4 to
permit up to four simultaneous positions, subject to the same sizing, total open
risk, exposure, cooldown, entry-count and loss limits. A running process keeps its
loaded settings; changing either value requires a restart and, for LIVE, renewed
local authorization.

The profile is shown by `/simple` and `simple status`, and PAPER backtests
use its matching indicator and execution rules. Switch only while flat: changing
profiles with open positions is refused. A live profile/configuration or source change
invalidates the existing local arm; stop the service and complete local authorization
before restarting. Telegram cannot switch profiles or create that authorization.

### Simple exchange execution and BNFCR

The production runtime is hosted at `root@176.31.163.20:/var/www/paperfrog`.
After committing changes on local `master`, deploy with:

```bash
./depoy.sh
./depoy.sh --status
```

`depoy.sh` pushes committed changes and connects over SSH. It refuses a dirty
checkout or a still-running local bot/review. On the server it checks the exact
commit in an isolated worktree (pytest excluding external testnet tests, Ruff,
strict mypy and systemd units), then stops both runtime writers and verifies a
private SQLite/state backup under `/var/backups/paperfrog`. Only a fast-forward
Git update is accepted. Runtime `.env`, databases, market history, reports and
logs stay on the server and are never uploaded from the stale local copy.

The script preserves the current execution mode. LIVE rearming uses the existing
CLI account preflight and requires a previously valid arm plus unchanged account
and configuration. A mode/configuration change requires a separate deliberate
operator workflow; deployments never clear risk/manual locks or increase limits.
Systemd restarts do not rearm or clear locks. PAPER remains the application default.
If a deployment fails after shutdown, it does not restore an old trading ledger
or automatically undo exchange activity. Its verified backup remains available
for investigation. Check the service before attempting another start.

```bash
ssh root@176.31.163.20 'systemctl status paperfrog --no-pager'
ssh root@176.31.163.20 'journalctl -u paperfrog -n 60 --no-pager'
```

`paperfrog.service` starts on boot and restarts after process failures. The
`paperfrog-daily-review.timer` replaces the local cron job, preserving the 10:00
Europe/Sofia review schedule and its persisted delivery ledger. After migration,
the original local state stays as an archive; its LIVE arm is removed and
`state/MIGRATED_TO_OVH` marks it inactive. Do not run a second bot or review against
the same account/Telegram token. Initial migration retains a full runtime archive;
routine deploy backups contain state, `.env`, dependency lockfiles and row-count
verification, while historical data remains in the live runtime directory.

The simple profile can run the same current rules on Binance production or testnet.
For a Futures Credits account, set `FUTURES_ACCOUNT_MODE=credits` and
`MARGIN_TYPE=CROSSED`. Binance requires Cross Margin and Multi-Assets Mode for credits.
The adapter uses `totalMarginBalance` as USD/BNFCR equity once; it does not add BNFCR
and the underlying collateral balances together. Contracts remain USDT perpetuals.
Selected symbols are checked for the expected margin mode and prepared at 2x leverage
while flat. Existing risk sizing and entry-count limits remain in force.

Production execution also requires `TRADING_MODE=live`, `BINANCE_ENV=live`,
`LIVE_TRADING_ENABLED=true`, `I_UNDERSTAND_LIVE_RISK=YES`, and
`SIMPLE_LIVE_RESEARCH_WAIVER=true`, with private local API credentials.
The waiver records unproven profitability; it does not fabricate a passing readiness report.
The simple workflow does not require historical performance approval, paper/testnet waiting
periods, a promoted legacy model, or a news/LLM decision. PAPER remains the default.

```bash
uv run paperfrog live arm --confirm "ARM LIVE TRADING"
uv run paperfrog run live
uv run paperfrog simple status
# Pause entries, retaining hosted protection and exit monitoring:
uv run paperfrog live disarm
# Request the running process to close its positions:
uv run paperfrog emergency flatten --confirm "FLATTEN ALL POSITIONS"
```

The local arm binds the account, configuration and strategy code; changing them or
deleting the record disables entries. It does not expire automatically in the waived simple
workflow. Telegram cannot create an arm record or raise risk. Exchange and PAPER ledgers
are separate (`state/simple-live`, `state/simple-testnet`, `state/simple`). Unknown
positions/orders, stale data, uncertain submissions and incomplete journals block entries.

After each confirmed IOC entry, install and verify a Binance-hosted MARK_PRICE
STOP_MARKET close-all order. Partial entries and rejected protection unwind confirmed fills.
Ambiguous orders are queried rather than submitted again. The hosted initial stop stays
active throughout the trade; tighter ATR trailing exits run in the monitoring process.
If the process is offline, the initial stop remains available, while software trailing exits
wait for recovery. Confirmed hosted stop executions are reconciled after restart.
Account equity includes actual exchange costs and funding; per-trade PnL notifications
currently use configured fee estimates and authoritative funding income.

Stop verification journals the exchange algo ID and retries a temporarily missing
lookup at most twice, within the existing protection deadline. It never resubmits
the stop or entry. The identifiers follow Binance's [algo order API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#query-algo-order).
Signed timestamp failures resync and retry reads only. Market scans offload disk and
indicator work so monitoring can continue, and snapshot ages use the current
exchange clock. A failed entry can recover automatically after its unwind, position
reconciliation and protection cleanup are verified, with fresh data, local LIVE
authorization and all existing risk gates still required. Unknown order outcomes,
incomplete journals and unverified protection continue to block entries.

### Historical tests

Use existing BTC/ETH/SOL history (two-day warm-up is required before START):

```bash
uv run paperfrog simple backtest 2024-10-15 2026-10-13 reports/simple-study-new
```

To prepare a larger pool using free futures candles and actual funding settlements:

```bash
uv run paperfrog simple collect --days 30
# Use the UTC start/end and usable_symbols printed by collect:
uv run paperfrog simple backtest START END reports/simple-pool-study-new \
  --lake data/simple --symbols BTCUSDT,ETHUSDT,SOLUSDT,OTHERUSDT
```

The pool snapshot records today's eligibility and collection failures. A larger historical
replay requires every requested symbol to have complete candles, two-day warm-up and
funding coverage. It ranks symbols using only information available at each timestamp,
but today's pool has survivorship limitations: historical delistings and membership are
not reconstructed. Retrospective results remain exploratory.

Reports retain frozen rules/input hashes, selections, trades, equity, cash comparison,
doubled costs and delayed entries. The replay shares the PAPER engine's decisions and
risk ledger, but assumes fractional historical filters, 5bp half-spread and volume-derived
depth. Trailing/monitor updates occur on 15-minute boundaries; stops use adverse candle
extremes. These are explicit simulation assumptions, not historical executable quotes.

### First real-data results (2026-10-14)

| Exploratory sample | Baseline return | Trades | Doubled costs | Extra entry delay |
| --- | ---: | ---: | ---: | ---: |
| Two-year BTC/ETH/SOL pool | -5.94% | 185 | -6.03% | -6.02% |
| Recent 30-day, 97-coin pool | -2.41% | 85 | -3.25% | -2.73% |

Cash returned 0% in both comparisons. The two-year baseline hit its 6% high-water
risk lock and stayed locked; the replay did not reset losses to continue trading.
These results do **not** support real-money deployment. Reports and data provenance
are in `reports/simple-trend-v1-verified`, `reports/simple-pool-30d` and
`reports/simple-delivery.json`. No parameter search was used to turn these losses into
a positive-looking result. Binance clock synchronization was corrected in the forward
scanner/monitor after evaluation; historical decision, sizing, risk and replay code stayed
unchanged.

### Reversal candidates

`SIMPLE_ENTRY_MODE` accepts `trend`, `reverse` or `failed_breakout`.
The `reverse` candidate takes the opposite side of the same eligible breakout and
mirrors trend invalidation, while stops and trailing protection follow its actual position.
The `failed_breakout` candidate waits for the next closed candle to return through the
breakout level, with opposite taker flow, and exits if price reclaims that level.
Selection, fee assumptions, exposure, stops, holding limits and loss locks are unchanged.
Changing modes with open PAPER positions is refused; risk history is retained when flat.

```bash
uv run paperfrog simple backtest 2024-10-15 2026-10-13 reports/reverse-new --mode reverse
uv run paperfrog simple backtest 2024-10-15 2026-10-13 reports/rejection-new --mode failed_breakout
```

The first follow-up comparison froze these two candidates before evaluation, verified
identical reference inputs, and reproduced the original 30-day baseline. It was prompted
by the earlier losses and reused previously viewed history; it is exploratory, not an
independent holdout. The protocol and immutable outputs are in
`reports/simple-reversal-study-v1`. New PAPER entries are paused after both candidates
failed the recent baseline, doubled-cost and delayed-entry tests; collection and monitoring
continue. No real-money deployment is authorized by these reports.

### Longer-move research

The follow-up research implements configurable, frozen entry/exit rules while retaining
the guarded PAPER execution engine. Three declared families contain 24 candidates in total:
longer intraday breakouts, sustained trends with wider stops, and multi-day trends with
32-day warm-up and up to 28-day holding periods. Wider initial stops reduce position size
under the original planned cash-risk budget. The running default rules remain unchanged.

Candidates are ranked on calibration history, checked on a later validation period, then
frozen before final-period, continuous-history, doubled-cost and delayed-entry evaluation.
All candidate outcomes are retained. These periods were already viewed during earlier
research, so this parameter search does not create an independent holdout. Calendar-month
account returns include cash months and mark-to-market PnL; partial months are flagged.
Continuous replays retain loss locks, risk anchors and cooldowns without periodic resets.

```bash
uv run python scripts/study_momentum.py --family sustained_trend \
  --output reports/sustained-study-new --workers 3
uv run python scripts/study_momentum.py --family weekly_trend \
  --collection reports/weekly-warmup-collection-v1.json \
  --output reports/weekly-study-new --workers 3
uv run python scripts/study_aggressive_paper.py \
  --source reports/weekly-study-new --output reports/aggressive-study-new --workers 3
```

The weekly sample starts on 2024-11-15 to allow full warm-up. Its recent pool uses 95 of
the original 97 coins after additional public candle collection; PONSUSDT and MARSCOINUSDT
lack complete extended warm-up and were excluded before evaluation. Historical pool
membership and delistings are still unproven. Coverage exclusions and input hashes are
retained; these pools are not presented as identical comparisons.

The separate offline budget validates PAPER-only mode, 2% planned risk per trade, 4% open
risk, individual notional up to 100% of equity and total direct exposure up to 150%.
Daily, weekly and persistent drawdown halt thresholds are 5%, 10% and 20%, respectively.
Existing entry-count, liquidity, cooldown and profit-protection gates still apply.
Environment flags and credentials cannot configure this offline budget. It is used only
inside isolated historical ledgers; it does not change runtime settings or Telegram controls.
These thresholds are not guaranteed monthly loss caps, and no 20% monthly return is promised.

Retained studies are `reports/simple-momentum-study-v1`,
`reports/simple-sustained-trend-study-v1` and `reports/simple-weekly-trend-study-v1`.
The first twelve candidates all lost on calibration; the first study ended after those
failures and two negative validation comparisons, with that protocol deviation recorded.
The sustained candidate returned +0.09% over two years, -2.02% with doubled costs and
-1.11% over the recent 97-coin pool. It did not qualify for activation.

Simple historical experiments remain isolated PAPER simulations. Explicit local research
waiver and arming enable the current default rules through the separate exchange execution
path; experimental rule sets cannot use that path. The simple strategy cannot promote
itself or satisfy legacy model readiness. No old research/model/news/hedge pipeline runs in this profile.
The old losing candle experiment was removed; its immutable reports remain available.
Legacy code below is retained for existing safety/operations and reproducible research.


Legacy research/account commands and the earlier architecture are retained in [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md). They require the legacy profile and do not validate or authorize the simple strategy.
