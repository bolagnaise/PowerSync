"""Capability-matrix tests for Profit Max direct-solar export holds."""
from __future__ import annotations

import ast
import importlib.util
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "solar_export.py"
)
_SPEC = importlib.util.spec_from_file_location("solar_export_capabilities", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
resolve_solar_export_adapter = _MODULE.resolve_solar_export_adapter


_CAPABILITY_METHODS = (
    "_solar_export_capability",
    "_sync_solar_export_capability_notice",
    "_sync_solar_export_limit_issue",
)


def _load_solar_export_capability_method(
    warnings: list[str], notices: list[str] | None = None
):
    """Exec the capability methods standalone, without a real coordinator."""
    path = _PATH.with_name("coordinator.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "OptimizationCoordinator"
    )
    methods = [
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name in _CAPABILITY_METHODS
    ]
    assert {node.name for node in methods} == set(_CAPABILITY_METHODS)
    module_constants = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id
            in {"_SOLAR_EXPORT_LIMIT_REASONS", "_SOLAR_EXPORT_NOTICE_UNSYNCED"}
            for target in node.targets
        )
    ]
    recorded = notices if notices is not None else []
    namespace = {
        "Any": Any,
        "math": math,
        "frozenset": frozenset,
        "_LOGGER": SimpleNamespace(
            warning=lambda message, *args: (
                warnings.append(message % args if args else message)
            ),
            info=lambda message, *args: (
                recorded.append(message % args if args else message)
            ),
            debug=lambda message, *args: None,
        ),
    }
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=[*module_constants, *methods], type_ignores=[])
            ),
            str(path),
            "exec",
        ),
        namespace,
    )

    def _call(coordinator):
        for name in _CAPABILITY_METHODS[1:]:
            setattr(
                coordinator,
                name,
                lambda *args, _fn=namespace[name]: _fn(coordinator, *args),
            )
        return namespace["_solar_export_capability"](coordinator)

    return _call


class _LimitController:
    async def get_charge_rate_limit_kw(self):
        return 5.0


class _SolaxController:
    def __init__(self):
        self._entity_map = {"charge_current": "number.solax_charge_current"}

    def _ensure_entity_map(self):
        return None

    def _read_float(self, key):
        return 25.0 if key == "charge_current" else None


class _FroniusController:
    def get_status(self):
        return {"mode": "Auto"}


class _NeovoltChild:
    def get_dispatch_mode(self):
        return "Normal"


def test_every_configured_system_resolves_an_explicit_capability():
    systems = {
        "tesla",
        "sigenergy",
        "sungrow",
        "foxess",
        "goodwe",
        "alphaess",
        "esy_sunhome",
        "solax",
        "saj_h2",
        "fronius_reserva",
        "neovolt",
        "solaredge",
        "anker_solix",
        "custom",
    }
    for system in systems:
        capability = resolve_solar_export_adapter(system, SimpleNamespace()).capability()
        assert capability.reason


def test_explicit_negative_capability_reasons_are_stable():
    expected = {
        "tesla": "no_independent_charge_block",
        "goodwe": "no_proven_reversible_charge_block",
        "alphaess": "no_proven_reversible_charge_block",
        "esy_sunhome": "no_independent_charge_block",
        "saj_h2": "no_proven_reversible_charge_block",
        "solaredge": "no_proven_reversible_charge_block",
        "anker_solix": "no_proven_reversible_charge_block",
        "custom": "no_safe_semantic_charge_block_configured",
    }
    for system, reason in expected.items():
        capability = resolve_solar_export_adapter(system, SimpleNamespace()).capability()
        assert not capability.supported
        assert capability.reason == reason


def test_positive_control_planes_publish_versioned_adapter_identity():
    positive = {
        "sigenergy": (
            SimpleNamespace(
                _controller=_LimitController(),
                data={"export_limit_kw": 5.0},
            ),
            "sigenergy.modbus.charge_limit.v1",
        ),
        "sungrow": (
            SimpleNamespace(
                data={"charge_rate_limit_kw": 5.0, "export_limit_w": 5000},
                set_charge_rate_limit=lambda value: None,
            ),
            "sungrow.modbus.charge_limit.v1",
        ),
        "solax": (
            SimpleNamespace(_controller=_SolaxController(), data={}),
            "solax.entity.charge_current.v1",
        ),
        "fronius_reserva": (
            SimpleNamespace(_controller=_FroniusController(), data={}),
            "fronius_reserva.entity.block_charging.v1",
        ),
        "neovolt": (
            SimpleNamespace(
                _controller=SimpleNamespace(_controllers=[_NeovoltChild()]),
                data={},
            ),
            "neovolt.entity.no_battery_charge.v1",
        ),
    }
    for system, (coordinator, adapter_key) in positive.items():
        capability = resolve_solar_export_adapter(system, coordinator).capability()
        assert capability.supported
        assert capability.adapter == adapter_key
        assert capability.targets == 1


