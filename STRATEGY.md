# Funding feedback breakdown

Basis is `(perpetual midpoint / matching spot midpoint) - 1`. The core episode requires |30-day robust basis z| ≥ 2.5, |funding z| ≥ 2, aligned signs, OI-growth z ≥ 1.25 or volume-ratio z ≥ 1.5, persistence ≥ .65, liquidity and active dated universe membership. It expires on normalization below |z| 1, 24 hours, invalid membership or material event.

Persistence uses minute observations F−10m…F−2m and F+5m…F+20m; it is not available before F+20m. No overlapping per-bar labels are created for an episode. An unwind requires at least three confirmations: contracting basis in two of three bars, declining 30-minute OI, opposite flow for two bars, failed new price extreme while basis contracts.

Positive crowding produces a short alt / long BTC and ETH pair; negative crowding reverses these sides. Trailing Ridge coefficients are capped and rejected for instability. Quantitative entry requires calibrated probability ≥ .75, positive predicted return, net edge ≥ .0035 after uncertainty and safety, and no strong baseline disagreement. The trained regressor already predicts net returns; the operational decision does not subtract the same training costs twice. This does not eliminate the need to validate actual live costs against training assumptions.

Labels use a four-hour path, volatility-scaled bounded barriers, at least 35% basis contraction and historical executability. Fees, funding, gross returns and excursions are recorded separately. Bar-resolution barriers cannot establish intrabar order of touches; tick/minute validation is necessary before live claims.

The trained feature schema is intentionally explicit and small. Extra diagnostics do not silently become model inputs. Funding normalization currently operates on the last observed settlement rate sampled at closed bars, rather than an event-count-normalized distribution; this modeling choice requires validation. Exact formulas and units are in [docs/FEATURES.md](docs/FEATURES.md).

The backtester reuses episode, sizing, risk, coordinator, paper broker and exit logic. It delays entry to a subsequent quote, models IOC/partial fills, charges all legs, processes actual funding flags, and applies account locks. The initial release permits one active pair, a conservative subset of the allowed maximum two, to avoid silently mixing shared hedge allocations.
