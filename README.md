# paperfrog

paperfrog is a Python 3.12 futures research bot. Its default simple profile scans recent Binance USDT perpetual data, ranks up to 100 liquid crypto coins, selects up to two, chooses long/short from trend breakouts and trading flow, and monitors exits. Cash and zero trades are valid outcomes.

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

## Simple bot (default)

```bash
uv run paperfrog run paper
uv run paperfrog simple status
curl http://127.0.0.1:8081/simple
```

Set `STRATEGY_PROFILE=simple`, `SIMPLE_SCAN_LIMIT=100`,
`SIMPLE_SELECTED_COUNT=2` and `SIMPLE_POLL_SECONDS=5` in `.env`.
The new profile uses `state/simple` and `data/simple`; its health server uses the
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

Telegram sends OPEN LONG/SHORT and CLOSE explanations, price/size/stop and realized
PnL after fees/funding. `/status`, `/positions`, `/last_signal`, `/pause`, `/resume`
and confirmed `/flatten` work with the simple profile. Notification retries run separately
from monitoring. `/pause` pauses entries while exits continue. Restart with open PAPER
positions restores protection and stays paused until explicit `/resume`.

Current limits are 12 entries total and two per coin per UTC day, with at most two
positions. Selection may change during the day. The bot does not force a 5–20-trade
quota. These are frozen deterministic rules: no automatic ML retraining, no changes
triggered by individual losses, and no promised profitability.

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

The simple strategy is PAPER-only: it cannot submit exchange orders, arm live, promote
itself or satisfy legacy model readiness. Real-money deployment needs separate execution
review and genuine research/PAPER/testnet evidence. Changing environment flags cannot
remove this restriction. No old research/model/news/hedge pipeline runs in this profile.
The old losing candle experiment was removed; its immutable reports remain available.
Legacy code below is retained for existing safety/operations and reproducible research.


Legacy research/account commands and the earlier architecture are retained in [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md). They require the legacy profile and do not validate or authorize the simple strategy.
