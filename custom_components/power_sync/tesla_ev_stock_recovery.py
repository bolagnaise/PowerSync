"""Home Assistant bindings for stock Tesla BLE restart and reconciliation.

Restart retires an old RAM queue.  It never proves the outcome of a vehicle
command.  The durable recovery manager grants new authority only after the
separate, exact native-cloud observation has converged.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import logging
import math
import time
from typing import Any, Mapping

DOMAIN = "power_sync"
_LOGGER = logging.getLogger(__name__)
RESET_TIMEOUT = 180.0
POLL_SECONDS = 0.1

# Supported source profiles, rather than an arbitrary project-name heuristic.
# These profiles use ordinary supported stock-device trust, not binary
# attestation. The transaction must additionally exercise and verify reboot.
STOCK_PROFILES = {
    ("PedroKTFC.esphome-tesla-ble", "2026.2.0", "2026.1.5"):
        "PedroKTFC/esphome-tesla-ble@3906351db4bba7e533ee7c7af13b460306ad5d65",
    ("PedroKTFC.esphome-tesla-ble", "2026.8.0", "2026.6.5"):
        "PedroKTFC/esphome-tesla-ble@00ff23e74da1ec6beebca486898d48745862cd07",
}


def _identity(hass: Any, entity_id: str) -> dict[str, str] | None:
    try:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
    except (ImportError, AttributeError, TypeError):
        registry = getattr(hass, "entity_registry", None)
    getter = getattr(registry, "async_get", None)
    entity = getter(entity_id) if callable(getter) else None
    if entity is None or getattr(entity, "disabled_by", None) is not None:
        return None
    identity = {key: getattr(entity, key, None) for key in (
        "platform", "config_entry_id", "device_id", "unique_id")}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        return None
    return {**identity, "entity_id": entity_id}


def _stamp(state: Any) -> float | None:
    stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    try:
        value = stamp.timestamp() if hasattr(stamp, "timestamp") else float(stamp)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _uptime(hass: Any, entity_id: str) -> dict[str, Any] | None:
    state = hass.states.get(entity_id)
    try:
        value = float(state.state)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    stamp = _stamp(state)
    if not math.isfinite(value) or value < 0 or stamp is None:
        return None
    # The source entity is pinned separately. A reconnect must publish a new
    # sample; restoring an old HA state does not set fresh=True here.
    return {"uptime": value, "observed_at": stamp}


@dataclass(frozen=True)
class StockBridge:
    vin: str
    prefix: str
    identities: Mapping[str, Mapping[str, str]]
    runtime_data: Any
    runtime_ref: Any
    metadata: tuple[str, str, str]

    @property
    def producer(self) -> str:
        identity = self.identities["charger"]
        return f"esphome:{identity['config_entry_id']}:{identity['device_id']}:{identity['unique_id']}"

    @property
    def identity(self) -> dict[str, Any]:
        return {**self.identities["charger"], "vin": self.vin,
                "prefix": self.prefix,
                "entities": {key: dict(value) for key, value in self.identities.items()},
                "metadata": list(self.metadata), "producer_identity": self.producer}

    @property
    def profile(self) -> dict[str, Any]:
        return {"profile_id": "esphome_tesla_ble_volatile_v1",
                "queue_persistence": "ram_only", "host_replay": "none",
                "qualification": "source_and_runtime_exercise",
                "source": STOCK_PROFILES[self.metadata],
                "host_source": "homeassistant/core@2026.10.4;aioesphomeapi@46.6.0",
                "max_uptime_after_reset": 120.0, "advance_wait": 0.0}


def resolve_stock_bridge(
    hass: Any, entry: Any, vin: str, *, require_uptime: bool = True,
) -> StockBridge | None:
    """Resolve only the configured VIN's enabled exact ESPHome producer."""
    from .tesla_ble_mapping import resolve_ble_prefixes, vehicle_ble_prefix
    config = {**entry.data, **entry.options}
    prefix = vehicle_ble_prefix(config, vin, resolved_prefixes=resolve_ble_prefixes(hass, config))
    if not prefix:
        return None
    entities = {"charger": f"switch.{prefix}_charger",
                "restart": f"button.{prefix}_restart",
                "uptime": f"sensor.{prefix}_uptime",
                "radio": f"switch.{prefix}_ble_connection"}
    identities = {key: _identity(hass, entity_id) for key, entity_id in entities.items()}
    if any(identity is None for identity in identities.values()):
        return None
    charger = identities["charger"]
    if any(identity["platform"] != "esphome" or any(
        identity[key] != charger[key] for key in ("config_entry_id", "device_id")
    ) for identity in identities.values()):
        return None
    config_entry = hass.config_entries.async_get_entry(charger["config_entry_id"])
    runtime_data = getattr(config_entry, "runtime_data", None)
    info = getattr(runtime_data, "device_info", None)
    metadata = tuple(str(getattr(info, key, "")) for key in (
        "project_name", "project_version", "esphome_version"))
    if metadata not in STOCK_PROFILES or getattr(runtime_data, "client", None) is None:
        return None
    # The reviewed host no-replay contract is deliberately versioned too.
    try:
        from homeassistant.const import __version__
        from importlib.metadata import version
        if __version__ != "2026.10.4" or version("aioesphomeapi") != "46.6.0":
            return None
    except (ImportError, AttributeError):
        return None
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(runtime, dict) or (
        require_uptime and _uptime(hass, entities["uptime"]) is None
    ):
        return None
    return StockBridge(str(vin), prefix, identities, runtime_data, runtime, metadata)


