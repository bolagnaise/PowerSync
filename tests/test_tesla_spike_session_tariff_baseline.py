"""Regression coverage for OB-34: Tesla session/spike managers must never adopt
their OWN uploaded tariff (AEMO-SPIKE / OCTOPUS-SAVING-SESSION) as the restore
baseline, and must never clobber an already-captured genuine baseline when
re-entering spike/session mode (e.g. after a reload mid-event finds its own
prior upload still live at Tesla).

Follows the AST source-extraction pattern from
``tests/test_money_event_manager_optimizer_gate.py`` and
``tests/test_generic_force_event_rearm.py``: the module-level tariff-filter
helpers and the manager classes are located dynamically inside ``__init__.py``
(no hardcoded line numbers), re-embedded verbatim, and exec'd against a stub
namespace with lightweight fakes standing in for aiohttp/HomeAssistant.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"

_SOURCE = INIT_PATH.read_text()
_MODULE = ast.parse(_SOURCE)


def _top_level_node(name: str) -> ast.AST:
    for node in _MODULE.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            targets = [node.name]
        if name in targets:
            return node
    raise AssertionError(f"{name} not found at module level")


def _tariff_helpers_source() -> str:
    """Verbatim source for the whole tariff-filter helper block: from the
    ``_FORCE_TARIFF_TEXT_MARKERS`` constant through the end of
    ``_extract_tesla_tariff_content``. Sliced by line range (not by
    enumerating individual names) so any new constant the fix adds inside
    this block — e.g. a manager-own-code rejection list — is picked up
    automatically without the test needing to know its name.
    """
    start = _top_level_node("_FORCE_TARIFF_TEXT_MARKERS")
    end = _top_level_node("_extract_tesla_tariff_content")
    lines = _SOURCE.splitlines()
    return "\n".join(lines[start.lineno - 1 : end.end_lineno])


def _class_source(class_name: str) -> str:
    node = _top_level_node(class_name)
    segment = ast.get_source_segment(_SOURCE, node)
    assert segment is not None
    return segment


_TARIFF_HELPERS_SOURCE = _tariff_helpers_source()


class _FakeLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeDtUtil:
    @staticmethod
    def utcnow():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    @staticmethod
    def now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _BlockingJsonResponse(_FakeResponse):
    """Response whose body can be held while stop/option changes race it."""

    def __init__(self, status, payload, started, release):
        super().__init__(status, payload)
        self.started = started
        self.release = release

    async def json(self):
        self.started.set()
        await self.release.wait()
        return await super().json()


class _FakeCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _BlockingCtx(_FakeCtx):
    """Hold a fake command response until the test changes live state."""

    def __init__(self, response, started, release):
        super().__init__(response)
        self.started = started
        self.release = release

    async def __aenter__(self):
        self.started.set()
        await self.release.wait()
        return await super().__aenter__()


class _FakeSession:
    """Fake aiohttp session: GET routed by URL substring, POST always OK."""

    def __init__(
        self,
        *,
        tariff_rate_response,
        site_info_response,
        post_status=200,
        post_statuses=None,
        post_started=None,
        post_release=None,
        events=None,
    ):
        self._tariff_rate_response = tariff_rate_response
        self._site_info_response = site_info_response
        self.post_status = post_status
        self.post_statuses = list(post_statuses) if post_statuses is not None else None
        self.post_started = post_started
        self.post_release = post_release
        self._post_block_used = False
        self.events = events
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append(url)
        if "tariff_rate" in url:
            return _FakeCtx(self._tariff_rate_response)
        if "site_info" in url:
            return _FakeCtx(self._site_info_response)
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, timeout=None, **markers):
        current = markers.get("_power_sync_is_current")
        if current is not None and not current():
            raise RuntimeError("event control superseded")
        self.post_calls.append((url, json))
        if self.events is not None:
            self.events.append(("post", url, json))
        if self.post_statuses is not None:
            status = self.post_statuses.pop(0)
        else:
            status = self.post_status
        response = _FakeResponse(status, {})
        if (
            self.post_started is not None
            and self.post_release is not None
            and not self._post_block_used
        ):
            self._post_block_used = True
            return _BlockingCtx(response, self.post_started, self.post_release)
        return _FakeCtx(response)


def _build_namespace(session, upload_calls, send_result=True, send_results=None):
    async def _fake_send_tariff_to_tesla(hass, site_id, tariff, token, provider, fleet_base_url=None, is_current=None):
        if is_current is not None and not is_current():
            return False
        upload_calls.append(tariff)
        if send_results is not None:
            return send_results.pop(0)
        return send_result

    def _fake_get_tesla_api_base_url(provider, fleet_base_url):
        return "https://api.example.com"

    class _FakeClientTimeout:
        def __init__(self, total=None):
            self.total = total

    aiohttp_stub = SimpleNamespace(ClientTimeout=_FakeClientTimeout)

    class _FakeAsyncio:
        """Stands in for the real ``asyncio`` module inside exec'd class
        source so ``_exit_spike_mode``/``_exit_session_mode`` don't block
        the test suite on the real 5-second ``asyncio.sleep(5)`` call."""

        @staticmethod
        async def sleep(_seconds):
            return None

        Lock = asyncio.Lock

    package = "tesla_event_manager_probe"
    if package not in sys.modules:
        module = ModuleType(package)
        module.__path__ = [str(ROOT / "custom_components/power_sync")]
        sys.modules[package] = module
    namespace = {
        "__package__": package,
        "HomeAssistant": object,
        "ConfigEntry": object,
        "Any": object,
        "copy": copy,
        "dt_util": _FakeDtUtil(),
        "_LOGGER": _FakeLogger(),
        "aiohttp": aiohttp_stub,
        "asyncio": _FakeAsyncio(),
        "async_get_clientsession": lambda hass: session,
        "get_tesla_api_base_url": _fake_get_tesla_api_base_url,
        "send_tariff_to_tesla": _fake_send_tariff_to_tesla,
        "TESLA_PROVIDER_TESLEMETRY": "teslemetry",
        "CONF_FLEET_API_BASE_URL": "fleet_api_base_url",
        "CONF_AEMO_SPIKE_ENABLED": "aemo_spike_enabled",
        "CONF_MONITORING_MODE": "monitoring_mode",
        "DOMAIN": "power_sync",
    }
    exec(compile(_TARIFF_HELPERS_SOURCE, "<tariff_helpers>", "exec"), namespace)
    return namespace


def _load_class(class_name: str, namespace: dict):
    exec(compile(_class_source(class_name), f"<{class_name}>", "exec"), namespace)
    return namespace[class_name]


class _FakeAemoClient:
    def __init__(self, result=(True, 5000.0, None), *, started=None, release=None):
        self.result = result
        self.started = started
        self.release = release
        self.calls = 0

    async def check_price_spike(self, region, threshold, *, was_active):
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return self.result


GENUINE_TARIFF = {
    "code": "AGL-TOU-1",
    "utility": "AGL",
    "name": "AGL Time of Use",
}

LIVE_AEMO_SPIKE_TARIFF = {
    "code": "AEMO-SPIKE",
    "utility": "AEMO Spike Response",
    "name": "Spike Tariff ($5000/MWh)",
}

LIVE_SAVING_SESSION_TARIFF = {
    "code": "OCTOPUS-SAVING-SESSION",
    "utility": "Octopus Saving Session",
    "name": "Saving Session (10 octopoints/kWh)",
}


# ---------------------------------------------------------------------------
# Pure-function coverage: the restorable-tariff filter must reject the
# managers' own uploaded tariff codes, not just force-charge/discharge markers.
# ---------------------------------------------------------------------------

def test_is_powersync_force_tariff_rejects_managers_own_codes():
    namespace: dict = {"Any": object, "copy": copy}
    exec(compile(_TARIFF_HELPERS_SOURCE, "<tariff_helpers>", "exec"), namespace)
    is_force = namespace["_is_powersync_force_tariff"]
    select_restorable = namespace["_select_restorable_tesla_tariff"]

    assert is_force(LIVE_AEMO_SPIKE_TARIFF) is True
    assert is_force(LIVE_SAVING_SESSION_TARIFF) is True
    assert select_restorable(LIVE_AEMO_SPIKE_TARIFF) is None
    assert select_restorable(LIVE_SAVING_SESSION_TARIFF) is None

    # Sanity: a genuine user tariff must still be accepted.
    assert is_force(GENUINE_TARIFF) is False
    assert select_restorable(GENUINE_TARIFF) == GENUINE_TARIFF


# ---------------------------------------------------------------------------
# AEMOSpikeManager: a reload mid-event (in-memory _in_spike_mode reset to
# False) that re-enters spike mode while its OWN AEMO-SPIKE tariff is still
# live at Tesla must not clobber an already-captured genuine baseline.
# ---------------------------------------------------------------------------

def _make_aemo_manager(namespace, saved_tariff):
    manager_cls = _load_class("AEMOSpikeManager", namespace)
    manager = object.__new__(manager_cls)
    manager.hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})
    manager.entry = SimpleNamespace(
        data={"tesla_energy_site_id": "site-1"},
        options={"aemo_spike_enabled": True, "monitoring_mode": False},
        entry_id="entry-1",
    )
    manager.region = "NSW1"
    manager.threshold = 300.0
    manager.site_id = "site-1"
    manager._api_token = "token-123"
    manager._token_getter = None
    manager.api_provider = "teslemetry"
    manager._in_spike_mode = False
    manager._spike_start_time = None
    manager._saved_tariff = saved_tariff
    manager._saved_operation_mode = None
    manager._last_price = None
    manager._last_check = None
    manager._stopped = False
    manager._restore_pending = False
    manager._recovery_unreadable = False
    manager._spike_control_lock = None
    manager._operation_lock = None
    manager.hass.data["power_sync"]["entry-1"]["aemo_spike_manager"] = manager
    return manager


def test_aemo_spike_manager_preserves_genuine_baseline_on_reentry():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": LIVE_AEMO_SPIKE_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200,
            {
                "response": {
                    "default_real_mode": "autonomous",
                    "tariff_content_v2": LIVE_AEMO_SPIKE_TARIFF,
                }
            },
        ),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))

    asyncio.run(manager._enter_spike_mode(5000.0))

    assert manager._saved_tariff == GENUINE_TARIFF, (
        "genuine baseline must survive re-entry into spike mode even when the "
        f"manager's own prior upload is still live at Tesla; got {manager._saved_tariff!r}"
    )


def test_aemo_spike_manager_captures_genuine_tariff_on_first_entry():
    """Sanity: the fix must not block a legitimate first-time capture."""
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": GENUINE_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200, {"response": {"default_real_mode": "self_consumption"}}
        ),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)

    asyncio.run(manager._enter_spike_mode(400.0))

    assert manager._saved_tariff == GENUINE_TARIFF
    assert manager._in_spike_mode is True
    assert len(upload_calls) == 1


def test_aemo_spike_manager_disabled_blocks_check_and_entry_and_reports_status():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {"tariff_content_v2": GENUINE_TARIFF}}),
        site_info_response=_FakeResponse(200, {"response": {"default_real_mode": "self_consumption"}}),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager.entry.options["aemo_spike_enabled"] = False
    manager._aemo_client = _FakeAemoClient()

    asyncio.run(manager.check_and_handle_spike())
    asyncio.run(manager._enter_spike_mode(5000.0))

    assert manager._aemo_client.calls == 0
    assert session.post_calls == []
    assert upload_calls == []
    assert manager.get_status()["enabled"] is False


def test_aemo_spike_manager_disabled_during_price_fetch_makes_no_write():
    price_started = asyncio.Event()
    price_release = asyncio.Event()
    client = _FakeAemoClient(started=price_started, release=price_release)
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {}}),
        site_info_response=_FakeResponse(200, {"response": {}}),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._aemo_client = client

    async def scenario():
        check_task = asyncio.create_task(manager.check_and_handle_spike())
        await price_started.wait()
        manager.entry.options["aemo_spike_enabled"] = False
        price_release.set()
        await check_task

    asyncio.run(scenario())

    assert client.calls == 1
    assert session.post_calls == []
    assert upload_calls == []
    assert manager._in_spike_mode is False


@pytest.mark.parametrize("blocked_endpoint", ["tariff_rate", "site_info"])
def test_aemo_spike_manager_disabled_during_baseline_fetch_makes_no_write(blocked_endpoint):
    baseline_started = asyncio.Event()
    baseline_release = asyncio.Event()
    blocked_response = _BlockingJsonResponse(
        200,
        {"response": {"tariff_content_v2": GENUINE_TARIFF, "default_real_mode": "self_consumption"}},
        baseline_started,
        baseline_release,
    )
    if blocked_endpoint == "tariff_rate":
        tariff_rate_response = blocked_response
        site_info_response = _FakeResponse(200, {"response": {"default_real_mode": "self_consumption"}})
    else:
        tariff_rate_response = _FakeResponse(
            200, {"response": {"tariff_content_v2": GENUINE_TARIFF}}
        )
        site_info_response = blocked_response

    session = _FakeSession(
        tariff_rate_response=tariff_rate_response,
        site_info_response=site_info_response,
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)

    async def scenario():
        enter_task = asyncio.create_task(manager._enter_spike_mode(5000.0))
        await baseline_started.wait()
        manager.entry.options["aemo_spike_enabled"] = False
        baseline_release.set()
        await enter_task

    asyncio.run(scenario())

    assert session.post_calls == []
    assert upload_calls == []
    assert manager._in_spike_mode is False


def test_aemo_spike_manager_disabled_active_restores_without_price_fetch():
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager.entry.options["aemo_spike_enabled"] = False
    manager._saved_operation_mode = "self_consumption"
    manager._in_spike_mode = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"
    manager._aemo_client = _FakeAemoClient()

    asyncio.run(manager.check_and_handle_spike())

    assert manager._aemo_client.calls == 0
    assert upload_calls == [GENUINE_TARIFF]
    assert len(session.post_calls) == 2
    assert manager._in_spike_mode is False


def test_aemo_spike_manager_failed_restore_stays_active_and_retries_when_disabled():
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(
        session,
        upload_calls,
        send_results=[False, True],
    )
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager.entry.options["aemo_spike_enabled"] = False
    manager._saved_operation_mode = "self_consumption"
    manager._in_spike_mode = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"
    manager._aemo_client = _FakeAemoClient()

    asyncio.run(manager.check_and_handle_spike())
    assert manager._in_spike_mode is True
    assert len(upload_calls) == 1

    asyncio.run(manager.check_and_handle_spike())
    assert manager._in_spike_mode is False
    assert len(upload_calls) == 2
    assert manager._aemo_client.calls == 0


def test_aemo_spike_manager_stop_during_baseline_fetch_prevents_new_write():
    baseline_started = asyncio.Event()
    baseline_release = asyncio.Event()
    session = _FakeSession(
        tariff_rate_response=_BlockingJsonResponse(
            200,
            {"response": {"tariff_content_v2": GENUINE_TARIFF}},
            baseline_started,
            baseline_release,
        ),
        site_info_response=_FakeResponse(200, {"response": {"default_real_mode": "self_consumption"}}),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._aemo_client = _FakeAemoClient()

    async def scenario():
        check_task = asyncio.create_task(manager.check_and_handle_spike())
        await baseline_started.wait()

        stop_task = asyncio.create_task(manager.stop())
        for _ in range(100):
            if manager._stopped:
                break
            await asyncio.sleep(0)
        assert manager._stopped is True

        baseline_release.set()
        await check_task
        await stop_task

    asyncio.run(scenario())

    assert session.post_calls == []
    assert upload_calls == []
    assert manager._in_spike_mode is False


def test_aemo_spike_manager_persists_obligation_before_partial_operation_and_restores():
    operation_started = asyncio.Event()
    operation_release = asyncio.Event()
    events: list = []
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": GENUINE_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200, {"response": {"default_real_mode": "self_consumption"}}
        ),
        post_started=operation_started,
        post_release=operation_release,
        events=events,
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._aemo_client = _FakeAemoClient()
    manager._store = _FakeStore(events=events)

    async def scenario():
        check_task = asyncio.create_task(manager.check_and_handle_spike())
        await operation_started.wait()

        first_post = next(i for i, event in enumerate(events) if event[0] == "post")
        first_save = next(i for i, event in enumerate(events) if event[0] == "save")
        assert first_save < first_post
        assert events[first_save][1]["in_event"] is True

        manager.entry.options["aemo_spike_enabled"] = False
        operation_release.set()
        await check_task

    asyncio.run(scenario())

    assert manager._in_spike_mode is False
    assert manager._restore_pending is False
    assert upload_calls == [GENUINE_TARIFF]
    assert len(session.post_calls) == 3


def test_aemo_spike_manager_failed_tariff_restore_does_not_restore_autonomous_mode():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {}}),
        site_info_response=_FakeResponse(200, {"response": {}}),
        post_status=200,
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_results=[False])
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._saved_operation_mode = "autonomous"
    manager._in_spike_mode = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"

    asyncio.run(manager._exit_spike_mode(50.0))

    assert upload_calls == [GENUINE_TARIFF]
    assert len(session.post_calls) == 1
    assert session.post_calls[0][1] == {"default_real_mode": "self_consumption"}
    assert manager._in_spike_mode is True
    assert manager._restore_pending is True


def test_aemo_spike_manager_rejected_operation_keeps_cleanup_pending_without_spike_tariff():
    """A rejected autonomous-mode request must clean up safely and retain
    the obligation when restoring the genuine tariff also fails."""
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": GENUINE_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200, {"response": {"default_real_mode": "self_consumption"}}
        ),
        post_statuses=[500, 200],
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_results=[False])
    manager = _make_aemo_manager(namespace, saved_tariff=None)

    asyncio.run(manager._enter_spike_mode(5000.0))

    assert all(tariff.get("code") != "AEMO-SPIKE" for tariff in upload_calls)
    assert upload_calls == [GENUINE_TARIFF]
    assert len(session.post_calls) == 2
    assert session.post_calls[0][1] == {"default_real_mode": "autonomous"}
    assert session.post_calls[1][1] == {"default_real_mode": "self_consumption"}
    assert manager._in_spike_mode is True
    assert manager._restore_pending is True


def test_aemo_spike_manager_failed_operation_restore_retries_above_active_spot_price():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {}}),
        site_info_response=_FakeResponse(200, {"response": {}}),
        post_statuses=[200, 500, 200, 200],
    )
    upload_calls: list = []
    namespace = _build_namespace(
        session,
        upload_calls,
        send_results=[True, True],
    )
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._saved_operation_mode = "self_consumption"
    manager._in_spike_mode = True
    manager._restore_pending = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"
    manager._aemo_client = _FakeAemoClient(result=(True, 5000.0, None))

    asyncio.run(manager.check_and_handle_spike())

    assert manager._in_spike_mode is True
    assert manager._restore_pending is True
    assert len(session.post_calls) == 2
    assert len(upload_calls) == 1
    assert manager._aemo_client.calls == 0

    asyncio.run(manager.check_and_handle_spike())

    assert manager._in_spike_mode is False
    assert manager._restore_pending is False
    assert len(session.post_calls) == 4
    assert len(upload_calls) == 2
    assert manager._aemo_client.calls == 0


@pytest.mark.parametrize(
    "baseline_payload",
    [
        {"response": {"default_real_mode": "self_consumption"}},
        {
            "response": {
                "tariff_content_v2": LIVE_AEMO_SPIKE_TARIFF,
                "default_real_mode": "self_consumption",
            }
        },
    ],
)
def test_aemo_spike_manager_refuses_unknown_or_non_restorable_baseline(baseline_payload):
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, baseline_payload),
        site_info_response=_FakeResponse(200, baseline_payload),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)

    asyncio.run(manager._enter_spike_mode(5000.0))

    assert manager._saved_tariff is None
    assert manager._in_spike_mode is False
    assert manager._restore_pending is False
    assert session.post_calls == []
    assert upload_calls == []


@pytest.mark.parametrize("option_value", [False, "false"])
def test_aemo_spike_manager_options_false_overrides_data_true_strictly(option_value):
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {}}),
        site_info_response=_FakeResponse(200, {"response": {}}),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager.entry.data["aemo_spike_enabled"] = True
    manager.entry.options["aemo_spike_enabled"] = option_value
    manager._aemo_client = _FakeAemoClient()

    asyncio.run(manager.check_and_handle_spike())
    asyncio.run(manager._enter_spike_mode(5000.0))

    assert manager._aemo_client.calls == 0
    assert manager.get_status()["enabled"] is False
    assert session.post_calls == []
    assert upload_calls == []


def test_aemo_spike_manager_monitoring_blocks_new_commands():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {}}),
        site_info_response=_FakeResponse(200, {"response": {}}),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager.entry.options["monitoring_mode"] = True
    manager._aemo_client = _FakeAemoClient()

    asyncio.run(manager.check_and_handle_spike())
    asyncio.run(manager._enter_spike_mode(5000.0))

    assert manager._aemo_client.calls == 0
    assert manager.get_status()["enabled"] is True
    assert manager._spike_commands_allowed() is False
    assert session.post_calls == []
    assert upload_calls == []


def test_aemo_spike_manager_monitoring_preserves_pending_recovery_until_allowed():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {}}),
        site_info_response=_FakeResponse(200, {"response": {}}),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager.entry.options["monitoring_mode"] = True
    manager._saved_operation_mode = "self_consumption"
    manager._in_spike_mode = True
    manager._restore_pending = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"
    manager._aemo_client = _FakeAemoClient(result=(True, 5000.0, None))

    asyncio.run(manager.check_and_handle_spike())

    assert manager._aemo_client.calls == 0
    assert manager._in_spike_mode is True
    assert manager._restore_pending is True
    assert session.post_calls == []
    assert upload_calls == []

    manager.entry.options["monitoring_mode"] = False
    asyncio.run(manager.check_and_handle_spike())

    assert manager._aemo_client.calls == 0
    assert manager._in_spike_mode is False
    assert manager._restore_pending is False
    assert upload_calls == [GENUINE_TARIFF]


def test_aemo_spike_manager_monitoring_marks_active_cleanup_pending_for_later_retry():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {}}),
        site_info_response=_FakeResponse(200, {"response": {}}),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager.entry.options["monitoring_mode"] = True
    manager._saved_operation_mode = "self_consumption"
    manager._in_spike_mode = True
    manager._restore_pending = False
    manager._spike_start_time = "2026-07-10T12:00:00Z"
    manager._aemo_client = _FakeAemoClient(result=(True, 5000.0, None))

    asyncio.run(manager.check_and_handle_spike())

    assert manager._aemo_client.calls == 0
    assert manager._in_spike_mode is True
    assert manager._restore_pending is True
    assert session.post_calls == []
    assert upload_calls == []

    manager.entry.options["monitoring_mode"] = False
    asyncio.run(manager.check_and_handle_spike())

    assert manager._aemo_client.calls == 0
    assert manager._in_spike_mode is False
    assert manager._restore_pending is False
    assert upload_calls == [GENUINE_TARIFF]


# ---------------------------------------------------------------------------
# SavingSessionTariffManager: identical shape, mirrored.
# ---------------------------------------------------------------------------

def _make_session_manager(namespace, saved_tariff):
    manager_cls = _load_class("SavingSessionTariffManager", namespace)
    manager = object.__new__(manager_cls)
    manager.hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})
    manager.entry = SimpleNamespace(data={"tesla_energy_site_id": "site-1"}, options={}, entry_id="entry-1")
    manager._session_coordinator = None
    manager.site_id = "site-1"
    manager._api_token = "token-123"
    manager._token_getter = None
    manager.api_provider = "teslemetry"
    manager._octopoints_per_penny = 8
    manager._in_session_mode = False
    manager._session_start_time = None
    manager._saved_tariff = saved_tariff
    manager._saved_operation_mode = None
    manager._active_session_code = None
    manager.hass.data["power_sync"]["entry-1"]["saving_session_tariff_manager"] = manager
    return manager


def test_saving_session_manager_preserves_genuine_baseline_on_reentry():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": LIVE_SAVING_SESSION_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200,
            {
                "response": {
                    "default_real_mode": "autonomous",
                    "tariff_content_v2": LIVE_SAVING_SESSION_TARIFF,
                }
            },
        ),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_session_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))

    active_session = SimpleNamespace(
        code="octopus-session-1",
        start="2026-07-10T17:00:00Z",
        end="2026-07-10T18:00:00Z",
        octopoints_per_kwh=10,
    )

    active_session.joined = True
    active_session.session_type = "saving"
    active_session.is_active = lambda: True
    manager._session_coordinator = SimpleNamespace(data={"sessions": [active_session]})
    asyncio.run(manager._enter_session_mode(active_session))

    assert manager._saved_tariff == GENUINE_TARIFF, (
        "genuine baseline must survive re-entry into session mode even when the "
        f"manager's own prior upload is still live at Tesla; got {manager._saved_tariff!r}"
    )


# ---------------------------------------------------------------------------
# OB-40: _saved_operation_mode must not be re-captured on reentry either —
# mirrors the OB-34 tariff-baseline guard onto the operation-mode axis. A
# reload mid-event re-enters enter_*_mode while site_info now reflects the
# manager's OWN event mode ("autonomous"), which must not clobber the user's
# real pre-event mode.
# ---------------------------------------------------------------------------

def test_aemo_spike_manager_preserves_operation_mode_on_reentry():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": LIVE_AEMO_SPIKE_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200,
            {
                "response": {
                    "default_real_mode": "autonomous",
                    "tariff_content_v2": LIVE_AEMO_SPIKE_TARIFF,
                }
            },
        ),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._saved_operation_mode = "self_consumption"

    asyncio.run(manager._enter_spike_mode(5000.0))

    assert manager._saved_operation_mode == "self_consumption", (
        "genuine operation mode must survive re-entry into spike mode even "
        "when the manager's own event mode (autonomous) is live at Tesla; "
        f"got {manager._saved_operation_mode!r}"
    )


def test_aemo_spike_manager_captures_operation_mode_on_first_entry():
    """Sanity: the fix must not block a legitimate first-time capture."""
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": GENUINE_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200, {"response": {"default_real_mode": "self_consumption"}}
        ),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_aemo_manager(namespace, saved_tariff=None)

    asyncio.run(manager._enter_spike_mode(400.0))

    assert manager._saved_operation_mode == "self_consumption"


def test_saving_session_manager_preserves_operation_mode_on_reentry():
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": LIVE_SAVING_SESSION_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200,
            {
                "response": {
                    "default_real_mode": "autonomous",
                    "tariff_content_v2": LIVE_SAVING_SESSION_TARIFF,
                }
            },
        ),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_session_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._saved_operation_mode = "self_consumption"

    active_session = SimpleNamespace(
        code="octopus-session-1",
        start="2026-07-10T17:00:00Z",
        end="2026-07-10T18:00:00Z",
        octopoints_per_kwh=10,
    )

    active_session.joined = True
    active_session.session_type = "saving"
    active_session.is_active = lambda: True
    manager._session_coordinator = SimpleNamespace(data={"sessions": [active_session]})
    asyncio.run(manager._enter_session_mode(active_session))

    assert manager._saved_operation_mode == "self_consumption", (
        "genuine operation mode must survive re-entry into session mode even "
        "when the manager's own event mode (autonomous) is live at Tesla; "
        f"got {manager._saved_operation_mode!r}"
    )


def test_saving_session_manager_captures_operation_mode_on_first_entry():
    """Sanity: the fix must not block a legitimate first-time capture."""
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": GENUINE_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200, {"response": {"default_real_mode": "self_consumption"}}
        ),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls)
    manager = _make_session_manager(namespace, saved_tariff=None)

    active_session = SimpleNamespace(
        code="octopus-session-1",
        start="2026-07-10T17:00:00Z",
        end="2026-07-10T18:00:00Z",
        octopoints_per_kwh=10,
    )

    active_session.joined = True
    active_session.session_type = "saving"
    active_session.is_active = lambda: True
    manager._session_coordinator = SimpleNamespace(data={"sessions": [active_session]})
    asyncio.run(manager._enter_session_mode(active_session))

    assert manager._saved_operation_mode == "self_consumption"


# ---------------------------------------------------------------------------
# OB-38: exit-path must not forget it owes a restore when send_tariff_to_tesla
# fails. A failed upload must leave _in_*_mode (and dependent start-time /
# session-code state) untouched so the next detection cycle retries the exit;
# a successful upload must still clear state as before.
# ---------------------------------------------------------------------------

def _exit_session(session_status_ok=True, *, send_result):
    """Build a fake aiohttp session whose POSTs (operation-mode switches)
    succeed, independent of the tariff-upload result under test."""
    return _FakeSession(
        tariff_rate_response=_FakeResponse(200, {"response": {}}),
        site_info_response=_FakeResponse(200, {"response": {}}),
        post_status=200 if session_status_ok else 500,
    )


def test_aemo_spike_manager_retries_exit_after_failed_restore():
    session = _exit_session(send_result=False)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=False)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._in_spike_mode = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"

    asyncio.run(manager._exit_spike_mode(50.0))

    assert manager._in_spike_mode is True, (
        "a failed tariff restore must leave _in_spike_mode set so the next "
        "cycle retries the exit instead of silently abandoning the restore"
    )
    assert manager._spike_start_time == "2026-07-10T12:00:00Z"


def test_aemo_spike_manager_clears_state_after_successful_restore():
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._in_spike_mode = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"

    asyncio.run(manager._exit_spike_mode(50.0))

    assert manager._in_spike_mode is False
    assert manager._spike_start_time is None


def test_saving_session_manager_retries_exit_after_failed_restore():
    session = _exit_session(send_result=False)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=False)
    manager = _make_session_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._in_session_mode = True
    manager._session_start_time = "2026-07-10T17:00:00Z"
    manager._active_session_code = "octopus-session-1"

    asyncio.run(manager._exit_session_mode())

    assert manager._in_session_mode is True, (
        "a failed tariff restore must leave _in_session_mode set so the next "
        "cycle retries the exit instead of silently abandoning the restore"
    )
    assert manager._session_start_time == "2026-07-10T17:00:00Z"
    assert manager._active_session_code == "octopus-session-1"


def test_saving_session_manager_clears_state_after_successful_restore():
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_session_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._in_session_mode = True
    manager._session_start_time = "2026-07-10T17:00:00Z"
    manager._active_session_code = "octopus-session-1"

    asyncio.run(manager._exit_session_mode())

    assert manager._in_session_mode is False
    assert manager._session_start_time is None
    assert manager._active_session_code is None


def test_aemo_spike_manager_keeps_event_when_no_saved_tariff_to_restore():
    """A missing baseline must retain the restore obligation and avoid
    restoring autonomous mode while the spike tariff may still be live."""
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._in_spike_mode = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"

    asyncio.run(manager._exit_spike_mode(50.0))

    assert manager._in_spike_mode is True
    assert manager._restore_pending is True
    assert manager._spike_start_time == "2026-07-10T12:00:00Z"
    assert upload_calls == []
    assert len(session.post_calls) == 1
    assert session.post_calls[0][1] == {"default_real_mode": "self_consumption"}


# ---------------------------------------------------------------------------
# OB-41: event state (in-event flag + saved tariff/mode) must survive a
# reload, and a confirmed successful restore must clear the saved baseline
# so a genuine tariff change between two events is never clobbered by the
# capture-once guard (OB-34/42f21caa) on the next event.
# ---------------------------------------------------------------------------

class _FakeStore:
    """Stands in for homeassistant.helpers.storage.Store."""

    def __init__(self, initial=None, events=None, load_error=None):
        self._data = initial
        self.events = events
        self.load_error = load_error
        self.load_calls = 0
        self.save_calls: list = []

    async def async_load(self):
        self.load_calls += 1
        if self.load_error is not None:
            raise self.load_error
        return self._data

    async def async_save(self, data):
        self.save_calls.append(data)
        if self.events is not None:
            self.events.append(("save", data))
        self._data = data


def test_aemo_spike_manager_clears_saved_baseline_after_successful_restore():
    """The stale-genuine-tariff corner: after a confirmed successful exit,
    _saved_tariff/_saved_operation_mode must be reset to None so a real
    tariff change the user makes between two events isn't clobbered by the
    capture-once guard on the next spike."""
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._saved_operation_mode = "self_consumption"
    manager._in_spike_mode = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"

    asyncio.run(manager._exit_spike_mode(50.0))

    assert manager._in_spike_mode is False
    assert manager._saved_tariff is None, (
        "a confirmed successful restore must clear the saved baseline, not "
        f"leave it stale for the next event; got {manager._saved_tariff!r}"
    )
    assert manager._saved_operation_mode is None


def test_aemo_spike_manager_keeps_saved_baseline_after_failed_restore():
    """Mirror of the above on the OB-38 retry path: a FAILED restore must
    keep the baseline so the retry has something to restore from."""
    session = _exit_session(send_result=False)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=False)
    manager = _make_aemo_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._saved_operation_mode = "self_consumption"
    manager._in_spike_mode = True
    manager._spike_start_time = "2026-07-10T12:00:00Z"

    asyncio.run(manager._exit_spike_mode(50.0))

    assert manager._in_spike_mode is True
    assert manager._saved_tariff == GENUINE_TARIFF
    assert manager._saved_operation_mode == "self_consumption"


def test_saving_session_manager_clears_saved_baseline_after_successful_restore():
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_session_manager(namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager._saved_operation_mode = "self_consumption"
    manager._in_session_mode = True
    manager._session_start_time = "2026-07-10T17:00:00Z"
    manager._active_session_code = "octopus-session-1"

    asyncio.run(manager._exit_session_mode())

    assert manager._in_session_mode is False
    assert manager._saved_tariff is None
    assert manager._saved_operation_mode is None


def test_aemo_spike_manager_persists_state_on_enter_and_clears_on_exit():
    """Persistence surface: a successful enter must persist the active
    event + baseline; a successful exit must persist the cleared state —
    otherwise a reload mid-event has nothing to read back from."""
    session = _FakeSession(
        tariff_rate_response=_FakeResponse(
            200, {"response": {"tariff_content_v2": GENUINE_TARIFF}}
        ),
        site_info_response=_FakeResponse(
            200, {"response": {"default_real_mode": "self_consumption"}}
        ),
    )
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    fake_store = _FakeStore()
    manager._store = fake_store

    asyncio.run(manager._enter_spike_mode(5000.0))

    assert fake_store.save_calls, "entering spike mode must persist event state"
    persisted = fake_store.save_calls[-1]
    assert persisted["in_event"] is True
    assert persisted["saved_tariff"] == GENUINE_TARIFF
    assert persisted["saved_operation_mode"] == "self_consumption"

    exit_session = _exit_session(send_result=True)
    exit_namespace = _build_namespace(exit_session, [], send_result=True)
    manager2 = _make_aemo_manager(exit_namespace, saved_tariff=dict(GENUINE_TARIFF))
    manager2._saved_operation_mode = "self_consumption"
    manager2._in_spike_mode = True
    manager2._store = fake_store

    asyncio.run(manager2._exit_spike_mode(50.0))

    persisted_after_exit = fake_store.save_calls[-1]
    assert persisted_after_exit is None, (
        "a confirmed successful exit must persist the cleared state so a "
        f"reload afterwards finds no owed restore; got {persisted_after_exit!r}"
    )


def test_aemo_spike_manager_restore_or_exit_exits_conservatively_for_persisted_event():
    """OB-41 boot-time behaviour: a persisted active event found on setup
    must NOT be resumed — restore the saved tariff/mode and exit spike mode
    immediately, since the Powerwall may have been stranded on the inflated
    event tariff for an unknown duration."""
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._store = _FakeStore(initial={
        "in_event": True,
        "saved_tariff": GENUINE_TARIFF,
        "saved_operation_mode": "self_consumption",
    })

    asyncio.run(manager.restore_or_exit())

    assert upload_calls == [GENUINE_TARIFF], (
        "restore_or_exit must restore the persisted saved tariff, not resume "
        "the event"
    )
    assert manager._in_spike_mode is False


def test_aemo_spike_manager_restore_or_exit_retries_on_failed_restore():
    """If the boot-time restore attempt fails (e.g. Tesla API unreachable),
    _in_spike_mode must stay True so the existing OB-38 retry-next-cycle
    contract picks it up — no second retry mechanism needed."""
    session = _exit_session(send_result=False)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=False)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._store = _FakeStore(initial={
        "in_event": True,
        "saved_tariff": GENUINE_TARIFF,
        "saved_operation_mode": "self_consumption",
    })

    asyncio.run(manager.restore_or_exit())

    assert manager._in_spike_mode is True


def test_aemo_spike_manager_restore_or_exit_keeps_non_restorable_persisted_event():
    """A persisted PowerSync tariff is not a user baseline to restore from;
    retain the event obligation and avoid restoring the old mode."""
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    store = _FakeStore(initial={
        "in_event": True,
        "saved_tariff": LIVE_AEMO_SPIKE_TARIFF,
        "saved_operation_mode": "autonomous",
    })
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._store = store

    asyncio.run(manager.restore_or_exit())

    assert upload_calls == []
    assert len(session.post_calls) == 1
    assert session.post_calls[0][1] == {"default_real_mode": "self_consumption"}
    assert manager._in_spike_mode is True
    assert manager._restore_pending is True
    assert store._data["in_event"] is True


def test_aemo_spike_manager_unreadable_store_retries_without_price_or_new_writes():
    store = _FakeStore(load_error=RuntimeError("storage unavailable"))
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._store = store
    manager._aemo_client = _FakeAemoClient(result=(True, 5000.0, None))

    asyncio.run(manager.restore_or_exit())
    asyncio.run(manager.check_and_handle_spike())
    asyncio.run(manager.check_and_handle_spike())

    assert manager._recovery_unreadable is True
    assert store.load_calls == 3
    assert manager._aemo_client.calls == 0
    assert session.post_calls == []
    assert upload_calls == []


def test_aemo_spike_manager_restore_or_exit_noop_when_nothing_persisted():
    session = _exit_session(send_result=True)
    upload_calls: list = []
    namespace = _build_namespace(session, upload_calls, send_result=True)
    manager = _make_aemo_manager(namespace, saved_tariff=None)
    manager._store = _FakeStore(initial=None)

    asyncio.run(manager.restore_or_exit())

    assert upload_calls == []
    assert manager._in_spike_mode is False
