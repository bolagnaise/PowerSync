"""Explicit, bounded readback recovery for retained Tesla EV demand."""
from __future__ import annotations

import asyncio
from copy import deepcopy
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
SETTLE_SECONDS = 3
COOLDOWN_SECONDS = 60


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
    if record.get("entry_id") != entry.entry_id or record.get("site") != site:
        return result("reservation_scope_mismatch")
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
        return result("native_source_unavailable")
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
                observation, meter_result = await asyncio.gather(
                    async_read_independent_tesla_vehicle(
                        hass, vin, preferred_entity_id=native_entity,
                        timeout=READ_TIMEOUT, updated_after=cutoff,
                        allow_paid_refresh=source[0] == "teslemetry", authorize=current),
                    asyncio.wait_for(refresh_meter(), timeout=READ_TIMEOUT),
                    return_exceptions=True,
                )
                if not current():
                    return result("recovery_scope_changed")
                if isinstance(observation, BaseException) or isinstance(meter_result, BaseException):
                    return result("fresh_readback_unavailable")
                native = native_charge_setting(hass, vin, native_entity, observation=observation) if observation else None
                if native is None or not getattr(local, "last_update_success", False):
                    return result("fresh_readback_unavailable")
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
