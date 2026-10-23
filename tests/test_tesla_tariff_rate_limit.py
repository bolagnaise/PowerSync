"""TOU callers share site backpressure without queuing stale force tariffs."""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def upload_harness(monkeypatch):
    source = (ROOT / "custom_components/power_sync/__init__.py").read_text()
    tree = ast.parse(source)
    names = {"send_tariff_to_tesla", "_send_tariff_to_tesla", "_tou_retry_after_too_long"}
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    clock = SimpleNamespace(now=0.0, sleeping=None, resume=None, confirmed=True)

    async def sleep(delay):
        if clock.sleeping is not None:
            clock.sleeping.set()
            await clock.resume.wait()
        clock.now += delay
        await asyncio.sleep(0)

    class Response:
        def __init__(self, status):
            self.status = status
            self.headers = {"Retry-After": "282"} if status == 429 else {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return "rate_limited"

        async def json(self):
            return {"response": {}}

    class Session:
        def __init__(self):
            self.responses = [429, 200, 200, 200]
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((clock.now, url, kwargs["json"]))
            return Response(self.responses.pop(0))

    session = Session()
    coordinator = SimpleNamespace(
        _parse_retry_after=lambda response: float(response.headers["Retry-After"])
    )
    monkeypatch.setitem(sys.modules, "power_sync.coordinator", coordinator)

    async def confirm(*_args, **_kwargs):
        return clock.confirmed

    namespace = {
        "__name__": "power_sync.tariff_upload_test",
        "__package__": "power_sync",
        "asyncio": SimpleNamespace(sleep=sleep, TimeoutError=asyncio.TimeoutError),
        "time": SimpleNamespace(monotonic=lambda: clock.now),
        "aiohttp": aiohttp,
        "DOMAIN": "power_sync",
        "TESLA_PROVIDER_TESLEMETRY": "teslemetry",
        "async_get_clientsession": lambda _hass: session,
        "get_tesla_api_base_url": lambda provider, region: region or provider,
        "_confirm_tesla_tariff_uploaded": confirm,
        "_LOGGER": logging.getLogger(__name__),
    }
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<tariff-upload>", "exec"), namespace)
    return namespace["send_tariff_to_tesla"], SimpleNamespace(data={}), session, clock


def test_final_429_defers_fresh_call_until_retry_after_even_with_rotated_token(upload_harness):
    upload, hass, session, clock = upload_harness

    async def run():
        assert await upload(hass, "site-a", {"name": "normal"}, "token", max_retries=1) is False
        accepted = {}
        assert await upload(hass, "site-a", {"name": "force"}, "new-token", accepted_status=accepted) is False
        assert accepted == {}
        assert len(session.posts) == 1
        clock.now = 281.9
        assert await upload(hass, "site-a", {"name": "restore"}, "token") is False
        assert len(session.posts) == 1
        clock.now = 282.0
        assert await upload(hass, "site-a", {"name": "fresh"}, "token") is True
        assert [post[0] for post in session.posts] == [0.0, 282.0]

    asyncio.run(run())


def test_overlapping_force_upload_is_deferred_during_normal_upload_retry(upload_harness):
    upload, hass, session, clock = upload_harness

    async def run():
        clock.sleeping, clock.resume = asyncio.Event(), asyncio.Event()
        normal = asyncio.create_task(upload(hass, "site-a", {"name": "normal"}, "token", max_retries=2))
        await clock.sleeping.wait()
        try:
            assert await upload(hass, "site-a", {"name": "force"}, "token") is False
            assert len(session.posts) == 1
        finally:
            clock.resume.set()
            assert await normal is True
        assert [post[0] for post in session.posts] == [0.0, 282.0]
        # Deferred payloads are never queued and replayed after the caller ends.
        assert all(post[2]["tou_settings"]["tariff_content_v2"]["name"] == "normal" for post in session.posts)

    asyncio.run(run())


def test_other_sites_and_api_providers_are_not_blocked(upload_harness):
    upload, hass, session, _clock = upload_harness

    async def run():
        assert await upload(hass, "site-a", {}, "token", max_retries=1) is False
        assert await upload(hass, "site-b", {}, "token") is True
        assert await upload(hass, "site-a", {}, "token", api_provider="fleet_api") is True
        assert len(session.posts) == 3

    asyncio.run(run())


def test_cancelled_retry_releases_busy_slot_but_preserves_cooldown(upload_harness):
    upload, hass, session, clock = upload_harness

    async def run():
        clock.sleeping, clock.resume = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(upload(hass, "site-a", {}, "token"))
        await clock.sleeping.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await upload(hass, "site-a", {}, "token", max_retries=1) is False
        assert len(session.posts) == 1
        clock.now = 282.0
        assert await upload(hass, "site-a", {}, "token") is True

    asyncio.run(run())


def test_accepted_unconfirmed_upload_preserves_status_and_releases_slot(upload_harness):
    upload, hass, session, clock = upload_harness
    session.responses = [200, 200]

    async def run():
        clock.confirmed = False
        accepted, readback = {}, {}
        assert await upload(hass, "site-a", {}, "token", accepted_status=accepted, readback_status=readback) is False
        assert accepted == {"accepted": True}
        assert readback == {"status": "unconfirmed"}
        clock.confirmed = True
        assert await upload(hass, "site-a", {}, "token") is True
        assert len(session.posts) == 2

    asyncio.run(run())


@pytest.mark.parametrize("limited_site,expected_delay", [("site-a", 283), ("other-site", 60)])
def test_restore_retry_waits_out_its_site_cooldown(limited_site, expected_delay):
    """A known five-minute cooldown must not burn three one-minute retries."""
    tree = ast.parse((ROOT / "custom_components/power_sync/__init__.py").read_text())
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_schedule_tesla_restore_retry")
    scheduled = []
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    namespace = {
        "restore_retry_count": 0,
        "restore_was_force_charging": True,
        "force_charge_state": {"active": True},
        "force_discharge_state": {"active": False},
        "_command_generation": [7],
        "site_configs": [("site-a", "token", "teslemetry")],
        "entry": SimpleNamespace(data={}),
        "CONF_FLEET_API_BASE_URL": "fleet_base_url",
        "get_tesla_api_base_url": lambda provider, region: region or provider,
        "hass": SimpleNamespace(data={"power_sync": {"_tesla_tou_uploads": {
            ("teslemetry", "teslemetry", limited_site): {"busy": False, "retry_at": 382.0},
        }}}),
        "DOMAIN": "power_sync",
        "time": SimpleNamespace(monotonic=lambda: 100.0),
        "dt_util": SimpleNamespace(utcnow=lambda: now),
        "timedelta": timedelta,
        "async_track_point_in_utc_time": lambda hass, callback, when: scheduled.append(when),
        "_LOGGER": logging.getLogger(__name__),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "<restore-retry>", "exec"), namespace)
    assert namespace["_schedule_tesla_restore_retry"]("tariff deferred") is True
    assert scheduled == [now + timedelta(seconds=expected_delay)]


