# Live readiness: BLOCKED

No genuine research history, approved production model, 30-day paper observation, or 7-day testnet observation was supplied or generated. There is no evidence yet that the strategy has an edge. Missing evidence is **unproven**, not a fabricated failed market backtest and not a pass.

`paperfrog report readiness` creates machine-readable JSON and a readable report and exits 2 for missing/failed gates. It checks nine OOS months, 250 strategy trades, Sharpe ≥1, profit factor ≥1.25, drawdown ≤8%, positive 2x-cost results, symbol/month concentration ≤25%, 60% positive months, calibration documentation, positive results excluding the best symbol/month, and zero material invariants violated. Operational evidence includes 30 paper days/100 trades, seven testnet days, reconciliation/duplicate/risk incident counts, slippage review, Telegram controls, emergency flatten and restart tests. Sparse trading requires longer observation, never an automatic relaxed threshold.

Additional explicit reviews cover point-in-time history, leakage and implementation. All fields are listed in `reporting/readiness.py:GATES`; missing, nonfinite or failing fields block. Evidence is a local operator attestation and must be backed by retained raw observations, ledger exports and immutable research results. Do not set `implementation_reviewed` true until limitations in IMPLEMENTATION_REPORT.md are resolved or formally accepted with live trading still disabled for unsupported scenarios.

`reports/shadow-review.json` for model promotion must contain the exact `model_hash` plus `backtest_reviewed`, `shadow_reviewed`, `baseline_compared` and `leakage_reviewed`, each true only after real review. A promoted model does not itself grant live readiness.

A readiness report is bound to the current configuration and model hash and expires after 24 hours. A local live arm additionally binds the Binance credential identity and expires after four hours by default. This is a credential-scoped account binding: the verified USD-M account response does not expose a stable UID used by this implementation. Rotating an API key invalidates the arm. Independent exchange-UID attestation remains an operational limitation.

Never repeatedly tune on the final held-out months. Paper/testnet observation and real out-of-sample validation take calendar time. Unit tests and synthetic replay cannot substitute for them.
