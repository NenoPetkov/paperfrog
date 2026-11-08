# Holding time and A/B/C momentum review — November 8, 2026

This records the original deployed review. See the
[90-day follow-up](MOMENTUM_90_DAY_REVIEW_2026-11-08.md) for subsequent research
and the undeployed review branch; the original PAPER-only generic-profile
restriction described below applies to that earlier release.

The review does **not** establish a profitable strategy. Do not raise live
leverage. Disable unvalidated live probes, retain evidence-required automatic
entries, and observe the simpler strategy in an isolated PAPER account.

## Actual journal

The production snapshot contained 409 closed journal trades from October 14
through November 8. Their recorded net total was -81.94897 BNFCR and estimated
fees were 63.78714 BNFCR. This is the bot journal, not a fresh reconciliation of
all exchange commissions, deposits or account equity changes.

The latest seven days through the snapshot's last close contained 102 trades:
-8.76207 BNFCR net, -0.26291 gross price PnL, 8.50939 estimated fees, and
+0.01023 recorded funding. Win rate was 42.16%; median hold was 20.98 minutes.
Of these closes, 31 were profit-runner retracements, 29 invalidated opposite
entries, 21 one-hour caps, 10 stops, 10 portfolio-risk exits and one trend exit.
The configured eight-hour limit was not the main cause of short holding times:
unvalidated automatic probes had their own one-hour cap and earlier exits.

## Same entries, different holding times

The paired sample contains 138 recent entries with complete eight-hour forward
five-minute candle coverage. Every scenario uses the same entry price, quantity
and side. The sample's actual journal net was **-9.20130 BNFCR**.

| Total time from entry | With original protective stop | Without stops — diagnostic only |
|---|---:|---:|
| 30 minutes | -14.108 | -15.200 |
| 1 hour | -4.439 | -1.077 |
| 2 hours | -8.183 | +1.785 |
| 4 hours | -0.855 | +3.061 |
| 8 hours | -3.792 | -3.962 |

Four hours improved this retrospective sample but did not make the protected
scenario profitable. Removing stops produces attractive numbers in some columns
while exposing positions to adverse moves. It is not an acceptable live change.
These are independent trade counterfactuals: overlapping extended positions may
exceed available capital. They are **not** portfolio returns or a causal estimate
of what the account would have earned. Horizons round up to a five-minute close.
Entry-bar stop extremes may occur before the actual fill, making that bar's stop
test conservative. The original entry fee is retained; modeled exit fee is 5bps,
exit spread/slippage 7bps, plus an adverse funding reserve of 1bp per hour.

## Independent momentum portfolio experiment

Training: October 18–31 UTC. Validation: November 1–7 UTC. Public Binance
five-minute futures candles cover 19 fixed liquid coins, with prior warmup.
No open position or label crosses the split. Training ranks the 24 prespecified
variants; validation results are reported without selecting another winner.

The rule uses each coin's own completed observations:

- **A / long:** established upward EMA trend, matching one-hour momentum and
  flow, and adequate one-hour path efficiency.
- **B / wait:** mixed, weak, costly or incomplete inputs.
- **C / short:** the symmetric downward case.

Five-minute entries also retain the existing completed-candle slope checks.
Rank eligible coins by `abs(one-hour return) / 15-minute ATR fraction × one-hour
path efficiency`, select up to ten, and allow up to four concurrent positions.
The experiment uses 10% equity margin per position, 2x leverage, 40 entries/day,
four entries/coin/day, a 30-minute symbol cooldown, risk caps and a daily loss
lock. No BTC/ETH directional gate, AI, opposite strategy or profit runner is used.

Variants compare 5m/15m signals; 1/2/4/8-hour caps; and single-candle reversal,
two-candle reversal, or stop/time-only exits. All retain initial and tightening
ATR stops. Execution is at the next bar open; entry-bar stop hits count.

**All 24 variants lost in both training and validation under modeled costs.**
Five-minute validation returns ranged from -17.36% to -10.03%; fifteen-minute
returns ranged from -15.52% to -6.51%. The training leader was still a loser:
5m, eight-hour cap, stop/time-only exits, -17.42% training and -10.03% validation.

| Training leader, validation only | Return | Conservative drawdown |
|---|---:|---:|
| 2x, base costs | -10.03% | 10.74% |
| 4x, same margin allocation | -19.36% | 20.63% |
| 2x, doubled modeled costs | -19.22% | 19.37% |
| 4x, doubled modeled costs | -36.29% | 36.61% |

Base costs are 5bps fee and 7bps spread/slippage per side, plus an adverse
funding reserve of 1bp/hour. These are assumptions, not reconstructed fills or
historical funding. Drawdown marks concurrent positions at their adverse candle
extremes; simultaneity is unknown. Historical top-100 membership, delistings,
lot sizes, order books, liquidation and all live risk-lock semantics are not
replayed. The fixed universe and retrospective design limit generalization.

Fees and funding scale with position notional. Increasing leverage at unchanged
notional does not improve the strategy's price PnL; increasing notional at fixed
margin magnifies gains, losses and costs. See [Binance's futures fee explanation](https://www.binance.com/en/support/faq/detail/98488a516eb84e3eb34605683dffd554).

## Delivered change and operation

LIVE stays at 2x and switches from `auto-active` to strict `auto`: no unvalidated
probe entries. Existing positions, stops, financial locks, loss history and
activity reservations are preserved. This does not assert that the existing
strict strategy has proven future profitability; it requires its existing
historical evidence checks before opening a position.

`config/momentum-paper.yaml` implements five-minute A/B/C selection, ten watchlist
slots, a four-hour ceiling and two completed opposite EMA observations for trend
exits. Stops and time limits act immediately. This is an illustrative forward
PAPER experiment, not a historically selected profitable configuration. Its
default position sizing and risk limits are substantially smaller than the
portfolio screening assumptions above. Production defaults remain unchanged.
The shared emergency BTC crisis protection remains; ordinary direction selection
does not require BTC/ETH agreement, correlation or a broad-market regime.

The profile refuses LIVE and TESTNET settings and cannot mix AI, confidence exits
or automatic/opposite routing. Existing positions without its version marker keep
their original exits. The separate `paperfrog-momentum-paper.service` loads explicit
safe defaults and its preset, ignoring production credentials and environment
settings. It uses `state-momentum-paper/`, separate public history/report folders,
and loopback port 8082. It has no Telegram or private exchange credentials. The
production process keeps its configured port (8081 on this server) and its existing ledger. A PAPER restart with
open simulated positions retains the existing conservative manual-pause behavior.

Run the isolated experiment with `uv run --no-sync python scripts/run_momentum_paper.py`.
The two research scripts never submit orders. Reproduce the public portion with:

```bash
uv run --no-sync python scripts/fetch_momentum_history.py \
  --folder reports/holding-momentum-20261108/history \
  --start-ms 1792080000000 --end-ms 1794164400000
uv run --no-sync python scripts/study_holding_momentum.py \
  --folder reports/holding-momentum-20261108/history \
  --trades reports/holding-momentum-20261108/trades.json \
  --output reports/holding-momentum-20261108/results.json
```

The private allowlisted trade snapshot stays untracked. The local result includes
public-history SHA-256 hashes and every variant's aggregate metrics. Software tests
check implementation and safety behavior; they are not evidence of profitability.
