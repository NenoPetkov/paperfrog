-- Canonical runtime schema is applied transactionally and idempotently by Store.migrate().
-- Version 1: kv, audit, events, entries, orders, fills; SQLite WAL + synchronous FULL.
-- Do not manually edit state while a service owns its process lock.
PRAGMA user_version = 1;
