import os

import pytest

from paperfrog.exchange.binance import BinanceAdapter
from paperfrog.settings import Settings


@pytest.mark.testnet
@pytest.mark.skipif(
    os.getenv("PAPERFROG_TESTNET_SMOKE") != "1",
    reason="explicit credentialed testnet smoke opt-in required",
)
async def test_testnet_read_only_smoke():
    s = Settings(_env_file=None, trading_mode="testnet", binance_env="testnet")
    a = BinanceAdapter(s)
    try:
        await a.sync_time()
        assert (await a.exchange_info())["symbols"]
        assert "totalMarginBalance" in await a.account()
        await a.positions()
        await a.open_orders()
        await a.open_stops()
    finally:
        await a.close()
