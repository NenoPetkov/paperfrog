import importlib.util

from paperfrog.backtest.engine import Stress, replay

spec = importlib.util.spec_from_file_location("replay_fixture", "tests/fixtures/replay.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


async def test_event_replay_complete_pair(settings, filters, tmp_path):
    panel = module.replay_panel()
    result = await replay(panel, settings, tmp_path / "replay", filters, Stress(), strategy="rules")
    assert result["trades"] >= 1
    assert result["fees"] > 0
    assert not result["live_permitted"]
    assert (tmp_path / "replay" / "equity.parquet").exists()


async def test_cash_comparator(settings, filters, tmp_path):
    panel = module.replay_panel().head(90)
    result = await replay(panel, settings, tmp_path / "cash", filters, strategy="cash")
    assert result["net_return"] == 0 and result["trades"] == 0
