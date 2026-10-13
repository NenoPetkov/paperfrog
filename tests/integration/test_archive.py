import hashlib
import io
import zipfile
from datetime import UTC, datetime

import httpx
import pytest

from paperfrog.data.archive import backfill_open_interest, metrics_frame
from paperfrog.data.lake import Lake
from paperfrog.domain.types import SafeError

DAY = datetime(2026, 7, 15, tzinfo=UTC)
START = int(DAY.timestamp() * 1000)
NAME = "SOLUSDT-metrics-2026-07-15"


def archive_payload(symbol="SOLUSDT", time="2026-07-15 00:00:00", oi="100"):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            NAME + ".csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value\n"
            f"{time},{symbol},{oi},10000\n",
        )
    value = payload.getvalue()
    return value, hashlib.sha256(value).hexdigest()


def test_archive_checksum_and_timestamp_validation():
    payload, checksum = archive_payload()
    frame = metrics_frame(payload, checksum, "SOLUSDT", DAY)
    assert frame.iloc[0].timestamp_ms == START
    assert frame.iloc[0].open_interest == 100
    with pytest.raises(SafeError, match="checksum"):
        metrics_frame(payload, "0" * 64, "SOLUSDT", DAY)


@pytest.mark.parametrize(
    "changes",
    [{"symbol": "BTCUSDT"}, {"time": "2026-07-16 00:00:00"}, {"oi": "-1"}, {"oi": "NaN"}],
)
def test_archive_invalid_data_cannot_enter_lake(changes):
    payload, checksum = archive_payload(**changes)
    with pytest.raises(SafeError):
        metrics_frame(payload, checksum, "SOLUSDT", DAY)


async def test_archive_import_is_verified_and_idempotent(tmp_path):
    payload, checksum = archive_payload()

    def respond(request):
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=checksum + "  " + NAME + ".zip\n")
        return httpx.Response(200, content=payload)

    lake = Lake(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first = await backfill_open_interest(lake, "SOLUSDT", START, START + 86400000, client)
        second = await backfill_open_interest(lake, "SOLUSDT", START, START + 86400000, client)
    assert first["verified_days"] == 1 and first["rows_written"] == 1
    assert second["rows_written"] == 0
    assert len(lake.read("open_interest", "SOLUSDT")) == 1


async def test_missing_archive_is_reported_not_filled(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        report = await backfill_open_interest(
            Lake(tmp_path), "SOLUSDT", START, START + 86400000, client
        )
    assert report["missing_days"] == ["2026-07-15"]
    assert report["rows_written"] == 0


async def test_missing_checksum_does_not_write_observations(tmp_path):
    payload, _ = archive_payload()

    def respond(request):
        return httpx.Response(
            404 if request.url.path.endswith(".CHECKSUM") else 200, content=payload
        )

    lake = Lake(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SafeError, match="checksum missing"):
            await backfill_open_interest(lake, "SOLUSDT", START, START + 86400000, client)
    assert lake.read("open_interest", "SOLUSDT").empty


async def test_archive_cannot_mix_with_demo_lake(tmp_path):
    lake = Lake(tmp_path)
    lake.bind_environment("https://demo-fapi.binance.com")
    with pytest.raises(SafeError, match="another exchange environment"):
        await backfill_open_interest(lake, "SOLUSDT", START, START + 86400000)


def test_zero_oi_observations_are_excluded_without_imputation():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            NAME + ".csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value\n"
            "2026-07-15 00:00:00,SOLUSDT,100,10000\n"
            "2026-07-15 00:05:00,SOLUSDT,0,0\n"
            "2026-07-15 00:10:00,SOLUSDT,101,10100\n",
        )
    value = payload.getvalue()
    frame = metrics_frame(value, hashlib.sha256(value).hexdigest(), "SOLUSDT", DAY)
    assert frame.open_interest.tolist() == [100, 101]
    assert frame.attrs["rejected_observations"] == 1
    assert START + 300000 not in frame.timestamp_ms.tolist()


def test_unordered_archive_is_sorted_by_observation_time():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            NAME + ".csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value\n"
            "2026-07-15 00:05:00,SOLUSDT,101,10100\n"
            "2026-07-15 00:00:00,SOLUSDT,100,10000\n",
        )
    value = payload.getvalue()
    frame = metrics_frame(value, hashlib.sha256(value).hexdigest(), "SOLUSDT", DAY)
    assert frame.timestamp_ms.tolist() == [START, START + 300000]
    assert frame.open_interest.tolist() == [100, 101]
    assert frame.attrs["reordered_archive"] is True
