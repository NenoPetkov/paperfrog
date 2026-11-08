# Ninety-day follow-up: no demonstrated live trading edge

This is research and prepared code on `research/momentum-90d-20261108`, not a
production deployment. No new strategy was activated, no local live authorization
was renewed, no live leverage was raised, and no orders were submitted by these
experiments. Existing production behavior was not changed in this follow-up.

## What was tested

Nineteen fixed futures symbols, 93 days of continuous five-minute public candles,
three days of warmup and three consecutive 30-day evaluation periods:

1. August 10–September 8, 2026 UTC.
2. September 9–October 8, 2026 UTC.
3. October 9–November 7, 2026 UTC.

The first two periods select a candidate by its worse period return, subject to
a minimum trade count. That choice is saved before the final period is evaluated.
The final period overlaps the earlier study and was already partly observed:
this is retrospective screening, **not a pristine holdout or prospective proof**.
The second family comparison was designed after the first comparison failed.
Repeatedly searching until something looks profitable would compound selection
bias; all candidates and their failures are retained.
See the methodological discussion in [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

Each simulation starts with 10,000 units of equity. Position margin is 10% of
equity at 2x, at most two concurrent positions, twelve entries/day, two entries
per symbol/day, and a 30-minute symbol cooldown. Eligible setups rank into a
top-ten candidate list within the fixed 19-coin universe. Signals use completed
candles, enter at the next open, and retain protective and tightening ATR stops.
There is no ordinary BTC/ETH direction gate.

Fees are 5bps per fill; spread plus slippage is 3.5bps per fill. This uses a
1.5bps half-spread and 2bps slippage assumption; the previous recent journal
sample's 90th-percentile full entry spread was 2.64bps. Historical order books
are unavailable, so this is not a reconstruction of fills. Actual public funding
rates and marks replace the earlier conservative hourly funding reserve.
Doubled-cost sensitivity doubles fees, friction and funding debits, leaving
funding credits unchanged. Funding at the exact entry instant is excluded and
at the exit instant is included.

## Momentum comparison

Sixteen variants combine four setup families with 1/2/4/8-hour holding caps:
baseline A/B/C momentum, selective trend, breakout, and pullback. The three
additional families require the coin's own one-hour/four-hour context. All use
two completed opposite EMA candles for trend exits; stops and time limits remain
immediate. At least twenty trades in each earlier period are required to rank.

**All sixteen variants lost in every 30-day period.** The frozen selection was
pullback with an eight-hour ceiling. Its first two returns were -9.49% and -8.58%.
The final period had 315 trades, a 29.2% win rate, 0.698 profit factor, a 90-minute
median hold, nine profitable days, and **-9.09% overall**. Profitable days did not
make the full period profitable; an eight-hour ceiling did not imply positions
usually survived for eight hours.

| Frozen momentum selection, final period | Return | Conservative drawdown |
|---|---:|---:|
| 2x, base costs | -9.09% | 12.99% |
| 4x, same margin allocation | -17.57% | 24.39% |
| 2x, doubled costs | -18.28% | 19.79% |

Exact results, the registered plan, frozen selection and input hashes are saved
under [research/momentum-20261108](research/momentum-20261108/results.json).

## Slower entries and reversal comparison

The second registered comparison contains fifteen variants: hourly bounded
EMA 8/24 crossovers, selective momentum sampled only at hourly boundaries,
RSI 30/70 recoveries, RSI 25/75 exhaustion reversals, and inverted selective
momentum as a diagnostic. Each has 1/4/8-hour caps. Hourly/inverse variants use
stop/time exits; RSI variants also exit when RSI returns to neutral. The hourly
averages use the previous 48 completed hours. These alternatives are offline
research only, not additional runtime profiles.

At least twelve trades in each earlier period are required to rank; the inverse
diagnostic is excluded from selection. **All fifteen alternatives lost in every
period**, including the inverse diagnostic. Combined with the sixteen momentum
variants, all 31 additional configurations lost across all three periods.

The frozen alternative was hourly selective momentum with a four-hour ceiling:
-2.48%, -2.23%, then **-0.70%**. The final period contained 56 trades, a 46.43%
win rate, 0.895 profit factor, 200-minute median hold and ten profitable days.
Doubled costs reduced its final-period return to **-2.59%**. The eight-hour
hourly variant's -0.12% final-period result was the closest to flat, but it still
lost, and selecting it after seeing that result would change the selection rule.
No profitable candidate was found or promoted. The earlier 24-variant study
also failed; it must not be discarded from the research history.

### Complete return comparison

Returns below are net screening estimates with the stated fees, friction and
funding. Each column starts from fresh equity, not a concatenated account curve.

| Family | Maximum hours | Aug 10–Sep 8 | Sep 9–Oct 8 | Oct 9–Nov 7 |
|---|---:|---:|---:|---:|
| momentum | 1 | -13.58% | -13.33% | -8.91% |
| momentum | 2 | -12.57% | -14.22% | -11.41% |
| momentum | 4 | -13.81% | -13.53% | -15.69% |
| momentum | 8 | -14.37% | -13.59% | -13.56% |
| momentum_breakout | 1 | -14.20% | -16.71% | -12.01% |
| momentum_breakout | 2 | -11.49% | -13.22% | -13.77% |
| momentum_breakout | 4 | -11.28% | -17.06% | -14.88% |
| momentum_breakout | 8 | -8.02% | -15.60% | -14.21% |
| momentum_pullback | 1 | -13.80% | -12.01% | -10.88% |
| momentum_pullback | 2 | -13.21% | -9.86% | -10.96% |
| momentum_pullback | 4 | -10.70% | -8.01% | -9.83% |
| momentum_pullback | 8 | -9.49% | -8.58% | -9.09% |
| momentum_selective | 1 | -12.13% | -14.58% | -14.95% |
| momentum_selective | 2 | -6.18% | -15.53% | -14.31% |
| momentum_selective | 4 | -3.04% | -15.45% | -14.82% |
| momentum_selective | 8 | -0.86% | -15.16% | -13.23% |
| hour_cross | 1 | -4.88% | -4.71% | -1.67% |
| hour_cross | 4 | -4.77% | -7.22% | -0.68% |
| hour_cross | 8 | -5.71% | -8.34% | -0.77% |
| hour_sample | 1 | -2.69% | -3.40% | -1.71% |
| hour_sample | 4 | -2.48% | -2.23% | -0.70% |
| hour_sample | 8 | -3.38% | -2.06% | -0.12% |
| rsi_recovery | 1 | -8.57% | -9.98% | -17.56% |
| rsi_recovery | 4 | -7.42% | -9.56% | -19.57% |
| rsi_recovery | 8 | -7.42% | -9.56% | -19.57% |
| rsi_exhaustion | 1 | -8.87% | -11.94% | -14.59% |
| rsi_exhaustion | 4 | -9.82% | -11.74% | -15.63% |
| rsi_exhaustion | 8 | -9.82% | -11.74% | -15.63% |
| inverse_diagnostic | 1 | -7.83% | -7.12% | -13.78% |
| inverse_diagnostic | 4 | -15.12% | -11.96% | -7.41% |
| inverse_diagnostic | 8 | -14.81% | -11.13% | -6.92% |

## Prepared implementation and operational limits

The review branch adds the three explicit momentum profiles, shared classifiers,
own-coin context and a researched-universe restriction. The generic profiles can
use the existing local LIVE authorization workflow, with a new dispatch check
that recomputes eligibility against the exact order, current configuration,
fresh quote, saved candle, fixed strategy and current costs. Stale or changed
permissions reject entry. The separate paper runner still strips credentials.
PAPER remains the default; Telegram cannot arm LIVE or increase risk.
These are manual profiles: enabling one does not invoke strict automatic mode's
historical profitability selection. Its required research waiver is an operator
acknowledgment, not evidence that the strategy has passed a profitability test.

An unused local migration utility requires a stopped process and a flat,
reconciled account without unresolved orders, preserves account binding and all
financial locks, and caps margin at 10%, positions at two and entries at twelve
per day. It retains 2x. Margin sizing permits allocations below 10% after fees
and lot rounding; forcing an exact 10% floor under a 10% ceiling could otherwise
block every order. No migration or deployment command was executed in this review.

These are screening simulations, not a full exchange or production engine
replay. They omit historical constituent changes, delistings, order-book depth,
liquidation, exact lot rounding and all production loss-lock semantics. The
fixed surviving universe can bias results. Conservative drawdown combines
concurrent positions' adverse candle extremes, whose simultaneity is unknown.
Software tests verify mechanics and safety properties, never profitability.

## Verification and production observation

The full non-testnet suite passed: **1,036 tests**, one testnet test deselected.
The focused momentum suite passed **22 tests**, including a subsequently added
future-price isolation check for hourly context. Ruff, strict mypy on the
configured risk/execution targets and momentum module, shell syntax and diff
whitespace checks passed. All exchange execution tests used a simulated exchange.

A read-only server check found production still on `3229c82`, LIVE, RUNNING,
armed and reconciled, with no open positions and a healthy idle monitor. This
follow-up did not restart it or replace its strategy. The prepared code and all
public aggregate evidence were committed on a separate review branch.

## Reproduce the public research

```bash
uv run --no-sync python scripts/fetch_momentum_history.py \
  --folder reports/momentum-live-20261108/history \
  --start-ms 1786060800000 --end-ms 1794096000000
uv run --no-sync python -m scripts.study_momentum_live \
  --folder reports/momentum-live-20261108/history \
  --output reports/momentum-live-20261108/results.json
uv run --no-sync python -m scripts.study_alternative_entries \
  --folder reports/momentum-live-20261108/history \
  --output reports/momentum-live-20261108/alternatives.json
```

The momentum script fetches public funding needed by both comparisons. No
private trade snapshot or credentials are needed. The older paired-entry
holding analysis is retained in the [original report](HOLDING_MOMENTUM_REVIEW_2026-11-08.md):
extending holds to four hours improved that sample but remained negative with
protective stops, and overlapping counterfactual trades were not portfolio returns.
