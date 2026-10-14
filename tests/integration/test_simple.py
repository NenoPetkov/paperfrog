from decimal import Decimal

import httpx
import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from pydantic import ValidationError

from paperfrog.domain.types import Quote, SafeError, State
from paperfrog.settings import Settings
from paperfrog.simple.backtest import replay
from paperfrog.simple.data import funding, normalize
from paperfrog.simple.engine import Engine
from paperfrog.simple.service import SimpleService
from paperfrog.simple.strategy import (
    ACTIVE_RULES,
    BAR_MS,
    RULES,
    choose,
    entry,
    execution_rules,
    exit_reason,
    indicators,
    tighten_stop,
)
from paperfrog.storage.database import Store
from paperfrog.telegram.control import Controls, Telegram


def source(n=360, side=1):
    close = 100 * np.exp(side * np.arange(n) * 0.0025)
    opened = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "timestamp_ms": (np.arange(n) + 1) * BAR_MS,
            "open": opened,
            "high": np.maximum(opened, close) * 1.002,
            "low": np.minimum(opened, close) * 0.998,
            "close": close,
            "quote_volume": 1_000_000.0,
            "taker_buy_quote": 700_000.0 if side > 0 else 300_000.0,
        }
    )


def row(now_ms=200 * BAR_MS, direction=1):
    return {
        "timestamp_ms": now_ms,
        "close": 100.0,
        "trend_atr": direction * 2.0,
        "prior_high": 99.0 if direction > 0 else 101.0,
        "prior_low": 101.0 if direction < 0 else 99.0,
        "atr": 0.8,
        "atr_fraction": 0.008,
        "flow": direction * 0.4,
        "daily_volume": 100_000_000,
        "score": 10.0,
    }


def quote(symbol="SOLUSDT", price=100, now_ms=200 * BAR_MS):
    return Quote(
        symbol,
        Decimal(str(price * 0.9995)),
        Decimal(str(price * 1.0005)),
        Decimal(100000),
        Decimal(100000),
        Decimal(str(price)),
        now_ms,
    )


def test_ten_coin_watchlist_preserves_eligibility_and_position_cap(settings):
    s = Settings(_env_file=None, **{**settings.public(), "simple_selected_count": 10})
    rows = {f"COIN{i:03}USDT": {**row(), "score": float(i)} for i in range(100)}
    rows["COIN099USDT"]["daily_volume"] = 1
    rows["COIN098USDT"]["atr_fraction"] = float("nan")
    assert choose(rows, s) == [f"COIN{i:03}USDT" for i in range(97, 87, -1)]
    assert len(choose(rows, settings)) == 2
    assert s.max_concurrent_strategy_trades == 2
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**settings.public(), "simple_selected_count": 11})


async def engine_fixture(settings, store, filters):
    engine = Engine(settings, store, filters)
    await engine.restore()
    store.put("simple_selected", ["SOLUSDT"])
    await engine.quote(quote(), 200 * BAR_MS)
    await engine.resume(200 * BAR_MS)
    return engine


def test_indicators_and_selection_cannot_see_future():
    x = source()
    earlier = indicators(x.iloc[:250])
    selected = choose(
        {"SOLUSDT": earlier.iloc[-1].to_dict()},
        __import__("paperfrog.settings", fromlist=["Settings"]).Settings(_env_file=None),
    )
    x.loc[250:, ["open", "high", "low", "close"]] *= 100
    assert_frame_equal(earlier, indicators(x).iloc[:250])
    assert selected == ["SOLUSDT"]


@pytest.mark.parametrize("direction", [1, -1])
def test_entry_requires_direction_and_confirming_flow(direction):
    x = row(direction=direction)
    assert entry(x, 0.001)[0] == direction
    x["flow"] *= -1
    assert entry(x, 0.001)[0] == 0


