# Momentum entry timing — November 9, 2026

All wall-clock times below use Europe/Sofia (UTC+02:00 on this date).

## Why the production shorts arrived late

At 06:00 the service was still running the previous strict historical automatic
policy. Its audit explicitly recorded: “No validated net edge for this market
pattern; collecting shadow evidence.” The corresponding selection was empty.
That is a permission decision, not evidence that the market had no downward move.

The locally authorized switch to fixed LIVE momentum occurred at 09:21:55;
the new process started at 09:22:14. The first LINK short followed at 09:25,
and BNB/NEAR/DOGE shorts followed around 09:30. The new momentum policy was not
running at 06:00 and cannot retrospectively place an earlier order.

The v1 policy also had a timing weakness: it could enter an already established
move without measuring its age or exhaustion. Its one-hour direction/efficiency,
25-minute confirmation and previous-bar strength requirements could defer a fresh
turn, while its absolute one-hour ranking could favor a move that had already
travelled far. These are lagging measurements, not forecasts of a reversal.

## New entry specification: abc-momentum-v2

Only the base `momentum` profile changes; legacy v1 research and the three named
research profiles retain their existing classifiers.

- Use consecutive completed five-minute candles, EMA 5/20, a moving fast EMA,
  a confirming candle body and signed trading flow of at least 0.05.
- Require aligned five- and fifteen-minute returns and at least 0.3 ATR of EMA
  separation. Do not wait for the old hour or 25-minute direction to catch up.
- Require a proven EMA regime transition within the preceding sixty minutes.
  A continuing old trend or an unobservable start cannot authorize a new entry.
- Require fifteen-minute path efficiency of at least 0.3. Reject new shorts
  below RSI 30 and new longs above RSI 70. Reject price extensions or candle
  bodies beyond 1.5 of the entry-timeframe ATR. These are explicit heuristics,
  not calibrated probabilities that a reversal will happen.
- Rank the recent fifteen-minute move relative to five-minute ATR, multiplied
  by recent path efficiency and discounted by trend age.
- Recheck the exact bound candle, quantity, side, configuration and fresh quote
  before exchange dispatch. A rebound greater than half an entry ATR still vetoes
  submission. Missing, nonfinite, changed or expired evidence fails closed.

The service fetches 144 closed five-minute candles instead of 120 to observe
past EMA transitions. The EMA remains bounded to the same 119-price window;
current and previous EMA values, reversal exits and ATR protection are unchanged.
An unverified start is stored explicitly as -1 rather than nonfinite JSON.

The change does not increase sizing, leverage, frequency, concurrent-position,
exposure, loss or margin limits. Existing positions retain their original entry
record, side, stops, two-candle momentum exit and holding ceiling. Restart accepts
only this specific LIVE v1-to-v2 entry upgrade when all exit-rule fields match
and current local authorization succeeds. Other held-position rule changes remain
blocked. PAPER remains the installation default.

`/why` and local diagnostics now report the entry-window and exhaustion rejections.
New trade journals retain the observed trend start, trend age and fifteen-minute
path efficiency used at entry.

## Chronological check of this morning

The [public aggregate observations](research/entry-timing-20261109/summary.json)
come from public Binance five- and fifteen-minute candles fetched after the event.
The raw input SHA-256 is included. Each signal receives only candles already
closed at its decision time and the risk context closed at its preceding
fifteen-minute boundary. One-way costs are assumed to be 0.12% for eligibility.

| Coin | First eligible short at or after 06:00 under v2 | Later production-entry candle under v2 |
|---|---|---|
| BNB | 06:00 | 09:30 rejected: old/unverified trend start |
| DOGE | 06:00 | 09:30 rejected: old/unverified trend start |
| NEAR | 06:05 | 09:30 rejected: old/unverified trend start |
| LINK | 07:10 | 09:25 rejected: old/unverified trend start |

LINK was **not a confirmed short at 06:00** under this rule: its five-minute
close was flat and the EMA spread was still positive. The implementation does
not invent a 06:00 LINK entry to match a later chart. BNB also had an earlier
eligible signal before 06:00; the table deliberately limits its start to the
user's requested comparison time.

These are signal permissions, not reconstructed fills, a portfolio backtest or
profitability evidence. Historical spreads, account capacity, prior entries,
selection ranking, cooldowns and actual fills have not been replayed. Public
candles retrieved later can also differ slightly from the service's archived
near-close observations. This retrospective morning was already observed; it
cannot validate future trading performance or guarantee an earlier entry.

Reproduce from the public input retained under `reports/entry-timing-20261109`:

```bash
uv run --no-sync python scripts/review_entry_timing.py \
  reports/entry-timing-20261109/market.json \
  --start 2026-11-09T05:00:00+02:00 --end 2026-11-09T09:30:00+02:00 \
  --output reports/entry-timing-20261109/comparison.json
```

Focused regression tests cover earlier confirmation, old/unverified starts,
exhaustion, cost and price checks, future-candle isolation, exact dispatch binding,
extra-history fetching, original exits and a restart with held positions and
unchanged financial locks. Software checks establish those behaviors only.
