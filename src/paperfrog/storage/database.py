from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, event, text

from paperfrog.domain.types import SafeError


class Store:
    """One local SQLite ledger; critical reservations use BEGIN IMMEDIATE."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30})

        @event.listens_for(self.engine, "connect")
        def pragmas(dbapi: Any, _: Any) -> None:
            dbapi.execute("PRAGMA journal_mode=WAL")
            dbapi.execute("PRAGMA synchronous=FULL")
            dbapi.execute("PRAGMA foreign_keys=ON")

        self.migrate()

    def migrate(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, payload TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, day TEXT NOT NULL, symbol TEXT NOT NULL, ts INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0, risk REAL NOT NULL, gross REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, intent TEXT NOT NULL, result TEXT, state TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
        ]
        with self.engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
            self.put("schema_version", 1, conn)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def get(self, key: str, default: Any = None, conn: Connection | None = None) -> Any:
        if conn is None:
            with self.engine.connect() as connection:
                return self.get(key, default, connection)
        value = conn.execute(text("SELECT value FROM kv WHERE key=:k"), {"k": key}).scalar()
        return json.loads(value) if value is not None else default

    def paper_summary(self) -> dict[str, Any]:
        """Read current account/protection without decoding terminal order/funding history."""
        with self.engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT json_extract(value,'$.cash','$.positions','$.average','$.stops') "
                    "FROM kv WHERE key='paper_account'"
                )
            ).scalar()
        if value is None:
            raise SafeError("paper account unavailable")
        fields = json.loads(value)
        if not isinstance(fields, list) or len(fields) != 4 or any(v is None for v in fields):
            raise SafeError("paper account summary incomplete")
        return dict(zip(("cash", "positions", "average", "stops"), fields, strict=True))

    def unresolved_orders(self) -> list[dict[str, Any]]:
        """Never let a large terminal history hide an older unresolved order."""
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT * FROM orders WHERE state IN ('INTENDED','UNKNOWN') ORDER BY rowid DESC"
                    )
                ).mappings()
            ]

    def put(self, key: str, value: Any, conn: Connection | None = None) -> None:
        if conn is None:
            with self.engine.begin() as connection:
                self.put(key, value, connection)
            return
        conn.execute(
            text("INSERT INTO kv VALUES (:k,:v) ON CONFLICT(key) DO UPDATE SET value=:v"),
            {"k": key, "v": json.dumps(value, allow_nan=False)},
        )

    def audit(self, kind: str, payload: Any, conn: Connection | None = None) -> None:
        if conn is None:
            with self.engine.begin() as connection:
                self.audit(kind, payload, connection)
            return
        conn.execute(
            text("INSERT INTO audit(ts,kind,payload) VALUES (:t,:k,:p)"),
            {"t": int(time.time() * 1000), "k": kind, "p": json.dumps(payload, allow_nan=False)},
        )

    def once(self, key: str, payload: Any) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text("INSERT OR IGNORE INTO events VALUES (:k,:p)"),
                {"k": key, "p": json.dumps(payload)},
            )
            return result.rowcount == 1

    def rows(self, table: str, limit: int = 100) -> list[dict[str, Any]]:
        if table not in {"orders", "fills", "entries", "audit"}:
            raise SafeError("invalid table")
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT :n"), {"n": limit}
                ).mappings()
            ]
