"""Static battery connection-profile registry.

Profiles are intentionally pure data. Importing this module must never import or
construct a hardware client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..const import (
    ALPHAESS_CONNECTION_CLOUD_ONLY,
    ALPHAESS_CONNECTION_MODBUS_CLOUD,
    ANKER_SOLIX_CONNECTION_CLOUD_HA,
    ANKER_SOLIX_CONNECTION_MODBUS,
    ANKER_SOLIX_CONNECTION_OFFICIAL_HA,
    BATTERY_SYSTEM_ALPHAESS,
    BATTERY_SYSTEM_ANKER_SOLIX,
    BATTERY_SYSTEM_CUSTOM,
    BATTERY_SYSTEM_ESY_SUNHOME,
    BATTERY_SYSTEM_FOXESS,
    BATTERY_SYSTEM_FRONIUS_RESERVA,
    BATTERY_SYSTEM_GOODWE,
    BATTERY_SYSTEM_NEOVOLT,
    BATTERY_SYSTEM_SAJ_H2,
    BATTERY_SYSTEM_SIGENERGY,
    BATTERY_SYSTEM_SOLAREDGE,
    BATTERY_SYSTEM_SOLAX,
    BATTERY_SYSTEM_SUNGROW,
    BATTERY_SYSTEM_TESLA,
    CONF_ALPHAESS_CONNECTION_TYPE,
    CONF_ANKER_SOLIX_CONNECTION_TYPE,
    CONF_BATTERY_CONNECTION_PROFILE,
    CONF_BATTERY_SYSTEM,
    CONF_FOXESS_CONNECTION_TYPE,
    CONF_SUNGROW_CONNECTION_TYPE,
    FOXESS_CONNECTION_CLOUD,
    FOXESS_CONNECTION_ENTITY,
    FOXESS_CONNECTION_SERIAL,
    FOXESS_CONNECTION_TCP,
    SUNGROW_CONNECTION_DIRECT,
    SUNGROW_CONNECTION_IHOMEMANAGER,
)


@dataclass(frozen=True, slots=True)
class BatteryConnectionProfile:
    """One validated user-selectable connection bundle."""

    profile_id: str
    battery_system: str
    label: str
    route_kind: str
    upstream_domains: tuple[str, ...] = ()
    monitoring_only: bool = False
    requires_upstream: bool = False
    route_value: str | None = None
    controls_summary: str = "Existing PowerSync control route"


def _p(
    profile_id: str,
    battery_system: str,
    label: str,
    route_kind: str,
    **kwargs: Any,
) -> BatteryConnectionProfile:
    return BatteryConnectionProfile(
        profile_id=profile_id,
        battery_system=battery_system,
        label=label,
        route_kind=route_kind,
        **kwargs,
    )


_PROFILES: tuple[BatteryConnectionProfile, ...] = (
    _p("tesla_provider", BATTERY_SYSTEM_TESLA, "Configured Tesla provider", "provider"),
    _p(
        "tesla_powerwall_monitoring",
        BATTERY_SYSTEM_TESLA,
        "Home Assistant Powerwall integration — monitoring only",
        "ha_monitoring",
        upstream_domains=("powerwall",),
        monitoring_only=True,
        requires_upstream=True,
        controls_summary="Monitoring only; Tesla controls disabled",
    ),
    _p("sigenergy_direct", BATTERY_SYSTEM_SIGENERGY, "PowerSync direct Modbus", "direct"),
    _p(
        "sigenergy_ha_monitoring",
        BATTERY_SYSTEM_SIGENERGY,
        "Sigenergy Local Modbus integration — monitoring only",
        "ha_monitoring",
        upstream_domains=("sigen",),
        monitoring_only=True,
        requires_upstream=True,
        controls_summary="Monitoring only; upstream writes are not inferred",
    ),
    _p(
        "sungrow_direct",
        BATTERY_SYSTEM_SUNGROW,
        "PowerSync direct inverter Modbus",
        "direct",
        route_value=SUNGROW_CONNECTION_DIRECT,
    ),
    _p(
        "sungrow_ihomemanager",
        BATTERY_SYSTEM_SUNGROW,
        "iHomeManager forwarding — monitoring only",
        "direct_monitoring",
        monitoring_only=True,
        route_value=SUNGROW_CONNECTION_IHOMEMANAGER,
    ),
    _p(
        "sungrow_ha_monitoring",
        BATTERY_SYSTEM_SUNGROW,
        "Home Assistant Sungrow entities — monitoring only",
        "ha_monitoring",
        upstream_domains=("modbus", "modbus_manager"),
        monitoring_only=True,
        requires_upstream=True,
        controls_summary="Monitoring only; no PowerSync Modbus connection",
    ),
    _p("foxess_tcp", BATTERY_SYSTEM_FOXESS, "PowerSync Modbus TCP", "direct", route_value=FOXESS_CONNECTION_TCP),
    _p("foxess_serial", BATTERY_SYSTEM_FOXESS, "PowerSync RS485 serial", "direct", route_value=FOXESS_CONNECTION_SERIAL),
    _p("foxess_cloud", BATTERY_SYSTEM_FOXESS, "FoxESS Cloud API", "cloud", route_value=FOXESS_CONNECTION_CLOUD),
    _p(
        "foxess_ha_modbus",
        BATTERY_SYSTEM_FOXESS,
        "FoxESS Modbus Home Assistant integration",
        "ha_control",
        upstream_domains=("foxess_modbus",),
        requires_upstream=True,
        route_value=FOXESS_CONNECTION_ENTITY,
        controls_summary="Telemetry and verified controls through Home Assistant entities",
    ),
    _p("goodwe_direct", BATTERY_SYSTEM_GOODWE, "PowerSync direct IP control", "direct"),
    _p(
        "goodwe_ha",
        BATTERY_SYSTEM_GOODWE,
        "Home Assistant GoodWe telemetry and EMS control",
        "ha_control",
        upstream_domains=("goodwe",),
        requires_upstream=True,
        controls_summary="Entity telemetry and EMS controls; unsupported operations fail closed",
    ),
    _p(
        "goodwe_ha_monitoring",
        BATTERY_SYSTEM_GOODWE,
        "Home Assistant GoodWe integration — monitoring only",
        "ha_monitoring",
        upstream_domains=("goodwe",),
        monitoring_only=True,
        requires_upstream=True,
        controls_summary="Monitoring only; no PowerSync IP connection",
    ),
    _p(
        "alphaess_modbus_cloud",
        BATTERY_SYSTEM_ALPHAESS,
        "PowerSync Modbus with optional cloud data",
        "direct",
        route_value=ALPHAESS_CONNECTION_MODBUS_CLOUD,
    ),
    _p(
        "alphaess_cloud_monitoring",
        BATTERY_SYSTEM_ALPHAESS,
        "AlphaESS Cloud API — monitoring only",
        "cloud_monitoring",
        monitoring_only=True,
        route_value=ALPHAESS_CONNECTION_CLOUD_ONLY,
    ),
    _p(
        "alphaess_ha_monitoring",
        BATTERY_SYSTEM_ALPHAESS,
        "AlphaESS Home Assistant integration — monitoring only",
        "ha_monitoring",
        upstream_domains=("alphaess",),
        monitoring_only=True,
        requires_upstream=True,
    ),
    _p("esy_ha", BATTERY_SYSTEM_ESY_SUNHOME, "ESY Sunhome integration", "ha_control", upstream_domains=("esy_sunhome",), requires_upstream=True),
    _p("solax_ha", BATTERY_SYSTEM_SOLAX, "SolaX Modbus integration", "ha_control", upstream_domains=("solax_modbus",), requires_upstream=True),
    _p("saj_ha", BATTERY_SYSTEM_SAJ_H2, "SAJ H2 Modbus integration", "ha_control", upstream_domains=("saj_h2_modbus",), requires_upstream=True),
    _p("fronius_ha", BATTERY_SYSTEM_FRONIUS_RESERVA, "Fronius Modbus integration", "ha_control", upstream_domains=("fronius_modbus",), requires_upstream=True),
    _p("neovolt_ha", BATTERY_SYSTEM_NEOVOLT, "Neovolt integration", "ha_control", upstream_domains=("neovolt",), requires_upstream=True),
    _p("solaredge_composite", BATTERY_SYSTEM_SOLAREDGE, "HA telemetry plus direct curtailment", "hybrid"),
    _p(
        "solaredge_ha_only",
        BATTERY_SYSTEM_SOLAREDGE,
        "SolarEdge Modbus Multi — Home Assistant only",
        "ha_control",
        upstream_domains=("solaredge_modbus_multi",),
        requires_upstream=True,
        controls_summary="Battery entity controls only; direct curtailment disabled",
    ),
    _p("anker_direct", BATTERY_SYSTEM_ANKER_SOLIX, "PowerSync direct X1 Modbus", "direct", route_value=ANKER_SOLIX_CONNECTION_MODBUS),
    _p(
        "anker_ha_official",
        BATTERY_SYSTEM_ANKER_SOLIX,
        "Official Anker SOLIX Home Assistant integration",
        "ha_control",
        upstream_domains=("anker_solix_official",),
        requires_upstream=True,
        route_value=ANKER_SOLIX_CONNECTION_OFFICIAL_HA,
    ),
    _p(
        "anker_ha_cloud",
        BATTERY_SYSTEM_ANKER_SOLIX,
        "Unofficial Anker SOLIX cloud integration",
        "ha_control",
        upstream_domains=("anker_solix",),
        requires_upstream=True,
        route_value=ANKER_SOLIX_CONNECTION_CLOUD_HA,
    ),
    _p("custom_entities", BATTERY_SYSTEM_CUSTOM, "Selected Home Assistant entities — monitoring only", "manual_monitoring", monitoring_only=True),
)

PROFILE_REGISTRY = {profile.profile_id: profile for profile in _PROFILES}


def profiles_for_system(battery_system: str) -> tuple[BatteryConnectionProfile, ...]:
    """Return stable profiles registered for one battery system."""
    return tuple(p for p in _PROFILES if p.battery_system == battery_system)


def _value(data: Mapping[str, Any], options: Mapping[str, Any], key: str, default: Any = None) -> Any:
    return options.get(key, data.get(key, default))


def legacy_profile_id(
    battery_system: str,
    data: Mapping[str, Any],
    options: Mapping[str, Any],
) -> str:
    """Map existing settings to an equivalent profile without changing route."""
    if battery_system == BATTERY_SYSTEM_SUNGROW:
        route = _value(data, options, CONF_SUNGROW_CONNECTION_TYPE, SUNGROW_CONNECTION_DIRECT)
        return "sungrow_ihomemanager" if route == SUNGROW_CONNECTION_IHOMEMANAGER else "sungrow_direct"
    if battery_system == BATTERY_SYSTEM_FOXESS:
        route = _value(data, options, CONF_FOXESS_CONNECTION_TYPE, FOXESS_CONNECTION_TCP)
        return {
            FOXESS_CONNECTION_SERIAL: "foxess_serial",
            FOXESS_CONNECTION_CLOUD: "foxess_cloud",
            FOXESS_CONNECTION_ENTITY: "foxess_ha_modbus",
        }.get(route, "foxess_tcp")
    if battery_system == BATTERY_SYSTEM_GOODWE:
        return "goodwe_ha" if _value(data, options, "goodwe_ems_control_mode") == "entity" else "goodwe_direct"
    if battery_system == BATTERY_SYSTEM_ALPHAESS:
        return "alphaess_cloud_monitoring" if _value(data, options, CONF_ALPHAESS_CONNECTION_TYPE) == ALPHAESS_CONNECTION_CLOUD_ONLY else "alphaess_modbus_cloud"
    if battery_system == BATTERY_SYSTEM_ANKER_SOLIX:
        route = _value(data, options, CONF_ANKER_SOLIX_CONNECTION_TYPE, ANKER_SOLIX_CONNECTION_MODBUS)
        return {
            ANKER_SOLIX_CONNECTION_OFFICIAL_HA: "anker_ha_official",
            ANKER_SOLIX_CONNECTION_CLOUD_HA: "anker_ha_cloud",
        }.get(route, "anker_direct")
    return {
        BATTERY_SYSTEM_TESLA: "tesla_provider",
        BATTERY_SYSTEM_SIGENERGY: "sigenergy_direct",
        BATTERY_SYSTEM_ESY_SUNHOME: "esy_ha",
        BATTERY_SYSTEM_SOLAX: "solax_ha",
        BATTERY_SYSTEM_SAJ_H2: "saj_ha",
        BATTERY_SYSTEM_FRONIUS_RESERVA: "fronius_ha",
        BATTERY_SYSTEM_NEOVOLT: "neovolt_ha",
        BATTERY_SYSTEM_SOLAREDGE: "solaredge_composite",
        BATTERY_SYSTEM_CUSTOM: "custom_entities",
    }.get(battery_system, "tesla_provider")


def resolve_connection_profile(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    battery_system: str | None = None,
) -> BatteryConnectionProfile:
    """Resolve an explicit profile or the exact legacy-equivalent default."""
    system = battery_system or str(_value(data, options, CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA))
    requested = _value(data, options, CONF_BATTERY_CONNECTION_PROFILE)
    profile = PROFILE_REGISTRY.get(str(requested)) if requested else None
    if profile is None or profile.battery_system != system:
        profile = PROFILE_REGISTRY[legacy_profile_id(system, data, options)]
    return profile
