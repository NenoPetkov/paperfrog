# Entry frequency review, October 30, 2026

Read-only production inspection at approximately 14:55 UTC found a healthy,
reconciled bot in active auto, no open positions, no manual lock and no financial
risk lock. The active fallback, rather than the account configuration, imposed
one open position, one attempt per hour and six attempts per rolling 24 hours.
The account configuration already allowed four simultaneous trades, 40 entries
per UTC day and four per coin per day.

Over the preceding 24 hours, 42 of 96 historical reviews withheld entry
permission. Among 209 five-minute scans, 29 scans had candidates (46 candidate
occurrences); 18 of those scans and 29 candidate occurrences fell under a review
that withheld permission. These are candidate observations, not 46 executable
trades: subsequent execution and risk checks could still veto them.

The preceding rolling 72 hours contained 12 closed trades, with exchange-verified
net PnL of approximately -0.15881 BNFCR. This small sample is not evidence that
higher activity will be profitable. No entry-direction threshold was loosened to
force more trades.

The change permits up to four concurrent positions (subject to lower configured
limits), four exploratory attempts per rolling 15 minutes, and 24 per rolling
24 hours. Half-size probe allocation is retained. Aggregate risk, margin, gross
exposure, loss locks and symbol cooldowns remain enforced. Activity reservations
persist and remain atomic, including under concurrent submissions.

An active policy continues taking qualifying entries on its current interval
while a replacement interval awaits confirmation/dwell. Exact market-pattern
label changes no longer repeatedly disable otherwise compatible active routing.
Missing or stale research/context still blocks new entries. Initial policy
activation and candle-size changes remain confirmed and time-bounded.

Short-interval scans now retry the same candle when waiting for slow context or
when the strategy epoch changes mid-scan. Current/opposite routing, reversal
confirmation, per-trade journal metadata and Telegram trade strategy labels remain
in use. Software validation checks behavior and safety gates, not profitability.
