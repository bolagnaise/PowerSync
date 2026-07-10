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
from pathlib import Path
from types import SimpleNamespace

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
    ``_select_restorable_tesla_tariff``. Sliced by line range (not by
    enumerating individual names) so any new constant the fix adds inside
    this block — e.g. a manager-own-code rejection list — is picked up
    automatically without the test needing to know its name.
    """
    start = _top_level_node("_FORCE_TARIFF_TEXT_MARKERS")
    end = _top_level_node("_select_restorable_tesla_tariff")
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


class _FakeCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    """Fake aiohttp session: GET routed by URL substring, POST always OK."""

    def __init__(self, *, tariff_rate_response, site_info_response, post_status=200):
        self._tariff_rate_response = tariff_rate_response
        self._site_info_response = site_info_response
        self.post_status = post_status
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append(url)
        if "tariff_rate" in url:
            return _FakeCtx(self._tariff_rate_response)
        if "site_info" in url:
            return _FakeCtx(self._site_info_response)
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append((url, json))
        return _FakeCtx(_FakeResponse(self.post_status, {}))


def _build_namespace(session, upload_calls):
    async def _fake_send_tariff_to_tesla(hass, site_id, tariff, token, provider, fleet_base_url=None):
        upload_calls.append(tariff)
        return True

    def _fake_get_tesla_api_base_url(provider, fleet_base_url):
        return "https://api.example.com"

    class _FakeClientTimeout:
        def __init__(self, total=None):
            self.total = total

    aiohttp_stub = SimpleNamespace(ClientTimeout=_FakeClientTimeout)

    namespace = {
        "HomeAssistant": object,
        "ConfigEntry": object,
        "Any": object,
        "dt_util": _FakeDtUtil(),
        "_LOGGER": _FakeLogger(),
        "aiohttp": aiohttp_stub,
        "async_get_clientsession": lambda hass: session,
        "get_tesla_api_base_url": _fake_get_tesla_api_base_url,
        "send_tariff_to_tesla": _fake_send_tariff_to_tesla,
        "TESLA_PROVIDER_TESLEMETRY": "teslemetry",
        "CONF_FLEET_API_BASE_URL": "fleet_api_base_url",
    }
    exec(compile(_TARIFF_HELPERS_SOURCE, "<tariff_helpers>", "exec"), namespace)
    return namespace


def _load_class(class_name: str, namespace: dict):
    exec(compile(_class_source(class_name), f"<{class_name}>", "exec"), namespace)
    return namespace[class_name]


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
    namespace: dict = {}
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
    manager.hass = SimpleNamespace()
    manager.entry = SimpleNamespace(data={})
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


# ---------------------------------------------------------------------------
# SavingSessionTariffManager: identical shape, mirrored.
# ---------------------------------------------------------------------------

def _make_session_manager(namespace, saved_tariff):
    manager_cls = _load_class("SavingSessionTariffManager", namespace)
    manager = object.__new__(manager_cls)
    manager.hass = SimpleNamespace()
    manager.entry = SimpleNamespace(data={})
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

    asyncio.run(manager._enter_session_mode(active_session))

    assert manager._saved_tariff == GENUINE_TARIFF, (
        "genuine baseline must survive re-entry into session mode even when the "
        f"manager's own prior upload is still live at Tesla; got {manager._saved_tariff!r}"
    )
