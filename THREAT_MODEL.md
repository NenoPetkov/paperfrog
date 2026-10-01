# Threat model

| Threat | Control | Residual risk |
|---|---|---|
| Stolen exchange credentials | No logs/serialization, dedicated key, IP allowlist, no withdrawal permission | Host compromise; operator misconfiguration |
| Telegram impersonation/replay | Dual allowlist, private chats, exact commands, update deduplication, expiring confirmation | Authorized account takeover; delivery outages |
| Web prompt injection | Public-only request, factual schema, deterministic policy, no trading tools | Misclassification or denial of service |
| Duplicate order after timeout | Durable intent, deterministic ID, query-only unresolved state | Exchange unavailability; unresolved state requires review |
| Restart erases risk history | WAL, FULL sync, persisted anchors and reservations, startup reconciliation | Disk corruption; backup operator error |
| Unknown manual account activity | Compare expected and actual positions, block new entries | Reconciliation/account stream gaps |
| Leakage or backtest overfit | Causal features, unique episodes, chronological purge/embargo, immutable artifacts | Data vendor revisions, unmodeled effects, research selection bias |
| Broken stop or partial hedge | Hosted stop, fill journal, bounded repair/unwind, emergency reduction | Gaps, exchange downtime, slippage outside model |
| Malicious model artifact | JSON models, path checks, content/schema hashes, explicit local promotion | Trusted operator compromise; library vulnerability |
| Publicly reachable service | Loopback health only; no published container ports | Host/container configuration drift |

Dependency audit results are recorded in the implementation report. Passing an audit or tests does not eliminate operational or model risk.