@pytest.mark.parametrize("payload_name", ["normal", "force-charge", "force-discharge", "restore"])
def test_superseded_tariff_is_not_retried_after_cooldown(upload_harness, payload_name):
    """A newer command during Retry-After revokes the old payload's authority."""
    upload, hass, session, clock = upload_harness

    async def run():
        generation = [7]
        clock.sleeping, clock.resume = asyncio.Event(), asyncio.Event()
        accepted = {}
        old = asyncio.create_task(upload(
            hass, "site-a", {"name": payload_name}, "token",
            accepted_status=accepted, is_current=lambda: generation[0] == 7,
        ))
        await clock.sleeping.wait()
        generation[0] = 8
        clock.resume.set()
        assert await old is False
        assert len(session.posts) == 1
        assert accepted == {}
        # The latest caller can submit a freshly evaluated payload once the
        # provider cooldown expires. The obsolete payload is never replayed.
        assert await upload(hass, "site-a", {"name": "latest"}, "token") is True
        assert [post[2]["tou_settings"]["tariff_content_v2"]["name"]
                for post in session.posts] == [payload_name, "latest"]

    asyncio.run(run())


def test_superseded_tariff_is_blocked_before_first_post(upload_harness):
    upload, hass, session, _clock = upload_harness
    assert asyncio.run(upload(
        hass, "site-a", {}, "token", is_current=lambda: False,
    )) is False
    assert session.posts == []


