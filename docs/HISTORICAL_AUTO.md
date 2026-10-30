# Historical automatic strategy selection

Both automatic modes choose `current` or confirmed `opposite` separately for each
new trade. Stable, aligned trends favor current; changing flow, weak/unestablished
breakouts, choppy paths, mixed/fading trends or a matched loss pattern favor
opposite. Opposite still waits for a completed failed breakout or structure
reversal; a preference alone never submits a reversed order.

The historical controller chooses `5m` or `15m` entry candles and a holding cap
of 1, 2, 4 or 8 hours (never above the configured maximum). These are controller
settings, while strategy direction is per trade. In strict `/strategy auto`, the
actual chosen strategy/candle/holding combination must itself be eligible; evidence
for current cannot authorize opposite, or vice versa. `/strategy current` and `/strategy opposite` restore manual selection
and the configured candle/holding settings. Automatic mode requires the normal
trend profile, broad-market protection and AI disabled; two independent policy
controllers must not compete for entry permission.

The current BULL/BEAR/MIXED checks remain independent entry vetoes. A second,
fixed classification distinguishes persistent and fading trends, and MIXED
reversals versus disagreement. Persistent means at least 65% directional breadth
and an aligned one-hour move at least one quarter of the four-hour move. These
bins are not fitted to the winning trades. Opposite still requires a completed
failed breakout or structure reversal, reversed flow, and a live price beyond
the confirmation level. A rising win percentage alone never selects opposite.

## Historical evidence and switching

An hourly background task incrementally caches ten days of public five-minute
candles for a fixed 19-coin research universe. It aggregates exact closed
15-minute candles and compares all 16 strategy/candle/holding combinations over
the last seven days. Automatic entries are restricted to researched coins.
Recorded market context is joined only after its actual availability; missing
historical context is not reconstructed using today's knowledge.

Both intervals share live indicator/signal code with replay. Slow context joins
point backwards. Simulated entries use subsequent opens; trailing updates use only already-known opens, then subsequent adverse candle
extremes check stops. Each variant holds at most one simulated
position per coin. Entry and exit fees, configured slippage, assumed half-spread
and a funding reserve of 1 basis point per permitted holding hour are deducted.
Current profit-runner logic is included, with a fixed 0.30% net activation for
automatic positions. Unclosed trades supply no outcome.

Eligibility requires all of:

- At least 24 completed simulated trades, three UTC days and eight distinct
  four-hour entry blocks in the same market pattern.
- A chronological 70/30 split with training trades purged if their exits overlap
  the validation start; at least 12 training trades, six validation trades and
  three validation blocks.
- Positive training mean, validation net mean above 2 basis points, and positive
  mean under doubled assumed costs.
- A block-mean lower screening bound (`mean - 2.5 × standard error`) above
  2 basis points. Coins trading together do not count as independent samples.

This is a conservative screening heuristic, not a formal profitability guarantee
or a confidence interval corrected for all repeated comparisons. The simulation
does not reproduce portfolio limits, BTC tick vetoes, five-second monitoring,
exchange fills, funding settlements or liquidity failures. Its per-trade returns
must not be reported as account profit. The fixed universe also limits what can
be inferred about other coins. Software tests verify implementation only.

The best eligible variant must win two consecutive quarter-hour reviews. Changes
require at least one hour since the previous switch and a 5-basis-point improvement
in the screening bound over an eligible incumbent. A validated incumbent remains
active during confirmation. In strict mode, when no variant qualifies, new entries wait while
research and position monitoring continue. Missing, failed or over-four-hour-old
research also blocks automatic entry. Every permission expires at the next
15-minute context boundary and is rechecked immediately before live dispatch.

Selection, evidence, reasons and epochs persist in SQLite and appear in `/strategy`
and `/simple`. No loss streak automatically flips direction or clears risk locks.

## Optional active automatic mode

`/strategy auto-active` explicitly enables a limited exploration fallback.
`/strategy auto` keeps the original strict historical screening and disables
the fallback. Both choices persist across restarts; neither clears risk locks.
Validated historical variants still take priority and retain all their original
eligibility rules. A probe is explicitly **not** a validated historical edge.

