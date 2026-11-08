# New strategy research — November 8, 2026

## Result and scope

**A fixed BTC/ETH funding-carry hedge was profitable in both tested 360-day
periods, after modeled commissions, friction, actual funding and basis changes.**
This is a modest-return research candidate, not demonstrated future profitability.
Each annual sample contains only two hedges; the periods and assets are not a
large set of independent trials. No live strategy, position, leverage, account
authorization or service was changed by this research.

The new algorithms are implemented in `src/paperfrog/research/`. They do not use
the old bot's indicators, signal gates, opposite routing, AI, confidence exits
or short holding timers. They cannot submit orders and do not read credentials.
The existing production bot remains separate. Replacing its futures-only order
execution with a spot-and-futures hedge would require a separate implementation;
this offline result is not authorization or readiness to do that.

## Internet sources and what they support

- [BIS, Crypto carry](https://www.bis.org/publications/working-paper-1087-crypto-carry)
  studies spot/futures carry and explains the risks that prevent it being free
  money. Its dated-futures analysis is motivation, not a backtest of our perpetual
  funding rules.
- [Binance's funding arbitrage mechanics](https://www.binance.com/en-AE/support/faq/detail/f330e17d6fc04679b9b21d6f9350e787)
  describes matching spot and perpetual positions, variable funding and the
  need to cover execution costs. Our model uses fully paid long spot and equal
  coin quantities short perpetual futures, without borrowing spot.
- [AQR, A Century of Evidence on Trend-Following Investing](https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing)
  motivates slower trend following across markets. It does not establish that
  our particular daily cryptocurrency channel rules work.
- [Kozlowski, Puleo and Zhou, Cryptocurrency return reversals](https://digitalcommons.fairfield.edu/business-facultypubs/246/)
  reports cross-sectional reversal effects, especially in smaller, less-liquid
  coins. This does not guarantee executable returns in our liquid fixed universe.
  [Cryptocurrency Momentum and Reversal](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3913263)
  also motivates the relative portfolio comparison; only its indexed abstract
  was accessible in this session.
- [Gatev, Goetzmann and Rouwenhorst](https://www.nber.org/papers/w7032)
  motivates distance-based pairs formation. The crypto-specific
  [Pairs Trading in Cryptocurrency Markets](https://ieeexplore.ieee.org/document/9200323/)
  reports sensitivity to costs and execution windows. Our log-ratio distance
  model does not claim that a formal cointegration test passed.

## Data and execution assumptions

Public Binance spot and perpetual hourly candles plus historical funding rates
and marks cover ten fixed coins: BTC, ETH, BNB, SOL, XRP, DOGE, ADA, AVAX, LINK
and LTC. The current dataset contains 420 days: sixty warmup days and 360 days
from November 13, 2025 to November 8, 2026, with the end boundary excluded.
An additional 420-day download supports an earlier 360-day evaluation from
November 18, 2024 to November 13, 2025. Inputs are checked for gaps, duplicate
timestamps, invalid prices and funding coverage. SHA-256 hashes are retained.

Signals use completed observations; funding-based entry eligibility has a
one-hour publication lag. Execution uses the subsequent hourly open with adverse
spread/slippage. Base fees are **5bps per futures fill, 10bps per spot fill,
and 3.5bps friction per fill**. A complete carry entry/exit therefore includes
four transactions, approximately 44bps of one leg's notional at unchanged prices.
These are cost assumptions, not a verified current account fee schedule.

Double-cost tests double commissions, friction and funding debits without
doubling credits. Delayed tests move daily decisions one hour later. Quantities,
cash, realized PnL, fees and funding reconcile at the end of each replay.
Funding at an entry instant is excluded; funding at the exit boundary is included.
If a protective stop occurs within an hourly candle, funding through that
candle's close is used because intrabar ordering is unknown.

## Search history: failures retained

The first fourteen configurations compare:

- Funding carry with 7/30-day observations and 7/30-day holding caps.
- Daily channel breakouts using 20/55-day entries, 10/20-day exits and 2/3 ATR stops.
- Relative momentum portfolios using 7/30-day rankings and weekly rebalances.
- Relative reversal portfolios using 1/7-day rankings and daily/weekly rebalances.

Selection used the worse of the first two 120-day returns, with at least six
trades per period. The final 120 days were evaluated after saving that selection.
No candidate passed both earlier periods. Some channel variants had profitable
individual periods. Four additional 60/90-day carry variants and four monthly
formed pairs variants were then registered and tested; none established a
consistent edge under their selection rules. These later experiments reused
viewed history and are explicitly exploratory.

The 55-day entry / 20-day exit / 3 ATR channel candidate was frozen after seeing
its positive recent results, then checked continuously and against the additional
earlier year. Its recent 360-day return was **+7.84%**, or **+2.51%** with both
double costs and delay, but the earlier year lost **28.66%**, with a **33.90%**
hourly-close drawdown. It was rejected as a robust replacement. The recent
positive number must not be shown without this failure.

The twenty-third hypothesis is a **static funding hedge**. It was introduced
after the earlier results, so both annual samples are retrospective exploration
for this hypothesis. No claim of an untouched holdout or multiplicity-adjusted
statistical significance is made. No parameters were tuned after this hypothesis's
results. A positive number is not an automatic promotion gate.

## New candidate: fixed funding hedge

1. Use only BTC and ETH; do not rank coins by retrospective profits or chase the
   largest current funding rate.
2. At the beginning of a declared holding period, enter each eligible hedge only
   if the previous thirty days' settled funding is positive, prior-day futures
   volume exceeds 10 million, and the modeled futures/spot basis is between
   -0.1% and +0.5%.
3. Per asset, allocate 18% of equity to fully paid spot and reserve 27% as futures
   collateral. Short exactly the same coin quantity. With both assets, this is
   36% long spot, approximately 36% short futures, 54% reserved collateral and
   10% residual cash before costs. Spot collateral is not counted twice.
4. Hold through brief negative funding periods rather than repeatedly closing
   and paying entry/exit costs. Negative funding is paid in the simulation.
5. Close both legs at the declared period end, a combined net loss exceeding
   3% of the hedge's original spot notional, or a conservative futures margin
   buffer breach. No rotation, averaging down, reentry or leverage increase.

The hedge aims to offset coin price moves and collect variable funding. It does
not lock in a guaranteed rate, and the two markets' execution and basis can differ.

| Continuous period | Base net return | Double costs | One-hour delay | Both stresses |
|---|---:|---:|---:|---:|
| Nov 18, 2024–Nov 13, 2025 (360 days) | +2.42% | +2.12% | +2.41% | +2.11% |
| Nov 13, 2025–Nov 8, 2026 (360 days) | +0.68% | +0.29% | +0.68% | +0.29% |

Returns are on **all starting equity**, including reserved margin and cash, and
are not annualized. On 10,000 units of starting equity, the recent sample earned
67.97 units: 81.85 funding, -0.42 gross basis PnL, -9.18 commission and -4.28
friction. The earlier sample earned 241.87 units. Each sample closed two hedges.

Separate 120-day starts were +1.11%, +0.39%, +0.61%, +0.16%, 0.00% and +0.58%.
The zero period opened no hedge because entry conditions failed. These are
independent-entry sensitivity tests, not components that sum to the continuous
annual return. They are also not six independent discoveries.

Hourly-close drawdowns were approximately 0.11% and 0.13% in the annual base
cases. Those figures are **not a reliable upper bound on live risk**. Marking
spot lows and futures highs simultaneously gives adverse-extreme drawdowns of
5.73% and 1.68%; those extremes may not occur together. Actual exchange mark
prices, margin tiers, outages and hedge execution can produce different outcomes.

## Limitations and next evidence

- This is 23 redesign hypotheses after the previous failed research, with
  repeated use of the same data. Two profitable annual samples with two hedges
  each are insufficient to prove a durable trading edge.
- Fixed surviving coin membership and approximate hourly fills can bias results.
  There is no historical order-book replay, partial-fill/legging model, exact
  lot-size simulation, account fee-tier validation, tax or opportunity-cost model.
- The margin-buffer check is not Binance's full liquidation engine. Spot and
  futures capital may be segregated; gains in spot do not automatically cover
  a futures margin call. Funding can turn negative, and basis or venue problems
  can defeat the hedge.
- The static candidate's returns are small. It is a different business model
  from rapidly trading directional crypto moves, and is not compared with the
  return on a cash investment of the same risk.
- Future evidence must use a frozen specification and new observations. A
  real-time simulation must track executable bid/ask prices, both legs, missed
  fills, funding settlements and collateral availability. It must not change
  its rules after each loss to preserve an attractive backtest.

All public aggregate evidence, plans and frozen selections are in
[`research/redesign-20261108`](research/redesign-20261108/static-carry.json).
Detailed public-data paths remain in the workspace under `reports/redesign-20261108`.
No private credentials, account trade journal or live order identifiers are
needed to reproduce this study.

## Verification and reproduction

The full non-testnet suite passed **1,043 tests**, with one testnet test deselected.
After later research additions, the focused redesign suite passed **10 tests**.
Ruff and strict mypy on configured risk/execution targets passed. Tests cover
past-only signals, funding publication timing, entry/exit funding boundaries,
both hedge legs' costs, gap stops, pair formation, accounting conservation and
static-hold behavior. These checks establish software properties, not profits.

```bash
uv run --no-sync python scripts/fetch_redesign_history.py \
  --folder reports/redesign-20261108/history
uv run --no-sync python scripts/study_redesign.py \
  --folder reports/redesign-20261108/history --output reports/redesign-20261108/results.json
uv run --no-sync python -m scripts.study_long_carry \
  --folder reports/redesign-20261108/history --output reports/redesign-20261108/long-carry.json
uv run --no-sync python -m scripts.study_pairs_redesign \
  --folder reports/redesign-20261108/history --output reports/redesign-20261108/pairs.json
uv run --no-sync python -m scripts.audit_frozen_redesign \
  --folder reports/redesign-20261108/history --output reports/redesign-20261108/frozen-current.json
uv run --no-sync python scripts/fetch_redesign_history.py \
  --folder reports/redesign-20261108/earlier-history --end-ms 1762992000000
uv run --no-sync python -m scripts.audit_frozen_redesign \
  --folder reports/redesign-20261108/earlier-history --output reports/redesign-20261108/frozen-earlier.json
uv run --no-sync python -m scripts.study_static_carry \
  --current reports/redesign-20261108/history --earlier reports/redesign-20261108/earlier-history \
  --output reports/redesign-20261108/static-carry.json
```
