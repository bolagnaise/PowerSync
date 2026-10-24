"""Provider boundary for deliberate native Max, with one restore baseline.

This actuator leaves grid-charge permission, export rules and hardware limits
unchanged. A software battery-input ceiling is not promised by native Max.
Every local write and tariff retry must use its immediately acquired fence.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import time

from .battery_dispatch import site_grid_limits_w, validate_dispatch_config
from .full_rate_charge import native_max_charge_eligibility
from .native_max_charge import NativeMaxChargeError


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
                 settle_interval=5):
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
                if not self._forcing_permitted():
                    raise NativeMaxChargeError("Native Max forcing policy changed")
            checked()
            return checked
        return before_owned_write

    def authority(self):
        try:
            validate_dispatch_config("charge", dict(self.config()))
            return (self.identity == self.current_identity() and self.policy() is True
                    and bool(self.client.local_access_enabled))
        except (ValueError, TypeError):
            return False

    def _forcing_permitted(self, native_config=None):
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
            raw = snapshot.raw.get("config") if native_config is None else native_config
            info = raw.get("site_info") if isinstance(raw, dict) else None
            imported, _ = site_grid_limits_w(type("NativeBounds", (), {"raw": {"config": raw}})())
            permission = info.get("disallow_charge_from_grid_with_solar_installed", False) if isinstance(info, dict) else None
            return native_max_charge_eligibility(
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
                control_permitted=self.authority()).eligible
        except (ValueError, TypeError, KeyError):
            return False

    async def guard(self):
        if not self.authority():
            return False
        await self.budget.load()
        observed_at = self.monotonic()
        try:
            self._snapshot = await asyncio.wait_for(self.client.get_snapshot(), timeout=10)
            self._observed_at = observed_at
            return self._forcing_permitted()
        except (ValueError, TypeError, KeyError, asyncio.TimeoutError):
            return False

    async def capture(self):
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
        return {"mode": mode, "reserve": reserve, "tariff": deepcopy(tariff),
                "forcing_tariff": deepcopy(self.forcing_tariff),
                "settle_charge_w": max(0, -snapshot.battery_w)}

    @staticmethod
    def _physical_sample(snapshot):
        values = [getattr(snapshot, key, None) for key in ("battery_w", "grid_w", "load_w", "solar_w")]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            return False
        battery, grid, load, solar = values
        return (snapshot.grid_status == "SystemGridConnected" and load >= 0 and solar >= 0
                and abs(grid + battery + solar - load) <= max(250, .05 * max(abs(grid), abs(battery), load)))

    async def settled(self, baseline):
        """Require two fresh, balanced physical samples before freeing EV capacity.

        Native TOU charging can persist after settings readback. Recovery starts
        these observations again; a previous process's sample is not reused.
        """
        original = baseline.get("settle_charge_w", 0)
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
                    or -snapshot.battery_w > original + 250):
                return False
        return True

    async def claim(self, token, deadline):
        self.token = token
        return await self.budget.claim_native_max(self.entry_id, self.site, token, deadline)

    async def release(self, token):
        result = await self.budget.release_native_max(self.entry_id, self.site, token)
        if result and self.token == token:
            self.token = None
        return result

    async def resume(self, fence):
        fence()
        return await self._mutate(self.client.resume_battery_operation(before_write=fence))

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
                    and info.get("backup_reserve_percent") in (100, baseline["reserve"])
                    and self._forcing_permitted(config))
        def checked_fence():
            fence()
            if checked_config is None or not self._forcing_permitted(checked_config):
                raise NativeMaxChargeError("Native Max policy changed before config submission")
        if not await self._mutate(self.client._transport.write_config(self.client._din,
                {"default_real_mode": mode, "site_info.backup_reserve_percent": reserve},
                before_write=checked_fence, validate_config=owned,
                before_submit=lambda until: write_intent("local", until))):
            return False
        current = await self.client._transport.read_config(self.client._din)
        confirmed = (isinstance(current, dict) and current.get("default_real_mode") == mode
                and isinstance(current.get("site_info"), dict)
                and current["site_info"].get("backup_reserve_percent") == reserve)
        if confirmed:
            await write_intent("local_confirmed")
        return confirmed

    async def _upload(self, tariff, target, before_write, write_intent):
        async def submitted():
            await before_write()
            await write_intent("tariff", target)
            # Storage must not extend a policy/identity grant.
            return await before_write()
        confirmed = await self._mutate(self.upload(tariff, submitted))
        if confirmed is True:
            await write_intent("tariff_confirmed")
        return confirmed

    async def apply(self, baseline, before_write, *, write_intent):
        before_write = self._owned_before(baseline, before_write)
        if not await self._upload(deepcopy(baseline["forcing_tariff"]), "forcing", before_write, write_intent):
            return False
        return await self._write_config("autonomous", 100, before_write, baseline, write_intent)

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
                            baseline["reserve"], 100)
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
        return not pending_tariff and all(progress.get(field) for field in
                   ("mode_released", "reserve_released", "tariff_released"))
