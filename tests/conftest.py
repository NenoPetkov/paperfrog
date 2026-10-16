from datetime import UTC, datetime
from decimal import Decimal

import pytest

from paperfrog.domain.types import Filters, Quote, State
from paperfrog.execution.coordinator import Coordinator
from paperfrog.paper.broker import PaperBroker
from paperfrog.risk.engine import Health, RiskEngine
from paperfrog.settings import Settings
from paperfrog.storage.database import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        # Existing mechanics fixtures isolate the pre-reference strategy. BTC
        # integration tests explicitly enable and exercise the complete new gate.
        simple_btc_reference_enabled=False,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        reports_dir=tmp_path / "reports",
    )


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "state.sqlite")
    db.put("state", State.RUNNING)
    db.put("reconciled", True)
    return db


@pytest.fixture
def now():
    return datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def health():
    return Health(True, True, True, 0, 0)


@pytest.fixture
def filters():
    return {
        s: Filters(
            Decimal(".001"), Decimal(".01"), Decimal(".001"), Decimal("1000000"), Decimal("5")
        )
        for s in ("SOLUSDT", "BTCUSDT", "ETHUSDT")
    }


@pytest.fixture
async def broker(store, filters):
    b = PaperBroker(store, filters, slippage_bps=0)
    for symbol, price in [("SOLUSDT", 100), ("BTCUSDT", 50000), ("ETHUSDT", 2000)]:
        await b.quote(
            Quote(
                symbol,
                Decimal(price) - Decimal(".01"),
                Decimal(price) + Decimal(".01"),
                Decimal("10000"),
                Decimal("10000"),
                Decimal(price),
                1800000,
            )
        )
    return b


@pytest.fixture
async def coordinator(broker, settings, store, filters):
    return Coordinator(broker, store, RiskEngine(settings, store), filters)
