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
| Daily loss | 1.25% |
| Weekly loss | 3.50% |
| Equity high-water drawdown | 6% |

Sizing reserves stop risk plus fees and execution cushion across all legs. Quantity/tick rounding and minimum notionals must pass. Liquidation distance must be at least 25% and five stop distances. Exchange runs estimate from current maintenance brackets before entry and check actual reported liquidation prices after fills; unknown/unsafe values require reductions.

Daily and weekly equity anchors, intraday peak and all-time peak include unrealized PnL and survive restart. At a .75% daily peak gain, new risk halves. At 1.25%, new entries lock for that day. A giveback of max(.35% starting equity,35% peak profit), after the .75% activation threshold, flattens and locks. Exact-boundary tests cover all account limits. Three losing strategy exits impose a 12-hour soft lock. Profitable/losing exits impose 45/180-minute symbol cooldowns.

Orders for reductions always use reduce-only. Exchange hosted stops use closePosition with MARK_PRICE on the Algo API; quantity/reduceOnly are not combined with closePosition. The software cannot guarantee a maximum realized loss during gaps, illiquidity, liquidation or exchange failures.

Crisis scoring accepts the ten conditions in the brief; CAUTION holds entries for at least 30 minutes, CRISIS requests one 50% reduction, EMERGENCY flattens and hard-locks. The runtime currently wires BTC absolute moves, mark/index deviation and reconciliation mismatch. Broader historical cross-sectional/liquidation/collateral observations need validated feeds and operational acceptance; their absence is a documented readiness limitation, not an assumed healthy measurement.

No LLM result authorizes an order. It may veto or reduce the deterministic sizing budget. A missing or stale model, missing feature history, severe drift, stale prices, account inconsistency or invalid arm cannot authorize an entry. Normal entry orders are bounded IOC limits. Unknown order results remain journaled for reconciliation; automatic mutation retries are forbidden.
