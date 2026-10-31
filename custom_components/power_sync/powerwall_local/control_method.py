"""Tesla control preference and conservative actuator selection.

A preference applies to Max and optimiser actions. Explicit numeric requests
retain their local watt target. No preference overrides control admission.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

CONF_CONTROL_METHOD = "tesla_control_method"
METHODS = ("tariff", "local", "auto")
LABELS = {"tariff": "Tariff", "local": "Local V1R", "auto": "Automatic"}
DEFAULT_METHOD = "tariff"
LOCAL_MAX_W = 10000


class ControlTargetKind(str, Enum):
    """Physical quantity requested, independent of the control preference."""

    NATIVE_MAX = "native_max"
    BATTERY_WATTS = "battery_watts"
    GRID_EXPORT_WATTS = "grid_export_watts"
    SITE_IMPORT_WATTS = "site_import_watts"


@dataclass(frozen=True)
class ControlRoutePlan:
    route: str
    target_kind: ControlTargetKind


POLICY_DENIAL_REASONS = frozenset({
    "native_max_call_unavailable", "native_max_explicit_target_requires_local_control",
    "native_max_runtime_stale", "native_max_startup_pending",
    "native_max_dispatch_stopping", "native_max_control_method_changing",
    "native_max_local_recovery_pending", "native_max_settings_unresolved",
    "native_max_local_settings_suspended", "native_max_restart_restore_pending",
    "native_max_restore_pending", "native_max_max_backup_active",
    "native_max_monitoring_mode", "native_max_demand_charge_protection",
    "native_max_network_export_guard_active", "native_max_local_control_unconfigured",
    "native_max_license_denied", "native_max_policy_evidence_unavailable",
    "native_max_control_policy_denied",
})


@dataclass(frozen=True)
class ControlPolicyResult:
    """An admission decision, never an implicit truthy permission.

    Provider adapters validate the fields before accepting the result. A
    denial reason is selected from code-owned names to keep device-provided
    strings, credentials and identity values out of support diagnostics.
    """

    allowed: bool
    reason: str | None = None

    def __bool__(self):
        return self.allowed is True and self.reason is None


def control_policy_result(value: object) -> ControlPolicyResult:
    """Accept exact legacy booleans or a valid structured policy decision."""
    if value is True:
        return ControlPolicyResult(True)
    if isinstance(value, ControlPolicyResult) and type(value.allowed) is bool:
        if value.allowed and value.reason is None:
            return value
        if (not value.allowed and type(value.reason) is str
                and value.reason in POLICY_DENIAL_REASONS):
            return value
    return ControlPolicyResult(False, "native_max_control_policy_denied")


def native_control_policy(runtime: Mapping, *, call_present: bool, optimizer_call: bool,
                          targetless: bool, generation_current: bool,
                          monitoring_blocked: bool, demand_charge_protected: bool,
                          local_configured: bool, license_allowed: bool,
                          direction: str) -> ControlPolicyResult:
    """Explain current runtime admission without accessing HA or a device.

    Inputs are current synchronous checks, not saved authority. Restoration
    and signed-settings incidents remain blockers even after a supervised
    cloud handoff: that handoff does not restore local signed-write safety.
    """
    def denied(reason):
        return ControlPolicyResult(False, reason)

    decisions = (call_present, optimizer_call, targetless, generation_current,
                 monitoring_blocked, demand_charge_protected, local_configured,
                 license_allowed)
    if (not isinstance(runtime, Mapping) or direction not in ("charge", "discharge")
            or any(type(value) is not bool for value in decisions)):
        return denied("native_max_policy_evidence_unavailable")
    if not call_present:
        return denied("native_max_call_unavailable")
    if optimizer_call and not targetless:
        return denied("native_max_explicit_target_requires_local_control")
    if not generation_current:
        return denied("native_max_runtime_stale")
    for key, reason in (
        ("native_max_startup_pending", "native_max_startup_pending"),
        ("powerwall_local_dispatch_stopping", "native_max_dispatch_stopping"),
        ("tesla_control_method_changing", "native_max_control_method_changing"),
        ("powerwall_local_recovery_pending", "native_max_local_recovery_pending"),
    ):
        if runtime.get(key):
            return denied(reason)
    pending = runtime.get("powerwall_settings_pending") or runtime.get("tesla_settings_pending")
    if pending:
        return denied("native_max_local_settings_suspended" if isinstance(pending, Mapping)
                      and pending.get("resume_cloud") is True else "native_max_settings_unresolved")
    for key, reason in (
        ("optimizer_force_restart_restore_pending", "native_max_restart_restore_pending"),
        ("pending_tesla_restore", "native_max_restore_pending"),
        ("max_backup_end_ts", "native_max_max_backup_active"),
    ):
        if runtime.get(key):
            return denied(reason)
    if monitoring_blocked:
        return denied("native_max_monitoring_mode")
    if direction == "charge" and demand_charge_protected:
        return denied("native_max_demand_charge_protection")
    guard = runtime.get("network_export_guard")
    if direction == "discharge" and guard is not None:
        mode = getattr(getattr(getattr(guard, "manager", None), "snapshot", None), "mode", None)
        if not isinstance(mode, str):
            return denied("native_max_policy_evidence_unavailable")
        if mode != "off":
            return denied("native_max_network_export_guard_active")
    if not local_configured:
        return denied("native_max_local_control_unconfigured")
    if not license_allowed:
        return denied("native_max_license_denied")
    return ControlPolicyResult(True)


def control_method(config):
    value = config.get(CONF_CONTROL_METHOD)
    return value if value in METHODS else DEFAULT_METHOD


def validate_method(value):
    if not isinstance(value, str) or value not in METHODS:
        raise ValueError("Choose tariff, local or auto")
    return value


def select_route(config, direction, snapshot=None, *, local_available=False, local_limit_w=None):
    """Select a pre-plan actuator; unknown Auto evidence uses tariff control."""
    method = control_method(config)
    if method != "auto":
        return method
    if not local_available or snapshot is None:
        return "tariff"
    try:
        from .battery_dispatch import site_grid_limits_w
        imported, exported = site_grid_limits_w(snapshot)
        load, solar, battery, grid = (getattr(snapshot, key) for key in
                                       ("load_w", "solar_w", "battery_w", "grid_w"))
        maximum = config.get(f"optimization_max_{direction}_w", 5000)
        values = (load, solar, battery, grid, maximum)
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            return "tariff"
        if (snapshot.grid_status != "SystemGridConnected" or load < 0 or maximum <= 0
                or abs(grid + battery + solar - load) > max(250, .05 * max(abs(grid), abs(battery), load))):
            return "tariff"
        ceiling = LOCAL_MAX_W
        if type(local_limit_w) in (int, float) and math.isfinite(local_limit_w) and local_limit_w > 0:
            ceiling = min(ceiling, local_limit_w)
        # A site can be restricted by the timed inverter envelope even when
        # the battery component alone is below its limit.
        if abs(solar) > ceiling:
            return "tariff"
        if direction == "discharge":
            configured = config.get("optimization_max_grid_export_w")
            bound = min(exported if exported is not None else math.inf,
                        configured if configured is not None else math.inf)
            required = min(maximum, max(0, bound + load - solar))
            inverter_flow = min(bound + load, maximum + max(0, solar))
        elif direction == "charge":
            configured = config.get("optimization_max_grid_import_w")
            bound = min(imported if imported is not None else math.inf,
                        configured if configured not in (None, 0) else math.inf)
            required = min(maximum, max(0, bound - load + solar))
            inverter_flow = required
        else:
            raise ValueError("Unknown battery direction")
        return "local" if 0 < required <= ceiling and inverter_flow <= ceiling else "tariff"
    except (ValueError, TypeError, AttributeError):
        return "tariff"


def runtime_route(runtime, config, direction):
    """Use fresh per-entry telemetry; retain a running Auto tariff episode."""
    from ..powerwall_host import local_battery_dispatch_configured
    method = control_method(config)
    state = runtime.get(f"force_{direction}_state", {})
    if method == "auto" and state.get("active") and state.get("native_tariff_force"):
        return "tariff"
    local = runtime.get("powerwall_local", {})
    coordinator = local.get("coordinator")
    at = getattr(coordinator, "last_success_monotonic", None)
    fresh = type(at) in (int, float) and 0 <= time.monotonic() - at <= 90
    client = local.get("client") or getattr(runtime.get("powerwall_battery_dispatch"), "client", None)
    return select_route(config, direction, getattr(coordinator, "data", None) if fresh else None,
        local_available=local_battery_dispatch_configured(config),
        local_limit_w=getattr(client, f"dispatch_{direction}_limit_w", None))


def plan_route(config, direction, snapshot=None, *,
               target_kind: ControlTargetKind = ControlTargetKind.NATIVE_MAX,
               local_available=False, local_limit_w=None) -> ControlRoutePlan:
    """Plan the actuator while retaining the requested physical quantity.

    Numeric watt targets require the local actuator regardless of preference;
    native tariff control cannot promise a battery, export or site-input watt
    target. This selects a route only; admission still belongs to its fences.
    """
    if not isinstance(target_kind, ControlTargetKind):
        raise ValueError("Unknown Powerwall control target kind")
    route = (select_route(config, direction, snapshot, local_available=local_available,
                          local_limit_w=local_limit_w)
             if target_kind is ControlTargetKind.NATIVE_MAX else "local")
    return ControlRoutePlan(route, target_kind)


def runtime_route_plan(runtime, config, direction, *,
                       target_kind: ControlTargetKind = ControlTargetKind.NATIVE_MAX) -> ControlRoutePlan:
    """Typed companion to the legacy string-returning runtime route API."""
    if not isinstance(target_kind, ControlTargetKind):
        raise ValueError("Unknown Powerwall control target kind")
    return ControlRoutePlan(runtime_route(runtime, config, direction)
                            if target_kind is ControlTargetKind.NATIVE_MAX else "local",
                            target_kind)


async def set_control_method(hass, entry, method):
    """One mutation boundary shared by the entity and authenticated API."""
    from homeassistant.exceptions import HomeAssistantError
    from ..const import DOMAIN
    method = validate_method(method)
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    change = runtime.get("set_tesla_control_method")
    if change is None:
        raise HomeAssistantError("Tesla battery control is unavailable")
    await change(method)
