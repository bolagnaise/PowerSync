"""Regression coverage for shared-profile charger and session-policy routing."""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS


ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "power_sync"


def _install_runtime_modules() -> None:
    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT)]
    sys.modules["power_sync"] = power_sync
    automations = types.ModuleType("power_sync.automations")
    automations.__path__ = [str(COMPONENT / "automations")]
    sys.modules["power_sync.automations"] = automations
    const = types.ModuleType("power_sync.const")
    const.DOMAIN = "power_sync"
    sys.modules[const.__name__] = const


def _read_source(path: str) -> str:
    return (COMPONENT / path).read_text()


def _load_ownership_modules() -> None:
    _install_runtime_modules()
    for leaf in ("ev_vehicle_capacity", "ev_loadpoint_profiles", "ev_ownership"):
        module = types.ModuleType(f"power_sync.automations.{leaf}")
        sys.modules[module.__name__] = module
        source = _read_source(f"automations/{leaf}.py")
        exec(compile(source, leaf, "exec"), vars(module))


def _planner_nodes(*names: str) -> dict[str, object]:
    tree = ast.parse(_read_source("automations/ev_charging_planner.py"))
    found = {}
    for name in names:
        found[name] = next(
            node for node in ast.walk(tree) if getattr(node, "name", None) == name
        )
    return found


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


def _hass(data: dict) -> NS:
    return NS(
        data={
            "power_sync": {
                "entry-1": {"automation_store": NS(_data=data)},
            }
        }
    )


def test_selected_shared_profile_supplies_generic_and_sigenergy_limits():
    _load_ownership_modules()
    nodes = _planner_nodes("_get_vehicle_charger_params", "_effective_vehicle_charging_config")
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    namespace = {
        "__name__": "power_sync.automations.test_runtime",
        "__package__": "power_sync.automations",
        "_safe_vehicle_charging_configs": lambda _entry, configs: [
            config for config in configs if isinstance(config, dict)
        ],
        "_vehicle_config_matches": lambda left, right: str(left) == str(right),
        "_vehicle_config_value": lambda config, preferred, fallback, default: config.get(
            preferred, config.get(fallback, default)
        ),
        "get_auto_schedule_executor": lambda: None,
    }
    unit = ast.Module(body=[future, *nodes.values()], type_ignores=[])
    exec(
        compile(ast.fix_missing_locations(unit), "planner_selection", "exec"),
        namespace,
    )

    for charger_type, extra in (
        ("generic", {"charger_switch_entity": "switch.driveway_charge"}),
        (
            "sigenergy",
            {
                "sigenergy_charger_host": "10.0.0.5",
                "sigenergy_charger_port": 502,
                "sigenergy_charger_slave_id": 247,
            },
        ),
    ):
        configs = [
            {
                "vehicle_id": "car-one",
                "charger_type": charger_type,
                "min_charge_amps": 6,
                "max_charge_amps": 16,
                **extra,
            },
            {
                "vehicle_id": "car-two",
                "charger_type": charger_type,
                "min_charge_amps": 8,
                "max_charge_amps": 32,
                **extra,
            },
        ]
        profiles = sys.modules["power_sync.automations.ev_loadpoint_profiles"]
        loadpoint = profiles.physical_loadpoint_id(configs[0])
        data = {
            "vehicle_charging_configs": configs,
            "connected_ev_by_loadpoint": {loadpoint: "car-two"},
        }
        params = namespace["_get_vehicle_charger_params"](
            _hass(data), "power_sync", _Entry(), loadpoint
        )
        assert params["_configured_vehicle_id"] == "car-two"
        assert params["max_charge_amps"] == 32

        unselected = namespace["_get_vehicle_charger_params"](
            _hass({"vehicle_charging_configs": configs}),
            "power_sync",
            _Entry(),
            loadpoint,
        )
        assert "_configured_vehicle_id" not in unselected


def test_explicit_external_session_actions_reach_non_tesla_runtime_boundary():
    _load_ownership_modules()
    nodes = _planner_nodes(
        "_claim_external_charging_ownership",
        "_external_session_policy",
        "_evaluate_non_tesla_loadpoint",
    )
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    namespace = {
        "__name__": "power_sync.automations.test_runtime",
        "__package__": "power_sync.automations",
        "AWAY_EV_DECISIONS": set(),
    }
    unit = ast.Module(body=[future, *nodes.values()], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), "planner_policy", "exec"), namespace)

    async def run(policy: str) -> tuple[int, str, bool]:
        data = {
            "vehicle_charging_configs": [
                {
                    "vehicle_id": "generic_ev",
                    "charger_type": "generic",
                    "external_control_policy": "override",
                    "external_session_policy": {"price_level": policy},
                }
            ]
        }
        hass = _hass(data)
        state = NS(is_charging=False, last_decision="waiting", last_decision_reason="")
        stop_calls: list[dict] = []

        async def decision(*_args, **_kwargs):
            return False, "price outside opportunity window", "opportunity"

        async def stop(*_args, **kwargs):
            stop_calls.append(kwargs)
            return True

        async def preserve(*_args):
            return None

        executor = NS(
            hass=hass,
            config_entry=_Entry(),
            _domain="power_sync",
            _state=NS(last_decision="waiting"),
            get_charging_decision=decision,
            _get_or_create_vehicle_state=lambda _vehicle_id: state,
            _has_current_non_tesla_charge=lambda _vehicle_id: True,
            _stop_charging=stop,
            apply_preserve_home_battery=preserve,
        )
        await namespace["_evaluate_non_tesla_loadpoint"](
            executor,
            "generic_ev",
            50,
        )
        ownership = sys.modules["power_sync.automations.ev_ownership"]
        lease = ownership.get_ev_ownership(hass, _Entry(), "generic_ev")[1]
        return len(stop_calls), state.last_decision, bool(lease)

    assert asyncio.run(run("adopt")) == (1, "stopped", False)
    assert asyncio.run(run("abort")) == (1, "stopped", False)
    assert asyncio.run(run("leave_alone")) == (0, "external", False)
