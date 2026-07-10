"""Tests for powerwall_local.dispatch.

Covers the four behavioural cases the dispatcher must get right:
    1. unpaired entry → cloud only, local never attempted
    2. paired + local success → cloud never called
    3. paired + local raises a fallback exception → cloud runs
    4. paired + local returns falsy → retry once, then cloud

Run with: pytest tests/test_dispatch.py

The integration's full __init__.py pulls in the entire HomeAssistant runtime,
which we don't want for a unit test. Instead, register the ``power_sync``
package as a stub namespace package whose __path__ points at the real source
tree — Python then imports submodules directly without running __init__.py.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# ----- HomeAssistant API stubs (just what dispatch.py and friends touch) -----
_ha_root = types.ModuleType("homeassistant")
_ha_config_entries = types.ModuleType("homeassistant.config_entries")
_ha_core = types.ModuleType("homeassistant.core")


class _FakeConfigEntry:
    def __init__(self, entry_id="abc", data=None):
        self.entry_id = entry_id
        self.data = dict(data or {})


_ha_config_entries.ConfigEntry = _FakeConfigEntry
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
sys.modules.setdefault("homeassistant", _ha_root)
sys.modules.setdefault("homeassistant.config_entries", _ha_config_entries)
sys.modules.setdefault("homeassistant.core", _ha_core)

# ----- Stub power_sync as a namespace package, skipping __init__.py -----
ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_pwl = types.ModuleType("power_sync.powerwall_local")
_pwl.__path__ = [str(ROOT / "powerwall_local")]
sys.modules["power_sync.powerwall_local"] = _pwl

# const.py and exceptions.py have no HA-runtime dependencies — import directly.
# transport.py, however, imports `aiohttp` and `cryptography` and large protobufs.
# Stub the parts dispatch.py needs (the exception classes + transport class).
_const_mod = types.ModuleType("power_sync.const")
_const_mod.CONF_POWERWALL_LOCAL_IP = "powerwall_local_ip"
_const_mod.CONF_POWERWALL_LOCAL_PAIRED = "powerwall_local_paired"
_const_mod.DOMAIN = "power_sync"
sys.modules["power_sync.const"] = _const_mod

_exc_mod = types.ModuleType("power_sync.powerwall_local.exceptions")
class PowerwallLocalError(Exception): ...
class PowerwallAuthError(PowerwallLocalError): ...
class PowerwallUnreachableError(PowerwallLocalError): ...
class PowerwallSignatureError(PowerwallLocalError): ...
_exc_mod.PowerwallLocalError = PowerwallLocalError
_exc_mod.PowerwallAuthError = PowerwallAuthError
_exc_mod.PowerwallUnreachableError = PowerwallUnreachableError
_exc_mod.PowerwallSignatureError = PowerwallSignatureError
sys.modules["power_sync.powerwall_local.exceptions"] = _exc_mod

_tr_mod = types.ModuleType("power_sync.powerwall_local.transport")
class TEDAPIv1rTransport:  # minimal — dispatch.py only uses isinstance
    pass
_tr_mod.TEDAPIv1rTransport = TEDAPIv1rTransport
sys.modules["power_sync.powerwall_local.transport"] = _tr_mod

from power_sync.powerwall_local.dispatch import (  # noqa: E402
    dispatch_powerwall_write,
    is_local_preferred,
)


# ----- Helpers -----
def _hass_with_transport(transport):
    hass = MagicMock()
    hass.data = {
        "power_sync": {
            "abc": {"powerwall_local": {"client": MagicMock(_transport=transport)}}
        }
    }
    return hass


def _paired():
    return _FakeConfigEntry(
        data={"powerwall_local_paired": True, "powerwall_local_ip": "192.168.1.50"}
    )


def _unpaired():
    return _FakeConfigEntry(data={})


# ----- Tests -----
async def _test_unpaired_skips_local():
    local = AsyncMock()
    cloud = AsyncMock(return_value="cloud-ok")
    hass = MagicMock()
    hass.data = {}
    result = await dispatch_powerwall_write(
        hass, _unpaired(), local_call=local, cloud_call=cloud, label="t"
    )
    assert result == "cloud-ok"
    assert local.await_count == 0
    assert cloud.await_count == 1


def test_unpaired_skips_local():
    asyncio.run(_test_unpaired_skips_local())


async def _test_paired_without_gateway_ip_skips_local():
    local = AsyncMock(return_value=True)
    cloud = AsyncMock(return_value="cloud-ok")
    hass = _hass_with_transport(TEDAPIv1rTransport())
    entry = _FakeConfigEntry(data={"powerwall_local_paired": True})
    result = await dispatch_powerwall_write(
        hass, entry, local_call=local, cloud_call=cloud, label="t"
    )
    assert result == "cloud-ok"
    assert local.await_count == 0
    assert cloud.await_count == 1


def test_paired_without_gateway_ip_skips_local():
    asyncio.run(_test_paired_without_gateway_ip_skips_local())


async def _test_paired_blank_gateway_ip_skips_local():
    local = AsyncMock(return_value=True)
    cloud = AsyncMock(return_value="cloud-ok")
    hass = _hass_with_transport(TEDAPIv1rTransport())
    entry = _FakeConfigEntry(
        data={"powerwall_local_paired": True, "powerwall_local_ip": "   "}
    )
    result = await dispatch_powerwall_write(
        hass, entry, local_call=local, cloud_call=cloud, label="t"
    )
    assert result == "cloud-ok"
    assert local.await_count == 0
    assert cloud.await_count == 1


def test_paired_blank_gateway_ip_skips_local():
    asyncio.run(_test_paired_blank_gateway_ip_skips_local())


async def _test_paired_local_success_skips_cloud():
    local = AsyncMock(return_value=True)
    cloud = AsyncMock(return_value="cloud-ok")
    hass = _hass_with_transport(TEDAPIv1rTransport())
    result = await dispatch_powerwall_write(
        hass, _paired(), local_call=local, cloud_call=cloud, label="t"
    )
    assert result is True
    assert local.await_count == 1
    assert cloud.await_count == 0


def test_paired_local_success_skips_cloud():
    asyncio.run(_test_paired_local_success_skips_cloud())


async def _test_paired_local_raises_falls_back_to_cloud():
    local = AsyncMock(side_effect=PowerwallUnreachableError("unreachable"))
    cloud = AsyncMock(return_value="cloud-ok")
    hass = _hass_with_transport(TEDAPIv1rTransport())
    result = await dispatch_powerwall_write(
        hass, _paired(), local_call=local, cloud_call=cloud, label="t",
        retry_local_once=False,
    )
    assert result == "cloud-ok"
    assert local.await_count == 1
    assert cloud.await_count == 1


def test_paired_local_raises_falls_back_to_cloud():
    asyncio.run(_test_paired_local_raises_falls_back_to_cloud())


async def _test_paired_local_falsy_retries_then_cloud():
    local = AsyncMock(return_value=False)
    cloud = AsyncMock(return_value="cloud-ok")
    hass = _hass_with_transport(TEDAPIv1rTransport())
    result = await dispatch_powerwall_write(
        hass, _paired(), local_call=local, cloud_call=cloud, label="t",
    )
    assert result == "cloud-ok"
    assert local.await_count == 2
    assert cloud.await_count == 1


def test_paired_local_falsy_retries_then_cloud():
    asyncio.run(_test_paired_local_falsy_retries_then_cloud())


async def _test_paired_signature_error_falls_back():
    local = AsyncMock(side_effect=PowerwallSignatureError("bad sig"))
    cloud = AsyncMock(return_value="cloud-ok")
    hass = _hass_with_transport(TEDAPIv1rTransport())
    result = await dispatch_powerwall_write(
        hass, _paired(), local_call=local, cloud_call=cloud, label="t",
        retry_local_once=False,
    )
    assert result == "cloud-ok"


def test_paired_signature_error_falls_back():
    asyncio.run(_test_paired_signature_error_falls_back())


async def _test_paired_local_failure_then_cloud_success_marks_fallback_pending():
    """PW-4 residual closure, part B: when local was attempted and failed
    but the cloud fallback succeeded, dispatch must flag the entry so the
    next local poll doesn't re-stamp the (still-stale) snapshot as fresh."""
    local = AsyncMock(side_effect=PowerwallUnreachableError("unreachable"))
    cloud = AsyncMock(return_value="cloud-ok")
    hass = _hass_with_transport(TEDAPIv1rTransport())
    result = await dispatch_powerwall_write(
        hass, _paired(), local_call=local, cloud_call=cloud, label="t",
        retry_local_once=False,
    )
    assert result == "cloud-ok"
    entry_data = hass.data["power_sync"]["abc"]
    assert entry_data.get("powerwall_local_cloud_fallback_pending") is True