@pytest.mark.parametrize("direction", [1, -1])
def test_active_entries_accept_weaker_alignment_but_require_confirming_flow(direction):
    x = row(direction=direction)
    x.update(trend_atr=direction * 0.45, flow=direction * 0.04)
    assert entry(x, 0.001, rules=RULES)[0] == 0
    assert entry(x, 0.001, rules=ACTIVE_RULES)[0] == direction
    x["flow"] *= -1
    assert entry(x, 0.001, rules=ACTIVE_RULES)[0] == 0
    x["flow"] = direction * 0.02
    assert entry(x, 0.001, rules=ACTIVE_RULES)[0] == 0


def test_active_profile_preserves_defaults_and_protective_rules(settings):
    assert settings.trading_mode == "paper"
    assert settings.simple_entry_profile == "standard"
    assert execution_rules(settings) == RULES
    active = settings.model_copy(update={"simple_entry_profile": "active"})
    assert execution_rules(active) == ACTIVE_RULES
    changed = {key for key, value in RULES.public().items() if ACTIVE_RULES.public()[key] != value}
    assert changed == {"version", "breakout_bars", "minimum_trend_atr", "minimum_flow"}
    for mode in ("reverse", "failed_breakout"):
        from paperfrog.settings import Settings

        with pytest.raises(ValidationError, match="requires trend mode"):
            Settings(_env_file=None, simple_entry_profile="active", simple_entry_mode=mode)
        with pytest.raises(SafeError, match="requires trend mode"):
            execution_rules(active.model_copy(update={"simple_entry_mode": mode}))


def test_active_breakout_uses_only_completed_two_hour_history():
    raw = source()
    raw.loc[239, "high"] = raw.high.max() * 2
    standard = indicators(raw.iloc[:250], RULES)
    active = indicators(raw.iloc[:250], ACTIVE_RULES)
    assert standard.iloc[-1].close < standard.iloc[-1].prior_high
    assert active.iloc[-1].close > active.iloc[-1].prior_high
    raw.loc[250:, ["open", "high", "low", "close"]] *= 100
    assert_frame_equal(active, indicators(raw, ACTIVE_RULES).iloc[:250])


