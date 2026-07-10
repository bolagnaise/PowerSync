"""Regression coverage for OB-32/OB-33/OB-36: generic saving-session and AEMO
spike managers must keep re-issuing SERVICE_FORCE_DISCHARGE while the event
persists, not stop after the default 30-minute duration.

Follows the AST source-extraction pattern from
``tests/test_sungrow_curtailment_runtime.py`` and
``tests/test_money_event_manager_optimizer_gate.py``: classes/functions are
located dynamically inside ``__init__.py`` (no hardcoded line numbers), then
re-embedded verbatim and exec'd against a stub namespace with lightweight
fakes standing in for HomeAssistant/ConfigEntry/logging.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"

DOMAIN = "power_sync"
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_RESTORE_NORMAL = "restore_normal"
CONF_AEMO_REGION = "aemo_region"
CONF_AEMO_SPIKE_THRESHOLD = "aemo_spike_threshold"
CONF_ELECTRICITY_PROVIDER = "electricity_provider"


def _class_source(class_name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{class_name} not found")


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


def _load_class(class_name: str):
    namespace = {
        "HomeAssistant": object,
        "ConfigEntry": object,
        "dt_util": _FakeDtUtil(),
        "_LOGGER": _FakeLogger(),
        "DOMAIN": DOMAIN,
        "SERVICE_FORCE_DISCHARGE": SERVICE_FORCE_DISCHARGE,
        "SERVICE_RESTORE_NORMAL": SERVICE_RESTORE_NORMAL,
        "async_get_clientsession": lambda hass: None,
    }
    exec(compile(_class_source(class_name), f"<{class_name}>", "exec"), namespace)
    return namespace[class_name]


class _FakeServices:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain, service, data, blocking=True):
        self.calls.append((domain, service, dict(data)))


class _FakeHass:
    def __init__(self):
        self.services = _FakeServices()


class _FakeAemoClient:
    def __init__(self, is_spike: bool, price: float):
        self.is_spike = is_spike
        self.price = price

    async def check_price_spike(self, region, threshold):
        return self.is_spike, self.price, {}


def _force_discharge_calls(hass: _FakeHass):
    return [c for c in hass.services.calls if c[1] == SERVICE_FORCE_DISCHARGE]


def test_generic_aemo_spike_re_arms_force_discharge_while_still_spiking():
    """OB-33: repeated ticks while a spike persists must keep re-issuing
    force_discharge, not just log "still in spike mode" (which lets the
    battery revert to self-consumption once the default 30-min duration
    from the initial call elapses)."""
    GenericAEMOSpikeManager = _load_class("GenericAEMOSpikeManager")

    manager = object.__new__(GenericAEMOSpikeManager)
    manager.hass = _FakeHass()
    manager.entry = SimpleNamespace()
    manager.region = "QLD1"
    manager.threshold = 300.0
    manager._battery_type = "sigenergy"
    manager._in_spike_mode = False
    manager._spike_start_time = None
    manager._last_price = None
    manager._last_check = None
    manager._aemo_client = _FakeAemoClient(is_spike=True, price=500.0)

    async def run():
        # Tick 1: enters spike mode, issues the first force_discharge.
        await manager.check_and_handle_spike()
        # Ticks 2-4: spike persists across simulated per-minute checks.
        await manager.check_and_handle_spike()
        await manager.check_and_handle_spike()
        await manager.check_and_handle_spike()

    asyncio.run(run())

    assert manager._in_spike_mode is True
    calls = _force_discharge_calls(manager.hass)
    assert len(calls) >= 4, (
        "force_discharge must be re-armed on every tick while the spike "
        f"persists, got {calls}"
    )


def test_generic_aemo_spike_does_not_rearm_once_spike_ends():
    """No extra force_discharge calls once the spike clears — only the
    restore_normal call on exit."""
    GenericAEMOSpikeManager = _load_class("GenericAEMOSpikeManager")

    manager = object.__new__(GenericAEMOSpikeManager)
    manager.hass = _FakeHass()
    manager.entry = SimpleNamespace()
    manager.region = "QLD1"
    manager.threshold = 300.0
    manager._battery_type = "sigenergy"
    manager._in_spike_mode = False
    manager._spike_start_time = None
    manager._last_price = None
    manager._last_check = None
    manager._aemo_client = _FakeAemoClient(is_spike=True, price=500.0)

    async def run():
        await manager.check_and_handle_spike()  # enter
        manager._aemo_client.is_spike = False
        manager._aemo_client.price = 100.0
        await manager.check_and_handle_spike()  # exit
        await manager.check_and_handle_spike()  # stays out, no-op

    asyncio.run(run())

    assert manager._in_spike_mode is False
    calls = _force_discharge_calls(manager.hass)
    assert len(calls) == 1, f"expected exactly one force_discharge (entry), got {calls}"


def test_generic_saving_session_re_arms_force_discharge_while_still_active():
    """OB-32: the same truncation bug in the saving-session manager — the
    "still active" branch previously fell through to a no-op."""
    GenericSavingSessionManager = _load_class("GenericSavingSessionManager")

    session = SimpleNamespace(
        is_active=lambda: True,
        joined=True,
        session_type="saving",
        code="EVENT-1",
        octopoints_per_kwh=800,
    )
    coordinator = SimpleNamespace(data={"sessions": [session]})

    manager = object.__new__(GenericSavingSessionManager)
    manager.hass = _FakeHass()
    manager.entry = SimpleNamespace()
    manager._session_coordinator = coordinator
    manager._battery_type = "sigenergy"
    manager._octopoints_per_penny = 8
    manager._in_session_mode = False
    manager._session_start_time = None
    manager._active_session_code = None

    async def run():
        await manager.check_and_handle_sessions()
        await manager.check_and_handle_sessions()
        await manager.check_and_handle_sessions()
        await manager.check_and_handle_sessions()

    asyncio.run(run())

    assert manager._in_session_mode is True
    calls = _force_discharge_calls(manager.hass)
    assert len(calls) >= 4, (
        "force_discharge must be re-armed on every tick while the saving "
        f"session stays active, got {calls}"
    )


def test_generic_saving_session_does_not_rearm_once_session_ends():
    GenericSavingSessionManager = _load_class("GenericSavingSessionManager")

    session = SimpleNamespace(
        is_active=lambda: True,
        joined=True,
        session_type="saving",
        code="EVENT-1",
        octopoints_per_kwh=800,
    )
    coordinator = SimpleNamespace(data={"sessions": [session]})

    manager = object.__new__(GenericSavingSessionManager)
    manager.hass = _FakeHass()
    manager.entry = SimpleNamespace()
    manager._session_coordinator = coordinator
    manager._battery_type = "sigenergy"
    manager._octopoints_per_penny = 8
    manager._in_session_mode = False
    manager._session_start_time = None
    manager._active_session_code = None

    async def run():
        await manager.check_and_handle_sessions()  # enter
        coordinator.data = {"sessions": []}
        await manager.check_and_handle_sessions()  # exit
        await manager.check_and_handle_sessions()  # stays out, no-op

    asyncio.run(run())

    assert manager._in_session_mode is False
    calls = _force_discharge_calls(manager.hass)
    assert len(calls) == 1, f"expected exactly one force_discharge (entry), got {calls}"


# ---------------------------------------------------------------------------
# OB-36: the third (VPP) copy hardcoded `is_spike = region_price >= 3000`,
# silently ignoring a configured CONF_AEMO_SPIKE_THRESHOLD (e.g. GloBird
# users on a non-default plan). Extract just the threshold-resolution +
# is_spike assignment via AST so the test doesn't need to drive the whole
# network-calling closure (which does its own AEMOAPIClient/aiohttp import).
# ---------------------------------------------------------------------------


def _find_check_aemo_spike_for_vpp() -> ast.AsyncFunctionDef:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in ast.walk(module):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "check_aemo_spike_for_vpp":
            return node
    raise AssertionError("check_aemo_spike_for_vpp not found")


def _resolve_vpp_threshold(entry) -> float:
    """Exec just the `aemo_spike_threshold = ...` assignment that feeds
    `is_spike` in the VPP AEMO spike setup (the assignment sits in the
    enclosing scope of check_aemo_spike_for_vpp, captured by the closure —
    confirm the closure still exists as a sanity check, but locate the
    assignment module-wide since ast.walk(fn) won't see an enclosing-scope
    statement)."""
    _find_check_aemo_spike_for_vpp()  # sanity: closure still present
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    assign_node = None
    for n in ast.walk(module):
        if (
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "aemo_spike_threshold" for t in n.targets)
        ):
            assign_node = n
            break
    assert assign_node is not None, (
        "expected an `aemo_spike_threshold = ...` assignment feeding is_spike "
        "in the VPP AEMO spike setup"
    )
    segment = ast.get_source_segment(source, assign_node)
    assert segment is not None

    namespace = {
        "CONF_AEMO_SPIKE_THRESHOLD": CONF_AEMO_SPIKE_THRESHOLD,
        "entry": entry,
    }
    exec(segment.strip(), namespace)
    return namespace["aemo_spike_threshold"]


def test_vpp_spike_threshold_honors_configured_value():
    entry = SimpleNamespace(
        options={CONF_AEMO_SPIKE_THRESHOLD: 500.0},
        data={},
    )
    assert _resolve_vpp_threshold(entry) == 500.0


def test_vpp_spike_threshold_falls_back_to_default_when_unset():
    entry = SimpleNamespace(options={}, data={})
    assert _resolve_vpp_threshold(entry) == 3000

    entry_data_only = SimpleNamespace(options={}, data={CONF_AEMO_SPIKE_THRESHOLD: 250.0})
    assert _resolve_vpp_threshold(entry_data_only) == 250.0
