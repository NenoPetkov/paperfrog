# Review of the five active-auto losses, October 26–27, 2026

The exchange-verified total is -2.40461421 BNFCR. All five entries used
`current`, 15-minute candles and a one-hour cap under the unvalidated active
fallback. No historical combination had qualified at these entries.

| Entry (Europe/Sofia) | Trade | Net BNFCR | Observation available at entry | Exit |
|---|---|---:|---|---|
| Oct 26 14:32 | XRP long | -0.70127977 | Previous flow -0.033, entry flow +0.366: direction changed | Stop |
| Oct 26 18:45 | ARB short | -0.69570938 | Previous trend -0.210 ATR, entry -0.442: newly emerging trend | One-hour cap |
| Oct 26 22:31 | SOL short | -0.33181875 | Previous flow +0.050, entry -0.144; breakout only 0.050 ATR | One-hour cap |
| Oct 27 02:00 | XRP long | -0.07899668 | New trend, previous strength 0.171 ATR; gross price PnL slightly positive | Monitoring data unavailable |
| Oct 27 09:00 | ETH short | -0.59680963 | Breakout only 0.0175 ATR outside previous range | Stop |

The original persistent-market classification did not establish that an individual
coin's entry trend was stable. The fallback was asymmetric: it could trade current
without validated evidence, while opposite required historical validation.

The fourth trade is an operational exit, not evidence that shorting was superior.
Hypothetically reversing the exact five entry/exit prices and subtracting their
recorded fees gives +1.5486 BNFCR with four winners. This does not reproduce the
opposite strategy's entries, stops, execution or exits and is not a backtest.

The broader historical neighborhoods contain similar winners as well as losses;
these five trades do not establish a reliable inverse edge. The implementation
therefore combines per-coin stability checks with outcome memory, rather than
labeling every losing entry a proven opposite opportunity. It learns only from
already closed outcomes and excludes monitoring-failure exits. New loss cohorts
can favor opposite once the documented sample conditions are met. A preference
for opposite still requires live reversal confirmation; otherwise the entry waits.

This change does not claim to recover the losses or establish future profitability.
Position sizing, activity budgets, financial loss locks and hosted-stop protection
are preserved. New Telegram trade messages identify the actual per-trade choice.