async def test_switching_entry_profile_with_open_positions_is_refused(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    active = settings.model_copy(update={"simple_entry_profile": "active"})
    with pytest.raises(SafeError, match="changing strategy rules"):
        Engine(active, store, filters)
    assert store.get("simple_entry_profile") == "standard"


def test_missing_and_extreme_data_block_selection(settings):
    x = row()
    x["atr_fraction"] = float("nan")
    assert choose({"SOLUSDT": x}, settings) == []
    x["atr_fraction"] = 0.1
    assert choose({"SOLUSDT": x}, settings) == []
    assert entry(x, 0.001)[0] == 0


def test_selection_is_bounded_and_liquidity_first(settings):
    s = settings.model_copy(update={"simple_scan_limit": 2, "simple_selected_count": 1})
    rows = {"BTCUSDT": row(), "ETHUSDT": row(), "SOLUSDT": row()}
    rows["SOLUSDT"].update(daily_volume=11_000_000, score=100)
    assert choose(rows, s) == ["BTCUSDT"]


@pytest.mark.parametrize("side", [1, -1])
def test_trailing_stop_only_tightens(side):
    p = {"side": side, "entry_price": 100, "initial_stop_distance": 1, "stop_price": 100 - side}
    better = tighten_stop(p, 100 + side * 4, 0.5)
    p["stop_price"] = better
    assert tighten_stop(p, 100 + side * 1, 2) == better


def test_exit_reasons_include_trend_stop_and_time():
    p = {"side": 1, "opened_ms": 0, "stop_price": 99}
    assert "stop" in exit_reason(p, 98, BAR_MS, row())
    assert "reversed" in exit_reason(p, 100, BAR_MS, row(direction=-1))
    assert "holding" in exit_reason(p, 100, 16 * BAR_MS, row())


@pytest.mark.parametrize("mode", ["testnet", "live"])
def test_simple_cannot_construct_exchange_mode(settings, tmp_path, mode):
    s = settings.model_copy(update={"trading_mode": mode})
    with pytest.raises(SafeError, match="no live approval"):
        SimpleService(s)
    assert not (s.state_dir / "simple").exists()


async def test_single_entry_keeps_risk_and_duplicate_caps(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    trade = engine.positions()["SOLUSDT"]
    reserved = store.rows("entries")[0]
    assert reserved["risk"] <= settings.paper_equity * settings.risk_per_trade
    assert reserved["gross"] <= settings.paper_equity * settings.max_target_notional
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    assert len(store.rows("entries")) == 1
    assert "LONG" in next(iter(store.get("simple_notices").values()))
    assert Decimal(trade["quantity"]) > 0


async def test_stop_fill_reconciles_and_pnl_matches_cash(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    await engine.quote(quote(price=90, now_ms=201 * BAR_MS), 201 * BAR_MS)
    assert not engine.positions()
    assert await engine.coordinator.reconcile()
    record = store.get("simple_closed")[0]
    assert record["pnl"] < 0 and "stop" in record["reason"]
    assert float(engine.broker.equity()) == pytest.approx(settings.paper_equity + record["pnl"])
    assert store.rows("entries")[0]["closed"] == 1


async def test_funding_is_real_deduplicated_and_never_credits_entry_boundary(
    settings, store, filters
):
    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    engine.funding("SOLUSDT", 200 * BAR_MS, Decimal("-.1"), Decimal(100))
    assert engine.positions()["SOLUSDT"]["funding"] == 0
    engine.funding("SOLUSDT", 201 * BAR_MS, Decimal(".001"), Decimal(100))
    once = engine.positions()["SOLUSDT"]["funding"]
    engine.funding("SOLUSDT", 201 * BAR_MS, Decimal(".001"), Decimal(100))
    assert engine.positions()["SOLUSDT"]["funding"] == once < 0
    await engine.quote(quote(now_ms=202 * BAR_MS), 202 * BAR_MS)
    await engine.close("SOLUSDT", 202 * BAR_MS, "test exit")
    assert float(engine.broker.equity()) == pytest.approx(
        settings.paper_equity + store.get("simple_closed")[0]["pnl"]
    )


async def test_restart_restores_protection_and_pauses(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    restarted = Engine(settings, store, filters)
    await restarted.restore()
    assert store.get("state") == State.PAUSED
    assert store.get("paper_account")["stops"]
    assert restarted.positions() == engine.positions()


async def test_pause_blocks_entries_but_exits_continue(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    store.put("state", State.PAUSED)
    store.put("manual_lock", True)
    await engine.quote(quote(now_ms=201 * BAR_MS), 201 * BAR_MS)
    await engine.monitor(201 * BAR_MS, {"SOLUSDT": row(direction=-1)})
    assert not engine.positions()
    assert store.get("manual_lock")


async def test_single_entry_point_rejects_nonpaper_broker_or_mode(
    coordinator, settings, now, health
):
    from paperfrog.domain.types import Order
    from paperfrog.risk.engine import Proposal

    p = Proposal("test", "SOLUSDT", 10000, 100, 100, 2, 0.01, 0.01, 1, 0.001)
    order = Order(
        client_id="pf-test", symbol="SOLUSDT", side=1, quantity=Decimal(1), limit_price=Decimal(100)
    )
    coordinator.risk.s = settings.model_copy(update={"trading_mode": "testnet"})
    with pytest.raises(SafeError, match="PAPER-only"):
        await coordinator.enter_single_paper(p, order, health, now, 0.01)
    with pytest.raises(SafeError, match="PAPER-only"):
        await coordinator._enter(p, [order], health, now, 0.01, single_paper=True)
    with pytest.raises(SafeError, match="paired entry"):
        await coordinator.enter(p, [order], health, now, 0.01)


@pytest.mark.parametrize("profile", ["standard", "active"])
async def test_backtest_is_chronological_and_uses_same_engine(settings, tmp_path, profile):
    settings = settings.model_copy(update={"simple_entry_profile": profile})
    rules = execution_rules(settings)
    frames = {
        "BTCUSDT": indicators(source(), rules),
        "SOLUSDT": indicators(source(side=-1), rules),
    }
    fund = {
        symbol: pd.DataFrame({"timestamp_ms": [0], "funding_rate": [0.0], "mark": [100]})
        for symbol in frames
    }
    path = tmp_path / "replay"
    result = await replay(frames, fund, settings, 192 * BAR_MS, 360 * BAR_MS, path)
    assert result["strategy"] == rules.version
    assert result["entry_profile"] == profile
    trades = pd.read_parquet(path / "trades.parquet")
    assert result["trades"] > 0 and not result["live_permitted"]
    assert (trades.entry_ms >= 193 * BAR_MS).all()
    ledger = Store(path / "paper-risk.sqlite")
    entries = ledger.rows("entries", 10000)
    by_day = pd.DataFrame(entries).groupby("day").size()
    by_symbol = pd.DataFrame(entries).groupby(["day", "symbol"]).size()
    assert by_day.max() <= settings.max_strategy_entries_per_day
    assert by_symbol.max() <= settings.max_entries_per_symbol_per_day
    assert result["ending_equity"] == pytest.approx(settings.paper_equity + trades.pnl.sum())


async def test_delay_stress_really_delays_entries(settings, tmp_path):
    frames = {"BTCUSDT": indicators(source()), "SOLUSDT": indicators(source(side=-1))}
    fund = {
        symbol: pd.DataFrame({"timestamp_ms": [0], "funding_rate": [0.0], "mark": [100]})
        for symbol in frames
    }
    for name, delay in (("base", 1), ("delay", 2)):
        await replay(
            frames, fund, settings, 192 * BAR_MS, 250 * BAR_MS, tmp_path / name, delay_bars=delay
        )
    a = pd.read_parquet(tmp_path / "base" / "trades.parquet")
    b = pd.read_parquet(tmp_path / "delay" / "trades.parquet")
    assert b.iloc[0].entry_ms == a.iloc[0].entry_ms + BAR_MS


async def test_cash_stays_flat(settings, tmp_path):
    frames = {"BTCUSDT": indicators(source())}
    fund = {"BTCUSDT": pd.DataFrame({"timestamp_ms": [0], "funding_rate": [0], "mark": [100]})}
    result = await replay(
        frames, fund, settings, 192 * BAR_MS, 250 * BAR_MS, tmp_path / "cash", cash_only=True
    )
    assert result["net_return"] == 0 and result["trades"] == 0


async def test_notification_failed_send_can_retry(settings, store):
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(503 if len(attempts) == 1 else 200, json={"ok": True, "result": {}})

    s = settings.model_copy(update={"telegram_allowed_chat_ids": (2,)})
    controls = Controls(s, store, None, None, None)
    telegram = Telegram(
        s, store, controls, httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    with pytest.raises(SafeError):
        await telegram.notify("entry", "long explanation")
    await telegram.notify("entry", "long explanation")
    assert len(attempts) == 2
    await telegram.client.aclose()


async def test_no_funding_event_between_settlements_is_allowed(settings):
    class Adapter:
        async def request(self, *args, **kwargs):
            return []

    from paperfrog.data.lake import Lake

    assert (
        await funding(Adapter(), Lake(settings.data_dir), "SOLUSDT", 100, 200, allow_empty=True)
        == 0
    )
    with pytest.raises(SafeError, match="unavailable"):
        await funding(Adapter(), Lake(settings.data_dir), "SOLUSDT", 100, 200)


def test_normalize_excludes_the_open_candle():
    rows = [
        [0, "100", "102", "99", "101", "1", BAR_MS - 1, "1000", 0, 0, "700"],
        [BAR_MS, "101", "102", "99", "101", "1", 2 * BAR_MS - 1, "1000", 0, 0, "700"],
    ]
    assert len(normalize(rows, BAR_MS)) == 1


async def test_entry_cannot_trade_an_unselected_coin(settings, store, filters):
    engine = await engine_fixture(settings, store, filters)
    store.put("simple_selected", ["BTCUSDT"])
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    assert not engine.positions() and not store.rows("entries")


def test_simple_cannot_use_a_legacy_live_arm(settings):
    from paperfrog.reporting.readiness import arm, verify_arm

    s = settings.model_copy(update={"trading_mode": "live", "live_trading_enabled": True})
    with pytest.raises(SafeError, match="no exchange execution approval"):
        arm(s, "legacy-model", "ARM LIVE TRADING")
    with pytest.raises(SafeError, match="no exchange execution approval"):
        verify_arm(s, "legacy-model")
    assert not (settings.state_dir / "live-arm.json").exists()


def test_finite_window_indicators_match_runtime_refresh():
    x = source(1000)
    complete = indicators(x).iloc[-1]
    refreshed = indicators(x.iloc[-288:]).iloc[-1]
    for name in ("fast", "slow", "score", "atr", "flow", "prior_high", "prior_low"):
        assert complete[name] == pytest.approx(refreshed[name])


async def test_local_flatten_uses_simple_ledger_and_keeps_entries_paused(
    settings, store, filters, monkeypatch
):
    import paperfrog.simple.service as service_module

    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    service = SimpleService(settings)
    service.store = store
    service.engine = engine
    service.rows = {"SOLUSDT": row()}
    service.selected = ["SOLUSDT"]
    service.crisis = False
    store.put("simple_selected", ["SOLUSDT"])
    store.put("local_flatten_request", "local-test")
    monkeypatch.setattr(service_module.time, "time", lambda: 200 * BAR_MS / 1000)

    async def request(method, path, params):
        if path.endswith("bookTicker"):
            return {
                "bidPrice": "99.95",
                "askPrice": "100.05",
                "bidQty": "100000",
                "askQty": "100000",
                "time": 200 * BAR_MS,
            }
        return {"markPrice": "100", "time": 200 * BAR_MS}

    async def sleep(seconds):
        service.stop.set()

    monkeypatch.setattr(service.exchange, "request", request)
    monkeypatch.setattr(service_module.asyncio, "sleep", sleep)
    await service.monitor()
    assert not engine.positions()
    assert store.get("local_flatten_request") is None
    assert store.get("manual_lock") and store.get("state") == State.PAUSED
    assert store.get("simple_closed")[0]["reason"] == "local emergency flatten requested"
    await service.exchange.close()


async def test_exchange_clock_offset_accepts_valid_quotes_but_rejects_future_data(
    settings, store, filters, monkeypatch
):
    import paperfrog.simple.service as service_module

    engine = await engine_fixture(settings, store, filters)
    service = SimpleService(settings)
    service.exchange.offset_ms = 348
    monkeypatch.setattr(service_module.time, "time", lambda: 200 * BAR_MS / 1000)
    ahead = quote(now_ms=200 * BAR_MS + 200)
    with pytest.raises(SafeError, match="future dated"):
        await engine.quote(ahead, 200 * BAR_MS)
    await engine.quote(ahead, service.now_ms())
    assert engine.health(service.now_ms()).market_age == pytest.approx(0.148)
    with pytest.raises(SafeError, match="future dated"):
        await engine.quote(quote(now_ms=service.now_ms() + 1), service.now_ms())
    await service.exchange.close()


@pytest.mark.parametrize("direction", [1, -1])
def test_reversal_changes_only_eligible_signal_direction(direction):
    x = row(direction=direction)
    original, _ = entry(x, 0.001, "trend")
    reversed_side, explanation = entry(x, 0.001, "reverse")
    assert reversed_side == -original
    assert "opposite side" in explanation and "unproven" in explanation
    x["flow"] = 0
    assert entry(x, 0.001, "reverse")[0] == 0
    x["atr_fraction"] = 0.001
    assert entry(x, 0.001, "reverse")[0] == 0


def failed_breakout_frame(direction):
    x = source(221, direction)
    before = indicators(x.iloc[:220]).iloc[-1]
    level = before.prior_high if direction > 0 else before.prior_low
    x.loc[220, "close"] = level * (0.995 if direction > 0 else 1.005)
    x.loc[220, "high"] = max(x.loc[220, "open"], x.loc[220, "close"]) * 1.002
    x.loc[220, "low"] = min(x.loc[220, "open"], x.loc[220, "close"]) * 0.998
    x.loc[220, "taker_buy_quote"] = 300000 if direction > 0 else 700000
    return indicators(x)


@pytest.mark.parametrize("direction", [1, -1])
def test_failed_breakout_waits_for_real_rejection(direction):
    x = failed_breakout_frame(direction)
    assert entry(x.iloc[-2].to_dict(), 0.001, "failed_breakout")[0] == 0
    assert entry(x.iloc[-1].to_dict(), 0.001, "failed_breakout")[0] == -direction
    raw = source(221, direction)
    assert entry(indicators(raw).iloc[-1].to_dict(), 0.001, "failed_breakout")[0] == 0


def test_reversed_position_is_not_closed_merely_because_it_is_countertrend():
    p = {"side": -1, "entry_mode": "reverse", "opened_ms": 0, "stop_price": 105}
    assert exit_reason(p, 100, BAR_MS, row(direction=1)) is None
    assert "original breakout trend ended" in exit_reason(p, 100, BAR_MS, row(direction=-1))


def test_failed_breakout_exit_uses_invalidation_level():
    p = {
        "side": -1,
        "entry_mode": "failed_breakout",
        "opened_ms": 0,
        "stop_price": 105,
        "rejection_level": 101,
    }
    x = row(direction=1)
    assert exit_reason(p, 100, BAR_MS, x) is None
    x["close"] = 102
    assert "thesis invalidated" in exit_reason(p, 100, BAR_MS, x)


async def test_mode_changes_preserve_risk_history_and_refuse_open_positions(
    settings, store, filters
):
    engine = await engine_fixture(settings, store, filters)
    await engine.enter("SOLUSDT", row(), 200 * BAR_MS)
    s = settings.model_copy(update={"simple_entry_mode": "reverse"})
    with pytest.raises(SafeError, match="close existing PAPER positions"):
        Engine(s, store, filters)
    await engine.close("SOLUSDT", 200 * BAR_MS, "test mode change")
    before = store.get("anchors"), store.get("consecutive_losses"), store.rows("entries")
    changed = Engine(s, store, filters)
    assert changed.strategy == "simple-breakout-reverse-v1"
    assert (store.get("anchors"), store.get("consecutive_losses"), store.rows("entries")) == before


async def test_reversed_execution_short_has_same_risk_cap_and_paid_fees(settings, store, filters):
    s = settings.model_copy(update={"simple_entry_mode": "reverse"})
    engine = await engine_fixture(s, store, filters)
    await engine.enter("SOLUSDT", row(direction=1), 200 * BAR_MS)
    trade = engine.positions()["SOLUSDT"]
    assert trade["side"] == -1 and trade["entry_mode"] == "reverse"
    assert store.rows("entries")[0]["risk"] <= settings.paper_equity * settings.risk_per_trade
    await engine.quote(quote(now_ms=201 * BAR_MS), 201 * BAR_MS)
    await engine.close("SOLUSDT", 201 * BAR_MS, "flat market comparison")
    result = store.get("simple_closed")[0]
    assert result["pnl"] < 0 and result["fees"] > 0
    assert float(engine.broker.equity()) == pytest.approx(settings.paper_equity + result["pnl"])


@pytest.mark.parametrize("mode", ["reverse", "failed_breakout"])
async def test_new_modes_cannot_authorize_exchange_execution(settings, tmp_path, mode):
    s = settings.model_copy(
        update={"simple_entry_mode": mode, "trading_mode": "live", "live_trading_enabled": True}
    )
    with pytest.raises(SafeError, match="no live approval"):
        SimpleService(s)
    assert not (settings.state_dir / "simple").exists()
