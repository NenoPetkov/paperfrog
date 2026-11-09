"""Chronological signal-only comparison from public candles; never place orders."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from paperfrog.simple.adaptive import enrich
from paperfrog.simple.data import normalize
from paperfrog.simple.minute import MOMENTUM_HISTORY_BARS, frame
from paperfrog.simple.momentum import classify, timing_veto
from paperfrog.simple.strategy import EARLY_MOMENTUM_RULES, RULES, indicators


def review(path: Path, start: str, end: str) -> dict[str, object]:
    contents = path.read_bytes()
    payload = json.loads(contents)
    start_ms, end_ms = (int(datetime.fromisoformat(v).timestamp() * 1000) for v in (start, end))
    if datetime.fromisoformat(start).tzinfo is None or datetime.fromisoformat(end).tzinfo is None:
        raise ValueError("Explicit timezone offsets are required")
    if end_ms <= start_ms or end_ms > payload["end_ms"]:
        raise ValueError("Comparison window exceeds available completed candles")
    legacy = replace(RULES, version="abc-momentum-v1", entry_style="trend_follow")
    symbols = {}
    for symbol, raw in payload["candles"].items():
        context = enrich(indicators(normalize(raw["15m"], payload["end_ms"]), EARLY_MOMENTUM_RULES))
        contexts = {int(r["timestamp_ms"]): r for r in context.to_dict("records")}
        observations = []
        for candle in raw["5m"]:
            boundary = int(candle[6]) + 1
            if not start_ms <= boundary <= end_ms:
                continue
            # Slice before calculating indicators: later observations cannot
            # enter an earlier decision or its 15-minute risk snapshot.
            candles = [r for r in raw["5m"] if int(r[6]) < boundary][-MOMENTUM_HISTORY_BARS:]
            if len(candles) != MOMENTUM_HISTORY_BARS:
                raise ValueError("Insufficient continuous entry warmup")
            c = contexts.get(boundary // 900_000 * 900_000)
            if c is None:
                raise ValueError("Closed risk context missing")
            row = frame(candles, c, boundary, "5m", early_momentum=True).iloc[-1].to_dict()
            old = classify(row, 0.0012, legacy)
            new = classify(row, 0.0012, EARLY_MOMENTUM_RULES)
            observations.append(
                {
                    "timestamp_ms": boundary,
                    "sofia": datetime.fromtimestamp(
                        boundary / 1000, ZoneInfo("Europe/Sofia")
                    ).isoformat(),
                    "close": row["close"],
                    "legacy_side": old[1],
                    "new_side": new[1],
                    "trend_age_bars": row["momentum_trend_age_bars"],
                    "rsi_14": row["rsi_14"],
                    "new_timing_block": timing_veto(row, EARLY_MOMENTUM_RULES),
                }
            )
        symbols[symbol] = observations
    return {
        "policy": EARLY_MOMENTUM_RULES.public(),
        "input_sha256": hashlib.sha256(contents).hexdigest(),
        "start": start,
        "end": end,
        "one_way_cost_assumption": 0.0012,
        "scope": "signal eligibility only; historical spread, selection, margin, cooldowns and fills are not reconstructed; not profitability evidence",
        "symbols": symbols,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = review(a.input, a.start, a.end)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(a.output), "input_sha256": result["input_sha256"]}))


if __name__ == "__main__":
    main()
