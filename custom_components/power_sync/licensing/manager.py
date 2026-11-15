"""Home Assistant adapter for licensing, local persistence and owned recovery."""
import asyncio
import inspect
import json
from pathlib import Path
import logging
import time

from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .runtime import LicenseRuntime, DATA_KEY, ADMISSION_KEY
from .keys import PUBLIC_KEYS
from .guard import optimizer_command, recovery_scope, OptimizerAccessLost

_LOGGER = logging.getLogger(__name__)
API = "https://api.powersync.cc/ha-license/v1"


class LicensedBatteryController:
    """Fence every optimiser mutation, including vendor-specific battery implementations."""
    def __init__(self, manager, entry_id, controller):
        self.hass = manager.hass
        self.entry_id = entry_id
        self._controller = controller

    def __getattr__(self, name):
        target = getattr(self._controller, name)
        if inspect.iscoroutinefunction(target) and (name.startswith("set_") or name in ("force_charge", "force_discharge")):
            async def command(owner, *args, **kwargs):
                return await target(*args, **kwargs)
            command.__name__ = name
            return optimizer_command(command).__get__(self)
        return target


class HALicenseManager(LicenseRuntime):
    def __init__(self, hass):
        self.hass = hass
        self._store = Store(hass, 1, "power_sync.ha_licensing", private=True, atomic_writes=True)
        self._save_lock = asyncio.Lock()
        self.coordinators = {}
        self._task = None
        self._stop_unsub = None
        self._ready = asyncio.Event()
        super().__init__(self._store.async_load, self._verified_save, self._transport, PUBLIC_KEYS)

    async def _verified_save(self, data):
        async with self._save_lock:
            await self._store.async_save(data)
            # Supported HA versions log some WriteErrors rather than raising. Read the actual file,
            # not Store's memory cache, before allowing the first hardware write.
            def read_data():
                return json.loads(Path(self._store.path).read_text())["data"]
            written = await self.hass.async_add_executor_job(read_data)
            if written != data:
                raise OSError("licensing_persistence_not_confirmed")

    async def _transport(self, path, body):
        session = async_get_clientsession(self.hass)
        async with session.request("GET" if body is None else "POST", API + path,
                json=body, timeout=ClientTimeout(total=20), allow_redirects=False) as response:
            if response.status != 200:
                # Do not interpret an HTTP error as an authoritative signed denial.
                raise ValueError("licensing_service_unavailable")
            if response.content_length and response.content_length > 16384:
                raise ValueError("invalid_licensing_response")
            chunks = []
            size = 0
            while chunk := await response.content.read(8192):
                size += len(chunk)
                if size > 16384:
                    raise ValueError("invalid_licensing_response")
                chunks.append(chunk)
            raw = b"".join(chunks)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("invalid_licensing_response")
            return value

    async def async_referrals(self, *, force=False):
        """Fetch display-only account information without changing licensing state."""
        if not self.paired:
            self._referral_cache = None
            return {"paired": False, "unavailable": False}
        identity = (self.installation_id, self.credential)
        cached = getattr(self, "_referral_cache", None)
        now = int(time.time())
        if cached and cached[0] == identity and not force and now - cached[1]["fetched_at"] < 300:
            return dict(cached[1])
        try:
            raw = await self._transport("/referrals", {
                "installation_id": self.installation_id, "credential": self.credential,
            })
            # Never render arbitrary remote URLs or markup in the local HA UI.
            import re
            code = raw.get("code")
            if not isinstance(code, str) or not re.fullmatch(r"PS[A-HJ-NP-Z2-9]{10}", code):
                raise ValueError("invalid_referral_summary")
            value = {
                "paired": True, "unavailable": False, "stale": False, "enabled": raw.get("enabled") is True,
                "code": code, "share_url": "https://powersync.cc/app/refer/" + code,
                "account_url": "https://powersync.cc/app/referrals", "fetched_at": now,
            }
            for key in ("earned_this_year", "annual_limit", "year", "banked_rewards"):
                count = raw.get(key)
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError("invalid_referral_summary")
                value[key] = count
            self._referral_cache = (identity, value)
            return dict(value)
        except Exception:
            if cached and cached[0] == identity:
                return {**cached[1], "stale": True, "unavailable": True}
            return {"paired": True, "unavailable": True}

    def attach(self, coordinator):
        entry_id = coordinator.entry_id
        self.coordinators[entry_id] = coordinator
        state = self.entries.setdefault(entry_id, {})
        state["generation"] = state.get("generation", 0) + 1
        state["unloaded"] = False
        coordinator._licensing_entry_generation = state["generation"]
        if state.get("pre_idle_reserve") is not None:
            configured = coordinator._configured_startup_backup_reserve()[0]
            if "reserve_config_at_journal" in state and configured == state["reserve_config_at_journal"]:
                coordinator._pre_idle_backup_reserve = state["pre_idle_reserve"]
            else:
                # A newer persisted manual reserve takes precedence over an older journal.
                state.pop("pre_idle_reserve", None)
        executor = getattr(coordinator, "_executor", None)
        if executor and executor.battery_controller and not isinstance(executor.battery_controller, LicensedBatteryController):
            from ..optimization.battery_controller import BatteryControllerWrapper
            if isinstance(executor.battery_controller, BatteryControllerWrapper):
                executor.battery_controller.entry_id = entry_id
            executor.battery_controller = LicensedBatteryController(self, entry_id, executor.battery_controller)
            executor.battery_controller._licensing_entry_generation = state["generation"]
        ev = getattr(coordinator, "_ev_coordinator", None)
        if ev:
            ev.entry_id = entry_id
            ev._licensing_entry_generation = state["generation"]
        if not self._task:
            self._task = self.hass.async_create_background_task(self._loop(), "powersync_ha_licensing")
            self._stop_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stop)

    def detach(self, entry_id):
        self.coordinators.pop(entry_id, None)
        state = self.entries.setdefault(entry_id, {})
        state["generation"] = state.get("generation", 0) + 1
        state["unloaded"] = True

    def entry_loaded(self, entry_id):
        """Reopen a loaded entry without readmitting work from its retired optimizer."""
        self.entries.setdefault(entry_id, {})["unloaded"] = False

    def _stop(self, _event):
        self.admission.suspend()  # Fence queued commands before HA shuts down.
        if self._task:
            self._task.cancel()

    async def journal(self, entry_id):
        coordinator = self.coordinators.get(entry_id)
        if coordinator:
            reserve = getattr(coordinator, "_pre_idle_backup_reserve", None)
            if type(reserve) is int and 0 <= reserve <= 100:
                state = self.entries.setdefault(entry_id, {})
                state["pre_idle_reserve"] = reserve
                state["reserve_config_at_journal"] = coordinator._configured_startup_backup_reserve()[0]
        await super().journal(entry_id)

    async def relinquish(self, entry_id):
        await LicenseRuntime.relinquish(self, entry_id)
        coordinator = self.coordinators.get(entry_id)
        if coordinator:
            coordinator._pre_idle_backup_reserve = None
            coordinator._idle_hold_reserve = None
            coordinator._last_optimizer_self_consumption_reserve_target = None
            coordinator._last_optimizer_result = None
            coordinator._current_schedule = None

    def finish_manual_command(self, entry_id):
        coordinator = self.coordinators.get(entry_id)
        if not coordinator:
            return
        generation = self.entries.get(entry_id, {}).get("generation", 0)
        coordinator._licensing_entry_generation = generation
        executor = getattr(coordinator, "_executor", None)
        if executor and isinstance(executor.battery_controller, LicensedBatteryController):
            executor.battery_controller._licensing_entry_generation = generation
        ev = getattr(coordinator, "_ev_coordinator", None)
        if ev:
            ev._licensing_entry_generation = generation

    async def journal_ev(self, entry_id, config):
        async with self._journal_lock:
            state = self.entries.setdefault(entry_id, {})
            targets = state.setdefault("ev_targets", [])
            if config.entity_id not in targets:
                targets.append(config.entity_id)
                state["recovery_pending"] = True
                await self.persist()

    async def _loop(self):
        while True:
            try:
                try:
                    await self.maintain()
                except Exception:
                    _LOGGER.warning("Licence renewal or persistence failed; owned recovery will still run")
                await self.reconcile_entries()
            except OptimizerAccessLost:
                _LOGGER.warning("Licence recovery attempted a new force command; access remains suspended")
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning("HA licensing maintenance unavailable; fixed permission deadlines remain in force")
            await asyncio.sleep(30)

    async def reconcile_entries(self):
        for entry_id, coordinator in list(self.coordinators.items()):
            state = self.entries.setdefault(entry_id, {})
            enabled = bool(getattr(coordinator, "_enabled", False))
            entry = getattr(coordinator, "_entry", None)
            configured = not entry or entry.options.get("optimization_enabled", entry.data.get("optimization_enabled", True)) is not False
            permitted = self.permits(entry_id)
            if not permitted and (enabled or state.get("recovery_pending")):
                # Generation admission already closed synchronously. Persist cleanup before any await on hardware.
                state["resume_required"] = True
                state["recovery_pending"] = True
                reserve = getattr(coordinator, "_pre_idle_backup_reserve", None)
                if type(reserve) is int and 0 <= reserve <= 100:
                    state["pre_idle_reserve"] = reserve
                    state["reserve_config_at_journal"] = coordinator._configured_startup_backup_reserve()[0]
                try:
                    await self.persist()
                except Exception:
                    _LOGGER.warning("Licence storage is unavailable; admission is closed and owned recovery will still run")
                try:
                    async with self.control_lock(entry_id):
                        with recovery_scope():
                            await coordinator.disable(licensing=True)
                    force = coordinator._get_active_force_state()
                    hold = getattr(coordinator, "_solar_export_hold", None)
                    pending = bool(force and force.get("active") and force.get("source") == "optimizer")
                    pending |= bool(getattr(coordinator, "_idle_no_discharge_active", False) or getattr(coordinator, "_scheduled_ev_no_discharge_active", False) or (hold and hold.active))
                    pending |= getattr(coordinator, "_pre_idle_backup_reserve", None) is not None
                    pending |= bool(state.get("ev_targets"))
                    manual_owned = bool(force and force.get("active") and force.get("source") != "optimizer")
                    if (manual_owned and not state.get("ev_targets")) or (not pending and not coordinator._monitoring_mode_active()):
                        state["recovery_pending"] = False
                        state.pop("pre_idle_reserve", None)
                    await self.persist()
                except Exception:
                    _LOGGER.warning("HA optimiser access suspended for %s; owned restoration remains pending", entry_id)
            elif permitted and configured and not enabled and not state.get("resume_required"):
                # This is startup after policy verification, never resumption after an access gap.
                await coordinator.enable()

    async def resume_entry(self, entry_id):
        coordinator = self.coordinators.get(entry_id)
        if not coordinator:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if not entry or entry.domain != "power_sync":
                raise ValueError("optimizer_entry_not_found")
            await self.resume(entry_id)
            return  # A disabled/unconfigured optimiser can now be enabled through its normal options.
        force = coordinator._get_active_force_state()
        if force and force.get("active") and force.get("source") != "optimizer":
            raise ValueError("optimizer_external_control_active")
        await self.resume(entry_id)
        coordinator._last_optimizer_result = None
        coordinator._current_schedule = None
        if not await coordinator.enable():
            self.entries.setdefault(entry_id, {})["resume_required"] = True
            await self.persist()
            raise ValueError("optimizer_resume_failed")


async def async_get_manager(hass):
    manager = hass.data.get(DATA_KEY)
    if manager is None:
        manager = HALicenseManager(hass)
        hass.data[DATA_KEY] = manager
        hass.data[ADMISSION_KEY] = manager.admission
        try:
            await manager.initialize()
        finally:
            manager._ready.set()
    else:
        await manager._ready.wait()
    return manager
