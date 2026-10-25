# Paperfrog history review, 25 October 2026

Snapshot: 25 October 2026, 08:41:38 UTC. Monetary values below are the journal's
net BNFCR results for closed positions, excluding open/unrealized positions.
Historical commission estimates are not substituted for a reconciled account
equity statement. The ledger contains 269 closed positions from 14 October onward.

| Period | Closed | Winners | Net result | Recorded fees |
| --- | ---: | ---: | ---: | ---: |
| Previous calendar week, 12–18 October, Europe/Sofia | 91 | 39 | −10.55 | 19.74 |
| Trailing seven days at snapshot | 195 | 94 | −76.74 | 35.36 |
| Since 24 October, 14:00 UTC review | 25 | 22 | +13.24 | 4.21 |

The recent improvement is real in the journal, but does not erase the preceding
losses. The earlier calendar week earned about 9.34 before costs, then fees and
funding turned it negative. Across the full recorded history, five-minute current
entries had positive gross PnL before substantial fee drag. Eight-minute entries
performed poorly, and there were no ten-minute live observations.

Old opposite executions are not evidence for the newer confirmed-reversal rule.
The 35 recorded old opposite/15m trades won 20 times but lost about 14.22 net.
Their MIXED subset won 10 of 18 yet lost about 6.13. Neither MIXED nor a higher
win rate establishes a profitable reversal condition. Among 15m opposite trades
in BEAR, average winners were roughly 0.72 and average losers −1.93. In the recent
25 October UTC subset, 11 of 13 positions won but net profit was only about 1.16:
average winners were 0.42 versus average losses of −1.73.

The chief observed weaknesses are fee drag on small moves, losses much larger
than typical winners, and fragile performance when the prevailing trend changes.
Many earlier losses exited on trend reversal or protective stops. These are
descriptive associations, not proof that changing an exit alone will fix them.
Several different code versions, candle settings, profit targets and manual
interventions were used during this short history, so raw totals are not a fair
controlled comparison of strategy variants.

The new comparison applies the same current signal/exit rules to identical
historical candles for current/opposite, 5m/15m and 1/2/4/8-hour caps. Simulations
include costs and only market context recorded before the decision. Historical
broad-market records begin later than the first trades; missing context is excluded.
There is currently no variant with a sufficiently supported positive edge under
the conservative chronological validation and cost stress rules. Some subsets
improve in the later holdout, but their full-window return or uncertainty check
still fails. No permanently preferred opposite condition or universally optimal
candle interval is established by this sample.

Consequently auto collects and refreshes evidence hourly, evaluates the current
market every quarter hour, and waits for new entries when the evidence fails.
It can select a different strategy, interval and holding cap as later evidence
qualifies. Existing positions continue to receive their original monitoring and
protection. See [the policy and reproduction instructions](HISTORICAL_AUTO.md).
