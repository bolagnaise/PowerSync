"""Regression tests for provider pricing/account sensor integration."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import types


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


FLOW_POWER_EXCLUSIVE_SENSOR_KEYS = {
    "flow_power_price",
    "flow_power_export_price",
    "flow_power_twap",
    "flow_power_network_tariff",
    "flow_power_amber_comparison",
    "fp_account_pea",
    "fp_account_pea_30d",
    "fp_account_bpea",
    "fp_account_cpea",
    "fp_account_pea_import",
    "fp_account_lwap",
    "fp_account_lwap_actual",
    "fp_account_twap",
    "fp_account_avg_rrp",
    "fp_account_dlf",
    "fp_account_avg_usage",
    "fp_account_max_usage",
}


def _load_flow_power_cleanup(entity_registry, device_registry):
    """Load the registry cleanup without importing the full HA sensor platform."""
    source = (COMPONENT_ROOT / "sensor.py").read_text()
    tree = ast.parse(source)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_cleanup_inactive_flow_power_registry"
        ),
        None,
    )
    assert function is not None, "Flow Power inactive-provider cleanup is missing"

    namespace = {
        "HomeAssistant": object,
        "ConfigEntry": object,
        "DOMAIN": "power_sync",
        "SENSOR_FAMILY_FLOW_POWER": "flow_power",
        "_FLOW_POWER_EXCLUSIVE_SENSOR_KEYS": FLOW_POWER_EXCLUSIVE_SENSOR_KEYS,
        "_LOGGER": SimpleNamespace(
            debug=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
        ),
        "er": SimpleNamespace(async_get=lambda _hass: entity_registry),
        "dr": SimpleNamespace(async_get=lambda _hass: device_registry),
    }
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "sensor.py", "exec"), namespace)
    return namespace["_cleanup_inactive_flow_power_registry"]


class _FakeEntityRegistry:
    def __init__(self, entries):
        self.entities = {entry.entity_id: entry for entry in entries}
        self.removed: list[str] = []

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)
        self.entities.pop(entity_id, None)


class _FakeDeviceRegistry:
    def __init__(self, devices):
        self.devices = {device.id: device for device in devices}
        self.detached: list[tuple[str, str]] = []

    def async_update_device(self, device_id: str, *, remove_config_entry_id: str):
        self.detached.append((device_id, remove_config_entry_id))
        self.devices[device_id].config_entries.discard(remove_config_entry_id)


def test_provider_pricing_device_helper_links_to_entry_device():
    """Provider pricing devices should be linked under the PowerSync entry."""
    source = (COMPONENT_ROOT / "const.py").read_text()

    assert "def provider_pricing_device_info(" in source
    assert '"name": name' in source
    assert '"model": "Electricity Pricing"' in source
    assert 'info["via_device_id"] = parent_id' in source
    assert '"via_device": (DOMAIN, entry_id)' not in source
    assert 'f"{entry_id}_{provider_key}_pricing"' in source
    assert 'name = "GloBird Pricing"' in source
    assert 'name = "Flow Power Pricing"' in source


def test_provider_pricing_registration_uses_resolved_parent_id():
    """Provider registration must submit a resolved parent device ID."""
    source = (COMPONENT_ROOT / "const.py").read_text()
    tree = ast.parse(source)
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "provider_pricing_device_info"
    )
    namespace = {
        "DOMAIN": "power_sync",
        "SENSOR_FAMILY_GLOBIRD": "globird",
        "SENSOR_FAMILY_FLOW_POWER": "flow_power",
        "Any": object,
        "_entry_device_id": lambda _hass, _entry_id: "parent-id",
    }
    module = ast.Module(body=[helper], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "const.py", "exec"), namespace)

    info = namespace["provider_pricing_device_info"](
        "entry-1",
        "globird",
        hass=object(),
    )
    assert info["via_device_id"] == "parent-id"
    assert "via_device" not in info


def test_provider_pricing_resolves_parent_without_deprecated_registry_lookup(monkeypatch):
    """Parent lookup must use the scoped HA API before entity registration resumes."""
    source = (COMPONENT_ROOT / "const.py").read_text()
    tree = ast.parse(source)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_device_id_for_identifiers",
            "_entry_device_id",
            "powerwall_device_info",
            "provider_pricing_device_info",
        }
    ]

    class FakeRegistry:
        def __init__(self):
            self.created = None

        def async_get_device_by_identifier(self, identifier, config_entry_id):
            assert identifier == ("power_sync", "entry-1")
            assert config_entry_id == "entry-1"
            return None

        def async_get_or_create(self, **kwargs):
            self.created = kwargs
            return SimpleNamespace(id="parent-id")

    registry = FakeRegistry()
    fake_dr = types.ModuleType("homeassistant.helpers.device_registry")
    fake_dr.async_get = lambda _hass: registry
    fake_helpers = types.ModuleType("homeassistant.helpers")
    fake_helpers.device_registry = fake_dr
    fake_homeassistant = types.ModuleType("homeassistant")
    fake_homeassistant.helpers = fake_helpers
    monkeypatch.setitem(sys.modules, "homeassistant", fake_homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", fake_helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", fake_dr)

    namespace = {
        "DOMAIN": "power_sync",
        "SENSOR_FAMILY_GLOBIRD": "globird",
        "SENSOR_FAMILY_FLOW_POWER": "flow_power",
        "Any": object,
        "_LOGGER": SimpleNamespace(debug=lambda *_args, **_kwargs: None),
    }
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "const.py", "exec"), namespace)

    info = namespace["provider_pricing_device_info"](
        "entry-1",
        "globird",
        hass=object(),
    )
    assert info["via_device_id"] == "parent-id"
    assert registry.created == {
        "config_entry_id": "entry-1",
        "identifiers": {("power_sync", "entry-1")},
    }

    powerwall_info = namespace["powerwall_device_info"](
        "entry-1",
        hass=object(),
    )
    assert powerwall_info["via_device_id"] == "parent-id"


def test_globird_setup_creates_coordinator_and_gated_sensors():
    """GloBird portal sensors should only be added when credentials are configured."""
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    sensor_source = (COMPONENT_ROOT / "sensor.py").read_text()

    assert 'if electricity_provider == "globird":' in init_source
    assert "CONF_GLOBIRD_EMAIL" in init_source
    assert "CONF_GLOBIRD_PASSWORD" in init_source
    assert "GloBirdCoordinator(hass, entry)" in init_source
    assert "async_config_entry_first_refresh()" in init_source
    assert '"globird_coordinator": globird_coordinator' in init_source
    assert "await globird_coordinator.async_shutdown()" in init_source

    assert 'if electricity_provider == "globird" and globird_coordinator:' in sensor_source
    assert "build_globird_entities(globird_coordinator, entry)" in sensor_source


def test_globird_cold_start_failure_keeps_recovery_and_defers_account_entities():
    """A failed uncached portal refresh must remain retryable and attach later."""
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    sensor_source = (COMPONENT_ROOT / "sensor.py").read_text()
    coordinator_source = (COMPONENT_ROOT / "globird_coordinator.py").read_text()

    init_tree = ast.parse(init_source)
    globird_branch = next(
        node
        for node in ast.walk(init_tree)
        if isinstance(node, ast.If)
        and "electricity_provider == \"globird\""
        in (ast.get_source_segment(init_source, node.test) or "")
        and "GloBirdCoordinator" in (ast.get_source_segment(init_source, node) or "")
    )
    branch_source = ast.get_source_segment(init_source, globird_branch)

    assert branch_source is not None
    assert "globird_initial_refresh_failed = True" in branch_source
    assert "async_shutdown()" not in branch_source
    assert "globird_coordinator = None" not in branch_source
    assert "async_start_initial_retry()" in init_source
    assert "_initial_retry_task" in coordinator_source
    assert "_GLOBIRD_MAX_INITIAL_RETRY_DELAY_SECONDS" in coordinator_source
    assert "globird_sensor_unsub" in sensor_source
    assert "globird_entity_unique_id" in sensor_source
    assert "async_add_listener" in sensor_source


def test_globird_initial_retry_recovers_after_transient_failure():
    """The bounded cold-start retry must keep trying until data is published."""
    source = (COMPONENT_ROOT / "globird_coordinator.py").read_text()
    tree = ast.parse(source)
    coordinator_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GloBirdCoordinator"
    )
    methods = [
        node
        for node in coordinator_class.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {"_async_retry_initial_refresh"}
    ]
    start_method = next(
        node
        for node in coordinator_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "async_start_initial_retry"
    )
    methods.append(start_method)

    recorded_delays: list[int] = []

    async def _sleep_and_record(delay: int) -> None:
        recorded_delays.append(delay)

    namespace = {
        "asyncio": SimpleNamespace(
            CancelledError=asyncio.CancelledError,
            sleep=_sleep_and_record,
            create_task=asyncio.create_task,
        ),
        "DOMAIN": "power_sync",
        "_LOGGER": SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
        ),
        "_GLOBIRD_INITIAL_RETRY_DELAY_SECONDS": 30,
        "_GLOBIRD_MAX_INITIAL_RETRY_DELAY_SECONDS": 300,
    }
    module = ast.Module(body=methods, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "globird_coordinator.py", "exec"), namespace)

    async def exercise() -> int:
        attempts = 0

        class FakeHass:
            def __init__(self) -> None:
                self.tasks: list[asyncio.Task[None]] = []

            def async_create_task(self, coroutine, *, name: str):
                task = asyncio.create_task(coroutine, name=name)
                self.tasks.append(task)
                return task

        coordinator = SimpleNamespace(
            hass=FakeHass(),
            _initial_retry_task=None,
            last_update_success=False,
        )

        async def _refresh() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary portal failure")
            coordinator.last_update_success = True

        coordinator.async_refresh = _refresh
        coordinator._async_retry_initial_refresh = namespace[
            "_async_retry_initial_refresh"
        ].__get__(coordinator)
        namespace["async_start_initial_retry"].__get__(coordinator)()
        await coordinator.hass.tasks[0]
        return attempts

    attempts = asyncio.run(exercise())
    assert recorded_delays == [30, 60]
    assert attempts == 2


def test_globird_sensors_use_linked_device_and_stable_object_ids():
    """Ported GloBird sensors should use PowerSync-native IDs and device info."""
    source = (COMPONENT_ROOT / "globird_sensors.py").read_text()

    assert "def build_globird_entities(" in source
    assert "GloBirdLatestDayCostSensor" in source
    assert 'sensor_key = "latest_day_cost"' in source
    assert "provider_pricing_device_info(" in source
    assert "SENSOR_FAMILY_GLOBIRD" in source
    assert 'return "_".join(["power_sync", SENSOR_FAMILY_GLOBIRD, *safe_parts])' in source
    assert "usage_attributes(" in source
    assert "cost_attributes(" in source


def test_flow_power_api_account_sensors_use_provider_device_and_object_ids():
    """Flow Power API account sensors should retain their stable entity IDs."""
    source = (COMPONENT_ROOT / "sensor.py").read_text()
    const_source = (COMPONENT_ROOT / "const.py").read_text()

    assert "FLOW_POWER_ACCOUNT_SENSORS = [" in const_source
    assert '"fp_account_pea"' in const_source
    assert '"fp_account_lwap"' in const_source
    assert '"fp_account_avg_usage"' in const_source
    assert '"fp_account_max_usage"' in const_source
    assert "CONF_FLOWPOWER_API_KEY" in source
    assert "if fp_api_key:" in source
    assert "FlowPowerAccountSensor(" in source
    assert "return provider_pricing_device_info(" in source
    assert "SENSOR_FAMILY_FLOW_POWER" in source
    assert 'self._attr_suggested_object_id = f"power_sync_{sensor_type}"' in source
    assert "network_tariff_raw" in source


def test_inactive_flow_power_registry_cleanup_is_exact_and_idempotent():
    """Switching to AGL removes only Flow-exclusive registry rows and device."""
    entry_id = "entry-1"
    flow_entries = [
        SimpleNamespace(
            entity_id=f"sensor.power_sync_{sensor_key}",
            platform="power_sync",
            config_entry_id=entry_id,
            unique_id=(
                f"{entry_id}_{sensor_key}"
                if index % 2 == 0
                else f"power_sync_{entry_id}_{sensor_key}"
            ),
        )
        for index, sensor_key in enumerate(sorted(FLOW_POWER_EXCLUSIVE_SENSOR_KEYS))
    ]
    shared_and_unrelated = [
        SimpleNamespace(
            entity_id="sensor.power_sync_current_import_price",
            platform="power_sync",
            config_entry_id=entry_id,
            unique_id=f"{entry_id}_current_import_price",
        ),
        SimpleNamespace(
            entity_id="sensor.other_flow_power_price",
            platform="other_integration",
            config_entry_id="other-entry",
            unique_id="other-entry_flow_power_price",
        ),
    ]
    entity_registry = _FakeEntityRegistry(flow_entries + shared_and_unrelated)
    flow_device = SimpleNamespace(
        id="flow-device",
        identifiers={("power_sync", f"{entry_id}_flow_power_pricing")},
        config_entries={entry_id},
    )
    parent_device = SimpleNamespace(
        id="parent-device",
        identifiers={("power_sync", entry_id)},
        config_entries={entry_id},
    )
    device_registry = _FakeDeviceRegistry([flow_device, parent_device])
    cleanup = _load_flow_power_cleanup(entity_registry, device_registry)
    entry = SimpleNamespace(entry_id=entry_id)

    assert cleanup(object(), entry, "agl") == len(FLOW_POWER_EXCLUSIVE_SENSOR_KEYS)
    assert set(entity_registry.removed) == {
        f"sensor.power_sync_{sensor_key}"
        for sensor_key in FLOW_POWER_EXCLUSIVE_SENSOR_KEYS
    }
    assert "sensor.power_sync_current_import_price" in entity_registry.entities
    assert "sensor.other_flow_power_price" in entity_registry.entities
    assert device_registry.detached == [("flow-device", entry_id)]

    assert cleanup(object(), entry, "agl") == 0
    assert device_registry.detached == [("flow-device", entry_id)]


def test_active_flow_power_registry_cleanup_is_a_noop():
    """An active Flow configuration must retain its provider entities/device."""
    entry_id = "entry-1"
    entity_registry = _FakeEntityRegistry(
        [
            SimpleNamespace(
                entity_id="sensor.power_sync_flow_power_price",
                platform="power_sync",
                config_entry_id=entry_id,
                unique_id=f"{entry_id}_flow_power_price",
            )
        ]
    )
    device_registry = _FakeDeviceRegistry(
        [
            SimpleNamespace(
                id="flow-device",
                identifiers={("power_sync", f"{entry_id}_flow_power_pricing")},
                config_entries={entry_id},
            )
        ]
    )
    cleanup = _load_flow_power_cleanup(entity_registry, device_registry)

    assert cleanup(object(), SimpleNamespace(entry_id=entry_id), "flow_power") == 0
    assert entity_registry.removed == []
    assert device_registry.detached == []
