"""Explicit, bounded readback recovery for retained Tesla EV demand."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import re
import time

from .const import CONF_EV_PROVIDER, EV_PROVIDER_BOTH, EV_PROVIDER_FLEET_API
from .native_rate_admission import (
    _identity, _managed_native_entity, _managed_reconcile_allowed,
    refresh_managed_records,
)
from .native_rate_budget import budget_for, site_identity
from .native_rate_telemetry import _native_charge_source, native_charge_setting
from .tesla_ev_observation import async_read_independent_tesla_vehicle

READ_TIMEOUT = 8
# Allow healthy two-second meter polls to satisfy the existing >=3s
# acquisition-progress proof even when explicit refreshes are debounced.
SETTLE_SECONDS = 5
COOLDOWN_SECONDS = 60


def _sample_age(observation):
    """Rounded age of a rejected vehicle sample, without its raw timestamp."""
    stamp = getattr(observation, "source_charge_timestamp", None)
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        return None
    return round(time.time() - stamp, 1)


def saved_entry_removed(hass, record):
    """Whether demand belongs to a PowerSync entry that is no longer configured."""
    entry_id = record.get("entry_id")
    get_entry = getattr(getattr(hass, "config_entries", None), "async_get_entry", None)
    if not isinstance(entry_id, str) or not entry_id or not callable(get_entry):
        return False
    return get_entry(entry_id) is None and entry_id not in hass.data.get("power_sync", {})


async def async_recover_ev_headroom(hass, entry, vin, *, allow_provider_refresh=False):
    """Obtain stop proof on demand; never command the car or discard a journal."""
    from homeassistant.helpers import entity_registry as er

    def result(error=None, **details):
        return {"success": error is None, **({"error": error} if error else {}), **details}

    if not isinstance(vin, str) or not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin.strip().upper()):
        return result("invalid_vin")
    vin = vin.strip().upper()
    runtime = hass.data.get("power_sync", {}).get(entry.entry_id)
    if not isinstance(runtime, dict) or runtime.get("entry") is not entry:
        return result("entry_unavailable")
    budget = await budget_for(hass)
    site = site_identity(entry)
    record = budget.managed.get(vin)
    if record is None:
        return result("reservation_not_found")
    orphaned = False
    if record.get("entry_id") != entry.entry_id or record.get("site") != site:
        if record.get("site") != site or not saved_entry_removed(hass, record):
            return result("reservation_scope_mismatch")
        orphaned = True
    if allow_provider_refresh is not True:
        return result("provider_refresh_confirmation_required")

    config = deepcopy({**entry.data, **entry.options})
    if config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API) not in {
            EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH}:
        return result("native_provider_required")
    local = (runtime.get("powerwall_local") or {}).get("coordinator")
    refresh_meter = getattr(local, "async_request_refresh", None)
    if not callable(refresh_meter):
        return result("local_meter_unavailable")
    registry = er.async_get(hass)
    registration = registry.async_get(record.get("entity_id"))
    if _identity(registration) != record.get("identity"):
        return result("reservation_identity_changed")
    native_entity = _managed_native_entity(hass, entry, runtime, vin, record, registration)
    source = _native_charge_source(hass, vin, native_entity) if native_entity else None
    if source is None:
        from .tesla_ev_recovery import ev_headroom_recovery_status
        return result("native_source_unavailable", preflight=ev_headroom_recovery_status(hass, entry, vin))
    original = deepcopy(record)
    active = True

    def current():
        return (
            active and hass.data.get("power_sync", {}).get(entry.entry_id) is runtime
            and runtime.get("entry") is entry
            and {**entry.data, **entry.options} == config
            and not runtime.get("powerwall_local_dispatch_stopping")
            and not runtime.get("tesla_control_method_changing")
            and runtime.get("_tesla_ev_recovery_sidecar_hold") is None
            and (runtime.get("powerwall_local") or {}).get("coordinator") is local
            and budget.managed.get(vin) is record and record == original
            and not budget.site_armed(site) and not budget.native_max_claim(site)
            and _managed_reconcile_allowed(hass, budget, vin, record)
            and _identity(registry.async_get(record.get("entity_id"))) == record["identity"]
            and _managed_native_entity(hass, entry, runtime, vin, record,
                                      registry.async_get(record.get("entity_id"))) == native_entity
            and _native_charge_source(hass, vin, native_entity) == source
        )

    lock = budget.admission_lock(site)
    if lock.locked():
        return result("site_busy")
    async with lock:
        if orphaned:
            # A removed entry can never settle its own demand. Only the
            # explicitly selected entry for the same exact site may take
            # ownership, while the saved entry stays absent. The reservation
            # envelope and the release proof below are unchanged.
            if budget.site_armed(site) or budget.native_max_claim(site):
                return result("site_busy")
            try:
                adopted = await budget.adopt_orphaned_managed(
                    vin, record, entry.entry_id,
                    lambda: (active and hass.data.get("power_sync", {}).get(entry.entry_id) is runtime
                             and runtime.get("entry") is entry
                             and record.get("site") == site_identity(entry) == site
                             and saved_entry_removed(hass, record)))
            except asyncio.CancelledError:
                raise
            except Exception:
                return result("recovery_failed")
            if adopted is None:
                return result("recovery_scope_blocked")
            record, original = adopted, deepcopy(adopted)
        if not current():
            return result("recovery_scope_blocked")
        attempts = runtime.setdefault("_ev_headroom_recovery_attempts", {})
        previous = attempts.get(vin)
        if previous is not None and time.monotonic() - previous < COOLDOWN_SECONDS:
            return result("recovery_cooldown", retry_after_seconds=COOLDOWN_SECONDS)
        attempts[vin] = time.monotonic()
        cutoff = None
        try:
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(SETTLE_SECONDS)
                if not current():
                    return result("recovery_scope_changed")
                # The qualified reader calls the concrete native cloud API,
                # bypassing cached stream data and BLE command routers. Paid
                # Teslemetry refresh is authorized only by this explicit call.
                read_failure = {}
                observation, meter_result = await asyncio.gather(
                    async_read_independent_tesla_vehicle(
                        hass, vin, preferred_entity_id=native_entity,
                        timeout=READ_TIMEOUT, updated_after=cutoff,
                        allow_paid_refresh=source[0] == "teslemetry", authorize=current,
                        failure=read_failure),
                    asyncio.wait_for(refresh_meter(), timeout=READ_TIMEOUT),
                    return_exceptions=True,
                )
                if not current():
                    return result("recovery_scope_changed")
                # Non-identifying detail tells support which check failed; the
                # outcome and the retained reservation are unchanged.
                unavailable = {"round": attempt + 1}
                if isinstance(observation, BaseException):
                    return result("fresh_readback_unavailable", failed_check="vehicle_read_error",
                                  error_type=type(observation).__name__, **unavailable)
                if observation is None:
                    return result("fresh_readback_unavailable", failed_check="vehicle_read",
                                  vehicle_read=dict(read_failure) or {"reason": "unknown"},
                                  **unavailable)
                if isinstance(meter_result, BaseException):
                    return result("fresh_readback_unavailable", failed_check=(
                        "meter_refresh_timeout" if isinstance(meter_result, asyncio.TimeoutError)
                        else "meter_refresh_error"), **unavailable)
                native = native_charge_setting(hass, vin, native_entity, observation=observation)
                if native is None:
                    return result("fresh_readback_unavailable", failed_check="vehicle_report_rejected",
                                  sample_age_seconds=_sample_age(observation), **unavailable)
                if not getattr(local, "last_update_success", False):
                    return result("fresh_readback_unavailable", failed_check="meter_update_failed",
                                  **unavailable)
                if (native["observation"].charge_state.lower() not in {
                        "stopped", "complete", "disconnected", "nopower", "no_power"}
                        or native["observation"].charger_actual_current != 0
                        or native["observation"].charger_power != 0):
                    return result("vehicle_not_stopped")
                native["explicit_read"] = True
                cutoff = native["at"]
                await refresh_managed_records(
                    hass, budget, vins={vin}, site=site, native_reports={vin: native},
                    is_current=current)
                if vin not in budget.managed:
                    return result(vin=vin, reservation_cleared=True)
            return result("settlement_not_confirmed")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed read/persist never grants charging authority. The budget
            # owns persistence and retains demand until its commit succeeds.
            return result("recovery_failed")
        finally:
            active = False
