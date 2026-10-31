"""Actual auxiliary service registrations preserve explicit Tesla entry routing."""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from conftest import load_power_sync_function
from test_service_calls import CurrentServiceCall


COMPONENT = Path(__file__).parents[1] / "custom_components/power_sync"
SERVICES = (
    "schedule_max_backup", "set_storm_watch",
    "set_off_grid_ev_reserve", "set_vpp_enrollment",
)
PAYLOADS = {
    "schedule_max_backup": {"duration_minutes": 5},
    "set_storm_watch": {"enabled": True},
    "set_off_grid_ev_reserve": {"percent": 37},
    "set_vpp_enrollment": {"program_id": "program-a", "enrolled": False},
}


@lru_cache(maxsize=1)
def registration_code():
    """Compile the real entry handler map and the four HA registrations."""
    path = COMPONENT / "__init__.py"
    tree = ast.parse(path.read_text())
    maps, registrations = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if (isinstance(call.func, ast.Name) and call.func.id == "register_handlers"
                and len(call.args) == 3 and isinstance(call.args[2], ast.Dict)):
            keys = {key.value for key in call.args[2].keys if isinstance(key, ast.Constant)}
            if "force_charge" in keys and "set_grid_export" in keys:
                maps.append(node)
        if (isinstance(call.func, ast.Attribute) and call.func.attr == "async_register"
                and len(call.args) >= 3 and isinstance(call.args[1], ast.Constant)
                and call.args[1].value in SERVICES):
            registrations.append(node)
    assert len(maps) == 1, "Expected the actual root battery handler registry"
    assert len(registrations) == len(SERVICES), "Expected one actual registration per auxiliary service"
    nodes = [maps[0], *registrations]
    handler_names = {node.id for top in nodes for node in ast.walk(top)
                     if isinstance(node, ast.Name) and node.id.startswith("handle_")}
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    return compile(module, str(path), "exec"), handler_names


@pytest.fixture
def registered():
    namespace = {}
    for name in ("register_handlers", "routed_handler"):
        load_power_sync_function(name, namespace, path=COMPONENT / "licensing/entry_routing.py")
    callbacks, commands, results = {}, [], {}

    def async_register(domain, service, handler, **_kwargs):
        assert domain == "power_sync"
        callbacks[service] = handler

    finish = Mock()
    entries = {
        "entry-a": NS(entry_id="entry-a", data={"tesla_energy_site_id": "1001"}, options={}),
        "entry-b": NS(entry_id="entry-b", data={"tesla_energy_site_id": "2002"}, options={}),
    }
    hass = NS(data={"power_sync": {
        entry_id: {"entry": entry, "commands": []} for entry_id, entry in entries.items()
    }, "power_sync_license_manager": NS(finish_manual_command=finish)},
        services=NS(async_register=async_register))
    code, handler_names = registration_code()

    def entry_handler(entry, service):
        result = {"entry_id": entry.entry_id,
                  "site_id": entry.data["tesla_energy_site_id"], "service": service}
        results[entry.entry_id, service] = result

        async def handle(call):
            # The routed handler must deliver the same call to this site's
            # driver, including its context and private orchestration fields.
            command = (entry.entry_id, entry.data["tesla_energy_site_id"], service, call)
            commands.append(command)
            hass.data["power_sync"][entry.entry_id]["commands"].append(command)
            return result

        return handle

    # HA's global service callback is overwritten by the later entry. The
    # actual registration must still route explicit entry-a calls to site 1001.
    for entry in entries.values():
        scope = {**namespace, "hass": hass, "entry": entry, "DOMAIN": "power_sync",
                 "amber_smartshift_coordinator": False, "native_handlers": {},
                 "SupportsResponse": NS(OPTIONAL=object())}
        scope.update({name: entry_handler(entry, name.removeprefix("handle_"))
                      for name in handler_names})
        exec(code, scope)
    return NS(hass=hass, entries=entries, callbacks=callbacks, commands=commands,
              results=results, finish=finish)


def service_call(p, service, *, entry_id="entry-a", include_entry=True):
    token, context = object(), NS(user_id="manual-user")
    data = {**PAYLOADS[service], "source": "user", "_transition_token": token}
    if include_entry:
        data["entry_id"] = entry_id
    return CurrentServiceCall(hass=p.hass, domain="power_sync", service=service,
                              data=data, context=context, return_response=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("target,site_id", [("entry-a", "1001"), ("entry-b", "2002")])
async def test_actual_auxiliary_registration_routes_exact_entry_and_preserves_call(registered, service, target, site_id):
    p = registered
    call = service_call(p, service, entry_id=target)
    original_payload, original_data, original_context = call.data, dict(call.data), call.context
    result = await p.callbacks[service](call)
    assert result is p.results[target, service]
    assert p.commands == [(target, site_id, service, call)]
    assert p.commands[0][3] is call
    assert call.data is original_payload and call.data == original_data
    assert call.context is original_context
    assert call.return_response is True
    other = "entry-b" if target == "entry-a" else "entry-a"
    assert p.hass.data["power_sync"][other]["commands"] == []
    p.finish.assert_called_once_with(target)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", SERVICES)
async def test_auxiliary_registration_without_entry_keeps_current_entry_default(registered, service):
    p = registered
    call = service_call(p, service, include_entry=False)
    result = await p.callbacks[service](call)
    assert result is p.results["entry-b", service]
    assert p.commands == [("entry-b", "2002", service, call)]
    assert "entry_id" not in call.data
    p.finish.assert_called_once_with("entry-b")


@pytest.mark.asyncio
@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("invalid", [False, 7, ["entry-a"]])
async def test_auxiliary_registration_invalid_entry_never_falls_back(registered, service, invalid):
    p = registered
    with pytest.raises(ValueError, match="^invalid_control_entry$"):
        await p.callbacks[service](service_call(p, service, entry_id=invalid))
    assert p.commands == []
    p.finish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("missing", ["entry", "handler-map", "service-handler"])
async def test_auxiliary_registration_unavailable_explicit_target_never_falls_back(registered, service, missing):
    p = registered
    if missing == "entry":
        del p.hass.data["power_sync"]["entry-a"]
    elif missing == "handler-map":
        p.hass.data["power_sync"]["entry-a"].pop("licensing_control_handlers")
    else:
        p.hass.data["power_sync"]["entry-a"]["licensing_control_handlers"].pop(service, None)
    with pytest.raises(ValueError, match="^control_entry_not_available$"):
        await p.callbacks[service](service_call(p, service))
    assert p.commands == []
    assert p.hass.data["power_sync"]["entry-b"]["commands"] == []
    p.finish.assert_not_called()