def test_current_tariff_still_retries_and_confirms(upload_harness):
    upload, hass, session, _clock = upload_harness
    assert asyncio.run(upload(
        hass, "site-a", {}, "token", is_current=lambda: True,
    )) is True
    assert [post[0] for post in session.posts] == [0.0, 282.0]


@pytest.mark.parametrize("mode", ["charge", "discharge"])
@pytest.mark.parametrize("transition", ["current", "superseded", "expired"])
def test_force_upload_completion_preserves_newer_command_and_expiry(mode, transition):
    """Run each real force upload loop across a delayed provider response."""
    import copy

    tree = ast.parse((ROOT / "custom_components/power_sync/__init__.py").read_text())
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == f"handle_force_{mode}")
    upload_loop = next(node for node in ast.walk(handler)
                      if isinstance(node, ast.For)
                      and any(isinstance(child, ast.Name)
                              and child.id == "upload_status"
                              for child in ast.walk(node)))
    now = datetime(2026, 10, 20, 7, 25, tzinfo=timezone.utc)
    clock = SimpleNamespace(now=now)
    generation = [7]
    cleanup = []
    predicates = []

    async def upload(*args, accepted_status, is_current, **kwargs):
        assert is_current()
        predicates.append(is_current)
        if transition == "superseded":
            generation[0] += 1
        elif transition == "expired":
            clock.now += timedelta(minutes=5)
        accepted_status["accepted"] = True
        return True

    async def cleanup_failed(reason):
        cleanup.append(reason)

    ns = {
        "hass": None,
        "site_configs": [("site-a", "token", "teslemetry")],
        "entry": SimpleNamespace(data={}),
        "CONF_FLEET_API_BASE_URL": "fleet_base_url",
        "send_tariff_to_tesla": upload,
        "_command_generation": generation,
        "_restore_gen": 7,
        "actual_expiry": now + timedelta(minutes=1),
        "dt_util": SimpleNamespace(utcnow=lambda: clock.now),
        "accepted_sites": [], "unconfirmed_sites": [], "all_success": True,
        f"{mode}_tariff": {"name": mode},
        f"_cleanup_failed_tesla_force_{mode}": cleanup_failed,
        "_LOGGER": logging.getLogger(__name__),
    }
    wrapper = ast.AsyncFunctionDef(
        name="run", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                      kw_defaults=[], defaults=[]),
        body=[copy.deepcopy(upload_loop), ast.Return(value=ast.Constant("activate"))],
        decorator_list=[],
    )
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 "<force-upload-loop>", "exec"), ns)
    result = asyncio.run(ns["run"]())
    if transition == "current":
        assert result == "activate"
        assert ns["accepted_sites"] == ["site-a"]
        # The real caller's predicate must expire as well as detect ownership.
        clock.now = ns["actual_expiry"]
        assert predicates[0]() is False
        clock.now = now
        generation[0] += 1
        assert predicates[0]() is False
    else:
        assert result["success"] is False
        assert ns["accepted_sites"] == []
    assert bool(cleanup) == (transition == "expired")


@pytest.mark.parametrize("handler_name,generation_name", [
    ("_handle_sync_tou_internal", "sync_command_generation"),
    ("handle_restore_normal", "_restore_generation"),
])
def test_sync_and_restore_uploads_pass_their_own_generation(handler_name, generation_name):
    tree = ast.parse((ROOT / "custom_components/power_sync/__init__.py").read_text())
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == handler_name)
    upload = next(node for node in ast.walk(handler)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "send_tariff_to_tesla")
    predicate = next(keyword.value for keyword in upload.keywords if keyword.arg == "is_current")
    generation = [7]
    namespace = {"_command_generation": generation, generation_name: 7}
    is_current = eval(compile(ast.Expression(body=predicate), "<upload-authority>", "eval"), namespace)
    assert is_current() is True
    generation[0] += 1
    assert is_current() is False


def test_non_periodic_upload_waits_out_long_retry_after(upload_harness):
    """Manual syncs and restore_normal keep the uncapped Retry-After wait."""
    upload, hass, session, _clock = upload_harness

    assert asyncio.run(upload(hass, "site-a", {"name": "restore"}, "token")) is True
    assert [post[0] for post in session.posts] == [0.0, 282.0]


