"""Service authority and multi-entry routing for the native permission."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def fixture(*, monitoring=False, user=None, entries=1, explicit=None, confirmed=True, local=True, paired=True, monitoring_only=False):
    entry = SimpleNamespace(domain="power_sync", entry_id="target", data={"powerwall_local_paired": paired}, options={"monitoring_mode": monitoring})
    client = SimpleNamespace(local_access_enabled=local, set_on_grid_solar_curtailment=AsyncMock(return_value=confirmed))
    coordinator = SimpleNamespace(client=client, async_request_refresh=AsyncMock())
    ensure = AsyncMock(return_value=coordinator)
    namespace = dict(
        HomeAssistant=object, ServiceCall=object, HomeAssistantError=RuntimeError,
        DOMAIN="power_sync", CONF_POWERWALL_LOCAL_PAIRED="powerwall_local_paired", CONF_MONITORING_MODE="monitoring_mode",
        ensure_coordinator=ensure, ensure_client=AsyncMock(),
        resolve_connection_profile=lambda *args: SimpleNamespace(monitoring_only=monitoring_only),
    )
    source = Path(__file__).resolve().parents[1] / "custom_components/power_sync/powerwall_local/services.py"
    node = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_on_grid_solar_curtailment")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    selected = []
    def get_entry(identity):
        selected.append(identity)
        return entry if identity == "target" else None
    hass = SimpleNamespace(data={"power_sync": {"target": {}}}, config_entries=SimpleNamespace(async_entries=lambda domain: [entry] * entries, async_get_entry=get_entry))
    data = {"enabled": True}
    if explicit: data["entry_id"] = explicit
    call = SimpleNamespace(data=data, context=SimpleNamespace(user_id=user))
    return namespace["_handle_on_grid_solar_curtailment"], hass, call, client, coordinator, ensure, selected


@pytest.mark.parametrize("options, error", [
    ({"monitoring": True}, "Monitoring Mode"),
    ({"monitoring_only": True, "user": "owner"}, "monitoring only"),
    ({"entries": 2}, "Specify entry_id"),
    ({"explicit": "wrong"}, "paired Powerwall"),
    ({"paired": False}, "paired Powerwall"),
    ({"local": False}, "LAN control"),
    ({"confirmed": False}, "readback"),
])
def test_native_setting_fails_closed(options, error):
    handler, hass, call, client, coord, ensure, selected = fixture(**options)
    with pytest.raises(RuntimeError, match=error):
        asyncio.run(handler(hass, call))
    if not options.get("confirmed") is False:
        client.set_on_grid_solar_curtailment.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


def test_explicit_manual_setting_uses_only_selected_entry_and_readback():
    handler, hass, call, client, coord, ensure, selected = fixture(monitoring=True, user="owner", entries=2, explicit="target")
    asyncio.run(handler(hass, call))
    assert selected and set(selected) == {"target"}
    assert client.set_on_grid_solar_curtailment.await_args.args == (True,)
    client.set_on_grid_solar_curtailment.await_args.kwargs["before_write"]()
    coord.async_request_refresh.assert_awaited_once()
    assert ensure.await_args.args[1].entry_id == "target"


@pytest.mark.parametrize("change", ["monitoring", "pairing", "stopping"])
def test_authority_rechecked_after_delayed_client_acquisition(change):
    handler, hass, call, client, coord, ensure, selected = fixture()
    entry = hass.config_entries.async_get_entry("target")
    async def acquire(*args):
        if change == "monitoring": entry.options["monitoring_mode"] = True
        elif change == "pairing": entry.data["powerwall_local_paired"] = False
        else: hass.data["power_sync"]["target"]["powerwall_local_dispatch_stopping"] = True
        return coord
    ensure.side_effect = acquire
    with pytest.raises(RuntimeError): asyncio.run(handler(hass, call))
    client.set_on_grid_solar_curtailment.assert_not_awaited()


@pytest.mark.parametrize("change", ["monitoring", "unload", "reload"])
def test_authority_rechecked_inside_delayed_config_write(change):
    handler, hass, call, client, coord, ensure, selected = fixture()
    entry = hass.config_entries.async_get_entry("target")
    async def delayed_write(enabled, *, before_write):
        if change == "monitoring": entry.options["monitoring_mode"] = True
        elif change == "unload": hass.data["power_sync"].pop("target")
        else: hass.data["power_sync"]["target"] = {}
        before_write()
        raise AssertionError("write must be fenced")
    client.set_on_grid_solar_curtailment.side_effect = delayed_write
    with pytest.raises(RuntimeError): asyncio.run(handler(hass, call))
    coord.async_request_refresh.assert_not_awaited()
