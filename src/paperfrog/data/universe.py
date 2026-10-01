from __future__ import annotations

from typing import Any

from paperfrog.settings import Settings
from paperfrog.storage.database import Store

STABLE = {
    "USDC",
    "USDT",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "USDE",
    "USD1",
    "BUSD",
    "USTC",
    "EUR",
    "EURI",
    "PAXG",
    "XAUT",
}


def build_universe(
    rows: list[dict[str, Any]], asof_ms: int, s: Settings, store: Store
) -> dict[str, Any]:
    previous = store.get("active_universe")
    if previous and previous["asof_ms"] <= asof_ms < previous["expires_ms"]:
        return dict(previous)
    eligible, reasons = [], {}
    for r in rows:
        symbol = r["symbol"]
        failed = []
        if r["observed_ms"] > asof_ms:
            failed.append("future information")
        if r["base_asset"] in STABLE or symbol in {"BTCUSDT", "ETHUSDT"} or symbol in s.denylist:
            failed.append("excluded asset")
        if r["status"] != "TRADING" or r["contract_type"] != "PERPETUAL" or not r["spot_match"]:
            failed.append("contract or spot status")
        if r["valid_history_days"] < s.min_history_days or r["gap_fraction"] > 0.001:
            failed.append("insufficient continuous history")
        if min(r["perp_quote_median_30d"], r["spot_quote_median_30d"]) < s.min_daily_quote_volume:
            failed.append("volume")
        if r["spread_median"] > s.max_spread or r["depth_usdt"] <= 0:
            failed.append("spread or depth missing")
        reasons[symbol] = failed
        if not failed:
            eligible.append(r)
    eligible.sort(
        key=lambda r: (
            min(r["perp_quote_median_30d"], r["spot_quote_median_30d"]),
            r.get("oi_median", 0),
            r["depth_usdt"],
        ),
        reverse=True,
    )
    selected = [r["symbol"] for r in eligible[: s.max_universe]]
    for r in eligible[s.max_universe :]:
        reasons[r["symbol"]] = ["rank outside top 25"]
    result = {
        "asof_ms": asof_ms,
        "expires_ms": asof_ms + 7 * 86400000,
        "symbols": selected,
        "reasons": reasons,
        "inputs": rows,
    }
    store.put("active_universe", result)
    store.audit("universe_snapshot", result)
    return result
