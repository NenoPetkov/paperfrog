# Feature registry — v1

All rolling windows include only completed observations at or before the feature timestamp. Missing history gives NaN; missing mandatory model inputs reject entry. No future imputation or centered rolling windows are used. Winsorization (0.5%/99.5%), center and scale are fitted on the training partition only. Robust z is `(x - rolling median)/(1.4826 * rolling MAD)`; zero MAD gives NaN.

| Feature or family | Definition / units | Lookback | Model input? |
|---|---|---|---|
| basis | perp midpoint / spot midpoint −1; fraction | current | Yes |
| basis_z_7d/30d/90d | Robust z of basis; unitless | 7/30/90 days, 96 bars/day | 30d only |
| basis_percentile | Rolling rank / observations | 30 days | No |
| basis_distance | Basis minus rolling median; fraction | 30 days | No |
| basis_slope_15m/30m/1h/4h | Basis difference / bar count; fraction/bar | 1/2/4/16 bars | 1h only |
| basis_acceleration | Difference of one-bar basis changes | 3 bars | No |
| funding_rate | Last observed settlement rate; fraction/event | Most recent, max age 12h | Yes |
| funding_z / funding_percentile | Robust z / rank of last-rate sampled at bars | 30 days | z only |
| funding_sum_8h/24h/72h | Sum of actual settled funding rates; fraction | Actual rolling hours | No |
| funding_events_24h | Actual settlement count | 24 hours | No |
| funding_interval_hours | Difference of two known settlement timestamps | Two completed settlements | No |
| time_since_funding_minutes | Current bar time − most recent settlement | Current | No |
| time_to_next_funding_minutes | Published next timestamp − current time | Current known snapshot | No |
| sign_alignment | Basis and funding share a nonzero sign; 0/1 | Current | Episode gate |
| funding_persistence | abs(after median)/max(abs(before median),1e−10) | F−10…−2m, F+5…+20m | Yes |
| funding_efficiency | Reduction in absolute basis / abs(funding) | Same settlement windows | No |
| oi_change_15m/30m/1h/4h/12h/24h | OI percent change; fraction | 1/2/4/16/48/96 bars | 30m only |
| oi_z_* | Robust z of corresponding OI change | 30 days | 4h only |
| oi_price_interaction | 1h OI change × 1h return | 1h | No |
| oi_per_spot_volume / oi_per_perp_volume | Contract OI / quote volume | Current | No |
| volume_ratio / volume_ratio_change | Perp quote volume / spot quote volume; first difference | Current / 2 bars | No |
| volume_ratio_z | Robust z of quote volume ratio | 30 days | Yes |
| perp_imbalance / spot_imbalance | 2×taker buy quote / total quote −1 | Completed bar | Both |
| *_imbalance_2/4/16 | Mean imbalance | 2/4/16 bars | No |
| flow_divergence | Perp imbalance minus spot imbalance | Completed bar | No |
| return_15m/30m/1h/4h/12h/24h | Midpoint percent change | Matching trailing interval | 15m / 1h |
| volatility | Standard deviation of 15m returns | 96 bars | Yes |
| downside_volatility | Std of min(return,0) | 96 bars | No |
| volatility_of_volatility | Std of rolling volatility | 96 bars | No |
| abnormal_volume | Robust z of quote volume | 30 days | No |
| spread | (ask−bid)/mid; fraction | Current | Yes |
| spread_relative | Spread / rolling median spread | 30 days | No |
| amihud | abs(return) / quote volume | Completed bar | No |
| basis_change_sigma | Std of one-bar basis changes | 96 bars | Exit diagnostic |
| liquid | Spread ≤.2%, both quote volumes ≥10000 | Completed bar | Episode/label gate |

The exact ordered 15-column model schema and its hash are in `features/engineering.py`. Feature-prefix tests mutate all future rows and confirm past features are unchanged. Settlement-window tests enforce F+20m availability.

This release does not claim implementation of every exploratory feature listed in the original brief. Residual-return regressions are used for hedge/label volatility; five-minute returns, half-life/persistence streaks, full cross-sectional regime panels, lead/lag statistics, high/low failure refinements, liquidation z-scores and 10/25/50bp depth measures are not trained model inputs. They require additional validated history and research before promotion. Their absence is explicit in artifacts/readiness documentation.
