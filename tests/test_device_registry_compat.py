"""Regression coverage for Home Assistant device-registry API compatibility."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import types


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
PRODUCTION_PATHS = (
    COMPONENT_ROOT / "__init__.py",
    COMPONENT_ROOT / "sensor.py",
    COMPONENT_ROOT / "automations" / "actions.py",
    COMPONENT_ROOT / "automations" / "ev_charging_planner.py",
    COMPONENT_ROOT / "optimization" / "ev_coordinator.py",
    COMPONENT_ROOT / "inverters" / "foxess_entity.py",
)


def _load_device_removal_callback():
    tree = ast.parse((COMPONENT_ROOT / "__init__.py").read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_remove_config_entry_device"
    )
    namespace = {
        "HomeAssistant": object,
        "ConfigEntry": object,
        "DOMAIN": "power_sync",
        "CONF_ELECTRICITY_PROVIDER": "electricity_provider",
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "__init__.py", "exec"), namespace)
    return namespace["async_remove_config_entry_device"]


def test_inactive_globird_pricing_device_can_be_removed_after_switching_to_amber():
    """An old provider child must not be permanently attached to the new hub."""
    callback = _load_device_removal_callback()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"electricity_provider": "globird"},
        options={"electricity_provider": "amber"},
    )
    device = SimpleNamespace(identifiers={("power_sync", "entry-1_globird_pricing")})
    assert asyncio.run(callback(object(), entry, device)) is True


def test_device_removal_preserves_active_provider_and_other_devices():
    callback = _load_device_removal_callback()
    cases = [
        # Options are the effective provider even when data still says Amber.
        ({"electricity_provider": "amber"}, {"electricity_provider": "globird"},
         {("power_sync", "entry-1_globird_pricing")}, False),
        ({"electricity_provider": "globird"}, {},
         {("power_sync", "entry-1_globird_pricing")}, False),
        ({}, {}, {("power_sync", "entry-1_globird_pricing")}, False),
        ({"electricity_provider": "globird"}, {"electricity_provider": ""},
         {("power_sync", "entry-1_globird_pricing")}, False),
        ({"electricity_provider": "amber"}, {},
         {("power_sync", "entry-1_globird_pricing")}, True),
        ({"electricity_provider": "amber"}, {},
         {("power_sync", "entry-2_globird_pricing")}, False),
        ({"electricity_provider": "amber"}, {},
         {("another_integration", "entry-1_globird_pricing")}, False),
        ({"electricity_provider": "amber"}, {},
         {("power_sync", "entry-1")}, False),
        ({"electricity_provider": "amber"}, {},
         {("power_sync", "entry-1_powerwall")}, False),
        ({"electricity_provider": "amber"}, {},
         {("power_sync", "entry-1_flow_power_pricing")}, False),
        ({"electricity_provider": "globird"}, {},
         {("power_sync", "entry-1_pw_0")}, True),
        ({"electricity_provider": "amber"}, {}, set(), False),
    ]
    for data, options, identifiers, expected in cases:
        entry = SimpleNamespace(entry_id="entry-1", data=data, options=options)
        device = SimpleNamespace(identifiers=identifiers)
        assert asyncio.run(callback(object(), entry, device)) is expected


def _compat_module():
    spec = importlib.util.spec_from_file_location(
        "registry_compat",
        COMPONENT_ROOT / "registry_compat.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_device_iterator_preserves_old_mapping_and_new_iterable_entries():
    module = _compat_module()
    old_entries = {"a": SimpleNamespace(id="a")}
    new_entries = [SimpleNamespace(id="b")]

    assert [entry.id for entry in module.iter_device_entries(SimpleNamespace(devices=old_entries))] == ["a"]
    assert [entry.id for entry in module.iter_device_entries(SimpleNamespace(devices=new_entries))] == ["b"]


def test_production_code_never_uses_deprecated_device_mapping_methods():
    deprecated: list[str] = []
    for path in PRODUCTION_PATHS:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in {"values", "get"}:
                continue
            if isinstance(node.value, ast.Attribute) and node.value.attr == "devices":
                deprecated.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert not deprecated, "deprecated DeviceRegistry.devices access: " + ", ".join(deprecated)


def _load_child_device_helpers(monkeypatch, registry):
    """Load the device-info helpers with a controlled HA registry contract."""
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
            "powerwall_block_device_info",
        }
    ]

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
        "device_parent_info": _compat_module().device_parent_info,
    }
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "const.py", "exec"), namespace)
    return namespace


class _LegacyRegistry:
    def async_get_device_by_identifier(self, _identifier, _config_entry_id):
        return SimpleNamespace(id="parent-id")

    def async_get_or_create(
        self, *, config_entry_id, identifiers, via_device=None
    ):
        return SimpleNamespace(id="parent-id")


class _ModernRegistry:
    def async_get_device_by_identifier(self, _identifier, _config_entry_id):
        return SimpleNamespace(id="parent-id")

    def async_get_or_create(
        self, *, config_entry_id, identifiers, via_device_id=None
    ):
        return SimpleNamespace(id="parent-id")


def _register_legacy_child(**kwargs):
    """Model the supported legacy registration boundary strictly."""
    allowed = {
        "config_entry_id",
        "config_subentry_id",
        "identifiers",
        "name",
        "manufacturer",
        "model",
        "via_device",
    }
    unexpected = set(kwargs) - allowed
    if unexpected:
        raise TypeError(f"unexpected legacy fields: {sorted(unexpected)}")
    assert kwargs["via_device"] == ("power_sync", "entry-1") or kwargs[
        "via_device"
    ] == ("power_sync", "entry-1_powerwall")


def _register_modern_child(**kwargs):
    """Model the supported modern registration boundary strictly."""
    allowed = {
        "config_entry_id",
        "config_subentry_id",
        "identifiers",
        "name",
        "manufacturer",
        "model",
        "via_device_id",
    }
    unexpected = set(kwargs) - allowed
    if unexpected:
        raise TypeError(f"unexpected modern fields: {sorted(unexpected)}")
    assert kwargs["via_device_id"] == "parent-id"


def test_child_device_info_uses_only_installed_registry_contract(monkeypatch):
    """Powerwall/provider children bind on both supported HA API shapes."""
    for registry, parent_key, register in (
        (_LegacyRegistry(), "via_device", _register_legacy_child),
        (_ModernRegistry(), "via_device_id", _register_modern_child),
    ):
        helpers = _load_child_device_helpers(monkeypatch, registry)
        infos = (
            helpers["powerwall_device_info"]("entry-1", hass=object()),
            helpers["provider_pricing_device_info"](
                "entry-1", "globird", hass=object()
            ),
            helpers["powerwall_block_device_info"](
                "entry-1", 0, hass=object()
            ),
        )

        for info in infos:
            assert parent_key in info
            assert len({"via_device", "via_device_id"} & info.keys()) == 1
            register(
                config_entry_id="entry-1",
                config_subentry_id=None,
                **info,
            )
