"""Regression tests for transactional multi-resource Solcast aggregation."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = ROOT / "custom_components/power_sync/coordinator.py"
NOW = datetime(2026, 10, 10, 0, 5, tzinfo=timezone(timedelta(hours=10)))


class _FrozenDtUtil:
    @staticmethod
    def now() -> datetime:
        return NOW

    @staticmethod
    def utcnow() -> datetime:
        return NOW.astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def as_local(value: datetime) -> datetime:
        return value


class _Logger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    info = debug
    warning = debug
    error = debug


def _coordinator_class():
    tree = ast.parse(COORDINATOR_PATH.read_text())
    source = COORDINATOR_PATH.read_text()
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "SolcastForecastCoordinator"
    )
    class_source = ast.get_source_segment(source, class_node)
    assert class_source is not None
    namespace = {
        "Any": Any,
        "DataUpdateCoordinator": object,
        "DEFAULT_SOLCAST_ESTIMATE_TYPE": "estimate",
        "DOMAIN": "power_sync",
        "Store": object,
        "UpdateFailed": RuntimeError,
        "_LOGGER": _Logger(),
        "_SOLCAST_ESTIMATE_FIELDS": {
            "estimate": ("pv_estimate", "pv_estimate10", "pv_estimate50"),
            "estimate10": ("pv_estimate10", "pv_estimate", "pv_estimate50"),
            "estimate90": ("pv_estimate90", "pv_estimate", "pv_estimate50"),
        },
        "aiohttp": SimpleNamespace(ClientError=RuntimeError),
        "asyncio": asyncio,
        "datetime": datetime,
        "dt_util": _FrozenDtUtil,
        "timedelta": timedelta,
    }
    exec("from __future__ import annotations\n" + class_source, namespace)
    return namespace["SolcastForecastCoordinator"]


def _period(period_end: str, value: float) -> dict[str, Any]:
    return {"period_end": period_end, "pv_estimate": value}


def _make_coordinator(responses: list[list[dict[str, Any]] | None], calls: int = 2):
    coordinator_class = _coordinator_class()
    coordinator = object.__new__(coordinator_class)
    coordinator._resource_ids = ["site-a", "site-b"]
    coordinator._estimate_type = "estimate"
    coordinator._api_calls_date = _FrozenDtUtil.utcnow().strftime("%Y-%m-%d")
    coordinator._api_calls_today = calls
    coordinator._rate_limited = False
    coordinator._daily_forecast_date = None
    coordinator._daily_forecast_kwh = None
    coordinator._daily_forecast_peak_kw = None
    coordinator.data = None
    saved: list[dict[str, Any]] = []
    scheduled: list[asyncio.Task[Any]] = []
    tracked = 0

    async def no_external():
        return None

    async def fetch(_resource_id: str):
        return responses.pop(0)

    async def save(data: dict[str, Any]) -> None:
        saved.append(data)

    def track() -> None:
        nonlocal tracked
        tracked += 1
        coordinator._api_calls_today += 1

    def create_task(coro):
        scheduled.append(asyncio.create_task(coro))

    coordinator.hass = SimpleNamespace(async_create_task=create_task)
    coordinator._scheduled = scheduled
    coordinator._try_read_from_solcast_integration = no_external
    coordinator._fetch_forecast_for_resource = fetch
    coordinator._save_forecast_cache = save
    coordinator._track_api_call = track
    return coordinator, saved, lambda: tracked


def _run_update(coordinator):
    async def run():
        result = await coordinator._async_update_data()
        if coordinator._scheduled:
            await asyncio.gather(*coordinator._scheduled)
        return result

    return asyncio.run(run())


def test_exhausted_quota_resets_before_cached_return_on_new_utc_day():
    coordinator, saved, tracked = _make_coordinator([
        [_period("2026-10-10T01:00:00+10:00", 2.0)],
        [_period("2026-10-10T01:00:00+10:00", 1.0)],
    ], calls=10)
    coordinator._api_calls_date = (
        _FrozenDtUtil.utcnow() - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    coordinator._rate_limited = True
    coordinator._rate_limit_verified = True
    coordinator.data = {
        "available": True, "resource_ids": ["site-a", "site-b"],
        "forecasts": [_period("2026-10-10T01:00:00+10:00", 9.0)],
    }

    result = _run_update(coordinator)

    assert tracked() == 2  # Fetch again, rather than returning yesterday's cache.
    assert coordinator._api_calls_today == 2
    assert coordinator._rate_limited is False
    assert coordinator._api_calls_date == _FrozenDtUtil.utcnow().strftime("%Y-%m-%d")
    assert result["forecasts"][0]["pv_estimate"] == 3.0
    assert len(saved) == 1


def test_secondary_empty_response_never_publishes_single_site_forecast():
    coordinator, saved, tracked = _make_coordinator(
        [
            [_period("2026-10-10T01:00:00+10:00", 4.58)],
            [],
        ]
    )

    result = _run_update(coordinator)

    assert result == {"available": False}
    assert saved == []
    assert tracked() == 2
    assert coordinator._daily_forecast_date is None
    assert coordinator._daily_forecast_kwh is None


def test_misaligned_secondary_response_is_not_combined():
    coordinator, saved, _tracked = _make_coordinator(
        [
            [_period("2026-10-10T01:00:00+10:00", 4.58)],
            [_period("2026-10-10T01:30:00+10:00", 8.44)],
        ]
    )

    result = _run_update(coordinator)

    assert result == {"available": False}
    assert saved == []


def test_complete_multi_resource_response_is_combined_and_cached():
    coordinator, saved, tracked = _make_coordinator(
        [
            [_period("2026-10-10T01:00:00+10:00", 4.58)],
            [_period("2026-10-10T01:00:00+10:00", 8.44)],
        ]
    )

    result = _run_update(coordinator)

    assert result["available"] is True
    assert result["today_forecast_kwh"] == 6.51
    assert result["forecasts"] == [_period("2026-10-10T01:00:00+10:00", 13.02)]
    assert result["resource_ids"] == ["site-a", "site-b"]
    assert saved == [result]
    assert tracked() == 2


def test_secondary_failure_preserves_only_a_known_complete_fallback():
    coordinator, saved, _tracked = _make_coordinator(
        [
            [_period("2026-10-10T01:00:00+10:00", 4.58)],
            None,
        ]
    )
    coordinator.data = {
        "available": True,
        "today_forecast_kwh": 13.02,
        "resource_ids": ["site-a", "site-b"],
    }

    result = _run_update(coordinator)

    assert result is coordinator.data
    assert result["today_forecast_kwh"] == 13.02
    assert saved == []


def test_multi_resource_legacy_cache_without_completeness_metadata_is_rejected():
    coordinator, _saved, _tracked = _make_coordinator([], calls=10)
    coordinator._forecast_store = SimpleNamespace(
        async_load=lambda: asyncio.sleep(
            0,
            result={
                "date": NOW.strftime("%Y-%m-%d"),
                "today_forecast_kwh": 13.02,
                "forecasts": [],
                "estimate_type": "estimate",
            },
        )
    )

    restored = asyncio.run(coordinator._restore_forecast_cache())

    assert restored is None
