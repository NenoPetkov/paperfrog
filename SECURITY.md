# Security

Keep `.env` mode 0600; use environment injection or a secret manager in deployment. Never commit credentials. Settings exclude secret fields from dumps and repr. HTTP/SDK logging is suppressed, exceptions at transport boundaries use fixed messages, and structured logging redacts known secrets and sensitive field names. No API key is written to the database. Live arms contain a SHA-256 account-key binding, not the key.

Use dedicated Binance accounts/subaccounts, futures permission only, withdrawals disabled, and IP allowlisting. Do not trade manually in the bot-owned account while it runs. Unexpected positions or orders cause a stop and reconciliation. Binance account balances are operationally sensitive and only accessible in the local ledger or explicitly allowlisted Telegram status commands; they never enter OpenAI input.

Telegram checks both user and chat allowlists. Prefer private chats. Exact commands bypass LLM parsing. Forwarded/edited/stale messages are ignored. Rate limits and persisted update IDs limit replay; one-time flatten codes bind user, chat, expiry and consumption. No Telegram command enables live, changes models/leverage, raises risk or overrides hard locks.

OpenAI receives public asset identifiers only and has web search plus strict structured output, with no custom trading functions. Web content is untrusted evidence. The model is instructed to ignore embedded instructions. Refusals, malformed outputs, absent sources and unavailable models are handled as unavailable event evidence; no silent provider/model fallback exists.

The localhost health server has no mutation endpoints. Docker runs as UID 10001, drops capabilities, uses a read-only root filesystem and publishes no ports. Local processes sharing a state directory are blocked with an OS advisory lock. The process is not designed to defend against a malicious root user or an operator rewriting trusted readiness evidence.