class StockBridgeTransaction:
    """Bind every await to the same registry, API runtime and recovery scope."""

    def __init__(self, hass: Any, entry: Any, bridge: StockBridge):
        self.hass, self.entry, self.bridge = hass, entry, bridge
        self.deadline = time.monotonic() + RESET_TIMEOUT
        self.reset_at: float | None = None
        self.last_uptime: dict[str, Any] | None = None
        self.saw_disconnect = False
        self.generation: int | None = None

    def current(self) -> bool:
        from .tesla_ev_recovery import current_vehicle_generation
        current = resolve_stock_bridge(
            self.hass, self.entry, self.bridge.vin, require_uptime=False,
        )
        return bool(current is not None and current.identity == self.bridge.identity
                    and current.runtime_data is self.bridge.runtime_data
                    and current.runtime_ref is self.bridge.runtime_ref
                    and (self.generation is None or self.generation == current_vehicle_generation(
                        self.hass, self.entry, self.bridge.vin))
                    and self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id) is current.runtime_ref)

    async def wait(self, predicate: Any) -> None:
        while time.monotonic() < self.deadline:
            if not self.current():
                raise RuntimeError("stock_bridge_identity_changed")
            if predicate():
                return
            await asyncio.sleep(POLL_SECONDS)
        raise TimeoutError("stock_bridge_observation_timeout")

    async def restart(self) -> bool:
        if not self.current() or getattr(self.bridge.runtime_data, "available", None) is not True:
            return False
        # Observe availability concurrently with the blocking service; a fast
        # disconnect must not be missed while awaiting its normal return.
        self.reset_at = time.time()
        async def watch() -> None:
            await self.wait(lambda: getattr(self.bridge.runtime_data, "available", None) is False)
            self.saw_disconnect = True
        observer = asyncio.create_task(watch())
        try:
            await self.hass.services.async_call("button", "press", {
                "entity_id": self.bridge.identities["restart"]["entity_id"]}, blocking=True)
            await observer
        finally:
            if not observer.done():
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
        return self.current() and self.saw_disconnect

    async def disconnected(self) -> bool:
        return self.current() and self.saw_disconnect

    async def connected(self) -> bool:
        await self.wait(lambda: getattr(self.bridge.runtime_data, "available", None) is True)
        return True

    def identity(self) -> dict[str, Any]:
        return self.bridge.identity if self.current() else {}

    async def uptime(self) -> dict[str, Any]:
        entity = self.bridge.identities["uptime"]["entity_id"]
        if self.reset_at is None:
            requested_at = time.time()

            def fresh() -> bool:
                value = _uptime(self.hass, entity)
                if (value is None or value["observed_at"] <= requested_at
                        or not self.current()):
                    return False
                self.last_uptime = value
                return True

            await self.wait(fresh)
            if self.last_uptime is None:
                raise RuntimeError("stock_bridge_uptime_unavailable")
            return dict(self.last_uptime)
        before = self.last_uptime
        def changed() -> bool:
            value = _uptime(self.hass, entity)
            if value is None or value["observed_at"] <= self.reset_at:
                return False
            if before is not None and value["observed_at"] <= before["observed_at"]:
                return False
            self.last_uptime = value
            return True
        await self.wait(changed)
        return dict(self.last_uptime)

    async def contain(self) -> None:
        if self.current():
            entity_id = self.bridge.identities["radio"]["entity_id"]
            await self.hass.services.async_call("switch", "turn_off", {
                "entity_id": entity_id}, blocking=True)
            _LOGGER.warning(
                "Tesla transport recovery did not complete; requested BLE "
                "Connection off for %s to contain the unresolved transport. "
                "Charging commands remain held pending successful recovery",
                entity_id,
            )


