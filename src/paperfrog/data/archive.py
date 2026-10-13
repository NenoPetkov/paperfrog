from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np
import pandas as pd

from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError

BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
MAX_BYTES = 16 * 1024 * 1024


def metrics_frame(payload: bytes, checksum: str, symbol: str, day: datetime) -> pd.DataFrame:
    """Read a verified daily metrics CSV without extracting archive paths."""
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise SafeError("Binance archive checksum mismatch")
    expected = f"{symbol}-metrics-{day:%Y-%m-%d}.csv"
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.namelist() != [expected] or archive.getinfo(expected).file_size > MAX_BYTES:
                raise SafeError("unexpected or oversized Binance metrics archive")
            raw = pd.read_csv(io.BytesIO(archive.read(expected)))
        required = {"create_time", "symbol", "sum_open_interest", "sum_open_interest_value"}
        if raw.empty or not required <= set(raw) or not raw.symbol.eq(symbol).all():
            raise SafeError("Binance metrics schema or symbol mismatch")
        stamps = (
            pd.to_datetime(raw.create_time, utc=True, errors="raise")
            .astype("datetime64[ms, UTC]")
            .astype("int64")
        )
        frame = pd.DataFrame(
            {
                "timestamp_ms": stamps,
                "open_interest": pd.to_numeric(raw.sum_open_interest, errors="raise"),
                "oi_notional": pd.to_numeric(raw.sum_open_interest_value, errors="raise"),
            }
        )
    except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError, OverflowError):
        raise SafeError("malformed Binance metrics archive") from None
    start = int(day.timestamp() * 1000)
    reordered = not frame.timestamp_ms.is_monotonic_increasing
    frame = frame.sort_values("timestamp_ms", kind="stable").reset_index(drop=True)
    if (
        not frame.timestamp_ms.between(start, start + 86400000 - 1).all()
        or frame.timestamp_ms.duplicated().any()
    ):
        raise SafeError("invalid Binance metrics observations")
    valid = np.isfinite(frame[["open_interest", "oi_notional"]].to_numpy()).all(axis=1)
    valid &= (frame.open_interest > 0) & (frame.oi_notional > 0)
    rejected = int((~valid).sum())
    frame = frame[valid].copy()
    if frame.empty:
        raise SafeError("invalid Binance metrics observations")
    frame.attrs["rejected_observations"] = rejected
    frame.attrs["reordered_archive"] = reordered
    return frame


async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        async with client.stream("GET", url) as response:
            if response.status_code == 404:
                return None
            if response.is_error:
                raise SafeError(f"Binance archive rejected: HTTP {response.status_code}")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_BYTES:
                    raise SafeError("Binance archive exceeds download limit")
            return bytes(chunks)
    except httpx.HTTPError:
        raise SafeError("Binance archive transport unavailable") from None


async def backfill_open_interest(
    lake: Lake,
    symbol: str,
    start_ms: int,
    end_ms: int,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if (
        not re.fullmatch(r"[A-Z0-9]{2,24}", symbol)
        or start_ms % 86400000
        or end_ms % 86400000
        or start_ms >= end_ms
        or end_ms > int(datetime.now(UTC).timestamp() * 1000)
    ):
        raise SafeError("archive range requires completed UTC days and a valid symbol")
    lake.bind_environment("https://fapi.binance.com")
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=30)
    missing, rejected, written, days, rejected_observations, reordered_archives = [], [], 0, 0, 0, 0
    day = datetime.fromtimestamp(start_ms / 1000, UTC)
    try:
        while int(day.timestamp() * 1000) < end_ms:
            filename = f"{symbol}-metrics-{day:%Y-%m-%d}.zip"
            url = f"{BASE}/{symbol}/{filename}"
            payload = await _download(http, url)
            if payload is None:
                missing.append(day.strftime("%Y-%m-%d"))
                day += timedelta(days=1)
                continue
            checksum_bytes = await _download(http, url + ".CHECKSUM")
            if checksum_bytes is None:
                raise SafeError("Binance archive checksum missing")
            try:
                fields = checksum_bytes.decode("ascii").strip().split()
                if (
                    len(fields) != 2
                    or not re.fullmatch(r"[a-fA-F0-9]{64}", fields[0])
                    or fields[1].lstrip("*") != filename
                ):
                    raise SafeError("invalid Binance archive checksum file")
            except UnicodeError:
                raise SafeError("invalid Binance archive checksum file") from None
            # Preserve the integrity check as fatal; malformed observations are quarantined
            # by day and reported rather than stopping all subsequent historical collection.
            if hashlib.sha256(payload).hexdigest() != fields[0].lower():
                raise SafeError("Binance archive checksum mismatch")
            try:
                frame = metrics_frame(payload, fields[0].lower(), symbol, day)
            except SafeError as exc:
                rejected.append({"date": day.strftime("%Y-%m-%d"), "reason": str(exc)})
                day += timedelta(days=1)
                continue
            written += lake.append(
                "open_interest", symbol, frame, "binance_verified_metrics_archive"
            )
            rejected_observations += frame.attrs.get("rejected_observations", 0)
            reordered_archives += int(frame.attrs.get("reordered_archive", False))
            days += 1
            day += timedelta(days=1)
    finally:
        if own_client:
            await http.aclose()
    return {
        "symbol": symbol,
        "verified_days": days,
        "missing_days": missing,
        "rejected_days": rejected,
        "rejected_observations": rejected_observations,
        "reordered_archives": reordered_archives,
        "rows_written": written,
    }
