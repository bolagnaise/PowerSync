"""Bounded Tesla charger-capability refresh for Smart Schedule.

Tesla integrations can retain the previous EVSE pilot limit while a vehicle is
idle after moving chargers.  A wake is attempted first.  When an exact VIN is
uniquely associated with one connected Wall Connector and the VIN-scoped
providers remain stale, a short, minimum-current probe recreates the electrical
negotiation.  Probe ownership is per physical Wall Connector and persisted so
restart recovery cannot replay an unproven stop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from ..const import CONF_MONITORING_MODE, DOMAIN


_LOGGER = logging.getLogger(__name__)

PASSIVE_REFRESH_SECONDS = 5
POST_WAKE_REFRESH_SECONDS = 5
PROBE_CONFIRM_SECONDS = 20
PROBE_AMPS = 5

CapabilityResolver = Callable[[str], Awaitable[Optional[dict[str, Any]]]]
EligibilityCheck = Callable[[str, str], Awaitable[bool]]
PlanInvalidator = Callable[[str], None]


class TeslaCapabilityRefreshCoordinator:
    """Coordinate one safe capability acquisition pipeline per loadpoint."""

    def __init__(
        self,
        hass: Any,
        config_entry: Any,
        *,
        resolve_capability: CapabilityResolver,
        is_eligible: EligibilityCheck,
        invalidate_plan: PlanInvalidator,
    ) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self._resolve_capability = resolve_capability
        self._is_eligible = is_eligible
        self._invalidate_plan = invalidate_plan
        entry = hass.data.setdefault(DOMAIN, {}).setdefault(
            config_entry.entry_id, {}
        )
        records = entry.setdefault("ev_capability_refresh_records", {})
        self._records: dict[str, dict[str, Any]] = (
            records if isinstance(records, dict) else {}
        )
        entry["ev_capability_refresh_records"] = self._records
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    @staticmethod
    def _episode_key(vin: str, serial: str, connected_at: float) -> str:
        return f"{vin.upper()}|{serial.upper()}|{connected_at:.6f}"

    def _monitoring_mode(self) -> bool:
        return bool(
            self.config_entry.options.get(
                CONF_MONITORING_MODE,
                self.config_entry.data.get(CONF_MONITORING_MODE, False),
            )
        )

    async def request(
        self,
        *,
        vehicle_id: str,
        vehicle_vin: str,
        capability: Optional[dict[str, Any]],
        configured_max_amps: int,
        min_charge_amps: int,
        voltage: int,
        phases: int,
    ) -> bool:
        """Start a refresh task for a qualifying new connection episode."""
        if self._monitoring_mode() or not capability:
            return False
        if not capability.get("capability_refresh_required"):
            return False
        serial = str(capability.get("active_wall_connector_serial") or "")
        connected_at = capability.get("wall_connector_connected_observed_at")
        if not serial or not isinstance(connected_at, (int, float)):
            return False
        try:
            observed_max = int(capability.get("max_charge_amps") or 0)
        except (TypeError, ValueError):
            return False
        # A probe has no planning value when the conservative observation is
        # already at the configured ceiling.
        if configured_max_amps - observed_max < 2:
            return False

        key = self._episode_key(vehicle_vin, serial, float(connected_at))
        record = self._records.get(key)
        if isinstance(record, dict) and record.get("phase") in {
            "fresh",
            "failed",
            "cancelled",
        }:
            return False
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return False

        if isinstance(record, dict) and any(
            record.get(field)
            for field in (
                "start_command_pending",
                "start_issued",
                "stop_required",
            )
        ):
            coro = self._recover_interrupted_probe(
                key, vehicle_id, vehicle_vin, serial
            )
        else:
            if not await self._is_eligible(vehicle_vin, serial):
                return False
            self._records[key] = {
                "vehicle_id": vehicle_id,
                "vin": vehicle_vin.upper(),
                "connector_serial": serial.upper(),
                "connected_observed_at": float(connected_at),
                "phase": "passive",
                "session_id": None,
                "start_issued": False,
                "start_command_pending": False,
                "start_command_at": None,
                "start_confirmed": False,
                "stop_required": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._persist()
            coro = self._run_refresh(
                key=key,
                vehicle_id=vehicle_id,
                vehicle_vin=vehicle_vin.upper(),
                serial=serial.upper(),
                configured_max_amps=int(configured_max_amps),
                min_charge_amps=int(min_charge_amps),
                voltage=int(voltage),
                phases=int(phases),
            )

        create_task = getattr(self.hass, "async_create_task", None)
        task = (
            create_task(coro)
            if callable(create_task)
            else asyncio.create_task(coro)
        )
        self._tasks[key] = task
        task.add_done_callback(lambda _task, episode=key: self._tasks.pop(episode, None))
        return True

    async def _persist(self) -> None:
        from .ev_ownership import persist_ev_runtime_state

        await persist_ev_runtime_state(self.hass, self.config_entry)

    async def _fresh_capability(
        self,
        vehicle_vin: str,
        serial: str,
        *,
        observed_after: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        capability = await self._resolve_capability(vehicle_vin)
        if not capability:
            return None
        if str(capability.get("active_wall_connector_serial") or "").upper() != serial:
            return None
        if capability.get("capability_refresh_required"):
            provider_caps = capability.get("provider_max_charge_amps") or []
            capability_observed_at = capability.get("capability_observed_at")
            physically_refreshed_derate = bool(
                observed_after is not None
                and len(provider_caps) == 1
                and isinstance(capability_observed_at, (int, float))
                and capability_observed_at >= observed_after
            )
            if not physically_refreshed_derate:
                return None
        return capability

    async def _run_refresh(
        self,
        *,
        key: str,
        vehicle_id: str,
        vehicle_vin: str,
        serial: str,
        configured_max_amps: int,
        min_charge_amps: int,
        voltage: int,
        phases: int,
    ) -> None:
        from . import actions
        from .ev_ownership import (
            can_claim_ev_ownership,
            claim_ev_ownership,
            get_ev_ownership,
            release_ev_ownership,
        )

        record = self._records[key]
        session_id: Optional[str] = None
        physical_start_confirmed = False
        fresh_capability: Optional[dict[str, Any]] = None
        params = {
            "charger_type": "tesla",
            "vehicle_id": vehicle_vin,
            "vehicle_vin": vehicle_vin,
            "owner_mode": "capability_probe",
            "skip_ownership": True,
            "max_charge_amps": min(configured_max_amps, max(PROBE_AMPS, min_charge_amps)),
            "configured_max_charge_amps": configured_max_amps,
            "voltage": voltage,
            "phases": phases,
        }
        try:
            await asyncio.sleep(PASSIVE_REFRESH_SECONDS)
            if not await self._is_eligible(vehicle_vin, serial):
                record["phase"] = "cancelled"
                return
            fresh_capability = await self._fresh_capability(vehicle_vin, serial)
            if fresh_capability is not None:
                record["phase"] = "fresh"
                return

            record["phase"] = "wake"
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self._persist()
            await actions._wake_tesla_ev(
                self.hass,
                vehicle_vin,
                wait_timeout=15,
                max_retries=1,
            )
            await asyncio.sleep(POST_WAKE_REFRESH_SECONDS)
            if not await self._is_eligible(vehicle_vin, serial):
                record["phase"] = "cancelled"
                return
            fresh_capability = await self._fresh_capability(vehicle_vin, serial)
            if fresh_capability is not None:
                record["phase"] = "fresh"
                return

            baseline = actions._tesla_physical_charging_snapshot(
                self.hass,
                self.config_entry,
                vehicle_vin,
                params,
            )
            if baseline.get("charging") or baseline.get("measurements"):
                record["phase"] = "cancelled"
                return
            allowed, _lease_id, _lease, reason = can_claim_ev_ownership(
                self.hass,
                self.config_entry,
                vehicle_vin,
                owner_mode="capability_probe",
                allow_takeover=False,
            )
            if not allowed:
                _LOGGER.info(
                    "Tesla capability refresh skipped for %s: %s",
                    vehicle_id,
                    reason,
                )
                record["phase"] = "cancelled"
                return
            if not await self._is_eligible(vehicle_vin, serial):
                record["phase"] = "cancelled"
                return

            # The eligibility callback resolves live HA state and therefore
            # yields. Re-run the ownership admission synchronously after that
            # await so an external/manual claim cannot be overwritten in the
            # final gap before this probe records its lease.
            allowed, _lease_id, _lease, reason = can_claim_ev_ownership(
                self.hass,
                self.config_entry,
                vehicle_vin,
                owner_mode="capability_probe",
                allow_takeover=False,
            )
            if not allowed:
                _LOGGER.info(
                    "Tesla capability refresh skipped for %s after ownership "
                    "changed: %s",
                    vehicle_id,
                    reason,
                )
                record["phase"] = "cancelled"
                return

            session_id = uuid4().hex
            record.update({
                "phase": "probing",
                "session_id": session_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            claim_ev_ownership(
                self.hass,
                self.config_entry,
                vehicle_vin,
                owner_mode="capability_probe",
                session_id=session_id,
                reason="Refreshing exact Wall Connector capability",
                command="capability_probe_claim",
                extra={"connector_serial": serial, "episode_key": key},
            )
            await self._persist()

            probe_amps = min(PROBE_AMPS, max(1, min_charge_amps))
            if not await actions._set_vehicle_amps(
                self.hass,
                self.config_entry,
                vehicle_vin,
                probe_amps,
                params,
            ):
                record["phase"] = "failed"
                return
            command_started_at = datetime.now(timezone.utc)
            record.update({
                "start_command_pending": True,
                "start_command_at": command_started_at.isoformat(),
                "updated_at": command_started_at.isoformat(),
            })
            await self._persist()
            started = await actions._action_start_ev_charging(
                self.hass,
                self.config_entry,
                params,
                context=None,
            )
            record["start_command_pending"] = False
            record["start_issued"] = bool(started)
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self._persist()
            if not started:
                record["phase"] = "failed"
                return

            physical_start_confirmed, evidence = (
                await actions._wait_for_tesla_physical_start(
                    self.hass,
                    self.config_entry,
                    vehicle_vin,
                    params,
                    baseline,
                    command_started_at,
                    timeout_seconds=PROBE_CONFIRM_SECONDS,
                )
            )
            if not physical_start_confirmed:
                _LOGGER.warning(
                    "Tesla capability probe for %s was not physically confirmed: %s",
                    vehicle_id,
                    evidence,
                )
                record["phase"] = "failed"
                return

            record.update({
                "start_confirmed": True,
                "stop_required": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            await self._persist()
            fresh_capability = await self._fresh_capability(
                vehicle_vin,
                serial,
                observed_after=command_started_at.timestamp(),
            )
            record["phase"] = "fresh" if fresh_capability else "failed"
        except asyncio.CancelledError:
            record["phase"] = "cancelled"
            raise
        except Exception as err:
            record["phase"] = "failed"
            _LOGGER.warning(
                "Tesla capability refresh failed for %s: %s",
                vehicle_id,
                err,
            )
        finally:
            if physical_start_confirmed and session_id:
                lease_id, lease = get_ev_ownership(
                    self.hass, self.config_entry, vehicle_vin
                )
                still_probe_owned = bool(
                    lease_id == vehicle_vin
                    and lease
                    and lease.get("owner_mode") == "capability_probe"
                    and lease.get("session_id") == session_id
                )
                if still_probe_owned:
                    stopped = await actions._action_stop_ev_charging(
                        self.hass,
                        self.config_entry,
                        {
                            **params,
                            "_force_tesla_stop_request": True,
                        },
                    )
                    record["stop_required"] = not stopped
                    release_ev_ownership(
                        self.hass,
                        self.config_entry,
                        vehicle_vin,
                        reason="Capability probe complete",
                        command="capability_probe_stop",
                        success=stopped,
                    )
                else:
                    # An ownership change is an external takeover. Never let a
                    # stale probe issue a compensating stop against it.
                    record["stop_required"] = False
                    record["phase"] = "cancelled"
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self._persist()
            if fresh_capability is not None:
                self._invalidate_plan(vehicle_id)

    async def _recover_interrupted_probe(
        self,
        key: str,
        vehicle_id: str,
        vehicle_vin: str,
        serial: str,
    ) -> None:
        """Finish only a persisted, physically-confirmed probe after restart."""
        from . import actions
        from .ev_ownership import get_ev_ownership

        record = self._records[key]
        try:
            # A new runtime owner wins. Recovery must never stop that session.
            _lease_id, lease = get_ev_ownership(
                self.hass, self.config_entry, vehicle_vin
            )
            if lease is not None or self._monitoring_mode():
                record["phase"] = "cancelled"
                record["stop_required"] = False
                return
            capability = await self._resolve_capability(vehicle_vin)
            if not capability or str(
                capability.get("active_wall_connector_serial") or ""
            ).upper() != serial:
                record["phase"] = "cancelled"
                record["stop_required"] = False
                return
            params = {
                "charger_type": "tesla",
                "vehicle_id": vehicle_vin,
                "vehicle_vin": vehicle_vin,
                "owner_mode": "capability_probe",
                "skip_ownership": True,
                "_force_tesla_stop_request": True,
            }
            command_started_at = None
            try:
                command_started_at = datetime.fromisoformat(
                    str(record.get("start_command_at"))
                )
            except (TypeError, ValueError):
                pass
            snapshot = actions._tesla_physical_charging_snapshot(
                self.hass,
                self.config_entry,
                vehicle_vin,
                params,
                updated_after=command_started_at,
            )
            physically_proven = bool(
                record.get("start_confirmed")
                and (snapshot.get("charging") or snapshot.get("measurements"))
            ) or bool(
                snapshot.get("fresh_direct_measurements")
                or (
                    snapshot.get("charging")
                    and snapshot.get("fresh_measurements")
                )
            )
            if physically_proven:
                stopped = await actions._action_stop_ev_charging(
                    self.hass, self.config_entry, params
                )
                record["stop_required"] = not stopped
                record["phase"] = "cancelled" if stopped else "failed"
            else:
                record["stop_required"] = False
                record["phase"] = "cancelled"
            record["start_command_pending"] = False
        finally:
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self._persist()
