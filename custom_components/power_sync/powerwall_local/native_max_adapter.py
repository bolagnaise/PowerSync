"""Provider boundary for deliberate native Max, with one restore baseline.

This actuator preserves grid-charge permission and never raises site limits.
A stricter software export cap is applied natively before tariff control.
A software battery-input ceiling is not promised by native Max.
Every local write and tariff retry must use its immediately acquired fence.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import time

from .battery_dispatch import battery_watts, site_grid_limits_w, validate_dispatch_config
from .full_rate_charge import native_max_charge_eligibility
from .native_max_charge import NativeMaxChargeError, NativeMaxTariffRejected


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
    ``restorable`` rejects temporary controller tariffs. ``policy`` and
    ``config`` are current synchronous predicates/readers, not snapshots.
    """

    def __init__(self, *, client, identity, current_identity, policy, config,
                 read_tariff, upload, restorable, forcing_tariff,
                 budget, entry_id, site, tariff_matches=None, monotonic=time.monotonic,
                 settle_interval=5, direction="charge", clock=time.time):
        self.client, self.identity = client, identity
        self.current_identity, self.policy, self.config = current_identity, policy, config
        self.read_tariff, self.upload, self.restorable = read_tariff, upload, restorable
        self.forcing_tariff = deepcopy(forcing_tariff)
        self.budget, self.entry_id, self.site = budget, entry_id, site
        self.monotonic = monotonic
        self.tariff_matches = tariff_matches or (lambda expected, observed: expected == observed)
        self.token = None
        self._snapshot = None
        self._observed_at = None
        self.settle_interval = settle_interval
        self.direction = direction
        self.startup_kick = False
        self.planning_request = False
        self.clock = clock
        self._export_limit_ready = False
        self.guard_denial_reason = None

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
        async def before_owned_write():
            observed = await self.read_tariff()
            if not any(self._matches(candidate, observed) for candidate in
                    (baseline["tariff"], baseline["forcing_tariff"])):
                raise NativeMaxChargeError("Native Max tariff ownership changed; cleanup retained")
            fence = await before_write()
            def checked():
                if self.current_identity() != self.identity:
                    raise NativeMaxChargeError("Native Max paired identity changed")
                fence()
                if not self._forcing_permitted(require_export_limit=True):
                    raise NativeMaxChargeError("Native Max forcing policy changed")
            checked()
            return checked
        return before_owned_write

    def authority(self):
        try:
            validate_dispatch_config(self.direction, dict(self.config()))
            return (self.identity == self.current_identity() and self.policy() is True
                    and bool(self.client.local_access_enabled))
        except (ValueError, TypeError):
            return False

    def _forcing_permitted(self, native_config=None, *, require_export_limit=False):
        snapshot = self._snapshot
        if snapshot is None or self._observed_at is None or not self.authority():
            return False
        maximum = self.budget.native_max_claim(self.site)
        if maximum and maximum.get("token") != self.token:
            return False
        if any(r.get("site") == self.site for r in self.budget.managed.values()):
            return False
        try:
            config = dict(self.config())
            if self.planning_request:
                capacity = config.get("powerwall_native_capacity_w")
                if type(capacity) not in (int, float) or not math.isfinite(capacity) or capacity <= 0:
                    return False
            raw = snapshot.raw.get("config") if native_config is None else native_config
            info = raw.get("site_info") if isinstance(raw, dict) else None
            imported, _ = site_grid_limits_w(type("NativeBounds", (), {"raw": {"config": raw}})())
            permission = info.get("disallow_charge_from_grid_with_solar_installed", False) if isinstance(info, dict) else None
            if self.direction == "discharge":
                if (not isinstance(info, dict) or "min_site_meter_power_ac" not in info
                        or not 0 <= self.monotonic() - self._observed_at <= 5
                        or not self.authority()):
                    return False
                _signed_export_limit(info["min_site_meter_power_ac"])
                _, exported = site_grid_limits_w(type("NativeBounds", (), {"raw": {"config": raw}})())
                configured = config.get("optimization_max_grid_export_w")
                if configured is not None and (type(configured) not in (int, float)
                        or not math.isfinite(configured) or configured <= 0
                        # A known unset sentinel can be tightened at acquisition.
                        # Forcing still requires a verified finite software cap.
                        or ((self._export_limit_ready or require_export_limit)
                            and (exported is None or exported > configured))):
                    return False
                if info.get("customer_preferred_export_rule") not in ("battery_ok", "pv_only", "never"):
                    return False
                battery_watts("discharge", 1, snapshot, config)
                return True
            eligibility = native_max_charge_eligibility(
                source="native_manual_max", requested_power_w=0, grid_import_target_w=None,
                planning_charge_max_w=config.get("optimization_max_charge_w", 5000),
                software_import_max_w=config.get("optimization_max_grid_import_w"),
                native_import_max_w=imported, native_import_known=isinstance(info, dict),
                pending_ev_w=self.budget.unobserved_w(self.site),
                native_ev_active=self.budget.site_armed(self.site),
                grid_charging_allowed=permission is False,
                grid_connected=snapshot.grid_status == "SystemGridConnected",
                soc=snapshot.soc, soc_cap=config.get("optimization_grid_charge_soc_cap", 1)*100,
                evidence_fresh=0 <= self.monotonic()-self._observed_at <= 5,
                control_permitted=self.authority())
            if not eligibility.eligible:
                self.guard_denial_reason = eligibility.reason
            return eligibility.eligible
        except (ValueError, TypeError, KeyError):
            return False

    async def guard(self):
        # Keep the failed admission check for the caller's error report. It
        # must never become permission or survive a later successful check.
        self.guard_denial_reason = "native_max_control_not_permitted"
        if not self.authority():
            return False
        await self.budget.load()
        observed_at = self.monotonic()
        try:
            self._snapshot = await asyncio.wait_for(self.client.get_snapshot(), timeout=10)
            self._observed_at = observed_at
            self.guard_denial_reason = "native_max_policy_denied"
            permitted = self._forcing_permitted()
            if permitted:
                self.guard_denial_reason = None
            return permitted
        except (ValueError, TypeError, KeyError, asyncio.TimeoutError):
            self.guard_denial_reason = "native_limit_evidence_unavailable"
            return False

    async def capture(self):
        self._export_limit_ready = False
        if not self.authority():
            raise NativeMaxChargeError("Native Max ownership is unavailable")
        started = self.monotonic()
        snapshot, tariff = await asyncio.gather(self.client.get_snapshot(), self.read_tariff())
        config = snapshot.raw.get("config")
        info = config.get("site_info") if isinstance(config, dict) else None
        mode = config.get("default_real_mode") if isinstance(config, dict) else None
        reserve = info.get("backup_reserve_percent") if isinstance(info, dict) else None
        if (mode not in ("self_consumption", "autonomous", "backup")
                or type(reserve) not in (int, float) or not math.isfinite(reserve)
                or not 0 <= reserve <= 100 or not self.restorable(tariff)
                or not self._physical_sample(snapshot) or self.monotonic() - started > 5):
            raise NativeMaxChargeError("Exact native Max restoration baseline is unavailable")
        baseline = {"mode": mode, "reserve": reserve, "tariff": deepcopy(tariff),
                    "forcing_tariff": deepcopy(self.forcing_tariff),
                    "settle_charge_w": max(0, -snapshot.battery_w), "direction": self.direction}
        if self.direction == "discharge":
            export = info.get("customer_preferred_export_rule")
            if export not in ("battery_ok", "pv_only", "never"):
                raise NativeMaxChargeError("Native discharge export baseline is unavailable")
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
                export_rule=export, settle_discharge_w=max(0, snapshot.battery_w),
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
            if (self.monotonic() - started > 5 or not self._physical_sample(snapshot)
                    or (snapshot.battery_w > max(original, snapshot.load_w - snapshot.solar_w) + 250
                        if discharge else -snapshot.battery_w > max(original, snapshot.solar_w - snapshot.load_w) + 250)):
                return False
        return True

    async def claim(self, token, deadline):
        self.token = token
        return await self.budget.claim_native_max(self.entry_id, self.site, token, deadline)

    async def release(self, token):
        result = await self.budget.release_native_max(self.entry_id, self.site, token)
        if result:
            self._export_limit_ready = False
            if self.token == token:
                self.token = None
        return result

    async def kick(self, baseline, before_write, *, deadline, write_intent):
        """Optional one-shot pulse, never a renewable local controller."""
        if not self.startup_kick:
            return
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
            checked_config = config
            info = config.get("site_info") if isinstance(config, dict) else None
            return (isinstance(info, dict) and config.get("default_real_mode") in
                    ("autonomous", "self_consumption", baseline["mode"])
                    and info.get("backup_reserve_percent") in (reserve, baseline["reserve"])
                    and (self.direction != "discharge" or info.get("customer_preferred_export_rule")
                         in (baseline["export_rule"], "battery_ok"))
                    and self._forcing_permitted(config))
        def checked_fence():
            fence()
            if checked_config is None or not self._forcing_permitted(checked_config):
                raise NativeMaxChargeError("Native Max policy changed before config submission")
        updates = {"default_real_mode": mode, "site_info.backup_reserve_percent": reserve}
        if self.direction == "discharge":
            updates["site_info.customer_preferred_export_rule"] = "battery_ok"
        if not await self._mutate(self.client._transport.write_config(self.client._din,
                updates,
                before_write=checked_fence, validate_config=owned,
                before_submit=lambda until: write_intent("local", until))):
            return False
        current = await self.client._transport.read_config(self.client._din)
        confirmed = (isinstance(current, dict) and current.get("default_real_mode") == mode
                and isinstance(current.get("site_info"), dict)
                and current["site_info"].get("backup_reserve_percent") == reserve
                and (self.direction != "discharge" or current["site_info"].get("customer_preferred_export_rule") == "battery_ok"))
        if confirmed:
            await write_intent("local_confirmed")
        return confirmed

    async def _upload(self, tariff, target, before_write, write_intent):
        async def submitted():
            await before_write()
            await write_intent("tariff", target)
            # Storage must not extend a policy/identity grant.
            return await before_write()
        try:
            confirmed = await self._mutate(self.upload(tariff, submitted))
        except NativeMaxTariffRejected:
            # A definite rejection of this single POST cannot arrive later.
            # Leave unknown/timeout outcomes quarantined, and retain all other
            # cleanup ownership until its normal readback and settling checks.
            await write_intent("tariff_rejected", target)
            return False
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
            return (observed in (original, target) and self._forcing_permitted(config)
                    and (configured is None or -target * 1000 <= configured))
        if target != original:
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
        if (self._observed_at is None
                or not 0 <= self.monotonic() - self._observed_at <= 5):
            # The acknowledged write and its readback can outlive the earlier
            # telemetry grant. Acquire fresh evidence rather than rejecting a
            # correctly applied cap solely because that round trip was slow.
            # Use the new config too: a later external edit must not inherit
            # the earlier readback's permission. No cap write is replayed.
            if await self.guard() is not True:
                return False
            current = self._snapshot.raw.get("config")
        if not owned(current) or not self._forcing_permitted(current, require_export_limit=True):
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
                validate_config=lambda config: config.get("site_info", {}).get(field) == target,
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
        await write_intent("local_confirmed")
        return True

    async def apply(self, baseline, before_write, *, write_intent):
        if not await self._prepare_export_limit(baseline, before_write, write_intent):
            return False
        before_write = self._owned_before(baseline, before_write)
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
            for key in field.split("."):
                config = config.get(key) if isinstance(config, dict) else None
            return config

        async def write_field(field, target, expected):
            fence = await cleanup_fence()
            def owned(config):
                return value(config, field) == expected
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
            if written and isinstance(observed, dict) and value(observed, field) == target:
                await write_intent("local_confirmed")
            else:
                await write_intent("wait_local")
                observed = await self.client._transport.read_config(self.client._din)
            if not isinstance(observed, dict):
                return False, None
            return written and value(observed, field) == target, value(observed, field)

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
            if observed is None:
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
        # Remove the owned reserve even when tariff readback is unavailable.
        await release_field("reserve_released", "site_info.backup_reserve_percent",
                            baseline["reserve"], baseline.get("force_reserve", 100))
        if baseline.get("direction", "charge") == "discharge":
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
        if baseline.get("direction", "charge") == "discharge":
            fields.append("export_released")
        return not pending_tariff and all(progress.get(field) for field in fields)
