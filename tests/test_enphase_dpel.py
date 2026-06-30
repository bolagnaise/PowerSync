from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _load_enphase_controller_module():
    saved = {
        name: sys.modules.get(name)
        for name in (
            "power_sync",
            "power_sync.inverters",
            "power_sync.inverters.enphase",
        )
    }

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters
    sys.modules.pop("power_sync.inverters.enphase", None)

    module = importlib.import_module("power_sync.inverters.enphase")

    def restore() -> None:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return module, restore


def test_dpel_uses_structured_relay_config_when_base_missing_it():
    module, restore_module = _load_enphase_controller_module()
    try:
        controller = module.EnphaseController("192.0.2.10")
        payloads = []

        async def get_installed_capacity_w() -> float:
            return 4587.0

        async def get_dpel_base_settings() -> dict:
            return {
                "enable": False,
                "export_limit": True,
                "limit_value_W": 999.0,
            }

        async def post(endpoint: str, payload: dict) -> tuple[bool, int]:
            payloads.append((endpoint, payload))
            return True, 200

        controller._get_installed_capacity_w = get_installed_capacity_w
        controller._get_dpel_base_settings = get_dpel_base_settings
        controller._post = post

        assert asyncio.run(controller._set_dpel(enabled=True, limit_watts=0)) == (True, True)

        assert len(payloads) == 1
        endpoint, payload = payloads[0]
        assert endpoint == controller.ENDPOINT_DPEL

        settings = payload["dynamic_pel_settings"]
        assert settings["enable"] is True
        assert settings["export_limit"] is True
        assert settings["limit_value_W"] == 0.0
        assert settings["enable_dynamic_limiting"] is True
        assert settings["installed_capacity"] == 4587.0
        assert settings["relay_config"] == {
            "default_pct_limit": 0.0,
            "limit_levels": [
                {"relays": f"{relay_state:04b}", "pct_limit": 0.0}
                for relay_state in range(16)
            ],
        }
    finally:
        restore_module()


def test_dpel_keeps_boolean_relay_config_fallbacks_after_structured_default():
    module, restore_module = _load_enphase_controller_module()
    try:
        controller = module.EnphaseController("192.0.2.10")
        payloads = []

        async def get_installed_capacity_w() -> float:
            return 4587.0

        async def get_dpel_base_settings() -> None:
            return None

        async def post(endpoint: str, payload: dict) -> tuple[bool, int]:
            payloads.append(payload)
            relay_config = payload["dynamic_pel_settings"].get("relay_config")
            return relay_config is True, 200 if relay_config is True else 400

        controller._get_installed_capacity_w = get_installed_capacity_w
        controller._get_dpel_base_settings = get_dpel_base_settings
        controller._post = post

        assert asyncio.run(controller._set_dpel(enabled=True, limit_watts=0)) == (True, True)

        relay_configs = [
            payload["dynamic_pel_settings"].get("relay_config")
            for payload in payloads[:3]
        ]
        assert relay_configs == [
            {
                "default_pct_limit": 0.0,
                "limit_levels": [
                    {"relays": f"{relay_state:04b}", "pct_limit": 0.0}
                    for relay_state in range(16)
                ],
            },
            False,
            True,
        ]
    finally:
        restore_module()


def test_dpel_uses_load_following_percentage_relay_config():
    module, restore_module = _load_enphase_controller_module()
    try:
        controller = module.EnphaseController("192.0.2.10")
        payloads = []

        async def get_installed_capacity_w() -> float:
            return 4587.0

        async def get_dpel_base_settings() -> None:
            return None

        async def post(endpoint: str, payload: dict) -> tuple[bool, int]:
            payloads.append(payload)
            return True, 200

        controller._get_installed_capacity_w = get_installed_capacity_w
        controller._get_dpel_base_settings = get_dpel_base_settings
        controller._post = post

        assert asyncio.run(
            controller._set_dpel(
                enabled=True,
                limit_watts=2000,
                use_production_limit=True,
            )
        ) == (True, True)

        settings = payloads[0]["dynamic_pel_settings"]
        expected_pct = 2000 / 4587.0 * 100.0
        assert settings["export_limit"] is False
        assert settings["limit_value_W"] == 2000.0
        assert settings["relay_config"] == {
            "default_pct_limit": expected_pct,
            "limit_levels": [
                {"relays": f"{relay_state:04b}", "pct_limit": expected_pct}
                for relay_state in range(16)
            ],
        }
    finally:
        restore_module()


def test_dpel_restore_uses_full_percentage_relay_config():
    module, restore_module = _load_enphase_controller_module()
    try:
        controller = module.EnphaseController("192.0.2.10")
        payloads = []

        async def get_installed_capacity_w() -> float:
            return 4587.0

        async def get_dpel_base_settings() -> None:
            return None

        async def post(endpoint: str, payload: dict) -> tuple[bool, int]:
            payloads.append(payload)
            return True, 200

        controller._get_installed_capacity_w = get_installed_capacity_w
        controller._get_dpel_base_settings = get_dpel_base_settings
        controller._post = post

        assert asyncio.run(controller._set_dpel(enabled=False, limit_watts=0)) == (True, True)

        settings = payloads[0]["dynamic_pel_settings"]
        assert settings["relay_config"] == {
            "default_pct_limit": 100.0,
            "limit_levels": [
                {"relays": f"{relay_state:04b}", "pct_limit": 100.0}
                for relay_state in range(16)
            ],
        }
    finally:
        restore_module()