When no historical variant qualifies, active mode prepares per-trade probes on
15-minute candles in persistent markets and 5-minute candles in mixed/fading
markets, with a one-hour holding cap. Both current and opposite use the same
probe limits. Initial activation and changes of candle size require two consecutive
reviews agreeing on the proposed interval and at least one hour since the previous
controller change. A compatible, already active probe policy continues on its
current interval during that confirmation/dwell period, using fresh market context
and per-trade direction checks. Merely changing BULL/BEAR/MIXED or fading/persistent
labels does not pause it. Missing or stale research/market context still blocks
entries. The normal
coin, BTC, breadth, spread, liquidity, risk, authorization and hosted-stop checks
still apply to every order. Failed research cannot be bypassed by active mode.

Probe orders use half the normally computed notional, rounded down. Equity-range
allocation checks use the same scale, so a probe is never rounded back up to the
normal minimum. Exchange minimum quantities and notionals still apply. Probes can
share the portfolio with other probes or validated positions, up to the lower of
four positions and the configured concurrent-trade limit. Existing aggregate open
risk, gross exposure, total margin, symbol cooldown, per-symbol daily and overall
daily limits continue to apply across all positions.

Atomic ledger reservations limit probes to four attempts per rolling 15 minutes
and 24 per rolling 24 hours, including aborted attempts. Four different qualifying
coins can therefore enter during the same scan. These are ceilings, not quotas.
Restarting, changing modes and resuming do not reset the budget. A dispatch-time
veto rechecks the policy, own reservation, portfolio count, research freshness and
order binding; an order already reserved does not consume a second slot.

Short-interval research retries the same candle while awaiting its closed
quarter-hour risk context or when the controller changes during a fetch. Expected
context synchronization is not logged as a scan failure and does not skip that
candle. Freshness gates still reject genuinely stale entry observations.

`/strategy`, `/why`, `/simple` and entry journals distinguish probes from validated
entries. More entry opportunities do not establish profitability or guarantee a
particular number of trades.

## Per-trade route and loss memory

The current preference requires aligned one-hour/four-hour returns, persistent
market breadth, trend persistence over two completed candles, consistent flow,
and a one-hour directional efficiency of at least 0.55. A 15-minute breakout less than
0.15 ATR outside its old range, previous trend strength below 0.3 ATR, or a flow
sign change is treated as unstable. These are initial, interpretable heuristics,
not statistically validated thresholds. Path efficiency is absolute net movement
divided by the sum of absolute candle moves; no future candle is included.

Loss memory compares the candidate with up to 20 closest current-strategy trades
closed before the decision, from the last 30 days and the same configuration and
candle interval. Seven direction-normalized features describe trend strength,
previous strength, current/previous flow, one-hour/four-hour return in ATR units,
and breakout depth. Similar winners remain in the sample. At least three matches
across two four-hour blocks, a two-thirds loss share, negative mean net return,
and a positive same-exit inverse estimate are needed to favor opposite from memory.
The inverse estimate is a diagnostic, not a simulated opposite strategy result.
Monitoring failures and other operational exits cannot teach a reversal pattern.

The full route, matching trade IDs and reason are recorded with each order and
position. Entry permissions bind the exact order, chosen strategy, epoch, closed
candle and market context; routing is checked again at dispatch. Candidate choices
do not mutate the global strategy or flip existing positions. Telegram entry and
closed-trade messages display the strategy actually used, including AUTO on new
automatic trades; entry messages include the routing reason.

## Existing positions and holding periods

Every new position records its entry interval and selected holding cap. Existing
positions keep their original interval when another variant is selected; the
monitor continues collecting that interval. Legacy positions retain their previous
exit behavior. Changes never invert an open position or loosen a protective stop.

For positions opened by auto, fresh broad-market evidence can shorten the original
holding cap to one hour when the one-hour market return opposes the actual
position, or two hours when the four-hour direction disagrees or momentum fades.
The cap cannot exceed its entry value or the locally configured maximum. Stops,
portfolio protection and profit-runner exits continue to take precedence. Holding
periods are limits, not promises that a trade will stay open that long.

## Reproducing the analysis

First use SQLite's backup API to snapshot the live ledger. From the configured
project directory, run:

```sh
uv run --no-sync python scripts/review_strategy_history.py \
  --ledger /private/path/snapshot.sqlite \
  --output /private/path/report --replay
```

The script writes analysis and replay evidence to the supplied private directory
and snapshot, plus public candle caches. It never places orders, enables a
strategy, changes a risk limit or arms live trading. Reports separate the previous
calendar week in Europe/Sofia, the trailing seven days and the subsequent review
period. Fees in old trade journals are estimates; verified accounting can differ.

The implementation follows the [Binance closed-kline API](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data)
and guards against the [future-data leakage described in Freqtrade's documentation](https://www.freqtrade.io/en/stable/lookahead-analysis/).
