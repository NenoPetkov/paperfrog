"""Run synthetic execution mechanics. Results are NOT strategy validation."""

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from paperfrog.backtest.engine import replay
from paperfrog.domain.types import Filters
from paperfrog.settings import Settings

spec = importlib.util.spec_from_file_location("replay_fixture", "tests/fixtures/replay.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
folder = Path("reports") / ("synthetic-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S"))
filters = {
    s: Filters(Decimal(".001"), Decimal(".01"), Decimal(".001"), Decimal("1000000"), Decimal("5"))
    for s in ("SOLUSDT", "BTCUSDT", "ETHUSDT")
}
result = asyncio.run(
    replay(module.replay_panel(), Settings(_env_file=None), folder, filters, strategy="rules")
)
print(
    json.dumps(
        {"synthetic_mechanics_only": True, "report": str(folder), "metrics": result}, indent=2
    )
)
