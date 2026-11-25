"""site_info reads are single-flight, and invalidation fences in-flight reads."""
from __future__ import annotations

import __future__
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from conftest import load_source_node


ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
METHODS = (
    "async_get_site_info",
    "_fresh_site_info_cache",
    "_site_info_retry_pending",
    "_async_fetch_site_info",
    "_latch_site_info_failure",
    "invalidate_site_info_cache",
)
CONTROL_MAX_AGE = 60
RETRY_SECONDS = 300


class UpdateFailed(Exception):
    pass


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


class Tesla:
    """Scripted Tesla site_info endpoint; each GET blocks until released."""

    def __init__(self) -> None:
        self.calls = 0
        self.gates: list[asyncio.Event] = []
        self.replies: list[Any] = []

    async def fetch(self, session, url, headers, **kwargs):
        self.calls += 1
        gate = asyncio.Event()
        self.gates.append(gate)
        # Bounded so a broken single-flight fails instead of hanging the suite.
        await asyncio.wait_for(gate.wait(), 2)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"response": reply}

    def release(self, reply) -> None:
        self.replies.append(reply)
        next(gate for gate in self.gates if not gate.is_set()).set()


def coordinator():
    clock, tesla = Clock(), Tesla()
    namespace = dict(
        asyncio=asyncio, time=clock, Any=Any, _LOGGER=logging.getLogger(__name__),
        _fetch_with_retry=tesla.fetch, UpdateFailed=UpdateFailed, DOMAIN="power_sync",
        TESLA_SITE_INFO_CACHE_TTL_SECONDS=6 * 60 * 60,
        TESLA_SITE_INFO_RETRY_SECONDS=RETRY_SECONDS,
        TESLA_PROVIDER_FLEET_API="fleet_api",
    )
    nodes = [load_source_node(ROOT / "coordinator.py", name, parent="TeslaEnergyCoordinator")
             for name in METHODS]
    exec(compile(ast.fix_missing_locations(ast.Module(nodes, type_ignores=[])),
                 "site-info-single-flight", "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    cls = type("Coordinator", (), {name: namespace[name] for name in METHODS})
    obj = cls()
    obj.hass = NS(async_create_task=lambda coro, name=None: coro.close())
    obj.session, obj.site_id, obj.api_base_url = object(), "site", "https://tesla.test"
    obj.api_provider = "fleet_api"
    obj._get_current_token = lambda: "token"
    obj._tesla_headers = lambda token: {}
    obj._site_info_cache, obj._site_info_last_fetch = None, 0.0
    obj._site_info_fetch_failed, obj._site_info_retry_at = False, 0.0
    obj._capabilities_probed = True
    return obj, clock, tesla


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_concurrent_control_refreshes_share_one_tesla_read():
    obj, clock, tesla = coordinator()
    # Two numbers, two selects and three switches refresh on the same tick.
    readers = [asyncio.create_task(obj.async_get_site_info(max_age=CONTROL_MAX_AGE))
               for _ in range(7)]
    await settle()
    assert tesla.calls == 1
    tesla.release({"backup_reserve_percent": 20})
    results = await asyncio.gather(*readers)
    assert all(result == {"backup_reserve_percent": 20} for result in results)
    assert tesla.calls == 1

    clock.now += CONTROL_MAX_AGE - 1
    assert await obj.async_get_site_info(max_age=CONTROL_MAX_AGE) == {"backup_reserve_percent": 20}
    assert tesla.calls == 1, "a fresh cache is served without a read"

    clock.now += 2
    late = [asyncio.create_task(obj.async_get_site_info(max_age=CONTROL_MAX_AGE)) for _ in range(3)]
    await settle()
    tesla.release({"backup_reserve_percent": 30})
    assert await asyncio.gather(*late) == [{"backup_reserve_percent": 30}] * 3
    assert tesla.calls == 2, "an expired window still refreshes, once"


@pytest.mark.asyncio
async def test_queued_waiter_is_served_by_the_read_that_completes_after_it_asked():
    """Cache age counts from read completion, so any queued waiter is satisfied.

    Ordering against writes is enforced by invalidate_site_info_cache(), not by
    max_age; see the invalidation tests below.
    """
    obj, clock, tesla = coordinator()
    obj._site_info_cache, obj._site_info_last_fetch = {"v": 0}, clock.now - CONTROL_MAX_AGE - 1
    shared = asyncio.create_task(obj.async_get_site_info(max_age=CONTROL_MAX_AGE))
    await settle()
    strict = asyncio.create_task(obj.async_get_site_info(max_age=0))
    await settle()
    clock.now += 5
    tesla.release({"v": 1})
    assert await shared == {"v": 1}
    assert await strict == {"v": 1}
    assert tesla.calls == 1
    clock.now += 1
    later = asyncio.create_task(obj.async_get_site_info(max_age=0))
    await settle()
    assert tesla.calls == 2, "a caller arriving after completion still honours its own max_age"
    tesla.release({"v": 2})
    assert await later == {"v": 2}


@pytest.mark.asyncio
async def test_write_invalidation_during_a_read_is_not_overwritten():
    obj, _clock, tesla = coordinator()
    before_write = asyncio.create_task(obj.async_get_site_info(max_age=CONTROL_MAX_AGE))
    await settle()
    after_write = asyncio.create_task(obj.async_get_site_info(max_age=CONTROL_MAX_AGE))
    await settle()
    obj.invalidate_site_info_cache()  # a backup-reserve write landed
    tesla.release({"backup_reserve_percent": 20})  # pre-write snapshot
    assert await before_write == {"backup_reserve_percent": 20}
    assert obj._site_info_cache is None, "the pre-write snapshot is never cached"
    await settle()
    assert tesla.calls == 2, "the queued caller reads the post-write state"
    tesla.release({"backup_reserve_percent": 80})
    assert await after_write == {"backup_reserve_percent": 80}
    assert obj._site_info_cache == {"backup_reserve_percent": 80}


@pytest.mark.asyncio
async def test_failed_read_backs_off_queued_callers_without_a_retry_storm():
    obj, clock, tesla = coordinator()
    readers = [asyncio.create_task(obj.async_get_site_info(max_age=CONTROL_MAX_AGE))
               for _ in range(4)]
    await settle()
    tesla.release(UpdateFailed("tesla 503"))
    assert await asyncio.gather(*readers) == [None] * 4
    assert tesla.calls == 1
    assert obj._site_info_fetch_failed and obj._site_info_retry_at == clock.now + RETRY_SECONDS


@pytest.mark.asyncio
async def test_pre_write_failure_does_not_block_the_post_write_read():
    obj, _clock, tesla = coordinator()
    before_write = asyncio.create_task(obj.async_get_site_info(max_age=CONTROL_MAX_AGE))
    await settle()
    after_write = asyncio.create_task(obj.async_get_site_info(max_age=CONTROL_MAX_AGE))
    await settle()
    obj.invalidate_site_info_cache()
    tesla.release(UpdateFailed("timeout"))
    assert await before_write is None
    assert not obj._site_info_fetch_failed, "a pre-write failure does not re-arm the back-off"
    await settle()
    assert tesla.calls == 2
    tesla.release({"storm_mode_active": True})
    assert await after_write == {"storm_mode_active": True}
