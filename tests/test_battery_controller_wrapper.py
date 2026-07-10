from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _load_controller_module():
    saved = {
        name: sys.modules.get(name)
        for name in (
            "homeassistant",
            "homeassistant.core",
            "power_sync",
            "power_sync.const",
            "power_sync.optimization",
            "power_sync.optimization.battery_controller",
        )
    }

    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = type("HomeAssistant", (), {})

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    opt_module = types.ModuleType("power_sync.optimization")
    opt_module.__path__ = [str(COMPONENT_ROOT / "optimization")]
    const_module = types.ModuleType("power_sync.const")
    const_module.DOMAIN = "power_sync"
    const_module.TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS = 30

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["power_sync"] = ps_module
    sys.modules["power_sync.optimization"] = opt_module
    sys.modules["power_sync.const"] = const_module
    sys.modules.pop("power_sync.optimization.battery_controller", None)

    module = importlib.import_module("power_sync.optimization.battery_controller")

    def restore() -> None:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return module, restore


class _States:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, entity_id: str):
        value = self._values.get(entity_id)
        if value is None:
            return None
        return SimpleNamespace(state=value)


class _Services:
    def __init__(self) -> None:
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data, blocking))


def test_tesla_mode_reads_current_ha_entity_but_backup_reserve_prefers_cache():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({
                "select.power_sync_tesla_operation_mode": "autonomous",
                "number.power_sync_tesla_backup_reserve": "0.0",
            }),
            data={
                "power_sync": {
                    "entry-1": {
                        "coordinator": SimpleNamespace(
                            _site_info_cache={
                                "default_real_mode": "self_consumption",
                                "backup_reserve_percent": 20,
                            }
                        )
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        assert asyncio.run(controller.get_tesla_operation_mode()) == "autonomous"
        assert asyncio.run(controller.get_backup_reserve()) == 20
    finally:
        restore()


def test_tesla_backup_reserve_uses_cloud_site_info_cache_as_user_facing():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "entry": SimpleNamespace(data={"powerwall_local_paired": True}),
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={
                                "default_real_mode": "self_consumption",
                                "backup_reserve_percent": 5,
                            }
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        assert asyncio.run(controller.get_backup_reserve()) == 5
    finally:
        restore()


def test_tesla_backup_reserve_prefers_fresh_local_readback_over_cloud_cache():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "powerwall_local": {
                            "coordinator": SimpleNamespace(
                                data=SimpleNamespace(backup_reserve_percent=10),
                                last_success_ts=time.time(),
                            )
                        },
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={
                                "default_real_mode": "self_consumption",
                                "backup_reserve_percent": 18,
                            }
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        assert asyncio.run(controller.get_backup_reserve()) == 10
    finally:
        restore()


def test_tesla_backup_reserve_prefers_pending_local_write_over_readbacks():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "powerwall_local_backup_reserve_write_user_pct": 10,
                        "powerwall_local": {
                            "coordinator": SimpleNamespace(
                                data=SimpleNamespace(backup_reserve_percent=18),
                                last_success_ts=time.time(),
                            )
                        },
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={
                                "default_real_mode": "self_consumption",
                                "backup_reserve_percent": 18,
                            }
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        assert asyncio.run(controller.get_backup_reserve()) == 10
    finally:
        restore()


def test_tesla_backup_reserve_ignores_stale_local_readback():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "powerwall_local": {
                            "coordinator": SimpleNamespace(
                                data=SimpleNamespace(backup_reserve_percent=10),
                                last_success_ts=time.time() - 120,
                            )
                        },
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={
                                "default_real_mode": "self_consumption",
                                "backup_reserve_percent": 18,
                            }
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        assert asyncio.run(controller.get_backup_reserve()) == 18
    finally:
        restore()


def test_optimizer_backup_reserve_write_marks_source():
    module, restore = _load_controller_module()
    try:
        services = _Services()
        hass = SimpleNamespace(services=services)
        controller = module.BatteryControllerWrapper(hass, "tesla")

        assert asyncio.run(controller.set_backup_reserve(52))

        assert services.calls == [
            (
                "power_sync",
                "set_backup_reserve",
                {"percent": 52, "source": "optimizer"},
                True,
            )
        ]
    finally:
        restore()


def test_backup_reserve_reads_coordinator_data_before_controller():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "sungrow_coordinator": SimpleNamespace(
                            data={"backup_reserve": 15},
                            _controller=SimpleNamespace(),
                        )
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "sungrow")

        assert asyncio.run(controller.get_backup_reserve()) == 15
    finally:
        restore()


def test_read_backup_reserve_pending_local_write_is_live():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "powerwall_local_backup_reserve_write_user_pct": 10,
                        "powerwall_local": {
                            "coordinator": SimpleNamespace(
                                data=SimpleNamespace(backup_reserve_percent=18),
                                last_success_ts=time.time(),
                            )
                        },
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={"backup_reserve_percent": 18},
                            _site_info_last_fetch=time.monotonic(),
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        reading = asyncio.run(controller.read_backup_reserve())

        assert reading.percent == 10
        assert reading.trust == module.ReserveTrust.LIVE
        assert asyncio.run(controller.get_backup_reserve()) == reading.percent
    finally:
        restore()


def test_read_backup_reserve_fresh_local_snapshot_is_live():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "powerwall_local": {
                            "coordinator": SimpleNamespace(
                                data=SimpleNamespace(backup_reserve_percent=10),
                                last_success_ts=time.time(),
                            )
                        },
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={"backup_reserve_percent": 18},
                            _site_info_last_fetch=time.monotonic(),
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        reading = asyncio.run(controller.read_backup_reserve())

        assert reading.percent == 10
        assert reading.trust == module.ReserveTrust.LIVE
        assert asyncio.run(controller.get_backup_reserve()) == reading.percent
    finally:
        restore()


def test_read_backup_reserve_cloud_cache_fresh_fetch_is_cloud_fresh():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={"backup_reserve_percent": 20},
                            _site_info_last_fetch=time.monotonic(),
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        reading = asyncio.run(controller.read_backup_reserve())

        assert reading.percent == 20
        assert reading.trust == module.ReserveTrust.CLOUD_FRESH
        assert asyncio.run(controller.get_backup_reserve()) == reading.percent
    finally:
        restore()


def test_read_backup_reserve_cloud_cache_old_fetch_is_cloud_stale():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={"backup_reserve_percent": 20},
                            _site_info_last_fetch=time.monotonic()
                            - module.TESLA_SITE_INFO_MAX_AGE_SECONDS
                            - 1,
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        reading = asyncio.run(controller.read_backup_reserve())

        assert reading.percent == 20
        assert reading.trust == module.ReserveTrust.CLOUD_STALE
        assert asyncio.run(controller.get_backup_reserve()) == reading.percent
    finally:
        restore()


def test_read_backup_reserve_missing_last_fetch_treated_as_cloud_stale():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "entry": SimpleNamespace(data={"powerwall_local_paired": True}),
                        "tesla_coordinator": SimpleNamespace(
                            _site_info_cache={
                                "default_real_mode": "self_consumption",
                                "backup_reserve_percent": 5,
                            }
                        ),
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        reading = asyncio.run(controller.read_backup_reserve())

        assert reading.percent == 5
        assert reading.trust == module.ReserveTrust.CLOUD_STALE
        assert asyncio.run(controller.get_backup_reserve()) == reading.percent
    finally:
        restore()


def test_read_backup_reserve_no_cloud_cache_falls_back_to_entity():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({"number.power_sync_tesla_backup_reserve": "0.0"}),
            data={"power_sync": {"entry-1": {}}},
        )
        controller = module.BatteryControllerWrapper(hass, "tesla")

        reading = asyncio.run(controller.read_backup_reserve())

        assert reading.percent == 0
        assert reading.trust == module.ReserveTrust.ENTITY
        assert asyncio.run(controller.get_backup_reserve()) == reading.percent
    finally:
        restore()


def test_read_backup_reserve_non_tesla_coordinator_data_is_live():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(
            states=_States({}),
            data={
                "power_sync": {
                    "entry-1": {
                        "sungrow_coordinator": SimpleNamespace(
                            data={"backup_reserve": 15},
                            _controller=SimpleNamespace(),
                        )
                    }
                }
            },
        )
        controller = module.BatteryControllerWrapper(hass, "sungrow")

        reading = asyncio.run(controller.read_backup_reserve())

        assert reading.percent == 15
        assert reading.trust == module.ReserveTrust.LIVE
        assert asyncio.run(controller.get_backup_reserve()) == reading.percent
    finally:
        restore()
