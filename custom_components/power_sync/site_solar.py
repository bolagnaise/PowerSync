"""Explicit, read-only composition of solar outside the primary PV reading.

The selected sensor is a component, never a whole-site replacement. Its registry
identity is saved with the user's coverage declaration; polling an HA cache does
not refresh the observation. This module never constructs an inverter controller.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any

CONF_ENABLED = "additional_solar_enabled"
CONF_ENTITY = "additional_solar_power_entity"
CONF_IDENTITY = "additional_solar_source_identity"
CONF_LOAD = "additional_solar_load_basis"
MAX_AGE_SECONDS = 120


def configuration_signature(options: dict[str, Any]) -> str:
    """Track coverage/connection changes without fencing routine token refresh."""
    fields = {
        key: value for key, value in options.items()
        if key in (CONF_ENABLED, CONF_ENTITY, CONF_IDENTITY, CONF_LOAD,
                   "battery_system", "battery_connection_profile_id",
                   "custom_solar_power_entity", "custom_load_power_entity",
                   "custom_grid_power_entity", "custom_battery_power_entity")
        or key.endswith(("_host", "_port", "_slave_id", "_config_entry_id",
                         "_entity_prefix", "_connection_type"))
        or ("power" in key and key.endswith(("_multiplier", "_invert", "_sign")))
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def source_identity(registry_entry: Any) -> str | None:
    """Bind a declaration to a registered physical/logical source, not its name."""
    if registry_entry is None or not getattr(registry_entry, "unique_id", None):
        return None
    if getattr(registry_entry, "platform", None) == "power_sync":
        return None
    parts = [getattr(registry_entry, key, None) for key in
             ("platform", "config_entry_id", "device_id", "unique_id")]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def configuration_error(options: dict[str, Any], registry_entry: Any) -> str | None:
    """Validate explicit coverage and source identity, also used at runtime."""
    if not options.get(CONF_ENABLED, False):
        return None
    if options.get("battery_system") == "amber_smartshift":
        return "additional_solar_unsupported_source"
    if options.get(CONF_LOAD, "measured") not in ("measured", "balance"):
        return "additional_solar_invalid_load_basis"
    if not options.get(CONF_ENTITY) or source_identity(registry_entry) is None:
        return "additional_solar_source_required"
    # A whole-site override remains authoritative even when unavailable.
    if options.get("custom_solar_power_entity") and (
        options.get("battery_system") == "goodwe"
        or options.get(CONF_ENTITY) == options.get("custom_solar_power_entity")
    ):
        return "additional_solar_already_included"
    prefix = str(options.get("goodwe_ems_entity_prefix") or "").strip()
    if options.get("battery_system") == "goodwe" and prefix:
        if options[CONF_ENTITY] == f"sensor.{prefix}_pv_power":
            return "additional_solar_already_included"
    primary_entry = options.get("battery_integration_config_entry_id")
    if primary_entry and getattr(registry_entry, "config_entry_id", None) == primary_entry:
        return "additional_solar_already_included"
    return None


@dataclass(frozen=True)
class SolarSample:
    primary_kw: float | None
    additional_kw: float | None
    solar_kw: float
    raw_load_kw: float | None
    valid: bool
    reason: str
    segment: str
    observed_at: datetime | None
    load_is_derived: bool
    primary_observed_at: datetime
    valid_until: float | None


def resolve_solar(
    *, primary_kw: float, grid_kw: float, battery_kw: float,
    raw_load_kw: float | None, state: Any, identity: str | None,
    expected_identity: str | None, segment: str, now: datetime,
    load_basis: str = "measured", blocked_reason: str | None = None,
    primary_observed_at: datetime | None = None,
    primary_valid: bool = True,
) -> SolarSample:
    """Resolve one site sample without changing metered load or doing I/O."""
    def finite(value):
        try:
            numeric = float(value) if not isinstance(value, bool) else math.nan
        except (TypeError, ValueError, OverflowError):
            return None
        return numeric if math.isfinite(numeric) else None

    reason = blocked_reason
    if not primary_valid:
        reason = reason or "invalid_primary_power"
    primary_observed_at = primary_observed_at or now
    primary = finite(primary_kw)
    if primary is None or primary < 0:
        primary = None
        reason = reason or "invalid_primary_power"
    if (getattr(state, "attributes", {}) or {}).get("restored"):
        reason = "restored_observation"
    observed = getattr(state, "last_reported", None)
    if observed is None:
        observed = getattr(state, "last_updated", None)
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        observed = None
    if not reason and (not identity or identity != expected_identity):
        reason = "source_changed"
    if not reason and primary_observed_at is not None:
        if not isinstance(primary_observed_at, datetime) or primary_observed_at.tzinfo is None:
            reason = "invalid_primary_time"
        elif not -5 <= (now - primary_observed_at).total_seconds() <= MAX_AGE_SECONDS:
            reason = "stale_primary_observation"
    if not reason:
        if observed is None:
            reason = "missing_observation_time"
        else:
            age = (now - observed).total_seconds()
            if age < -5 or age > MAX_AGE_SECONDS:
                reason = "stale_observation"
            elif primary_observed_at is not None and abs(
                (primary_observed_at - observed).total_seconds()) > MAX_AGE_SECONDS:
                reason = "observation_skew"
    value = None
    if not reason:
        try:
            raw = getattr(state, "state", None)
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
            unit = str((getattr(state, "attributes", {}) or {}).get(
                "unit_of_measurement", "")).strip().lower()
            scale = {"w": .001, "kw": 1., "mw": 1000.}.get(unit)
            if scale is None or not math.isfinite(value) or value < 0:
                raise ValueError
            value *= scale
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            reason = "invalid_power"
            value = None
    valid = reason is None
    solar = primary + value if valid else (primary or 0.)
    if not math.isfinite(solar):
        valid, reason, solar = False, "invalid_site_power", primary or 0.
    load = finite(raw_load_kw)
    if load_basis == "balance":
        grid, battery = finite(grid_kw), finite(battery_kw)
        if grid is None or battery is None:
            valid, reason = False, reason or "invalid_site_balance"
        balance = solar + grid + battery if valid else math.nan
        load = max(0., balance) if math.isfinite(balance) else None
        if valid and load is None:
            valid, reason = False, "invalid_site_balance"
    deadline = (min(observed.timestamp(), primary_observed_at.timestamp()) + MAX_AGE_SECONDS
                if observed is not None and isinstance(primary_observed_at, datetime)
                and primary_observed_at.tzinfo is not None else None)
    return SolarSample(primary, value, solar, load, valid,
                       reason or "complete", segment, observed, load_basis == "balance",
                       primary_observed_at, deadline)


class SiteSolarPolicy:
    """Read an explicitly bound HA source and publish the same resolved sample."""

    def __init__(self, hass: Any, entry: Any, *, registry: Any = None,
                 battery_system: str | None = None) -> None:
        self.hass = hass
        self.entry = entry
        self.options = {**entry.data, **entry.options}
        self._configuration_signature = configuration_signature(self.options)
        self.battery_system = battery_system or self.options.get("battery_system")
        self.entity_id = self.options.get(CONF_ENTITY, "")
        self.identity = self.options.get(CONF_IDENTITY)
        self.load_basis = self.options.get(CONF_LOAD, "measured")
        self._registry = registry
        self._now = lambda: datetime.now(timezone.utc)
        self._runtime = hass.data.setdefault("power_sync", {}).setdefault(entry.entry_id, {})
        self.segment = hashlib.sha256(json.dumps([
            self.identity, self.load_basis, self.battery_system,
            self._configuration_signature,
            sorted((key, str(value)) for key, value in self.options.items()
                   if key.endswith(("_host", "_port", "_slave_id", "_config_entry_id"))),
        ]).encode()).hexdigest()

    def _registered_source(self):
        registry = self._registry
        if registry is None:
            from homeassistant.helpers import entity_registry as er
            registry = er.async_get(self.hass)
        registry_entry = registry.async_get(self.entity_id)
        if registry_entry is None:
            # HA entity renames preserve registry identity; replacement does not.
            matches = [row for row in registry.entities.values()
                       if source_identity(row) == self.identity]
            if len(matches) == 1:
                registry_entry = matches[0]
                self.entity_id = registry_entry.entity_id
        return registry_entry

    def prepare(self, accumulator: Any, solar: float, grid: float,
                battery: float, load: float | None, *,
                already_included: bool = False,
                load_basis: str | None = None,
                primary_source: str = "native",
                primary_observed_at: datetime | None = None,
                primary_valid: bool = True) -> tuple[float, float | None]:
        registry_entry = self._registered_source()
        error = configuration_error({**self.options, CONF_ENTITY: self.entity_id,
                                     "battery_system": self.battery_system}, registry_entry)
        current = {**self.entry.data, **self.entry.options}
        if configuration_signature(current) != self._configuration_signature:
            error = "configuration_changed"
        if self._runtime is not None and self.hass.data.get("power_sync", {}).get(
            self.entry.entry_id) is not self._runtime:
            error = "configuration_changed"
        if already_included:
            error = "already_included"
        owner = getattr(accumulator, "_site_solar_coordinator", None)
        if owner is not None and getattr(owner, "last_update_success", None) is False:
            accumulator._solar_interval_valid = False
        sample = resolve_solar(
            primary_kw=solar, grid_kw=grid, battery_kw=battery,
            raw_load_kw=load, state=self.hass.states.get(self.entity_id),
            identity=source_identity(registry_entry), expected_identity=self.identity,
            segment=f"{self.segment}:{primary_source}", now=self._now(),
            load_basis=load_basis or self.load_basis, blocked_reason=error,
            primary_observed_at=primary_observed_at,
            primary_valid=primary_valid,
        )
        accumulator._site_solar_sample = sample
        return sample.solar_kw, sample.raw_load_kw

    def publish(self, accumulator: Any, data: dict[str, Any]) -> dict[str, Any]:
        sample = getattr(accumulator, "_site_solar_sample", None)
        if sample is None:
            return data
        result = dict(data)
        valid = (sample.valid and data.get("telemetry_ready") is not False
                 and data.get("solar_power_valid") is not False
                 and not getattr(accumulator, "_solar_cutover_pending", False))
        current_options = {**self.entry.data, **self.entry.options}
        reason = sample.reason
        current_source = self._registered_source()
        if source_identity(current_source) != self.identity:
            valid, reason = False, "source_changed"
        elif configuration_error({**self.options, CONF_ENTITY: self.entity_id,
                                  "battery_system": self.battery_system}, current_source):
            valid, reason = False, "already_included"
        if configuration_signature(current_options) != self._configuration_signature:
            valid = False
            reason = "configuration_changed"
        if self._runtime is not None and self.hass.data.get("power_sync", {}).get(
            self.entry.entry_id) is not self._runtime:
            valid = False
            reason = "configuration_changed"
        if sample.observed_at is not None:
            if not -5 <= (self._now() - sample.observed_at).total_seconds() <= MAX_AGE_SECONDS:
                valid = False
                reason = "stale_observation"
        if sample.valid_until is None or self._now().timestamp() > sample.valid_until:
            valid, reason = False, "stale_observation"
        if not valid:
            accumulator._solar_interval_valid = False
            if sample.load_is_derived:
                result.update(load_power=None, raw_home_load_power=None, site_load_power=None)
        result.update(
            solar_power=sample.solar_kw if valid else None,
            solar_power_valid=valid,
            primary_solar_power=sample.primary_kw,
            additional_solar_power=sample.additional_kw,
            solar_accounting_reason=reason,
            solar_source_entity=self.entity_id,
            solar_source_identity=self.identity,
            solar_observed_at=sample.observed_at.isoformat() if sample.observed_at else None,
            solar_accounting_coverage="observed_since_enabled",
        )
        summary = dict(result.get("energy_summary") or {})
        summary["pv_today_kwh"] = accumulator.as_dict()["pv_today_kwh"]
        summary["solar_accounting_coverage"] = accumulator.as_dict().get(
            "solar_accounting_coverage", "partial")
        result["energy_summary"] = summary
        return result


async def checkpoint_solar(hass: Any, entry: Any, *, force: bool = False,
                           now: datetime | None = None) -> None:
    """Durably retain accepted daily totals before an options reload/unload."""
    seen = set()
    checkpoints = []
    pending = list(hass.data.get("power_sync", {}).get(entry.entry_id, {}).values())
    while pending:
        coordinator = pending.pop()
        if id(coordinator) in seen:
            continue
        seen.add(id(coordinator))
        for key in ("_coord1", "_coord2"):
            child = getattr(coordinator, key, None)
            if child is not None:
                pending.append(child)
        accumulator = getattr(coordinator, "_energy_acc", None)
        if accumulator is None or not hasattr(accumulator, "solar_kwh"):
            continue
        if not force and getattr(accumulator, "_site_solar_policy", None) is None and not getattr(
            accumulator, "_solar_segment_seen", False):
            continue
        previous = {key: getattr(accumulator, key) for key in (
            "_solar_cutover_pending", "_solar_segment_seen", "_solar_partial_today",
            "_solar_partial_mtd", "solar_kwh", "mtd_solar_kwh",
        )}
        checkpoints.append((accumulator, previous))
        accumulator._solar_cutover_pending = True
        if force:
            # Fence the old runtime before the first persistence await. The new
            # accumulator after reload starts unfenced; a late old publication
            # cannot advance the accepted total beyond this checkpoint.
            accumulator._solar_cutover_pending = True
            accumulator._solar_segment_seen = True
            accumulator._solar_partial_today = True
            accumulator._solar_partial_mtd = True
        data = getattr(coordinator, "data", None)
        if isinstance(data, dict):
            value = (data.get("energy_summary") or {}).get("pv_today_kwh")
            observed = data.get("last_update")
            # Only a current-day accepted counter can seed today's baseline.
            if now is None:
                from homeassistant.util import dt as dt_util
                checkpoint_time = dt_util.now()
                local_observed = dt_util.as_local(observed) if isinstance(observed, datetime) else None
            else:
                checkpoint_time = now
                local_observed = observed.astimezone(now.tzinfo) if isinstance(observed, datetime) else None
            if local_observed is not None and local_observed.date() == checkpoint_time.date():
                try:
                    baseline = float(value)
                except (TypeError, ValueError, OverflowError):
                    baseline = 0.
                if math.isfinite(baseline) and baseline >= 0:
                    accumulator.mtd_solar_kwh += max(0., baseline - accumulator.solar_kwh)
                    accumulator.solar_kwh = max(accumulator.solar_kwh, baseline)
    # Capture/fence every child before yielding, including dual-inverter sites.
    try:
        for accumulator, _previous in checkpoints:
            await accumulator.async_flush()
    except BaseException:
        for accumulator, previous in checkpoints:
            for key, value in previous.items():
                setattr(accumulator, key, value)
        raise
