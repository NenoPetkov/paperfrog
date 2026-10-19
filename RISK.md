# Risk policy

| Constraint | Default and maximum |
|---|---:|
| Intended risk per strategy trade | 0.20% |
| Total intended open risk | 0.50% |
| Target notional / equity | 8% |
| Gross notional / equity | 50% |
| Absolute expected net beta / equity | 0.10 |
| Exchange leverage | Exactly 2x |
| Strategy entries per UTC day | 12 default, 16 hard maximum |
| Entries per symbol/day | 2 |
| Concurrent pairs | Engine cap 2; service/replay conservatively 1 |
| Daily loss | 1.25% default, 12.5% configurable maximum |
| Weekly loss | 3.50% default, 35% configurable maximum |
| Equity high-water drawdown | 6% default, 60% configurable maximum |

Sizing reserves stop risk plus fees and execution cushion across all legs. Quantity/tick rounding and minimum notionals must pass. Liquidation distance must be at least 25% and five stop distances. Exchange runs estimate from current maintenance brackets before entry and check actual reported liquidation prices after fills; unknown/unsafe values require reductions.

Daily and weekly equity anchors, intraday peak and all-time peak include unrealized PnL and survive restart. At a .75% daily peak gain, new risk halves. At 1.25%, new entries lock for that day. A giveback of max(.35% starting equity,35% peak profit), after the .75% activation threshold, flattens and locks. Exact-boundary tests cover all account limits. Three losing strategy exits impose a configurable soft lock (12 hours by default). Further loss-triggered locks during the same losing spell double the configured starting pause, capped at 12 hours. The step persists across automatic recovery and restart; a profitable strategy exit or explicit operator risk reset returns future pauses to the starting duration. Existing lock expiries are never shortened. Profitable/losing exits impose 45/180-minute symbol cooldowns.

`profit_guard_scale` defaults to 1 and accepts 1–10. Scale 10 moves the activation,
minimum giveback and daily profit entry thresholds to 7.5%, 3.5% and 12.5% of
starting equity, while retaining the 35% relative giveback calculation. The
larger-position preset explicitly selects scale 10 and loss limits of 12.5% daily,
35% weekly and 60% high-water drawdown. These permit substantially larger losses.

An authenticated Telegram `/resume` or `/start_trading` explicitly acknowledges
all financial locks, including unexpired daily and loss cooldowns and indefinite
weekly/high-water locks. After execution and health checks pass, it atomically
rebases all equity anchors to fresh current equity, resets profit protection,
consecutive losses, the loss-cooldown escalation step, symbol cooldowns and manual pauses, and records previous values
in the audit. It preserves trade/order history and entry/exposure caps. Automatic
recovery still respects lock expiries and does not rebase anchors. A pending
emergency close, unresolved execution, stale data, crisis or missing local LIVE
authorization continues to block recovery.

Orders for reductions always use reduce-only. Exchange hosted stops use closePosition with MARK_PRICE on the Algo API; quantity/reduceOnly are not combined with closePosition. The software cannot guarantee a maximum realized loss during gaps, illiquidity, liquidation or exchange failures.

Crisis scoring accepts the ten conditions in the brief; CAUTION holds entries for at least 30 minutes, CRISIS requests one 50% reduction, EMERGENCY flattens and hard-locks. The runtime currently wires BTC absolute moves, mark/index deviation and reconciliation mismatch. Broader historical cross-sectional/liquidation/collateral observations need validated feeds and operational acceptance; their absence is a documented readiness limitation, not an assumed healthy measurement.

No LLM result authorizes an order. It may veto or reduce the deterministic sizing budget. A missing or stale model, missing feature history, severe drift, stale prices, account inconsistency or invalid arm cannot authorize an entry. Normal entry orders are bounded IOC limits. Unknown order results remain journaled for reconciliation; automatic mutation retries are forbidden.

The `config/loss-protection.yaml` recovery preset selects a 30-minute starting
pause and two concurrent strategy positions. Successive lock activations without
a profitable close pause for 30, 60, 120, 240, 480 and then 720 minutes. The
position cap blocks additional entries while existing positions retain their
normal protected exits; changing this cap does not itself flatten positions.
The preset changes neither entry predictions nor sizing of permitted positions.
Longer pauses and fewer positions reduce exposure and may also miss profitable
trades; they do not establish a predictive edge.
