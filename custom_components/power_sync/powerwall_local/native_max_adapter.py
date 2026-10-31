"""Provider boundary for deliberate native Max, with one restore baseline.

Optimiser charge setup temporarily enables a known grid-charge setting only
under current PowerSync permission, with durable ownership and restoration.
This actuator never raises site limits.
A stricter software export cap is applied natively before tariff control.
A software battery-input ceiling is not promised by native Max.
Every local write and tariff retry must use its immediately acquired fence.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
import math
import time

from .battery_dispatch import battery_watts, site_grid_limits_w, validate_dispatch_config
from .full_rate_charge import native_max_charge_eligibility
from .control_method import control_policy_result
from .native_max_charge import (NativeMaxChargeError, NativeMaxTariffNotSubmitted,
    NativeMaxTariffRejected, NativeMaxExportNotSubmitted, NativeMaxExportRejected)
from ..tesla_export_permissions import EXPORT_RULES, ExportRuleReading

_LOGGER = logging.getLogger(__name__)


def _signed_export_limit(value):
    """Preserve valid signed limits, including Tesla's explicit unset sentinel.

    Missing or malformed evidence must never become an unset-cap permission,
    a restoration target, or proof that an external controller edited a cap.
    """
    if type(value) not in (int, float) or not math.isfinite(value) or value > 0:
        raise ValueError("Native export limit is unavailable or invalid")
    return value


class NativeMaxChargeAdapter:
    """Translate a durable Max session into acknowledged provider settings.

    ``read_tariff`` returns a fresh raw tariff. ``upload`` must invoke its
    async ``before_write`` callback before EACH POST, then invoke the returned
    synchronous fence immediately at submission. It confirms tariff readback.
    ``restorable`` rejects temporary controller tariffs. ``policy`` returns a
    current ControlPolicyResult or an exact legacy boolean; ``config`` reads
    current settings. Neither grants authority from a saved snapshot.
    """

    def __init__(self, *, client, identity, current_identity, policy, config,
                 read_tariff, upload, restorable, forcing_tariff,
                 budget, entry_id, site, tariff_matches=None, monotonic=time.monotonic,
                 settle_interval=5, direction="charge", clock=time.time,
                 read_export_rule=None, write_export_rule=None,
                 reconcile_managed=None, reconcile_timeout=20):
        self.client, self.identity = client, identity
        self.current_identity, self.policy, self.config = current_identity, policy, config
        self.read_tariff, self.upload, self.restorable = read_tariff, upload, restorable
        self.forcing_tariff = deepcopy(forcing_tariff)
        self.budget, self.entry_id, self.site = budget, entry_id, site
        self.monotonic = monotonic
        self.tariff_matches = tariff_matches or (lambda expected, observed: expected == observed)
        self.token = None
        self._grid_charge_ready = False
        self._snapshot = None
        self._observed_at = None
        self.settle_interval = settle_interval
        self.direction = direction
        self.startup_kick = False
        self.planning_request = False
        self.clock = clock
        self._export_limit_ready = False
        self.guard_denial_reason = None
        self.read_export_rule, self.write_export_rule = read_export_rule, write_export_rule
        self.reconcile_managed = reconcile_managed
        self.reconcile_timeout = reconcile_timeout
        self._export_observation = None
        self._cloud_export_owned = False
        self._export_cloud_checked = False
        self._export_conflict = False
        self._export_missing_at = None
        self._export_untracked_ready = False
        self.ev_coexistence = False
        self._ev_coexistence_verified = False
        self.export_preparation_required = False

    def _can_prepare_untracked_export(self, config):
        """An omitted preference permits explicit setup, never inferred permission."""
        info = config.get("site_info") if isinstance(config, dict) else None
        return (isinstance(info, dict) and info.get("customer_preferred_export_rule") is None
                and self._export_missing_at is not None and not self._export_conflict
                and 0 <= self.monotonic() - self._export_missing_at <= 5
                and self.current_identity() == self.identity and self.client.local_access_enabled
                and callable(self.write_export_rule))

    @staticmethod
    def _local_export_rule(config):
        info = config.get("site_info") if isinstance(config, dict) else None
        rule = info.get("customer_preferred_export_rule") if isinstance(info, dict) else None
        return rule if rule in EXPORT_RULES else None

    def _export_rule(self, config):
        """Permission sidecar never changes raw config or native freshness."""
        info = config.get("site_info") if isinstance(config, dict) else None
        raw = info.get("customer_preferred_export_rule") if isinstance(info, dict) else None
        if raw is not None and raw not in EXPORT_RULES:
            return None
        local = self._local_export_rule(config)
        if local is not None and not self._cloud_export_owned:
            return local
        observed = self._export_observation
        if (observed is None or self.current_identity() != self.identity
                or not 0 <= self.monotonic() - observed[1] <= 5):
            return None
        if local is not None and local != observed[0]:
            return None
        return observed[0]

    async def _observe_export_rule(self, config, *, require_cloud=False):
        self._export_cloud_checked = False
        self._export_missing_at = None
        local = self._local_export_rule(config)
        if local is not None and not (require_cloud or self._cloud_export_owned):
            self._export_observation = None
            return local
        info = config.get("site_info") if isinstance(config, dict) else None
        raw = info.get("customer_preferred_export_rule") if isinstance(info, dict) else None
        self._export_observation = None
        if (raw is not None and raw not in EXPORT_RULES) or not callable(self.read_export_rule):
            return None
        started = self.monotonic()
        self._export_cloud_checked = True
        try:
            reading = await asyncio.wait_for(self.read_export_rule(), timeout=3)
        except Exception:
            return None
        if (self.current_identity() != self.identity
                or not self.client.local_access_enabled
                or not 0 <= self.monotonic() - started <= 5):
            return None
        rule = reading.value if isinstance(reading, ExportRuleReading) else reading
        if rule not in EXPORT_RULES:
            # Only a valid direct cloud response which actually omits the
            # preference can use acknowledgement-only preparation. Transport,
            # malformed and conflicting replies do not establish omission.
            if (isinstance(reading, ExportRuleReading) and reading.status in ("absent", "null")
                    and local is None and raw is None):
                self._export_missing_at = started
            return None
        if local is not None:
            self._export_conflict = local != rule
        if self._export_conflict:
            return None
        self._export_observation = (rule, started)
        return rule

    async def _write_cloud_export(self, rule, before_write, write_intent, *, restoring=False,
                                  require_readback=True):
        """Keep an uncertain cloud command owned until its effect is observed."""
        if not callable(self.write_export_rule):
            return False
        recorded = False
        async def submitted():
            nonlocal recorded
            await before_write()
            await write_intent("export", rule)
            recorded = True
            return await before_write()
        async def write():
            try:
                options = {} if require_readback else {"require_readback": False}
                confirmed = await self.write_export_rule(rule, submitted, restoring=restoring, **options)
                if confirmed is True:
                    # Persist a terminal receipt inside the drained provider
                    # task even when its caller was cancelled during submission.
                    await write_intent("export_confirmed")
                return confirmed
            except NativeMaxExportNotSubmitted:
                if recorded:
                    await write_intent("export_not_submitted")
                return False
            except NativeMaxExportRejected:
                if recorded:
                    await write_intent("export_rejected")
                return False
        confirmed = await self._mutate(write())
        return confirmed is True

    def _matches(self, expected, observed):
        # A same-priced foreign tariff is not this session's restore owner.
        return (isinstance(observed, dict) and expected.get("code") == observed.get("code")
                and self.tariff_matches(expected, observed))

    async def _mutate(self, command):
        # A cancelled HTTP await is not proof the remote command was unsent.
        # Keep the bounded provider task until it settles before restoration
        # can overtake it. Driver timeouts and one tariff attempt bound this;
        # retries/readback cannot submit another write without a new fence.
        task = asyncio.create_task(command)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    break
            try:
                task.result()
            except BaseException:
                pass
            raise

    def _owned_before(self, baseline, before_write):
        async def before_owned_write(native_config=None):
            observed = await self.read_tariff()
            if not any(self._matches(candidate, observed) for candidate in
                    (baseline["tariff"], baseline["forcing_tariff"])):
                raise NativeMaxChargeError("Native Max tariff ownership changed; cleanup retained")
            fence = (await before_write() if native_config is None else
                     await before_write(native_config))
            def checked():
                if self.current_identity() != self.identity:
                    raise NativeMaxChargeError("Native Max paired identity changed")
                fence()
                if not self._forcing_permitted(require_export_limit=True):
                    raise NativeMaxChargeError("Native Max forcing policy changed")
            checked()
            return checked
        return before_owned_write

    def _deny(self, reason, **values):
        """Record a failed check without logging gateway config or identities."""
        self.guard_denial_reason = reason
        # Never interpolate raw config, exception messages, ownership tokens or
        # unexpected strings supplied by a device into support logs.
        safe_values = {
            key: value if (type(value) is bool or
                           (type(value) in (int, float) and math.isfinite(value)))
            else None for key, value in values.items()
        }
        _LOGGER.warning("Native Powerwall %s admission denied: reason=%s diagnostics=%s",
                        self.direction, reason, safe_values)
        return False

    def authority(self):
        try:
            validate_dispatch_config(self.direction, dict(self.config()))
            if self.identity != self.current_identity():
                return self._deny("native_max_identity_changed")
            policy = control_policy_result(self.policy())
            if not policy.allowed:
                return self._deny(policy.reason, policy_allowed=False)
            if not self.client.local_access_enabled:
                return self._deny("native_max_local_access_disabled")
            return True
        except (ValueError, TypeError):
            return self._deny("native_max_dispatch_config_invalid")

    def _site_ev_coexistence(self, config, info, native_limit):
        """Grant coexistence only from fresh finite native/software site caps."""
        if not isinstance(info, dict) or type(native_limit) not in (int, float):
            return False
        if not math.isfinite(native_limit) or native_limit <= 0:
            return False
        # A current DCQ sample can carry an old signed config. Only the
        # config coordinator's provenance can authorize site-controller
        # coexistence; a missing stamp remains fail-closed.
        config_observed = getattr(self._snapshot, "config_observed_monotonic", None)
        if (type(config_observed) not in (int, float) or not math.isfinite(config_observed)
                or not 0 <= self.monotonic() - config_observed <= 5):
            return False
        key = ("optimization_max_grid_import_w" if self.direction == "charge"
               else "optimization_max_grid_export_w")
        software = config.get(key)
        if software in (None, 0):
            return True
        return (type(software) in (int, float) and math.isfinite(software)
                and software > 0 and native_limit <= software)

    def _forcing_permitted(self, native_config=None, *, require_export_limit=False,
                           preparing=False):
        snapshot = self._snapshot
        if snapshot is None or self._observed_at is None:
            return self._deny("native_snapshot_unavailable")
        if not self.authority():
            return False
        maximum = self.budget.native_max_claim(self.site)
        if maximum and maximum.get("token") != self.token:
            return self._deny("native_max_site_already_owned")
        if (maximum and maximum.get("direction", "charge") != self.direction):
            return self._deny("native_max_direction_changed")
        self.ev_coexistence = False
        self._ev_coexistence_verified = False
        self.export_preparation_required = False
        try:
            config = dict(self.config())
            if self.planning_request:
                capacity = config.get("powerwall_native_capacity_w")
                if type(capacity) not in (int, float) or not math.isfinite(capacity) or capacity <= 0:
                    return self._deny("native_capacity_unavailable", native_capacity_w=capacity)
            raw = snapshot.raw.get("config") if native_config is None else native_config
            info = raw.get("site_info") if isinstance(raw, dict) else None
            try:
                imported, _ = site_grid_limits_w(type("NativeBounds", (), {"raw": {"config": raw}})())
            except (ValueError, TypeError, KeyError):
                limits = info if isinstance(info, dict) else {}
                return self._deny("native_site_limits_invalid", site_info_present=isinstance(info, dict),
                                  native_import_limit_kw=limits.get("max_site_meter_power_ac"),
                                  native_export_limit_kw=limits.get("min_site_meter_power_ac"))
            permission = self._grid_value(raw)
            coexistence_limit = imported
            if self.direction == "discharge":
                try:
                    _, coexistence_limit = site_grid_limits_w(
                        type("NativeBounds", (), {"raw": {"config": raw}})())
                except (ValueError, TypeError, KeyError):
                    coexistence_limit = None
            self._ev_coexistence_verified = self._site_ev_coexistence(
                config, info, coexistence_limit)
            if self.direction == "discharge" and not self._ev_coexistence_verified:
                configured = config.get("optimization_max_grid_export_w")
                self.export_preparation_required = bool(
                    preparing and self._export_cap_can_be_tightened(
                        info, coexistence_limit, configured))
            if (any(r.get("site") == self.site for r in self.budget.managed.values())
                    and not self._ev_coexistence_verified
                    and not self.export_preparation_required):
                return self._deny("managed_ev_ownership_present")
            if self.direction == "discharge":
                if not isinstance(info, dict) or "min_site_meter_power_ac" not in info:
                    return self._deny("native_export_limit_missing")
                age = self.monotonic() - self._observed_at
                if not 0 <= age <= 5:
                    return self._deny("native_snapshot_stale", evidence_age_s=age)
                if not self.authority():
                    return False
                try:
                    _signed_export_limit(info["min_site_meter_power_ac"])
                except ValueError:
                    return self._deny("native_export_limit_invalid",
                                      native_export_limit_kw=info["min_site_meter_power_ac"])
                _, exported = site_grid_limits_w(type("NativeBounds", (), {"raw": {"config": raw}})())
                configured = config.get("optimization_max_grid_export_w")
                if configured is not None and (type(configured) not in (int, float)
                        or not math.isfinite(configured) or configured <= 0):
                    return self._deny("software_export_limit_invalid", software_export_limit_w=configured)
                if (configured is not None
                        # A known unset sentinel can be tightened at acquisition.
                        # Forcing still requires a verified finite software cap.
                        and (not preparing and (self._export_limit_ready or require_export_limit)
                            and (exported is None or exported > configured))):
                    return self._deny("native_export_limit_exceeds_software_limit",
                                      native_export_limit_w=exported, software_export_limit_w=configured)
                export = self._export_rule(raw)
                if ((export not in EXPORT_RULES and not self._can_prepare_untracked_export(raw))
                        or (self._export_untracked_ready and export in ("never", "pv_only"))):
                    return self._deny("native_export_rule_invalid",
                        local_export_field_present="customer_preferred_export_rule" in info,
                        cloud_read_attempted=self._export_cloud_checked)
                try:
                    battery_watts("discharge", 1, snapshot, config)
                except (ValueError, TypeError, KeyError) as error:
                    reason = {
                        "Battery is at its discharge reserve floor": "discharge_reserve_floor_reached",
                        "Local dispatch requires confirmed grid connection": "grid_connection_unconfirmed",
                        "Missing or invalid battery SOC": "battery_soc_unavailable",
                        "Non-finite battery SOC": "battery_soc_unavailable",
                        "Battery SOC is out of range": "battery_soc_invalid",
                        "Grid export limit blocks force discharge": "native_export_limit_blocks_discharge",
                    }.get(str(error), "discharge_inputs_invalid")
                    return self._deny(reason, soc_percent=snapshot.soc,
                                      gateway_reserve_percent=snapshot.backup_reserve_percent,
                                      software_reserve=config.get("optimization_backup_reserve", .2),
                                      hardware_reserve=config.get("hardware_backup_reserve", 0),
                                      load_w=snapshot.load_w, solar_w=snapshot.solar_w)
                self.ev_coexistence = self._ev_coexistence_verified
                return True
            if type(permission) is not bool:
                return self._deny("grid_charging_permission_unknown")
            eligibility = native_max_charge_eligibility(
                source="native_manual_max", requested_power_w=0, grid_import_target_w=None,
                planning_charge_max_w=config.get("optimization_max_charge_w", 5000),
                software_import_max_w=config.get("optimization_max_grid_import_w"),
                native_import_max_w=imported, native_import_known=isinstance(info, dict),
                pending_ev_w=self.budget.unobserved_w(self.site),
                native_ev_active=self.budget.site_armed(self.site),
                ev_coexistence=self._ev_coexistence_verified,
                grid_charging_allowed=(permission is False or (
                    permission is True and self.planning_request
                    and not self._grid_charge_ready
                    and config.get("optimization_allow_grid_charge") is True)),
                grid_connected=snapshot.grid_status == "SystemGridConnected",
                soc=snapshot.soc, soc_cap=config.get("optimization_grid_charge_soc_cap", 1)*100,
                evidence_fresh=0 <= self.monotonic()-self._observed_at <= 5,
                control_permitted=(authority_current := self.authority()))
            # Keep the precise runtime denial from the final authority fence;
            # the pure eligibility helper only knows its boolean outcome.
            if not authority_current:
                return False
            if not eligibility.eligible:
                return self._deny(eligibility.reason, native_import_limit_w=imported,
                                  software_import_limit_w=config.get("optimization_max_grid_import_w"),
                                  soc_percent=snapshot.soc,
                                  soc_cap_fraction=config.get("optimization_grid_charge_soc_cap", 1),
                                  evidence_age_s=self.monotonic()-self._observed_at)
            if eligibility.eligible:
                self.ev_coexistence = self._ev_coexistence_verified
            return eligibility.eligible
        except (ValueError, TypeError, KeyError):
            return self._deny("native_limit_or_config_invalid")

    def _sample_started(self, snapshot, fallback):
        started = getattr(snapshot, "observed_monotonic", None)
        return (started if type(started) in (int, float) and math.isfinite(started)
                and fallback <= started <= self.monotonic() else fallback)

    @staticmethod
    def _export_cap_can_be_tightened(info, native_limit, configured):
        return (isinstance(info, dict) and type(native_limit) in (int, float)
                and math.isfinite(native_limit) and native_limit > 0
                and type(configured) in (int, float) and math.isfinite(configured)
                and configured > 0 and native_limit > configured)

    async def guard(self, *, native_config=None, preparing=False):
        self.guard_denial_reason = "native_max_control_not_permitted"
        if not self.authority():
            return False
        await self.budget.load()
        reconcile = getattr(self, "reconcile_managed", None)
        try:
            started = self.monotonic()
            if native_config is not None:
                if not isinstance(native_config, dict):
                    return self._deny("native_config_unavailable")
                # A supplied writer config has no trustworthy timestamp. Read
                # a complete fresh snapshot and require exact config equality;
                # never promote DCQ receipt time into config provenance.
                self._snapshot = await asyncio.wait_for(self.client.get_snapshot(), timeout=10)
                fresh_config = (self._snapshot.raw.get("config")
                                 if isinstance(self._snapshot.raw, dict) else None)
                if fresh_config != native_config:
                    return self._deny("native_config_changed")
                config_provenance = getattr(self._snapshot, "config_observed_monotonic", None)
                observed = self._sample_started(self._snapshot, started)
                if not 0 <= self.monotonic() - observed <= 5:
                    refresh = getattr(self.client, "get_control_snapshot", None)
                    if not callable(refresh):
                        return self._deny("native_snapshot_stale")
                    refreshed_started = self.monotonic()
                    refreshed = await asyncio.wait_for(refresh(native_config), timeout=10)
                    refreshed_config = (refreshed.raw.get("config")
                                        if isinstance(refreshed.raw, dict) else None)
                    if refreshed_config != native_config:
                        return self._deny("native_config_changed")
                    # Preserve the original signed-config provenance exactly;
                    # only DCQ observation time may advance on this retry.
                    refreshed.config_observed_monotonic = config_provenance
                    self._snapshot = refreshed
                    started = refreshed_started
            else:
                self._snapshot = await asyncio.wait_for(self.client.get_snapshot(), timeout=10)
            self._observed_at = self._sample_started(self._snapshot, started)
            if self.direction == "discharge":
                await self._observe_export_rule(self._snapshot.raw.get("config"))
            if native_config is None and not 0 <= self.monotonic() - self._observed_at <= 5:
                # Refresh DCQ once without repeating the signed config read.
                # This path cannot grant config coexistence because the
                # supplied config has no fresh provenance; writer fences use
                # the explicit native_config branch above instead.
                started = self.monotonic()
                raw = self._snapshot.raw.get("config") if isinstance(self._snapshot.raw, dict) else None
                refresh = getattr(self.client, "get_control_snapshot", None)
                self._snapshot = await asyncio.wait_for(
                    refresh(raw) if isinstance(raw, dict) and callable(refresh)
                    else self.client.get_snapshot(), timeout=10)
                self._observed_at = self._sample_started(self._snapshot, started)
            self.guard_denial_reason = "native_max_policy_denied"
            permitted = self._forcing_permitted(preparing=preparing)
            if (not permitted and self.direction == "charge" and callable(reconcile)
                    and self.guard_denial_reason == "managed_ev_ownership_present"
                    and any(record.get("site") == self.site
                            for record in self.budget.managed.values())):
                try:
                    await asyncio.wait_for(reconcile(self.site), timeout=self.reconcile_timeout)
                except Exception:
                    _LOGGER.warning("Native Powerwall managed EV reconciliation unavailable")
                if not self.authority():
                    return False
                permitted = self._forcing_permitted()
            if permitted:
                self.guard_denial_reason = None
            return permitted
        except (ValueError, TypeError, KeyError, asyncio.TimeoutError):
            return self._deny("native_limit_evidence_unavailable")

    async def capture(self):
        self._export_limit_ready = False
        self._cloud_export_owned = False
        self._export_untracked_ready = False
        if not self.authority():
            raise NativeMaxChargeError("Native Max ownership is unavailable")
        started = self.monotonic()
        snapshot, tariff = await asyncio.gather(self.client.get_snapshot(), self.read_tariff())
        observed = self._sample_started(snapshot, started)
        config = snapshot.raw.get("config")
        if self.direction == "discharge":
            await self._observe_export_rule(config)
        refresh = getattr(self.client, "get_control_snapshot", None)
        if self.monotonic() - observed > 5 and isinstance(config, dict) and callable(refresh):
            observed = self.monotonic()
            snapshot = await asyncio.wait_for(refresh(config), timeout=10)
            observed = self._sample_started(snapshot, observed)
        info = config.get("site_info") if isinstance(config, dict) else None
        mode = config.get("default_real_mode") if isinstance(config, dict) else None
        reserve = info.get("backup_reserve_percent") if isinstance(info, dict) else None
        if (mode not in ("self_consumption", "autonomous", "backup")
                or type(reserve) not in (int, float) or not math.isfinite(reserve)
                or not 0 <= reserve <= 100 or not self.restorable(tariff)
                or not self._physical_sample(snapshot) or not 0 <= self.monotonic() - observed <= 5):
            raise NativeMaxChargeError("Exact native Max restoration baseline is unavailable")
        baseline = {"mode": mode, "reserve": reserve, "tariff": deepcopy(tariff),
                    "forcing_tariff": deepcopy(self.forcing_tariff),
                    "settle_charge_w": max(0, -snapshot.battery_w), "direction": self.direction}
        if (self.direction == "charge" and self.planning_request
                and info.get("disallow_charge_from_grid_with_solar_installed") is True):
            baseline["grid_charge_blocked"] = True
        if self.direction == "discharge":
            export = self._export_rule(config)
            untracked = export is None and self._can_prepare_untracked_export(config)
            if export not in EXPORT_RULES and not untracked:
                raise NativeMaxChargeError("Native discharge export baseline is unavailable")
            source = "untracked" if untracked else "local" if self._local_export_rule(config) is not None else "cloud"
            self._cloud_export_owned = source == "cloud"
            baseline["export_rule_source"] = source
            if untracked:
                baseline["export_permission_untracked"] = True
            else:
                baseline["export_rule"] = export
            _LOGGER.info("Native discharge original export permission source=%s rule=%s", source, export)
            config = dict(self.config())
            def percent(value):
                value = float(value)
                return value * 100 if value <= 1 else value
            try:
                original_limit = _signed_export_limit(info.get("min_site_meter_power_ac"))
            except ValueError as err:
                raise NativeMaxChargeError("Native discharge export limit baseline is unavailable") from err
            configured_limit = config.get("optimization_max_grid_export_w")
            force_limit = original_limit if configured_limit is None else max(original_limit, -configured_limit / 1000)
            baseline.update(export_limit=original_limit, force_export_limit=force_limit,
                settle_discharge_w=max(0, snapshot.battery_w),
                force_reserve=max(reserve, percent(config.get("optimization_backup_reserve", .2)),
                                  percent(config.get("hardware_backup_reserve", 0))))
        return baseline

    @staticmethod
    def _physical_sample(snapshot):
        values = [getattr(snapshot, key, None) for key in ("battery_w", "grid_w", "load_w", "solar_w")]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            return False
        battery, grid, load, solar = values
        # Signed PV meters can report small negative standby draw (the live
        # owner's gateway reports -3 W). Balance uses the signed value intact.
        return (snapshot.grid_status == "SystemGridConnected" and load >= 0
                and abs(grid + battery + solar - load) <= max(250, .05 * max(abs(grid), abs(battery), load)))

    async def settled(self, baseline):
        """Require two fresh, balanced physical samples before freeing EV capacity.

        Native TOU charging can persist after settings readback. Recovery starts
        these observations again; a previous process's sample is not reused.
        Solar surplus charging may continue normally after grid charging stops.
        """
        discharge = baseline.get("direction", "charge") == "discharge"
        original = baseline.get("settle_discharge_w" if discharge else "settle_charge_w", 0)
        if type(original) not in (int, float) or not math.isfinite(original) or original < 0:
            return False
        for index in range(2):
            if index:
                await asyncio.sleep(self.settle_interval)
            if self.current_identity() != self.identity:
                return False
            started = self.monotonic()
            snapshot = await self.client.get_snapshot()
            observed = self._sample_started(snapshot, started)
            raw = snapshot.raw.get("config")
            refresh = getattr(self.client, "get_control_snapshot", None)
            if self.monotonic() - observed > 5 and isinstance(raw, dict) and callable(refresh):
                observed = self.monotonic()
                snapshot = await asyncio.wait_for(refresh(raw), timeout=10)
                observed = self._sample_started(snapshot, observed)
            if (not 0 <= self.monotonic() - observed <= 5
                    or not self._physical_sample(snapshot)
                    or (snapshot.battery_w > max(original, snapshot.load_w - snapshot.solar_w) + 250
                        if discharge else -snapshot.battery_w > max(original, snapshot.solar_w - snapshot.load_w) + 250)):
                return False
        return True

    async def claim(self, token, deadline, *, preparing=False):
        self.token = token
        return await self.budget.claim_native_max(
            self.entry_id, self.site, token, deadline, direction=self.direction,
            ev_coexistence=self.ev_coexistence, preparing=preparing)

    async def release(self, token):
        result = await self.budget.release_native_max(self.entry_id, self.site, token)
        if result:
            self._export_limit_ready = False
            self._grid_charge_ready = False
            self._export_untracked_ready = False
            if self.token == token:
                self.token = None
        return result

    def _grid_value(self, config):
        reader = getattr(self.client, "native_grid_charging_reading", None)
        if callable(reader):
            value = reader(config).disallowed
        else:
            info = config.get("site_info") if isinstance(config, dict) else None
            value = info.get("disallow_charge_from_grid_with_solar_installed", False) if isinstance(info, dict) else None
        return value if type(value) is bool else None

    async def prepare(self, baseline, before_write, *, write_intent):
        """Enable an owned optimiser setting before any battery or tariff write."""
        if (self.direction == "discharge"
                and not await self._prepare_export_limit(baseline, before_write, write_intent)):
            return False
        if baseline.get("grid_charge_blocked") is True:
            field = "disallow_charge_from_grid_with_solar_installed"
            checked = None
            fence = await self._owned_before(baseline, before_write)()
            def owned(config):
                nonlocal checked
                checked = deepcopy(config)
                info = config.get("site_info", {}) if isinstance(config, dict) else {}
                return (self.direction == "charge" and self.planning_request
                        and self.config().get("optimization_allow_grid_charge") is True
                        and info.get(field) is True
                        and config.get("default_real_mode") == baseline["mode"]
                        and info.get("backup_reserve_percent") == baseline["reserve"]
                        and self.authority())
            async def record_grid_intent(until):
                nonlocal fence
                fence = await self._owned_before(baseline, before_write)(checked)
                await write_intent("grid_charge", until)
            def submit():
                fence()
                if checked is None or not owned(checked) or not self._forcing_permitted(checked):
                    raise NativeMaxChargeError("Grid charge setup policy changed")
            if not await self._mutate(self.client._transport.write_config(self.client._din,
                    {"site_info." + field: False}, before_write=submit,
                    validate_config=owned,
                    before_submit=record_grid_intent)):
                return False
            current = await self.client._transport.read_config(self.client._din)
            if not isinstance(current, dict) or self._grid_value(current) is not False:
                return False
            await write_intent("local_confirmed")
        self._grid_charge_ready = True
        return await self.guard()

    async def prepare_after_claim(self, baseline, before_write, *, write_intent):
        """Write omitted export permission only after full cap coexistence proof."""
        if baseline.get("export_permission_untracked") is not True:
            return True
        if not await self._write_cloud_export(
                "battery_ok", self._owned_before(baseline, before_write),
                write_intent, require_readback=False):
            return False
        self._export_untracked_ready = True
        return True

    async def kick(self, baseline, before_write, *, deadline, write_intent):
        """Optional one-shot pulse, never a renewable local controller."""
        if not self.startup_kick:
            return
        if baseline.get("export_permission_untracked") is True and not self._export_untracked_ready:
            raise NativeMaxChargeError("Native export preparation was not confirmed")
        if await self.guard() is not True:
            raise NativeMaxChargeError("Startup kick policy changed")
        from .hybrid_policy import startup_watts
        config = dict(self.config())
        for direction in ("charge", "discharge"):
            config[f"gateway_{direction}_limit_w"] = getattr(self.client, f"dispatch_{direction}_limit_w", None)
        watts = startup_watts(self.direction, self._snapshot, config)
        seconds = min(30, math.floor(deadline - self.clock()))
        if watts is None or seconds < 1:
            return
        async def submitted(until):
            await write_intent("kick", (until, seconds, watts))
        fence = await before_write()
        try:
            confirmed = await self._mutate(self.client.dispatch_battery_power(watts, seconds,
                before_write=fence, before_submit=submitted))
        except Exception:
            # An optional pulse failure may still have executed. Reconcile its
            # acceptance window before the session attempts explicit resume.
            await write_intent("wait_local")
            return
        if confirmed is True:
            await write_intent("local_confirmed")

    async def resume(self, fence, *, write_intent):
        fence()
        confirmed = await self._mutate(self.client.resume_battery_operation(before_write=fence,
            before_submit=lambda until: write_intent("local", until)))
        if confirmed is True:
            await write_intent("local_confirmed")
        return confirmed

    async def _write_config(self, mode, reserve, before_write, baseline, write_intent):
        fence = await before_write()
        fence()
        checked_config = None
        def owned(config):
            nonlocal checked_config
            checked_config = deepcopy(config)
            info = config.get("site_info") if isinstance(config, dict) else None
            return (isinstance(info, dict) and config.get("default_real_mode") in
                    ("autonomous", "self_consumption", baseline["mode"])
                    and info.get("backup_reserve_percent") in (reserve, baseline["reserve"])
                    and (self.direction != "discharge"
                         or (baseline.get("export_permission_untracked") is True
                             and self._export_untracked_ready
                             and (self._export_rule(config) == "battery_ok"
                                  or self._can_prepare_untracked_export(config)))
                         or (baseline.get("export_permission_untracked") is not True
                             and self._export_rule(config) in (baseline["export_rule"], "battery_ok")))
                    and self.authority())
        async def record_config_intent(until):
            nonlocal fence
            fence = await before_write(checked_config)
            await write_intent("local", until)
        def checked_fence():
            fence()
            if checked_config is None or not self._forcing_permitted(checked_config):
                raise NativeMaxChargeError("Native Max policy changed before config submission")
        updates = {"default_real_mode": mode, "site_info.backup_reserve_percent": reserve}
        if self.direction == "discharge" and baseline.get("export_rule_source", "local") == "local":
            updates["site_info.customer_preferred_export_rule"] = "battery_ok"
        if not await self._mutate(self.client._transport.write_config(self.client._din,
                updates,
                before_write=checked_fence, validate_config=owned,
                before_submit=record_config_intent)):
            return False
        current = await self.client._transport.read_config(self.client._din)
        if self.direction == "discharge" and baseline.get("export_rule_source") in ("cloud", "untracked"):
            await self._observe_export_rule(current, require_cloud=True)
        confirmed = (isinstance(current, dict) and current.get("default_real_mode") == mode
                and isinstance(current.get("site_info"), dict)
                and current["site_info"].get("backup_reserve_percent") == reserve
                and (self.direction != "discharge" or self._export_rule(current) == "battery_ok"
                     or (baseline.get("export_permission_untracked") is True
                         and self._export_untracked_ready and self._can_prepare_untracked_export(current))))
        if confirmed:
            await write_intent("local_confirmed")
        return confirmed

    async def _upload(self, tariff, target, before_write, write_intent):
        intent_recorded = False
        async def submitted():
            nonlocal intent_recorded
            await before_write()
            await write_intent("tariff", target)
            intent_recorded = True
            # Storage must not extend a policy/identity grant.
            return await before_write()
        async def upload():
            try:
                return await self.upload(tariff, submitted)
            except NativeMaxTariffNotSubmitted:
                if intent_recorded:
                    await write_intent("tariff_not_submitted", target)
                return False
            except NativeMaxTariffRejected:
                if intent_recorded:
                    await write_intent("tariff_rejected", target)
                return False
        # Record definite non-submission/rejection inside the provider task.
        # A revoked caller still drains that task before restoring, so it must
        # not lose this outcome when _mutate re-raises its cancellation.
        # All uncertain outcomes retain their existing journal and ownership.
        confirmed = await self._mutate(upload())
        if confirmed is True:
            await write_intent("tariff_confirmed")
        return confirmed

    async def _prepare_export_limit(self, baseline, before_write, write_intent):
        """Tighten, never raise, the fresh native cap before a forcing tariff."""
        if self.direction != "discharge":
            return True
        original, target = baseline["export_limit"], baseline["force_export_limit"]
        def owned(config):
            info = config.get("site_info", {}) if isinstance(config, dict) else {}
            observed = info.get("min_site_meter_power_ac")
            configured = self.config().get("optimization_max_grid_export_w")
            return (observed in (original, target)
                    and self._forcing_permitted(config, preparing=True)
                    and (configured is None or -target * 1000 <= configured))
        if target != original and not self._export_limit_ready:
            fence = await before_write()
            checked = None
            def validate(config):
                nonlocal checked
                checked = config
                return owned(config)
            def submit():
                fence()
                if checked is None or not owned(checked):
                    raise NativeMaxChargeError("Native export limit policy changed")
            if not await self._mutate(self.client._transport.write_config(self.client._din,
                    {"site_info.min_site_meter_power_ac": target}, before_write=submit,
                    validate_config=validate,
                    before_submit=lambda until: write_intent("local", until))):
                return False
        current = await self.client._transport.read_config(self.client._din)
        if not isinstance(current, dict):
            return False
        # The write readback has no independent provenance. Acquire a complete
        # no-argument snapshot and require exact config equality before the
        # coexisting/forcing proof; get_control_snapshot(raw) deliberately
        # leaves config provenance unknown and cannot authorize this boundary.
        started = self.monotonic()
        fresh = await asyncio.wait_for(self.client.get_snapshot(), timeout=10)
        fresh_config = fresh.raw.get("config") if isinstance(fresh.raw, dict) else None
        if fresh_config != current:
            return False
        self._snapshot = fresh
        self._observed_at = self._sample_started(fresh, started)
        if (not 0 <= self.monotonic() - self._observed_at <= 5
                or not owned(current)
                or not self._forcing_permitted(current, require_export_limit=True)):
            return False
        await write_intent("local_confirmed")
        self._export_limit_ready = True
        return True

    async def restore_limits(self, baseline, before_write, *, write_intent):
        """Keep the tightened cap through physical settling; restore only ours."""
        if baseline.get("direction") != "discharge" or not any(
                field in baseline for field in ("export_limit", "force_export_limit")):
            return True  # Journals from before temporary native limits.
        try:
            original = _signed_export_limit(baseline.get("export_limit"))
            target = _signed_export_limit(baseline.get("force_export_limit"))
        except ValueError:
            return False  # Keep cleanup for incomplete or malformed cap journals.
        if original == target:
            return True
        await write_intent("wait_local")
        field = "min_site_meter_power_ac"
        if self.current_identity() != self.identity or not self.client.local_access_enabled:
            return False
        current = await self.client._transport.read_config(self.client._din)
        expected_export = None
        cloud_export = baseline.get("export_rule_source") == "cloud"
        if cloud_export:
            self._cloud_export_owned = True
            expected_export = await self._observe_export_rule(current, require_cloud=True)
            if expected_export is None:
                return False
        observed = current.get("site_info", {}).get(field) if isinstance(current, dict) else None
        try:
            _signed_export_limit(observed)
        except ValueError:
            return False
        if observed != target:
            return True  # Already restored or edited externally.
        fence = await before_write()
        def checked():
            if self.current_identity() != self.identity or not self.client.local_access_enabled:
                raise NativeMaxChargeError("Native export limit cleanup identity changed")
            fence()
        if not await self._mutate(self.client._transport.write_config(self.client._din,
                {"site_info." + field: original}, before_write=checked,
                validate_config=lambda config: config.get("site_info", {}).get(field) == target
                    and (not cloud_export or self._export_rule(config) == expected_export),
                before_submit=lambda until: write_intent("local", until))):
            return False
        current = await self.client._transport.read_config(self.client._din)
        observed = current.get("site_info", {}).get(field) if isinstance(current, dict) else None
        try:
            _signed_export_limit(observed)
        except ValueError:
            return False
        if observed != original:
            return False
        if cloud_export and await self._observe_export_rule(current, require_cloud=True) is None:
            return False
        await write_intent("local_confirmed")
        return True

    async def apply(self, baseline, before_write, *, write_intent):
        if baseline.get("export_permission_untracked") is True and not self._export_untracked_ready:
            return False
        if not await self._prepare_export_limit(baseline, before_write, write_intent):
            return False
        before_write = self._owned_before(baseline, before_write)
        if self.direction == "discharge" and baseline.get("export_rule_source") == "cloud":
            self._cloud_export_owned = True
            current = await self.client._transport.read_config(self.client._din)
            observed = await self._observe_export_rule(current, require_cloud=True)
            if observed not in (baseline["export_rule"], "battery_ok"):
                return False
            if observed != "battery_ok" and not await self._write_cloud_export(
                    "battery_ok", before_write, write_intent):
                return False
        if not await self._upload(deepcopy(baseline["forcing_tariff"]), "forcing", before_write, write_intent):
            return False
        return await self._write_config("autonomous", baseline.get("force_reserve", 100),
                                        before_write, baseline, write_intent)

    async def restore(self, baseline, before_write, *, write_intent):
        """Release each owned field independently; retain uncertain cleanup.

        A foreign tariff/mode/reserve never authorizes overwriting that field
        and never prevents removing the other settings this session owns.
        Cleanup progress is part of the session's existing durable journal.
        """
        progress = deepcopy(baseline.get("_cleanup", {}))
        pending_tariff = baseline.get("_tariff_intent")
        cloud_export = baseline.get("export_rule_source") == "cloud"
        pending_export = baseline.get("_export_intent")
        self._cloud_export_owned = cloud_export
        self._export_conflict = self._export_conflict or baseline.get("_export_conflict", False)

        async def checkpoint(**changes):
            progress.update(changes)
            await before_write(progress)

        async def cleanup_fence():
            fence = await before_write()
            def checked():
                if self.current_identity() != self.identity or not self.client.local_access_enabled:
                    raise NativeMaxChargeError("Native Max paired cleanup identity changed")
                fence()
            checked()
            return checked

        def value(config, field):
            if field == "site_info.disallow_charge_from_grid_with_solar_installed":
                return self._grid_value(config)
            for key in field.split("."):
                config = config.get(key) if isinstance(config, dict) else None
            return config

        def matches(observed, expected):
            return (observed is expected if type(expected) is bool else observed == expected)

        async def write_field(field, target, expected):
            expected_export = None
            preserved_export = None
            if cloud_export:
                current = await self.client._transport.read_config(self.client._din)
                preserved_export = value(current, "site_info.customer_preferred_export_rule")
                expected_export = await self._observe_export_rule(current, require_cloud=True)
                await write_intent("export_conflict", self._export_conflict)
                if self._export_conflict:
                    return False, None
            fence = await cleanup_fence()
            def owned(config):
                return (matches(value(config, field), expected)
                        and (not cloud_export or (self._export_rule(config) == expected_export
                            if expected_export is not None else
                            value(config, "site_info.customer_preferred_export_rule") == preserved_export)))
            try:
                written = await self._mutate(self.client._transport.write_config(self.client._din,
                    {field: target}, before_write=fence, validate_config=owned,
                    before_submit=lambda until: write_intent("local", until)))
            except BaseException:
                # No subsequent read/checkpoint may outrun a signed full-config
                # envelope which can still be accepted after the HTTP timeout.
                await write_intent("wait_local")
                raise
            observed = await self.client._transport.read_config(self.client._din)
            if written and isinstance(observed, dict) and matches(value(observed, field), target):
                await write_intent("local_confirmed")
            else:
                await write_intent("wait_local")
                observed = await self.client._transport.read_config(self.client._din)
            if not isinstance(observed, dict):
                return False, None
            return written and matches(value(observed, field), target), value(observed, field)

        async def release_field(label, field, original, owned):
            if progress.get(label):
                return
            try:
                current = await self.client._transport.read_config(self.client._din)
            except Exception:
                return
            if not isinstance(current, dict):
                return
            observed = value(current, field)
            if observed is None or (type(owned) is bool and type(observed) is not bool):
                return
            if observed != owned or observed == original:
                # Already restored or changed externally: relinquish this field.
                await checkpoint(**{label: True})
                return
            try:
                success, observed = await write_field(field, original, owned)
            except Exception:
                return
            if success or (observed is not None and observed != owned):
                await checkpoint(**{label: True})

        await cleanup_fence()
        if baseline.get("_grid_charge_intent") is True:
            await release_field("grid_released", "site_info.disallow_charge_from_grid_with_solar_installed",
                                True, False)
        # Remove the owned reserve even when tariff readback is unavailable.
        await release_field("reserve_released", "site_info.backup_reserve_percent",
                            baseline["reserve"], baseline.get("force_reserve", 100))
        if baseline.get("direction", "charge") == "discharge":
            if baseline.get("export_permission_untracked") is True:
                if pending_export:
                    try:
                        current = await self.client._transport.read_config(self.client._din)
                        observed = await self._observe_export_rule(current, require_cloud=True)
                        if observed == pending_export:
                            await write_intent("export_confirmed")
                            pending_export = None
                    except Exception:
                        pass
                if not pending_export:
                    # No original preference was captured. Keep the accepted
                    # export selection and restore only settings we owned.
                    await checkpoint(export_released=True)
            elif cloud_export:
                try:
                    current = await self.client._transport.read_config(self.client._din)
                    observed = await self._observe_export_rule(current, require_cloud=True)
                    await write_intent("export_conflict", self._export_conflict)
                    if pending_export and observed == pending_export:
                        await write_intent("export_confirmed")
                        pending_export = None
                    if not pending_export and not progress.get("export_released"):
                        if observed in EXPORT_RULES and (observed != "battery_ok"
                                or observed == baseline["export_rule"]):
                            await checkpoint(export_released=True)
                        elif observed == "battery_ok" and await self._write_cloud_export(
                                baseline["export_rule"], cleanup_fence, write_intent, restoring=True):
                            await checkpoint(export_released=True)
                except Exception:
                    pass
            elif baseline["export_rule"] == "battery_ok":
                # Native discharge did not change this permission. There is
                # nothing to restore even if the optional readback disappears;
                # preserve any later external edit without writing the field.
                await checkpoint(export_released=True)
            else:
                await release_field("export_released", "site_info.customer_preferred_export_rule",
                                    baseline["export_rule"], "battery_ok")
        try:
            tariff = await self.read_tariff()
        except Exception:
            tariff = None
        if pending_tariff:
            target = baseline["forcing_tariff"] if pending_tariff == "forcing" else baseline["tariff"]
            if self._matches(target, tariff):
                # A unique late forcing tariff, or the baseline after an owned
                # restoration, proves this one submitted attempt arrived.
                await write_intent("tariff_confirmed")
                pending_tariff = None
        if not pending_tariff and isinstance(tariff, dict) and tariff and not self._matches(baseline["forcing_tariff"], tariff):
            await checkpoint(tariff_released=True)

        if not progress.get("mode_released"):
            if baseline["mode"] != "autonomous":
                await release_field("mode_released", "default_real_mode", baseline["mode"], "autonomous")
            elif not progress.get("mode_disarmed") and not progress.get("tariff_released"):
                try:
                    current = await self.client._transport.read_config(self.client._din)
                except Exception:
                    current = None
                mode = value(current, "default_real_mode")
                if mode == "autonomous":
                    # Persist ownership of the temporary disarm before writing
                    # it, so a restart can finish the original mode restoration.
                    await checkpoint(mode_disarmed=True)
                    try:
                        await write_field("default_real_mode", "self_consumption", "autonomous")
                    except Exception:
                        pass
                elif mode is not None:
                    await checkpoint(mode_released=True)
            elif not progress.get("mode_disarmed"):
                await checkpoint(mode_released=True)
            if progress.get("mode_disarmed") and not progress.get("tariff_released"):
                try:
                    await write_field("default_real_mode", "self_consumption", "autonomous")
                except Exception:
                    pass

        if not progress.get("tariff_released") and not pending_tariff:
            async def before_tariff_restore():
                observed = await self.read_tariff()
                if isinstance(observed, dict) and observed and not self._matches(baseline["forcing_tariff"], observed):
                    raise NativeMaxChargeError("Native Max tariff changed before restore")
                return await cleanup_fence()
            try:
                await self._upload(deepcopy(baseline["tariff"]), "baseline", before_tariff_restore, write_intent)
            except Exception:
                pass
            try:
                observed = await self.read_tariff()
                if isinstance(observed, dict) and observed and not self._matches(baseline["forcing_tariff"], observed):
                    await checkpoint(tariff_released=True)
            except Exception:
                pass

        if progress.get("mode_disarmed") and progress.get("tariff_released"):
            await release_field("mode_released", "default_real_mode", baseline["mode"], "self_consumption")
        fields = ["mode_released", "reserve_released", "tariff_released"]
        if baseline.get("_grid_charge_intent") is True:
            fields.append("grid_released")
        if baseline.get("direction", "charge") == "discharge":
            fields.append("export_released")
        if cloud_export:
            try:
                current = await self.client._transport.read_config(self.client._din)
                observed = await self._observe_export_rule(current, require_cloud=True)
                await write_intent("export_conflict", self._export_conflict)
                if observed is None:
                    return False
            except Exception:
                return False
        return not pending_tariff and not pending_export and all(progress.get(field) for field in fields)
