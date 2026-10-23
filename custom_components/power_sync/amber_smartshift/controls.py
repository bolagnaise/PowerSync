"""Entry-scoped service handlers and the optimiser's mode-only adapter."""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from ..licensing.guard import optimizer_command, optimizer_service_data, service_allowed


def handlers(runtime):
    """Use the existing routed PowerSync service namespace and licensing fences."""
    def mode_handler(mode):
        async def handle(call):
            source = str(call.data.get("source") or ("user" if getattr(call.context, "user_id", None) else "automation"))
            duration = call.data.get("duration", 30)
            if mode == "discharge" and source != "optimizer":
                from ..force_discharge_duration import selected_discharge_duration
                try:
                    duration = selected_discharge_duration(duration, runtime.entry.options)
                except ValueError as err:
                    raise HomeAssistantError(str(err)) from err
                if duration > 240:
                    raise HomeAssistantError("Amber overrides support at most 240 minutes")
            if not service_allowed(runtime.hass, call.data, "handle_force_" + mode):
                raise HomeAssistantError("HA optimiser access is required")
            manager = runtime.hass.data.get("power_sync_license_manager")
            if manager and source != "optimizer":
                await manager.relinquish(runtime.entry_id)
            result = await runtime.command(mode, duration, source=source,
                                           power_w=call.data.get("power_w", 0), call_data=call.data)
            return {"success": result, "state": runtime.command_state}
        return handle

    async def restore(call):
        source = call.data.get("source") or ("user" if getattr(call.context, "user_id", None) else "automation")
        result = await runtime.restore_normal(automatic=source not in ("manual", "user"))
        if not result:
            raise HomeAssistantError("A manual Amber override owns the battery")
        return {"success": result, "state": runtime.command_state}

    async def unsupported(call):
        raise HomeAssistantError("Amber does not support persistent reserve, off-grid or inverter settings")

    return {
        "force_charge": mode_handler("charge"), "force_discharge": mode_handler("discharge"),
        "hold_battery_soc": mode_handler("preserve-charge"), "preserve_battery": mode_handler("preserve-charge"),
        "set_self_consumption": mode_handler("self-consume"), "restore_normal": restore,
        "set_autonomous": restore, "set_backup_reserve": unsupported,
    }


class AmberOptimizerController:
    """Preserve the optimiser's admission scope without fabricating watt control."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.hass = runtime.hass
        self.entry_id = runtime.entry_id

    async def _mode(self, mode, duration):
        return await self.runtime.command(mode, duration, source="optimizer", call_data=optimizer_service_data(self, {"source": "optimizer"}))

    @optimizer_command
    async def force_charge(self, duration_minutes=10, power_w=0, **kwargs):
        return await self._mode("charge", min(duration_minutes, 10))

    @optimizer_command
    async def force_discharge(self, duration_minutes=10, power_w=0, **kwargs):
        return await self._mode("discharge", min(duration_minutes, 10))

    @optimizer_command
    async def set_self_consumption_mode(self):
        return await self._mode("self-consume", 10)

    @optimizer_command
    async def preserve(self):
        return await self._mode("preserve-charge", 10)

    async def restore_normal(self, **kwargs):
        return await self.runtime.restore_normal(automatic=True)