def reconciliation_evidence(observation: Any) -> dict[str, Any] | None:
    """Keep native requested settings distinct from actual charging current."""
    if observation is None or getattr(observation, "origin", None) != "native_cloud":
        return None
    paths = getattr(observation, "field_provenance", {})
    return {"vin": observation.vin, "requested_current": observation.requested_current,
            "charge_limit": observation.charge_limit_soc,
            "charging_state": observation.charge_state,
            "charger_actual_current": observation.charger_actual_current,
            "power": observation.charger_power,
            "observed_at": observation.source_charge_timestamp,
            "source_charge_timestamp": observation.source_charge_timestamp,
            "independent": True, "origin": observation.origin,
            "producer_identity": observation.producer_identity,
            "config_entry_id": observation.config_entry_id, "device_id": observation.device_id,
            "domain": observation.domain,
            "provenance": {
                "requested_current": paths.get("requested_current"),
                "charge_limit": paths.get("charge_limit_soc"),
                "charging": paths.get("charge_state"),
                "power": paths.get("charger_power"),
            }}


def _same_producer(identity: Any, bridge: StockBridge) -> bool:
    wanted = bridge.identities["charger"]
    return isinstance(identity, Mapping) and all(
        identity.get(key) == wanted.get(key)
        for key in ("platform", "config_entry_id", "device_id")
    )


def _covered_records(snapshot: Mapping[str, Any], bridge: StockBridge) -> list[dict[str, str]]:
    """Select actual surviving records on the pinned physical producer."""
    covered = []
    vehicles = snapshot.get("recovery", {}).get("vehicles", {})
    for vin, ledger in vehicles.items():
        if not isinstance(ledger, Mapping):
            continue
        for attempt_id, record in ledger.get("attempts", {}).items():
            route = record.get("route", {})
            if (record.get("outcome", "pending") in {"pending", "sent_unknown"}
                    and not record.get("recovery_retired") and route.get("kind") == "ble"
                    and route.get("prefix") == bridge.prefix
                    and _same_producer(route.get("entity_identity"), bridge)):
                covered.append({"vehicle_id": str(vin), "attempt_id": str(attempt_id)})
        for hazard_id, hazard in ledger.get("hazards", {}).items():
            binding = hazard.get("binding", {}).get("entities", {})
            if (hazard.get("kind") == "ble_quarantine"
                    and hazard.get("state") in {"pending", "isolated"}
                    and not hazard.get("recovery_retired")
                    and hazard.get("prefix") == bridge.prefix
                    and _same_producer(binding.get("charger"), bridge)):
                covered.append({"vehicle_id": str(vin), "hazard_id": str(hazard_id)})
    return covered


async def async_register_evidence_loss(
    hass: Any, entry: Any, *, vins: list[str], operator_confirmation: bool,
    provenance: Any, surviving_facts: Any,
) -> list[str]:
    """Preserve a real loss incident without manufacturing a historical command.

    Every current bridge is recorded before any scope is confirmed. A partial
    save therefore leaves held incidents, never a partially recovered site.
    The caller supplies the operator's actual producer-inventory confirmation.
    """
    from .tesla_ev_recovery import (
        async_confirm_recovery_scope, async_record_evidence_loss,
        recovery_sidecar_snapshot,
    )
    from .tesla_ble_mapping import parse_tesla_ble_vehicle_mapping
    config = {**entry.data, **entry.options}
    configured = parse_tesla_ble_vehicle_mapping(config.get("tesla_ble_vehicle_mapping"))
    if not configured or set(configured) != {str(vin) for vin in vins}:
        raise ValueError("evidence_loss_vehicle_inventory_incomplete")
    bridges = [resolve_stock_bridge(hass, entry, str(vin)) for vin in vins]
    if not bridges or any(bridge is None for bridge in bridges):
        raise ValueError("evidence_loss_producer_scope_incomplete")
    inventory = [bridge.identity for bridge in bridges]
    if len({bridge.producer for bridge in bridges}) != len(bridges):
        raise ValueError("evidence_loss_producer_scope_ambiguous")
    ids = []
    for bridge in bridges:
        incident = await async_record_evidence_loss(
            hass, entry, surviving_facts=surviving_facts, provenance=provenance,
            missing_fields=["historical_attempt_id", "historical_numeric_target",
                            "complete_historical_producer_binding"],
            candidate_producers=[bridge.identity], candidate_vehicles=[bridge.vin],
            covered_attempts=[],
            scope_basis={"kind": "operator_inventory_and_current_registry",
                         "complete_current_inventory": inventory,
                         "historical_identity_reconstructed": False},
        )
        if incident is None:
            raise RuntimeError("evidence_loss_incident_not_persisted")
        ids.append(incident["incident_id"])
    if operator_confirmation is True:
        for incident_id in ids:
            snapshot = recovery_sidecar_snapshot(hass, entry)
            result = await async_confirm_recovery_scope(
                hass, entry, incident_id, expected_revision=snapshot["revision"],
                operator_confirmation=True,
            )
            if not result:
                raise RuntimeError("evidence_loss_scope_not_persisted")
    return ids


