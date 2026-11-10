# Confirmed-short momentum entry policy

`SIMPLE_MOMENTUM_ENTRY_POLICY=confirmed_short` changes new momentum entries only.
It permits shorts whose closed 5-minute setup also has:

- Negative one-hour return and one-hour path efficiency of at least 0.3.
- Negative four-hour return and four-hour regression slope, from the latest
  completed quarter-hour context. Missing, stale or future context blocks entry.
- Fifteen-minute path efficiency of at least 0.6.
- Raw momentum strength of at least 1.5, calculated as
  `abs(return_15m) / (minute_atr / close) * efficiency_15m / (1 + age_bars / 12)`.

All existing fresh-trend, RSI, extension, trading-flow, liquidity, spread and
execution checks still apply. A new trend must have a verified transition within
60 minutes. Long positions already open retain their original protection and
exit policy; new long entries are blocked by this policy.

The installation default is `baseline`; PAPER remains the default mode. The
policy requires the base momentum profile. Selection and dispatch use the same
classifier and source/configuration binding. This release does not change
leverage, sizing caps, account loss limits, cooldowns, funding-hedge permissions,
or OpenAI configuration. Fixed ±5% gross initial-margin return brackets and
portfolio emergency exits retain their existing authority.

## Historical evidence and explicit scope

Public futures candles, mark-price candles and historical funding were replayed
with the production RiskEngine and current configured transaction costs. Each
simulation starts flat with 10,000 USDT. The liquid pool is reconstructed from
completed trailing volume among contracts in the captured current metadata.

| Window / cost assumption | Baseline | Confirmed short |
| --- | ---: | ---: |
| 10 Oct–8 Nov 2026, configured costs | −9.14% | +9.78% |
| Same window, doubled spread/slippage | −5.01% | +6.97% |
| Development: 10–29 Oct | −10.10% | +13.05% |
| Fresh account: 30 Oct–8 Nov | −2.49% | −2.89% |
| September 2026, configured costs | −21.10% | −23.79% |
| September 2026, doubled spread/slippage | not evaluated | −26.18% |

The selected full-month simulation ends at 10,978.34 USDT, with 184 closed
trades, 54.35% profitable trades, a 1.243 profit factor and 7.59% maximum sampled
drawdown. One position remains open and is marked to market. The higher-cost
simulation ends at 10,697.35 USDT with 8.17% maximum sampled drawdown.

Twenty initial entry-filter candidates were predeclared and ranked on the first
20 days. This policy ranked first. A candidate positive in both date slices
subsequently lost money under doubled friction. Twenty-four additional
volume/flow candidates also failed September. The selected policy does not
satisfy a requirement to remain positive across these periods.

On 10 November, after being told about the September loss and offered the choice
again, the operator explicitly selected **the requested month only, accepting
the failed September check**. Activation is based on that instruction, not a
claim of robust or future profitability. Previously reviewed history and
parameter selection create fitting bias. Software tests establish consistency,
not profitability.

Historical executable depth, actual spreads, fill latency, within-candle account
risk events and historical filter/metadata revisions are not reconstructed.
Current metadata cannot prove historical delisted-contract membership. Live
depth checks can reduce or reject modeled trades. The funding hedge is blocked
in the actual account and contributes no hypothetical profit to this study.

Input paths and detailed trade/equity outputs are under
`reports/fixed-roe-30d-20261109` and `reports/momentum-tuning-20261110`.
Compact evidence is retained in `docs/research/confirmed-short-20261110`.

## Reproduction and activation

Use Python 3.12 and uv. Research settings ignore environment credentials and
validate as PAPER-only; research never submits exchange orders.

```sh
uv run python -m scripts.fetch_fixed_roe_month --folder reports/fixed-roe-30d-20261109 --start 2026-10-10 --end 2026-11-09
uv run python -m scripts.replay_fixed_roe_month --folder reports/fixed-roe-30d-20261109 --stage features
uv run python -m scripts.replay_fixed_roe_month --folder reports/fixed-roe-30d-20261109 --stage inputs
uv run python -m scripts.search_momentum_month --source reports/fixed-roe-30d-20261109 --output reports/momentum-tuning-20261110 --prepare-only
uv run python -m scripts.search_momentum_month --source reports/fixed-roe-30d-20261109 --output reports/momentum-tuning-20261110
```

The reviewed server deployment policy is `momentum-filter`. It runs release
checks before shutdown, takes a consistent state backup, changes only the
reviewed new-entry policy, renews local source/account authorization, restarts
and verifies the service. It does not reset financial locks or replace existing
brackets. Pending or unresolved execution blocks activation. Telegram cannot
activate or arm this policy.
