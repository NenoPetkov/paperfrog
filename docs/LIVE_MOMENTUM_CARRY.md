# LIVE momentum and fixed funding hedge

Both execution paths use the existing local LIVE authorization, account binding,
source binding, financial locks and SQLite ledger. The default configuration stays
PAPER with carry disabled. Telegram cannot enable carry or arm LIVE.

## Momentum

The locally selected [fixed margin-return policy](FIXED_ROE_100_COIN_SELECTION.md)
uses the full liquid 100-coin pool and replaces normal exits for new entries with
hosted +/-5% gross margin-return brackets. The trend-policy behavior below remains
the installation default and the recorded behavior of older positions.

The combined deployment selects the existing five-minute A/B/C `momentum`
profile, with a four-hour holding ceiling. It lowers position margin to at most
10%, retains 2x leverage, and shares the existing entry, concurrency, exposure
and loss limits with carry. BTC and ETH are reserved for carry when enabled.
No research result or software check demonstrates future profitability.

The base profile now uses `abc-momentum-v2` entry timing. The
[November 9 timing audit](MOMENTUM_ENTRY_TIMING_2026-11-09.md) explains the earlier
entry window, exhaustion veto and the distinction between historical signal
eligibility and actual execution. Existing positions retain their recorded exits.

## Funding hedge

`SIMPLE_CARRY_ENABLED=true` enables real spot/perpetual execution alongside LIVE
momentum. BTC/USDT and ETH/USDT each receive at most one fixed holding cycle;
there is no rotation, averaging down or automatic reentry. Eligibility requires
positive prior thirty-day settled funding with continuous eight-hour settlements,
a one-hour publication lag, prior-day liquidity and a futures/spot basis between
-0.1% and +0.5%. A changed funding schedule blocks new entries.

The research allocation of 18% is a ceiling. Live sizing also reserves the entire
spot leg's value plus a 3% contingency against the existing per-trade and total
open-risk limits. This deliberately allows smaller positions than the research
model; exchange minimum orders may prevent an entry. Futures collateral reserves
reduce the balance available to momentum. The service never transfers funds,
borrows spot, increases leverage or relaxes financial limits.

A fully confirmed spot purchase precedes the matching futures short. Both legs
use price-limited IOC orders. Base-asset commission or a partial spot fill causes
an unwind; it cannot authorize an unmatched short. A partial short is protected
and then unwound with the spot leg. Every request has a durable identity. After
an ambiguous response, recovery queries that original identity and never repeats
the mutation. A crash before a request can therefore require local review rather
than risking duplicate orders.

The futures leg receives a hosted stop at 10% above its fill; loss of that hedge
closes the owned spot inventory. Other exits are the 360-day declared end,
a combined loss of 3% of initial spot value, portfolio risk locks, or flatten.
Disarming new entries does not disable reductions. Held inventory is checked
against spot balances, futures positions, original fills and protection records.
Known unsellable dust is recorded, valued at zero and cannot authorize reentry.

Only the strategy's own spot profit/loss adjusts the existing futures equity
anchors; unrelated spot cash and assets do not inflate risk budgets. Funding,
settlements come from exchange income records, archived with deduplication and
bounded pagination. Futures fees and realized results are verified against actual
fills while they are queryable. A settlement gap longer than a week
requires local accounting review. Spot fills and commissions are verified against
trade records. Fees paid in third assets receive a conservative 1% turnover
charge rather than an invented precise USDT conversion.

## Account availability and operations

General API permissions do not establish that a particular spot market is
available. The service evaluates all of the exchange symbol's `permissionSets`
against the account's trading groups. On November 9, 2026, the production key had
spot/futures permission, but its trading groups did not permit BTC/USDT or
ETH/USDT spot trading. Carry consequently reports `mode: live`, `state: blocked`
and sends no orders. Momentum remains independent of this unavailable entry path.
A permitted account/market arrangement is necessary before carry can trade; the
service does not replace these markets or circumvent exchange restrictions.

`/simple` and `scripts/server_ops.py status` expose the carry status and its reason.
Use the existing guarded remote deployment script with policy `combined`, profile
`momentum`, and holding hours `4`. It requires a flat reconciled account, checks
the candidate before shutdown, backs up state and environment privately, activates
the reviewed configuration, renews local authorization and verifies fresh startup.
It also disables the separate credential-free momentum PAPER service.

API contracts: [Binance Spot REST documentation](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)
and [symbol filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md).