def test_periodic_upload_drops_retry_after_longer_than_cap(upload_harness):
    """A periodic sync must not resend its tariff at the next sync boundary."""
    upload, hass, session, clock = upload_harness

    async def run():
        assert await upload(
            hass, "site-a", {"name": "stale"}, "token", max_retry_after_seconds=60.0,
        ) is False
        assert [post[0] for post in session.posts] == [0.0]
        # The dropped retry keeps the shared cooldown, so Retry-After holds.
        clock.now = 281.9
        assert await upload(hass, "site-a", {"name": "early"}, "token") is False
        clock.now = 300.0
        assert await upload(
            hass, "site-a", {"name": "fresh"}, "token", max_retry_after_seconds=60.0,
        ) is True
        assert [post[2]["tou_settings"]["tariff_content_v2"]["name"]
                for post in session.posts] == ["stale", "fresh"]

    asyncio.run(run())


def test_periodic_upload_still_retries_short_retry_after(upload_harness):
    upload, hass, session, _clock = upload_harness
    session.responses = [429, 200]
    original_post = session.post

    def post(url, **kwargs):
        response = original_post(url, **kwargs)
        if response.status == 429:
            response.headers = {"Retry-After": "30"}
        return response

    session.post = post
    assert asyncio.run(upload(
        hass, "site-a", {}, "token", max_retry_after_seconds=60.0,
    )) is True
    assert [post[0] for post in session.posts] == [0.0, 30.0]


def _sync_check_calls(tree):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "handle_sync_rest_api_check"]


def test_only_periodic_price_syncs_cap_retry_after():
    tree = ast.parse((ROOT / "custom_components/power_sync/__init__.py").read_text())
    periodic = {}
    for call in _sync_check_calls(tree):
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        name = keywords["check_name"].value
        periodic[name] = "periodic" in keywords and keywords["periodic"].value is True
    assert periodic == {
        "service call": False,
        "restore normal": False,
        "restore normal fallback": False,
        "aemo dispatch": True,
        "cron fallback": True,
        "octopus :00/:30": True,
        "agl static startup": False,
        "flow power startup": False,
    }

    sync = next(node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_handle_sync_tou_internal")
    upload = next(node for node in ast.walk(sync)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "send_tariff_to_tesla")
    cap = next(keyword.value for keyword in upload.keywords
               if keyword.arg == "max_retry_after_seconds")
    for is_periodic, expected in ((True, 60.0), (False, None)):
        assert eval(compile(ast.Expression(body=cap), "<cap>", "eval"), {
            "periodic": is_periodic,
            "TOU_PERIODIC_SYNC_MAX_RETRY_AFTER_SECONDS": 60.0,
        }) == expected


def test_restore_normal_marks_failed_sync_restores():
    """A failed restore sync must keep restore_normal's pending cleanup state."""
    source = (ROOT / "custom_components/power_sync/__init__.py").read_text()
    restore = next(node for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == "handle_restore_normal")
    restore_source = ast.get_source_segment(source, restore)
    assert "SERVICE_SYNC_TOU" not in restore_source
    assert 'if tariff_restored is not True:\n                    _mark_tesla_restore_failed(' in restore_source
    assert 'if fallback_restored is False:\n                        _mark_tesla_restore_failed(' in restore_source


def test_periodic_upload_keeps_cooldown_after_dropping_long_5xx_retry(upload_harness):
    """A dropped 503 retry must still hold every caller until Retry-After ends."""
    upload, hass, session, clock = upload_harness
    session.responses = [503, 200]
    original_post = session.post

    def post(url, **kwargs):
        response = original_post(url, **kwargs)
        if response.status == 503:
            response.headers = {"Retry-After": "600"}
        return response

    session.post = post

    async def run():
        assert await upload(
            hass, "site-a", {"name": "stale"}, "token", max_retry_after_seconds=60.0,
        ) is False
        clock.now = 300.0
        assert await upload(
            hass, "site-a", {"name": "early"}, "token", max_retry_after_seconds=60.0,
        ) is False
        assert len(session.posts) == 1
        clock.now = 600.0
        assert await upload(
            hass, "site-a", {"name": "fresh"}, "token", max_retry_after_seconds=60.0,
        ) is True
        assert [(post[0], post[2]["tou_settings"]["tariff_content_v2"]["name"])
                for post in session.posts] == [(0.0, "stale"), (600.0, "fresh")]

    asyncio.run(run())
