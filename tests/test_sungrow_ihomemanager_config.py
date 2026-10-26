"""Exercise native Sungrow setup/options without importing the HA integration."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"


@pytest.fixture
def flow(monkeypatch):
    ns = {"__package__": "native_flow_test"}
    names = ("CONF_SUNGROW_HOST", "CONF_SUNGROW_PORT", "CONF_SUNGROW_SLAVE_ID", "CONF_MONITORING_MODE",
             "CONF_SUNGROW_CONNECTION_TYPE", "CONF_SUNGROW_IHOMEMANAGER_HOST", "CONF_SUNGROW_IHOMEMANAGER_PORT",
             "CONF_BATTERY_CONNECTION_PROFILE", "CONF_ELECTRICITY_PROVIDER")
    ns.update({name: name.removeprefix("CONF_").lower() for name in names})
    ns.update(SUNGROW_CONNECTION_DIRECT="direct", SUNGROW_CONNECTION_IHOMEMANAGER="ihomemanager",
              SUNGROW_CONNECTION_IHOMEMANAGER_NATIVE="ihomemanager_native", DEFAULT_SUNGROW_PORT=502,
              DEFAULT_SUNGROW_IHOMEMANAGER_PORT=503, DEFAULT_SUNGROW_SLAVE_ID=1)
    ns["SUNGROW_CONNECTION_TYPES"] = {"direct": "Direct", "ihomemanager": "Forwarding", "ihomemanager_native": "Native"}
    ns["ELECTRICITY_PROVIDERS"] = {"amber": "Amber"}
    config_tree = ast.parse((COMPONENT / "sungrow_ihomemanager_config.py").read_text())
    function = next(n for n in config_tree.body if isinstance(n, ast.FunctionDef))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "native-settings", "exec"), ns)
    events = []
    snapshot = SimpleNamespace(supports_dispatch=True)
    probe_errors = {}
    class Probe:
        def __init__(self, host, port):
            events.append(("native_probe", host, port))
        async def read_snapshot(self):
            if "native" in probe_errors:
                raise probe_errors["native"]
            return snapshot
    for name in ("native_flow_test", "native_flow_test.inverters"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    config_module = ModuleType("native_flow_test.sungrow_ihomemanager_config")
    config_module.native_connection_settings = ns["native_connection_settings"]
    monkeypatch.setitem(sys.modules, config_module.__name__, config_module)
    driver_module = ModuleType("native_flow_test.inverters.sungrow_ihomemanager")
    driver_module.SungrowIHomeManagerController = Probe
    monkeypatch.setitem(sys.modules, driver_module.__name__, driver_module)
    async def inverter_probe(hass, host, port, slave_id):
        events.append(("inverter_probe", host, port, slave_id))
        if "inverter" in probe_errors:
            return {"success": False, "error": probe_errors["inverter"]}
        return {"success": True, "battery_soc": 65}
    async def handoff(hass, entry):
        events.append(("cleanup",))
        if hass.reject_cleanup:
            raise RuntimeError("restore failed")
    ns.update(test_sungrow_connection=inverter_probe, async_prepare_monitoring_handoff=handoff,
              finish_monitoring_handoff=lambda *args: events.append(("finish_cleanup",)),
              _LOGGER=SimpleNamespace(info=lambda *args: None, warning=lambda *args: None))
    marker = lambda key, **kwargs: key
    ns.update(vol=SimpleNamespace(Schema=lambda data: data, Required=marker, Optional=marker),
              sungrow_native_schema=lambda current: {},
              TextSelector=lambda value: value, TextSelectorConfig=lambda **kwargs: kwargs,
              TextSelectorType=SimpleNamespace(TEXT="text"),
              NumberSelector=lambda value: value, NumberSelectorConfig=lambda **kwargs: kwargs,
              NumberSelectorMode=SimpleNamespace(BOX="box"),
              SelectSelector=lambda value: value, SelectSelectorConfig=lambda **kwargs: kwargs,
              SelectOptionDict=lambda **kwargs: kwargs, SelectSelectorMode=SimpleNamespace(DROPDOWN="dropdown"))
    tree = ast.parse((COMPONENT / "config_flow.py").read_text())
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    selected = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "validate_sungrow_native_connection"]
    selected += [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name in
                 ("async_step_sungrow", "async_step_sungrow_connection", "async_step_init_sungrow")]
    assert len(selected) == 4
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[])), "native-flow", "exec"), ns)
    entry = SimpleNamespace(data={"sungrow_connection_type": "ihomemanager", "monitoring_mode": True}, options={})
    saved = []
    hass = SimpleNamespace(reject_cleanup=False)
    def save(entry, **values):
        events.append(("save",))
        saved.append(values)
    hass.config_entries = SimpleNamespace(async_update_entry=save)
    owner = SimpleNamespace(hass=hass, config_entry=entry, _ml_options={}, _battery_profile_data={})
    owner._get_option = lambda key, default=None: entry.options.get(key, entry.data.get(key, default))
    owner._sungrow_ac_inverter_conflicts = lambda *args: False
    owner._remove_legacy_sungrow_dual_options = lambda *args: None
    owner._schedule_entry_reload = lambda: events.append(("reload",))
    owner.async_create_entry = lambda **kwargs: kwargs
    owner.async_show_form = lambda **kwargs: kwargs
    owner.async_abort = lambda **kwargs: {"abort": kwargs}
    owner._create_final_entry = lambda: {"created": True}
    async def provider_step():
        return {"provider_step": "amber"}
    owner.async_step_amber_options = provider_step
    values = dict(sungrow_connection_type="ihomemanager_native", sungrow_host="inverter.test", sungrow_port=502,
                  sungrow_slave_id=1, sungrow_ihomemanager_host="ihm.test", sungrow_ihomemanager_port=503)
    return SimpleNamespace(owner=owner, ns=ns, entry=entry, values=values, events=events,
                           saved=saved, snapshot=snapshot, probe_errors=probe_errors)


def test_initial_setup_probes_both_endpoints_and_starts_in_monitoring(flow):
    result = asyncio.run(flow.ns["async_step_sungrow"](flow.owner, flow.values))
    assert result == {"created": True}
    assert flow.owner._ml_options["monitoring_mode"] is True
    assert flow.owner._sungrow_data["sungrow_ihomemanager_host"] == "ihm.test"
    assert flow.owner._battery_profile_data["battery_connection_profile"] == "sungrow_ihomemanager_native"
    assert {event[0] for event in flow.events} == {"native_probe", "inverter_probe"}


def test_options_transition_saves_explicit_native_profile_without_enabling_control(flow):
    asyncio.run(flow.ns["async_step_sungrow_connection"](flow.owner, flow.values))
    assert len(flow.saved) == 1
    for key in ("data", "options"):
        assert flow.saved[0][key]["battery_connection_profile"] == "sungrow_ihomemanager_native"
        assert flow.saved[0][key]["sungrow_ihomemanager_host"] == "ihm.test"
        assert flow.saved[0][key]["monitoring_mode"] is True
    assert [event[0] for event in flow.events] == ["inverter_probe", "native_probe", "save", "reload"]


@pytest.mark.parametrize("problem", ["missing_host", "port", "same_endpoint", "unsupported_protocol"])
def test_failed_native_validation_preserves_existing_settings(flow, problem):
    if problem == "missing_host": del flow.values["sungrow_ihomemanager_host"]
    elif problem == "port": flow.values["sungrow_ihomemanager_port"] = 502
    elif problem == "same_endpoint": flow.values.update(sungrow_host="ihm.test", sungrow_port=503)
    elif problem == "unsupported_protocol": flow.snapshot.supports_dispatch = False
    result = asyncio.run(flow.ns["async_step_sungrow_connection"](flow.owner, flow.values))
    assert result["errors"]
    assert not flow.saved
    assert flow.entry.data == {"sungrow_connection_type": "ihomemanager", "monitoring_mode": True}


def test_changing_active_native_route_requires_successful_cleanup(flow):
    flow.entry.data = {**flow.values, "monitoring_mode": False}
    flow.owner.hass.reject_cleanup = True
    flow.values = {**flow.values, "sungrow_connection_type": "direct"}
    result = asyncio.run(flow.ns["async_step_sungrow_connection"](flow.owner, flow.values))
    assert result["abort"]["reason"] == "monitoring_cleanup_failed"
    assert not flow.saved
    assert flow.events == [("cleanup",), ("finish_cleanup",)]


def test_changing_active_native_endpoint_cleans_up_before_save(flow):
    flow.entry.data = {**flow.values, "monitoring_mode": False}
    flow.values = {**flow.values, "sungrow_ihomemanager_host": "new-ihm.test"}
    asyncio.run(flow.ns["async_step_sungrow_connection"](flow.owner, flow.values))
    names = [event[0] for event in flow.events]
    assert names.index("cleanup") < names.index("save")
    assert flow.saved[0]["options"]["sungrow_ihomemanager_host"] == "new-ihm.test"


def test_provider_options_saves_native_endpoints_before_provider_step(flow):
    result = asyncio.run(flow.ns["async_step_init_sungrow"](flow.owner, flow.values))
    assert result == {"provider_step": "amber"}
    assert flow.saved[0]["data"]["battery_connection_profile"] == "sungrow_ihomemanager_native"
    assert flow.saved[0]["options"]["sungrow_ihomemanager_host"] == "ihm.test"
    assert flow.saved[0]["options"]["monitoring_mode"] is True


@pytest.mark.parametrize("step", ["async_step_sungrow", "async_step_sungrow_connection", "async_step_init_sungrow"])
@pytest.mark.parametrize("endpoint", ["native", "inverter"])
@pytest.mark.parametrize("port", [503, 504])
def test_native_setup_identifies_failed_local_endpoint_without_saving(flow, step, endpoint, port):
    flow.values["sungrow_ihomemanager_port"] = port
    flow.probe_errors[endpoint] = (
        ConnectionRefusedError("Local Modbus connection refused") if endpoint == "native" else "cannot_connect"
    )

    result = asyncio.run(flow.ns[step](flow.owner, flow.values))

    error = result["errors"]["base"]
    assert error == ("sungrow_native_cannot_connect" if endpoint == "native" else "sungrow_inverter_cannot_connect")
    # Both initial setup and options must explain the failed local check.
    for path in (COMPONENT / "strings.json", COMPONENT / "translations/en.json"):
        strings = json.loads(path.read_text())
        for section in ("config", "options"):
            message = strings[section]["error"][error]
            assert ("iHomeManager" if endpoint == "native" else "inverter") in message
            assert "HTTPS" not in message and "portal" not in message
    assert not flow.saved
    assert not hasattr(flow.owner, "_sungrow_data")
    assert flow.entry.data == {"sungrow_connection_type": "ihomemanager", "monitoring_mode": True}
    assert flow.entry.options == {}
    assert all(event[0] in ("native_probe", "inverter_probe") for event in flow.events)
    if endpoint == "native":
        assert ("native_probe", "ihm.test", port) in flow.events


@pytest.mark.parametrize("step", ["async_step_sungrow", "async_step_sungrow_connection", "async_step_init_sungrow"])
def test_native_setup_reports_invalid_inverter_battery_read_as_telemetry_failure(flow, step):
    flow.probe_errors["inverter"] = "modbus_conflict"
    result = asyncio.run(flow.ns[step](flow.owner, flow.values))
    assert result["errors"] == {"base": "sungrow_inverter_cannot_connect"}
    assert not flow.saved
