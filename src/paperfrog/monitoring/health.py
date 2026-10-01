from __future__ import annotations

import shutil
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from paperfrog.domain.types import State
from paperfrog.storage.database import Store


def health_app(store: Store) -> FastAPI:
    app = FastAPI(title="paperfrog", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> JSONResponse:
        payload = {
            "service": "paperfrog",
            "state": store.get("state", State.BOOTING),
            "schema": store.get("schema_version"),
            "disk_free_bytes": shutil.disk_usage(".").free,
            "heartbeat_age": time.time() - store.get("heartbeat", 0),
        }
        return JSONResponse(payload, status_code=200 if payload["heartbeat_age"] < 120 else 503)

    @app.get("/ready")
    async def ready() -> JSONResponse:
        allowed = (
            store.get("state") == State.RUNNING
            and bool(store.get("reconciled"))
            and time.time() - store.get("market_healthy_at", 0) < 60
        )
        return JSONResponse({"ready": allowed}, status_code=200 if allowed else 503)

    return app
