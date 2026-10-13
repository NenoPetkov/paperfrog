# Data contracts

All timestamps are UTC milliseconds. Closed kline timestamps are exchange close time +1ms, so a completed fifteen-minute bar ends on a 900000ms boundary. Original open time and normalized UTC time are retained. Snapshot quotes retain their exchange event timestamp and collection timestamp. Raw Parquet is immutable, content-addressed, and stored under `data/raw/{kind}/{symbol}/{date}/`. The lake is bound to its futures API environment to prevent mixing demo and production observations.

Kinds collected: `spot_1m`, `spot_15m`, `perp_1m`, `perp_15m`, `funding`, `open_interest`, `snapshots`; optional stream recorder captures aggregate trades, mark updates, liquidation snapshots and partial depth events. Event hashes preserve multiple events within a millisecond. Stream sequence breaks are recorded. Partial depth snapshots are diagnostics, not a reconstructed full depth book.

`data backfill-oi` imports free daily Binance USD-M metrics ZIPs after checking the
published SHA-256 checksum, expected filename, symbol, chronology and UTC day. Zero,
negative or nonfinite OI/notional observations are excluded and counted, without
imputation. Entire rejected/missing days remain gaps. Source is
`binance_verified_metrics_archive`; this importer binds to the production futures lake.
Forward REST OI observations retain their own exchange timestamp separately from books.

Core feature inputs require `timestamp_ms`, actual `perp_mid`/`spot_mid`, last observed `funding_rate`, `open_interest`, each market's quote volume and taker-buy quote volume, `bid`/`ask`. Backward joins bound OI age to 15 minutes, funding to 12 hours and quotes to 60 seconds. No long-gap forward fill occurs. Missing inputs remain NaN and block models. Funding persistence requires complete minute windows.

## Universe input JSON

An array of point-in-time records, for example this **schema illustration**, not live evidence:

```json
[{
  "symbol": "SOLUSDT", "base_asset": "SOL", "observed_ms": 1788220800000,
  "status": "TRADING", "contract_type": "PERPETUAL", "spot_match": true,
  "valid_history_days": 180, "gap_fraction": 0,
  "perp_quote_median_30d": 20000000, "spot_quote_median_30d": 20000000,
  "oi_median": 10000000, "spread_median": 0.001, "depth_usdt": 1000000
}]
```

The manifest preparer must derive these measurements solely from records available at `observed_ms`. Selection rejects future observations and saves the entire evidence/reason set. Unknown delisting/status history must not be retrospectively marked healthy. This release does not recover a historical exchange listing/status archive automatically.

Universe history defaults to 180 days. PAPER research may configure `MIN_HISTORY_DAYS=90`;
testnet/live retain a minimum of 180. Universe snapshots record the configured minimum,
and a cached shorter-history snapshot is not reused when the required history increases.
`valid_history_days` must reflect measured valid coverage, including required quote/OI
inputs; candle history alone does not prove complete strategy history.

## Training/replay panel

One row per `(timestamp_ms,symbol)`, including BTC/ETH hedge observations. Include computed feature columns, actual quote/depth sizes, mark, `in_universe`, `universe_asof_ms`, `universe_expires_ms`, and `funding_settlement` booleans at actual settlement timestamps. There must be sufficient trailing history for Ridge and core features. Model dataset construction takes one confirmed episode and its complete future four-hour path. Funding for all three legs uses actual settlement rows.

ML replay additionally requires OOS predictions joined **only** by candidate timestamp/symbol: `probability`, `predicted_return`, `prediction_std`, `baseline_probability`, `out_of_sample`. Never mark in-sample predictions OOS. The honest absence of historical event classifications means no historical LLM veto is included.

Exchange filters input is a JSON map of symbol to `step`, `tick`, `min_quantity`, `max_quantity`, `min_notional` as decimal strings. Retain the actual exchange snapshot and its validity period. The initial replay accepts one filter set per run; split runs or extend the event schema for filter changes before claiming long-span historical executability.

`model promote` requires a shadow review JSON with the exact model hash and true reviewed fields documented in LIVE_READINESS.md. Readiness evidence is a separate local review file. None is bundled with passing defaults.
