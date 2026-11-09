# Fixed margin-return exits and full-pool selection

The operator requested fixed **+5% / -5% return on position margin**, explicitly
accepting the wider per-position stop. This applies to **new momentum entries**.
At the existing, verified 2x leverage, each target is a 2.5% coin-price movement
from the actual average fill. It is gross marked-price return on initial margin,
before commission and funding. Actual fills, gaps and costs can change realized
net return; the program cannot guarantee an exact realized 5% result.

## Position lifecycle

`SIMPLE_EXIT_POLICY=fixed_roe` replaces normal trend, trailing, confidence and
holding-time exits. A position between its targets stays open even if its trend
reverses, four hours elapse or entry-candle data becomes unavailable. Fresh account
and quote checks continue. Existing account/portfolio loss locks, manual flatten,
liquidation checks and execution-repair safeguards retain their authority.

The live entry places two exchange-hosted close-all conditional orders, using
mark price: STOP_MARKET at the loss threshold and TAKE_PROFIT_MARKET at the gain
threshold. They cannot create a reverse position. Both levels use the actual fill
and round away from entry, so tick precision cannot intentionally move a trigger
inside the requested gross return. Sizing reserves the stop distance, fees,
friction and extra tick allowance against existing monetary risk limits.

The entry completes only after both original orders are verified. Mutating calls
are never automatically retried. An ambiguous order is queried by its original
identity; missing or inconsistent evidence pauses entries. A confirmed stop or
take-profit is journaled once, the flat position is verified and remaining hosted
protection is cancelled. Restart checks both brackets. Protection left over from
an earlier trade cannot authorize a new entry on that symbol.

Old positions retain their recorded exits and original stops. Changing the new
entry policy does not widen an already open position's risk. The default remains
PAPER with the previous trend exit policy until locally configured and authorized.
No Telegram command can activate this LIVE policy or increase financial limits.

## Selection across up to 100 liquid coins

The runtime considers up to 100 liquid, eligible USDT perpetual crypto markets,
ranked into the scan pool by trailing quote volume. Stablecoins, excluded symbols,
recent listings and BTC are filtered before the pool is filled. ETH is also
reserved when the separate funding hedge is enabled. If fewer than 100 markets
meet the existing liquidity and metadata requirements, it reports the actual
number rather than inventing candidates.

This removes the previous 19-coin research-universe restriction for this explicitly
selected policy. Each pool member receives completed candle and risk checks. The
existing recent-trend window, five/fifteen-minute confirmation, exhaustion and
extension vetoes remain. Held symbols, cooldowns and per-coin daily limits are
removed before ranking. All remaining setups receive fresh executable bid/ask,
mark, spread and depth checks, rather than selecting ten coins first.

The ranking uses recent volatility-adjusted momentum, recent path efficiency and
trend age, discounted by estimated round-trip execution cost relative to the fixed
price target. A wide spread, inadequate visible liquidity, stale quote or rebound
rejects that candidate. One best eligible setup is selected at each completed
five-minute scan; ordinary entry limits still apply. "Best" describes this
observable score, not knowledge of the best future return.

The exact order must still match the current single winner, pool identity,
configuration, fresh candle and fresh execution quote at dispatch. An expanded
pool does not relax margin, position, frequency, exposure, loss or authorization
limits. The wider universe and changed exits have no demonstrated profitability.

## OpenAI and deployment

Production already had `SIMPLE_AI_ENABLED=false`; the local activation explicitly
retains it. Neither the entry advisor nor the daily-review OpenAI client is
created while disabled. A configured fifteen-minute interval is inert, and status
shows no active AI review interval. Historical AI usage remains in the journal.

The guarded `fixed-roe` deployment checks the release, privately backs up state,
stops writers, updates only the reviewed exit/selection/AI settings and renews
local source/configuration/account authorization. Existing risk locks, equity
anchors, cooldowns, trades and leverage are preserved. A current loss cooldown
continues until its ordinary expiry and health checks.

Focused tests cover threshold symmetry, outward rounding, no ordinary early exit,
portfolio emergencies, both hosted orders, original-identity recovery, partial
failures, winner number 100, cost/depth/cooldown rejection, full-pool integration,
exact dispatch binding and the settings migration. Passing checks establish
software behavior, not profitability.

API contract: [Binance USD-M conditional orders](https://binance.github.io/binance-connector-js/classes/_binance_derivatives-trading-usds-futures.DerivativesTradingUsdsFuturesRestAPI.TradeApi.html).
