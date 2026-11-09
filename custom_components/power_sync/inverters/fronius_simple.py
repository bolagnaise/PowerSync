"""Durable ownership for the legacy Fronius simple-mode Ena=0 command."""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


async def async_recover_simple_curtailment(hass, entry):
    """Hydrate the existing restore gates, without writing to storage or hardware."""
    from .fronius import FroniusController

    def value(key, default=None):
        return entry.options.get(key, entry.data.get(key, default))

    if (not value("ac_inverter_curtailment_enabled", False)
            or value("inverter_brand") != "fronius"
            or value("fronius_load_following", False)
            or not value("inverter_host")):
        return
    runtime = hass.data.get("power_sync", {}).get(entry.entry_id)
    if runtime is None:
        return
    controller = FroniusController(
        value("inverter_host"), value("inverter_port", 502),
        value("inverter_slave_id", 1), value("inverter_model"),
        hass=hass, entry_id=entry.entry_id,
    )
    try:
        if not await controller.has_pending_simple_restore():
            return
        controller._simple_restore.check_current()
        runtime.update({
            "inverter_controller": controller,
            "inverter_last_state": "curtailed",
            "inverter_control_mode": "curtailed",
            "inverter_power_limit_w": None,
            "inverter_curtailment_device_limit_confirmed": False,
        })
    except Exception as err:
        _LOGGER.warning("Fronius simple-mode startup recovery deferred: %s", err)
    finally:
        if runtime.get("inverter_controller") is not controller:
            await controller.disconnect()


class FroniusSimpleRestore:
    """Remember only a change from verified normal output on an exact device."""

    def __init__(self, hass, entry_id, host, port, slave_id):
        from homeassistant.helpers.storage import Store

        self.hass = hass
        self.entry_id = entry_id
        self.endpoint = [str(host).strip().lower(), port, slave_id]
        self.runtime = getattr(hass, "data", {}).get("power_sync", {}).get(entry_id)
        self.generation = (self.runtime or {}).get("aemo_dispatch_generation")
        self.store = Store(
            hass, 1,
            f"power_sync.fronius_simple_restore.{self.endpoint[0]}.{port}.{slave_id}",
            atomic_writes=True,
        )

    def check_current(self):
        current = self.hass.data.get("power_sync", {}).get(self.entry_id)
        if current is not self.runtime or current is None or (
            current.get("aemo_dispatch_generation") is not self.generation
            or current.get("aemo_dispatch_stopping")
        ):
            raise ValueError("Fronius simple-mode controller belongs to an unloaded entry")

    async def load(self):
        self.check_current()
        saved = await self.store.async_load()
        self.check_current()
        if not saved:
            return None
        if not isinstance(saved, dict) or (
            saved.get("owner_entry_id") != self.entry_id
            or saved.get("endpoint") != self.endpoint
            or saved.get("mode") != "simple"
            or not isinstance(saved.get("serial"), list)
            or len(saved["serial"]) != 16
        ):
            raise ValueError("Fronius simple-mode restore ownership does not match")
        return saved

    async def read(self, controller):
        if not await controller.connect():
            raise ValueError("Fronius simple-mode recovery connection unavailable")
        # SunSpec Model 1: serial is 16 words at offset 52 from the SunS marker.
        serial = await controller._read_register(controller.REG_SUNSPEC_ID + 52, 16)
        enabled = await controller._read_register(controller.REG_WMAXLIM_ENA, 1)
        limit = await controller._read_register(controller.REG_WMAXLIMPCT, 1)
        self.check_current()
        if (not serial or len(serial) != 16
                or not all(isinstance(v, int) and 0 <= v < 65535 for v in serial)
                or not any(v not in (0, 0x2020) for v in serial)
                or not enabled or not limit):
            raise ValueError("Fronius simple-mode recovery readback unavailable")
        return serial, enabled[0], limit[0]

    async def validate(self, controller, saved):
        serial, enabled, limit = await self.read(controller)
        if serial != saved["serial"] or enabled not in (0, 1) or limit != 10000:
            raise ValueError("Fronius simple-mode device or controls changed; restore deferred")
        return enabled

    async def prepare(self, controller):
        saved = await self.load()
        if saved:
            await self.validate(controller, saved)
            return
        serial, enabled, limit = await self.read(controller)
        if enabled != 1 or limit != 10000:
            # A pre-existing disabled setting or user cap is not our provenance.
            raise ValueError("Fronius simple-mode normal baseline is unverified")
        saved = {
            "owner_entry_id": self.entry_id, "endpoint": self.endpoint,
            "mode": "simple", "serial": serial,
        }
        await self.store.async_save(saved)
        self.check_current()
        # A slow disk write must not turn pre-save readback into write authority.
        await self.validate(controller, saved)

    async def clear(self):
        try:
            self.check_current()
            await self.store.async_save({})
            return True
        except Exception as err:
            _LOGGER.warning("Fronius simple-mode restore journal retained: %s", err)
            return False