async def async_recover_vehicle_transport(
    hass: Any, entry: Any, vin: str, *, incident_id: str | None = None,
) -> bool:
    """Recover one exact stock producer without changing charging settings."""
    from .const import CONF_EV_PROVIDER, EV_PROVIDER_BOTH, EV_PROVIDER_FLEET_API
    from .tesla_ev_observation import (
        async_read_independent_tesla_vehicle,
        async_wake_independent_tesla_vehicle,
        independent_tesla_source_binding,
    )
    from .tesla_ev_recovery import (
        async_confirm_recovery_scope, async_record_evidence_loss, async_recover_stock,
        current_vehicle_generation, recovery_sidecar_snapshot,
    )
    bridge = resolve_stock_bridge(hass, entry, str(vin))
    snapshot = recovery_sidecar_snapshot(hass, entry)
    if bridge is None or snapshot is None:
        return False
    runtime = bridge.runtime_ref
    # Recovery work has its own lane; it never holds the car's Stop lane or
    # the site admission lock while rebooting/observing a provider.
    locks = runtime.setdefault("_tesla_stock_recovery_locks", {})
    lock = locks.setdefault(bridge.producer, asyncio.Lock())
    if lock.locked():
        return False
    async with lock:
        snapshot = recovery_sidecar_snapshot(hass, entry)
        incidents = snapshot.get("incidents", {})
        if incident_id is not None:
            incident = incidents.get(incident_id)
        else:
            incident = next((value for value in incidents.values()
                             if str(vin) in value.get("current_candidate_vehicles", [])
                             and value.get("phase") != "released"), None)
        if incident is None:
            covered = _covered_records(snapshot, bridge)
            if not covered:
                return False
            affected = list(dict.fromkeys(item["vehicle_id"] for item in covered))
            # A physical bridge can retire all its old VIN authority, but the
            # currently supported explicit mapping is one VIN per bridge.
            if affected != [str(vin)]:
                return False
            incident = await async_record_evidence_loss(
                hass, entry, kind="command_uncertainty", covered_attempts=covered,
                candidate_producers=[bridge.identity], candidate_vehicles=affected,
                scope_basis={"kind": "recorded_binding_and_current_registry"},
            )
            if incident is None:
                return False
            snapshot = recovery_sidecar_snapshot(hass, entry)
            confirmed = await async_confirm_recovery_scope(
                hass, entry, incident["incident_id"], expected_revision=snapshot["revision"],
                automatic=True,
            )
            if not confirmed:
                return False
            incident = confirmed
        if (not isinstance(incident, Mapping)
                or incident.get("current_candidate_vehicles") != [str(vin)]
                or incident.get("current_candidate_producers") != [bridge.identity]):
            return False
        if incident.get("phase") == "released":
            return True
        transaction = StockBridgeTransaction(hass, entry, bridge)
        source_binding = independent_tesla_source_binding(hass, str(vin))
        if source_binding is None:
            return False
        first_observations: dict[str, Any] = {}

        def source_current() -> bool:
            return (transaction.current()
                    and independent_tesla_source_binding(hass, str(vin)) == source_binding)

        def observation_matches(observation: Any) -> bool:
            return bool(observation is not None and all(
                getattr(observation, key, None) == source_binding.get(key)
                for key in ("vin", "domain", "config_entry_id", "device_id", "producer_identity")
            ))

        def quiesce(*_args: Any) -> bool:
            transaction.generation = current_vehicle_generation(hass, entry, str(vin))
            return source_current()

        def cloud_wake_allowed() -> bool:
            config = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
            return config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API) in (
                EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH,
            )

        async def read_observation(
            vehicle: str, *, updated_after: Any | None = None,
            allow_paid_refresh: bool | None = None,
        ) -> Any:
            """Read only while the exact recovery binding and policy remain current."""
            if vehicle != str(vin) or not source_current():
                return None
            paid = cloud_wake_allowed() if allow_paid_refresh is None else bool(
                allow_paid_refresh
            )
            if paid and not cloud_wake_allowed():
                return None
            return await async_read_independent_tesla_vehicle(
                hass, vehicle, timeout=20, updated_after=updated_after,
                allow_paid_refresh=paid,
                authorize=lambda: source_current() and (
                    not paid or cloud_wake_allowed()
                ),
            )

        async def observe(vehicle: str) -> dict[str, Any] | None:
            if vehicle != str(vin) or not source_current():
                return None
            observation = await read_observation(vehicle)
            if observation is None and source_current():
                # Wake is a separate non-charging operation. The old producer
                # has already been retired; retain the recovery generation
                # while allowing the ordinary bounded BLE wake/readiness wait.
                from .automations.actions import _wake_tesla_ble
                await _wake_tesla_ble(
                    hass, bridge.prefix, wait_timeout=120, allow_backoff=False,
                    authorize=source_current,
                )
                if not source_current():
                    return None
                observation = await read_observation(vehicle)
            if observation is None and source_current() and cloud_wake_allowed():
                # BLE remains the primary recovery path. Only after its
                # bounded wake/read attempt has produced no exact cloud
                # charge-state response may the independent native cloud
                # client request one wake. A wake acknowledgement is not
                # reconciliation evidence; the following fresh read is.
                cloud_wake = await async_wake_independent_tesla_vehicle(
                    hass, vehicle, expected_binding=source_binding,
                    authorize=lambda: source_current() and cloud_wake_allowed(),
                    timeout=60,
                )
                if not source_current():
                    return None
                if cloud_wake:
                    observation = await read_observation(
                        vehicle, allow_paid_refresh=True,
                    )
            if (not source_current() or not observation_matches(observation)):
                return None
            evidence = reconciliation_evidence(observation)
            if evidence is not None:
                first_observations[vehicle] = observation
            return evidence

        async def reconcile(_observations: Any) -> bool:
            # Use the native requested-setting endpoint contract and two new
            # measurements. This is operational convergence, not a claim that
            # higher counters or a Stop cancelled an old signed message.
            for vehicle, first in first_observations.items():
                await asyncio.sleep(1)
                if not source_current():
                    return False
                second = await read_observation(
                    vehicle, updated_after=first.source_charge_timestamp,
                )
                if (not source_current() or not observation_matches(second)
                        or second.origin != "native_cloud" or second.vin != vehicle
                        or second.producer_identity != first.producer_identity
                        or second.domain != first.domain
                        or second.config_entry_id != first.config_entry_id
                        or second.device_id != first.device_id
                        or second.requested_current != first.requested_current
                        or second.charge_limit_soc != first.charge_limit_soc
                        or second.charge_state != first.charge_state
                        or second.charge_state.lower() in {"starting", "unknown"}):
                    differences = [
                        field for field in (
                            "origin", "vin", "producer_identity", "domain",
                            "config_entry_id", "device_id", "requested_current",
                            "charge_limit_soc", "charge_state",
                        ) if getattr(second, field, None) != getattr(first, field, None)
                    ]
                    _LOGGER.warning(
                        "Tesla transport recovery for %s did not converge: "
                        "second independent observation present=%s; differing fields=%s",
                        bridge.prefix, second is not None, differences,
                    )
                    return False
                _observations[vehicle]["converged_observation"] = reconciliation_evidence(second)
            return bool(first_observations)

        succeeded = False
        try:
            succeeded = await async_recover_stock(
                hass, entry, incident_id=incident["incident_id"],
                profile=bridge.profile, identity=bridge.identity,
                restart=transaction.restart, wait_disconnected=transaction.disconnected,
                wait_connected=transaction.connected, read_identity=transaction.identity,
                read_uptime=transaction.uptime, quiesce=quiesce,
                observe=observe, reconcile=reconcile,
            )
            return succeeded
        finally:
            if not succeeded:
                # Best effort containment adds no success evidence. Durable
                # phase/incident gates remain held even if this service fails.
                try:
                    await asyncio.shield(transaction.contain())
                except BaseException:
                    pass