def test_foxess_control_planes_are_distinct_and_fail_closed_without_readback():
    modbus_type = type("FoxESSEnergyCoordinator", (), {})
    entity_type = type("FoxESSEntityEnergyCoordinator", (), {})
    cloud_type = type("FoxESSCloudEnergyCoordinator", (), {})

    modbus = modbus_type()
    modbus.data = {"max_charge_current_a": 40.0}
    entity = entity_type()
    entity.data = {"max_charge_current_a": 35.0}
    cloud = cloud_type()
    cloud.data = {}
    cloud._client = SimpleNamespace(get_device_setting=lambda key: None)

    assert resolve_solar_export_adapter("foxess", modbus).capability().adapter == (
        "foxess.modbus.max_charge_current.v1"
    )
    assert resolve_solar_export_adapter("foxess", entity).capability().adapter == (
        "foxess.entity.max_charge_current.v1"
    )
    assert resolve_solar_export_adapter("foxess", cloud).capability().adapter == (
        "foxess.cloud.max_charge_current.v1"
    )

    missing = modbus_type()
    missing.data = {"max_charge_current_a": None}
    capability = resolve_solar_export_adapter("foxess", missing).capability()
    assert not capability.supported
    assert capability.reason == "charge_limit_readback_unavailable"


def test_fronius_requires_normal_auto_baseline_before_advertising_hold():
    controller = _FroniusController()
    coordinator = SimpleNamespace(_controller=controller, data={})
    controller.get_status = lambda: {"mode": "Charge from Grid"}

    capability = resolve_solar_export_adapter(
        "fronius_reserva", coordinator
    ).capability()

    assert not capability.supported
    assert capability.reason == "storage_not_in_normal_auto_mode"


def test_fronius_rejects_stale_auto_entities_when_upstream_is_not_loaded():
    controller = _FroniusController()
    controller.upstream_integration_status = lambda: {
        "domain": "fronius_modbus",
        "state": "setup_error",
        "loaded": False,
    }
    coordinator = SimpleNamespace(_controller=controller, data={})

    capability = resolve_solar_export_adapter(
        "fronius_reserva", coordinator
    ).capability()

    assert not capability.supported
    assert capability.reason == "upstream_integration_not_loaded"
    assert capability.upstream_domain == "fronius_modbus"
    assert capability.upstream_state == "setup_error"
    assert capability.as_dict()["upstream_state"] == "setup_error"


def test_upstream_outage_warning_is_deduplicated_and_resets_on_recovery():
    warnings: list[str] = []
    capability_method = _load_solar_export_capability_method(warnings)
    state = {"loaded": False}

    class _Hold:
        def capability(self):
            if state["loaded"]:
                return {
                    "supported": True,
                    "reason": "supported",
                    "export_limit_kw": 5.0,
                }
            return {
                "supported": False,
                "reason": "upstream_integration_not_loaded",
                "upstream_domain": "fronius_modbus",
                "upstream_state": "setup_error",
            }

    coordinator = SimpleNamespace(
        _solar_export_hold=_Hold(),
        _config=SimpleNamespace(max_grid_export_w=5000),
        _monitoring_mode_active=lambda: False,
        _last_solar_export_upstream_outage=None,
        hass=SimpleNamespace(data={}),
        entry_id="entry-1",
    )

    capability_method(coordinator)
    capability_method(coordinator)
    assert len(warnings) == 1

    state["loaded"] = True
    assert capability_method(coordinator)["supported"] is True
    state["loaded"] = False
    capability_method(coordinator)
    assert len(warnings) == 2


def test_missing_site_export_limit_warns_once_with_the_setting_name():
    warnings: list[str] = []
    notices: list[str] = []
    capability_method = _load_solar_export_capability_method(warnings, notices)

    coordinator = SimpleNamespace(
        _solar_export_hold=SimpleNamespace(
            capability=lambda: {
                "supported": True,
                "reason": "supported",
                "adapter": "fronius_reserva.entity.block_charging.v1",
            }
        ),
        _config=SimpleNamespace(max_grid_export_w=None),
        _monitoring_mode_active=lambda: False,
        _last_solar_export_upstream_outage=None,
        hass=SimpleNamespace(data={}),
        entry_id="entry-1",
    )

    first = capability_method(coordinator)
    capability_method(coordinator)

    assert first["supported"] is False
    assert first["reason"] == "export_limit_not_configured"
    assert len(warnings) == 1
    assert "Maximum grid export" in warnings[0]

    coordinator._config.max_grid_export_w = 0
    zero = capability_method(coordinator)

    assert zero["reason"] == "zero_export_site"
    assert len(warnings) == 1
    assert len(notices) == 1
