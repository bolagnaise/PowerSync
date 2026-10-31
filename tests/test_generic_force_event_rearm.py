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
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"

DOMAIN = "power_sync"
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_RESTORE_NORMAL = "restore_normal"
CONF_AEMO_REGION = "aemo_region"
CONF_AEMO_SPIKE_ENABLED = "aemo_spike_enabled"
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
        "asyncio": asyncio,
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

    async def check_price_spike(self, region, threshold, *, was_active=False):
        return self.is_spike, self.price, {}


def _force_discharge_calls(hass: _FakeHass):
    return [c for c in hass.services.calls if c[1] == SERVICE_FORCE_DISCHARGE]


def _generic_spike_manager():
    manager = object.__new__(_load_class("GenericAEMOSpikeManager"))
    manager.hass = _FakeHass()
    manager.entry = SimpleNamespace(entry_id="entry-1", options={"aemo_spike_enabled": True}, data={})
    manager.region, manager.threshold, manager._battery_type = "NSW1", 3000, "sigenergy"
    manager._in_spike_mode = False
    manager._spike_start_time = manager._last_price = manager._last_check = None
    manager._aemo_client = _FakeAemoClient(True, 5000)
    return manager


def test_generic_saved_disable_restores_active_spike_without_rearming():
    manager = _generic_spike_manager()
    async def run():
        await manager.check_and_handle_spike()
        manager.entry.options[CONF_AEMO_SPIKE_ENABLED] = False
        await manager.check_and_handle_spike()
        await manager.check_and_handle_spike()
    asyncio.run(run())
    assert len(_force_discharge_calls(manager.hass)) == 1
    assert [c[1] for c in manager.hass.services.calls] == [SERVICE_FORCE_DISCHARGE, SERVICE_RESTORE_NORMAL]
    assert not manager.in_spike_mode


def test_generic_disable_during_price_fetch_blocks_discharge():
    manager = _generic_spike_manager()
    async def fetch(*args, **kwargs):
        manager.entry.options[CONF_AEMO_SPIKE_ENABLED] = False
        return True, 5000, {}
    manager._aemo_client.check_price_spike = fetch
    asyncio.run(manager.check_and_handle_spike())
    assert manager.hass.services.calls == []


def test_generic_cleanup_preserves_a_new_manual_command():
    manager = _generic_spike_manager()
    manager._in_spike_mode = True
    manager.hass.data = {DOMAIN: {manager.entry.entry_id: {
        "force_discharge_state": {"active": True, "source": "user"},
    }}}
    asyncio.run(manager.stop())
    assert manager.hass.services.calls == []
    assert not manager.in_spike_mode


def test_generic_unconfirmed_restore_retains_cleanup_obligation():
    manager = _generic_spike_manager()
    manager._in_spike_mode = True
    manager.hass.data = {DOMAIN: {manager.entry.entry_id: {
        "force_discharge_state": {"active": True, "source": "aemo_spike"},
    }}}
    asyncio.run(manager.stop())
    assert manager.in_spike_mode
    assert [c[1] for c in manager.hass.services.calls] == [SERVICE_RESTORE_NORMAL]