def test_paired_local_failure_then_cloud_success_marks_fallback_pending():
    asyncio.run(_test_paired_local_failure_then_cloud_success_marks_fallback_pending())


async def _test_unpaired_cloud_only_does_not_mark_fallback_pending():
    """Local was never attempted (unpaired) -- no marker, since the next
    local poll isn't re-fetching anything stale on this entry's behalf."""
    local = AsyncMock()
    cloud = AsyncMock(return_value="cloud-ok")
    hass = MagicMock()
    hass.data = {"power_sync": {"abc": {}}}
    result = await dispatch_powerwall_write(
        hass, _unpaired(), local_call=local, cloud_call=cloud, label="t"
    )
    assert result == "cloud-ok"
    entry_data = hass.data["power_sync"]["abc"]
    assert "powerwall_local_cloud_fallback_pending" not in entry_data


def test_unpaired_cloud_only_does_not_mark_fallback_pending():
    asyncio.run(_test_unpaired_cloud_only_does_not_mark_fallback_pending())


async def _test_paired_no_transport_short_circuits_to_cloud():
    hass = MagicMock()
    hass.data = {"power_sync": {"abc": {"powerwall_local": {"client": None}}}}
    local = AsyncMock(return_value=True)
    cloud = AsyncMock(return_value="cloud-ok")
    result = await dispatch_powerwall_write(
        hass, _paired(), local_call=local, cloud_call=cloud, label="t",
    )
    assert result == "cloud-ok"
    assert local.await_count == 0


def test_paired_no_transport_short_circuits_to_cloud():
    asyncio.run(_test_paired_no_transport_short_circuits_to_cloud())


async def _test_paired_cloud_only_client_short_circuits_to_cloud():
    transport = TEDAPIv1rTransport()
    client = MagicMock(_transport=transport)
    client.local_access_enabled = False
    hass = MagicMock()
    hass.data = {"power_sync": {"abc": {"powerwall_local": {"client": client}}}}
    local = AsyncMock(return_value=True)
    cloud = AsyncMock(return_value="cloud-ok")
    result = await dispatch_powerwall_write(
        hass, _paired(), local_call=local, cloud_call=cloud, label="t",
    )
    assert result == "cloud-ok"
    assert local.await_count == 0
    assert cloud.await_count == 1


def test_paired_cloud_only_client_short_circuits_to_cloud():
    asyncio.run(_test_paired_cloud_only_client_short_circuits_to_cloud())


def test_is_local_preferred():
    assert is_local_preferred(_paired()) is True
    assert is_local_preferred(_unpaired()) is False
    assert is_local_preferred(
        _FakeConfigEntry(data={"powerwall_local_paired": False})
    ) is False