def _vpp_spike_namespace(monkeypatch, *, enabled=True, monitoring=False, disable_during_fetch=False):
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    namespace = {
        "__package__": "power_sync", "DOMAIN": DOMAIN,
        "CONF_AEMO_SPIKE_ENABLED": CONF_AEMO_SPIKE_ENABLED,
        "SERVICE_FORCE_DISCHARGE": SERVICE_FORCE_DISCHARGE,
        "SERVICE_RESTORE_NORMAL": SERVICE_RESTORE_NORMAL,
        "_LOGGER": _FakeLogger(), "dt_util": _FakeDtUtil(),
        "aemo_region": "NSW1", "aemo_spike_threshold": 3000,
        "vpp_aemo_control_lock": asyncio.Lock(),
        "vpp_spike_state": {"stopped": False, "in_spike_mode": False, "spike_start_time": None},
        "entry": SimpleNamespace(entry_id="entry-1", options={CONF_AEMO_SPIKE_ENABLED: enabled}, data={}),
        "force_discharge_state": {"active": False},
        "_is_monitoring_mode": lambda: monitoring,
    }
    hass = _FakeHass()
    hass.data = {DOMAIN: {"entry-1": {}}}
    namespace["hass"] = hass
    fetches = []
    async def prices():
        fetches.append(True)
        if disable_during_fetch:
            namespace["entry"].options[CONF_AEMO_SPIKE_ENABLED] = False
        return {"NSW1": {"price": 5000}}
    async def service(domain, name, data, blocking=True):
        hass.services.calls.append((domain, name, data))
        if name == SERVICE_FORCE_DISCHARGE:
            namespace["force_discharge_state"].update(active=True, source=data.get("source"))
        else:
            namespace["force_discharge_state"]["active"] = False
    hass.services.async_call = service
    monkeypatch.setitem(sys.modules, "power_sync.aemo_api", SimpleNamespace(
        AEMOAPIClient=lambda session: SimpleNamespace(get_current_prices=prices),
    ))
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.aiohttp_client", SimpleNamespace(
        async_get_clientsession=lambda hass: None,
    ))
    # Pin the callback's actual lazy factory dependency. A different test's
    # power_sync stub must not decide whether the fake price fetch can start.
    monkeypatch.setitem(sys.modules, "power_sync.powerwall_control_http", SimpleNamespace(
        async_get_clientsession=lambda hass: None,
    ))
    for name in ("_vpp_aemo_commands_allowed", "_restore_vpp_aemo_spike",
                 "_check_aemo_spike_for_vpp", "check_aemo_spike_for_vpp"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<vpp-spike>", "exec"), namespace)
    return namespace, fetches


@pytest.mark.parametrize("enabled,monitoring,during_fetch", [(False, False, False), (True, True, False), (True, False, True)])
def test_vpp_saved_disable_or_monitoring_blocks_discharge(monkeypatch, enabled, monitoring, during_fetch):
    ns, fetches = _vpp_spike_namespace(monkeypatch, enabled=enabled, monitoring=monitoring,
                                      disable_during_fetch=during_fetch)
    asyncio.run(ns["check_aemo_spike_for_vpp"](None))
    assert ns["hass"].services.calls == []
    assert bool(fetches) == during_fetch


def test_vpp_disable_cleans_up_only_its_own_discharge(monkeypatch):
    ns, _ = _vpp_spike_namespace(monkeypatch)
    async def run():
        await ns["check_aemo_spike_for_vpp"](None)
        assert ns["force_discharge_state"]["source"] == "aemo_spike"
        ns["entry"].options[CONF_AEMO_SPIKE_ENABLED] = False
        await ns["check_aemo_spike_for_vpp"](None)
    asyncio.run(run())
    assert [c[1] for c in ns["hass"].services.calls] == [SERVICE_FORCE_DISCHARGE, SERVICE_RESTORE_NORMAL]
    assert not ns["vpp_spike_state"]["in_spike_mode"]


def test_vpp_cleanup_preserves_new_manual_command(monkeypatch):
    ns, _ = _vpp_spike_namespace(monkeypatch, enabled=False)
    ns["vpp_spike_state"]["in_spike_mode"] = True
    ns["force_discharge_state"].update(active=True, source="user")
    asyncio.run(ns["check_aemo_spike_for_vpp"](None))
    assert ns["hass"].services.calls == []
    assert ns["force_discharge_state"]["active"]


def test_vpp_unconfirmed_restore_is_retried(monkeypatch):
    ns, _ = _vpp_spike_namespace(monkeypatch, enabled=False)
    ns["vpp_spike_state"]["in_spike_mode"] = True
    ns["force_discharge_state"].update(active=True, source="aemo_spike")
    async def unconfirmed(domain, name, data, blocking=True):
        ns["hass"].services.calls.append((domain, name, data))
    ns["hass"].services.async_call = unconfirmed
    async def run():
        await ns["check_aemo_spike_for_vpp"](None)
        await ns["check_aemo_spike_for_vpp"](None)
    asyncio.run(run())
    assert ns["vpp_spike_state"]["in_spike_mode"]
    assert [c[1] for c in ns["hass"].services.calls] == [SERVICE_RESTORE_NORMAL, SERVICE_RESTORE_NORMAL]


def test_unload_retires_all_spike_handlers_before_joining_and_restoring():
    tree = ast.parse(INIT_PATH.read_text())
    unload = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_unload_entry")
    first = next(i for i, n in enumerate(unload.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "spike_managers" for t in n.targets))
    last = next(i for i, n in enumerate(unload.body) if isinstance(n, ast.If)
                and isinstance(n.test, ast.NamedExpr) and n.test.target.id == "vpp_restore")
    wrapper = ast.AsyncFunctionDef(name="shutdown", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=unload.body[first:last + 1], decorator_list=[])
    events = []
    runtime = {"vpp_spike_state": {"in_spike_mode": True, "stopped": False}}
    class Manager:
        _stopped = False
        async def stop(self):
            assert all(m._stopped for m in managers)
            assert runtime["vpp_spike_state"]["stopped"]
            events.append("manager restore")
    managers = [Manager(), Manager()]
    runtime.update(aemo_spike_manager=managers[0], generic_aemo_spike_manager=managers[1])
    for key in ("aemo_spike_cancel", "generic_aemo_spike_cancel", "vpp_aemo_cancel"):
        runtime[key] = lambda: events.append("cancel timer")
    async def pending_initial_check():
        assert all(m._stopped for m in managers)
        assert runtime["vpp_spike_state"]["stopped"]
        events.append("joined initial check")
    async def restore():
        assert runtime["vpp_aemo_control_lock"].locked()
        events.append("vpp restore")
    namespace = {"entry_data": runtime, "asyncio": asyncio, "_LOGGER": _FakeLogger()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "<spike-unload>", "exec"), namespace)
    async def run():
        runtime["vpp_aemo_control_lock"] = asyncio.Lock()
        runtime["vpp_aemo_restore"] = restore
        runtime["vpp_aemo_initial_task"] = asyncio.create_task(pending_initial_check())
        await namespace["shutdown"]()
    asyncio.run(run())
    assert events == ["cancel timer"] * 3 + ["joined initial check", "manager restore", "manager restore", "vpp restore"]


def test_generic_aemo_spike_re_arms_force_discharge_while_still_spiking():
    """OB-33: repeated ticks while a spike persists must keep re-issuing
    force_discharge, not just log "still in spike mode" (which lets the
    battery revert to self-consumption once the default 30-min duration
    from the initial call elapses)."""
    GenericAEMOSpikeManager = _load_class("GenericAEMOSpikeManager")

    manager = object.__new__(GenericAEMOSpikeManager)
    manager.hass = _FakeHass()
    manager.entry = SimpleNamespace(entry_id="entry-1", options={"aemo_spike_enabled": True}, data={})
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
    manager.entry = SimpleNamespace(entry_id="entry-1", options={"aemo_spike_enabled": True}, data={})
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


def _find_vpp_setup_guard() -> ast.If:
    """Locate the outer setup guard that owns the VPP spike closure."""
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    candidates = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.AsyncFunctionDef)
            and child.name == "check_aemo_spike_for_vpp"
            for child in node.body
        )
    ]
    assert len(candidates) == 1, (
        "expected exactly one setup guard containing "
        f"check_aemo_spike_for_vpp, found {len(candidates)}"
    )
    return candidates[0]


def _resolve_aemo_spike_enabled(entry) -> bool:
    """Execute the runtime options-over-data resolution used by setup."""
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    candidates = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "aemo_spike_enabled"
            for target in node.targets
        )
    ]
    assert len(candidates) == 1, (
        "expected exactly one aemo_spike_enabled assignment, "
        f"found {len(candidates)}"
    )
    segment = ast.get_source_segment(source, candidates[0])
    assert segment is not None
    namespace = {
        "CONF_AEMO_SPIKE_ENABLED": CONF_AEMO_SPIKE_ENABLED,
        "entry": entry,
    }
    exec(segment.strip(), namespace)
    return namespace["aemo_spike_enabled"]


def _vpp_setup_enabled(provider: str, aemo_spike_enabled: bool) -> bool:
    """Evaluate the current source guard with controlled setup state."""
    expression = ast.Expression(body=_find_vpp_setup_guard().test)
    ast.fix_missing_locations(expression)
    return bool(
        eval(
            compile(expression, "<vpp-setup-guard>", "eval"),
            {
                "aemo_spike_enabled": aemo_spike_enabled,
                "electricity_provider_for_vpp": provider,
            },
        )
    )


def test_vpp_spike_setup_honors_explicit_disabled_option():
    """A disabled GloBird spike toggle must not schedule force-discharge checks."""
    entry = SimpleNamespace(
        options={
            CONF_ELECTRICITY_PROVIDER: "globird",
            CONF_AEMO_SPIKE_ENABLED: False,
        },
        data={CONF_AEMO_SPIKE_ENABLED: True},
    )

    enabled = _resolve_aemo_spike_enabled(entry)
    assert enabled is False, "an explicit false option must override stale entry data"
    assert _vpp_setup_enabled("globird", enabled) is False


def test_vpp_spike_setup_still_runs_when_explicitly_enabled():
    entry = SimpleNamespace(
        options={
            CONF_ELECTRICITY_PROVIDER: "globird",
            CONF_AEMO_SPIKE_ENABLED: True,
        },
        data={},
    )

    enabled = _resolve_aemo_spike_enabled(entry)
    assert enabled is True
    assert _vpp_setup_enabled("globird", enabled) is True
    assert _vpp_setup_enabled("aemo_vpp", enabled) is True
    assert _vpp_setup_enabled("amber", enabled) is False


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
