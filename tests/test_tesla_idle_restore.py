"""Exercise idle Tesla restore and genuine recovery at the tariff boundary."""
from __future__ import annotations

import ast
import asyncio
import copy
import logging
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from conftest import load_power_sync_submodule

ROOT = Path(__file__).resolve().parents[1]


class TariffBoundary(BaseException):
    """Stop the real handler before any transport or later device writes."""


@pytest.fixture
def restore_probe():
    component = ROOT / "custom_components/power_sync"
    tree = ast.parse((component / "__init__.py").read_text())
    names = {
        "_iter_tariff_strings", "_is_powersync_force_tariff",
        "_select_restorable_tesla_tariff", "_track_tesla_settings_control",
        "_local_powerwall_generation_current", "handle_restore_normal",
    }
    functions = [node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
    functions.sort(key=lambda node: node.lineno)
    # Give lazy relative imports the same real module boundary as production.
    load_power_sync_submodule("powerwall_local.native_max_charge", "_idle_restore_probe")
    ns = {
        "__name__": "_idle_restore_probe", "__package__": "_idle_restore_probe",
        "asyncio": asyncio, "copy": copy, "_LOGGER": logging.getLogger(__name__),
        "_FORCE_TARIFF_TEXT_MARKERS": ("force charge", "force discharge"),
        "_FORCE_TARIFF_CODE_PREFIXES": ("charge_", "discharge_"),
        "_MANAGER_OWN_TARIFF_CODES": ("aemo-spike", "octopus-saving-session"),
        "HomeAssistantError": RuntimeError,
    }
    for node in ast.parse((component / "const.py").read_text()).body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    ns[target.id] = value
    cached = {"code": "POWER_SYNC:OCTOPUS", "name": "Octopus Energy (PowerSync)"}
    custom = {"code": "EDF_CUSTOM", "name": "EDF Custom"}
    entry = NS(entry_id="test", data={"battery_system": "tesla"},
               options={"electricity_provider": "other"})
    calls = []
    sessions = []

    async def sender(hass, site, tariff, *args, **kwargs):
        assert kwargs["is_current"]()
        calls.append(tariff)
        raise TariffBoundary

    async def confirmed_mode(*args, **kwargs):
        return True

    async def no_local_dispatch():
        return False

    hass = NS(data={"power_sync": {"test": {"tesla_coordinator": True}}})
    ns.update(
        entry=entry, hass=hass,
        force_charge_state={"active": False}, force_discharge_state={"active": False},
        hold_soc_state={"active": False}, self_consumption_state={"active": False},
        _command_generation=[0], _tesla_reserve_generation=[0],
        _stop_local_powerwall_dispatch=no_local_dispatch,
        _control_call_source=lambda call: "user",
        _sigenergy_restore_native_control=lambda call: False,
        _monitoring_mode_should_block_control=lambda call: False,
        _is_monitoring_mode=lambda: False,
        _uses_native_battery_integration=lambda coordinator: False,
        _cancel_all_force_timers=lambda reason: None,
        _get_neovolt_entry_ids=lambda *args: [],
        _cached_restorable_tesla_tariff=lambda: cached,
        _configured_restorable_tesla_tariff=lambda: custom,
        _get_tesla_site_configs=lambda *args: [("site", "fake-token", "powersync")],
        async_get_clientsession=lambda hass: sessions.append(hass),
        send_tariff_to_tesla=sender,
        _tesla_force_apply_operation_mode=confirmed_mode,
        _tesla_force_result_all_confirmed=lambda *args: True,
        persist_force_mode_state=confirmed_mode,
        _local_powerwall_runtime=hass.data["power_sync"]["test"],
        active_battery_system=ns["BATTERY_SYSTEM_TESLA"],
    )
    # The real decorator binds hass/entry at decoration time, so exec after seeding.
    module = ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0,
    ), *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<tesla-restore>", "exec"), ns)

    def run(**data):
        call = NS(data={"source": "user", **data}, context=NS(user_id=None, parent_id=None))
        try:
            return asyncio.run(ns["handle_restore_normal"](call))
        except TariffBoundary:
            return None

    return NS(ns=ns, calls=calls, sessions=sessions, cached=cached, custom=custom, run=run)


@pytest.mark.parametrize("configured", [False, True])
def test_repeated_idle_off_does_not_upload_historical_tariff(restore_probe, configured):
    probe = restore_probe
    if not configured:
        probe.ns["_configured_restorable_tesla_tariff"] = lambda: None
    probe.run()
    probe.run()
    assert probe.calls == []
    assert probe.sessions == []


@pytest.mark.parametrize("provider", ["other", "tou_only"])
def test_custom_force_recovery_prefers_configured_tariff_to_cache(restore_probe, provider):
    probe = restore_probe
    probe.ns["entry"].options["electricity_provider"] = provider
    probe.ns["force_charge_state"]["active"] = True
    probe.run()
    assert probe.calls == [probe.custom]


def test_active_force_restores_its_captured_baseline(restore_probe):
    probe = restore_probe
    baseline = {"name": "Live EDF baseline"}
    probe.ns["force_discharge_state"].update(active=True, saved_tariff=baseline)
    probe.run()
    assert probe.calls == [baseline]


def test_inactive_saved_episode_still_recovers(restore_probe):
    probe = restore_probe
    probe.ns["force_charge_state"]["saved_tariff"] = probe.custom
    probe.run()
    assert probe.calls == [probe.custom]


@pytest.mark.parametrize("fallback_available", [False, True])
def test_inactive_per_site_snapshot_still_recovers(restore_probe, fallback_available):
    probe = restore_probe
    baseline = {"name": "Site baseline"}
    if not fallback_available:
        probe.ns["_cached_restorable_tesla_tariff"] = lambda: None
        probe.ns["_configured_restorable_tesla_tariff"] = lambda: None
    probe.ns["force_charge_state"]["saved_states"] = {"site": {"saved_tariff": baseline}}
    probe.run()
    assert probe.calls == [baseline]


def test_pending_restore_survives_restart_without_active_force(restore_probe):
    probe = restore_probe
    probe.ns["hass"].data["power_sync"]["test"]["pending_tesla_restore"] = {"reason": "failed restore"}
    probe.run()
    assert probe.calls == [probe.custom]


def test_explicit_recovery_can_fall_back_to_cached_tariff(restore_probe):
    probe = restore_probe
    probe.ns["_configured_restorable_tesla_tariff"] = lambda: None
    probe.run(_force_restore=True)
    assert probe.calls == [probe.cached]


def test_monitoring_still_blocks_restore(restore_probe):
    probe = restore_probe
    probe.ns["force_charge_state"]["active"] = True
    probe.ns["_monitoring_mode_should_block_control"] = lambda call: True
    probe.run()
    assert probe.calls == []


def test_hold_cleanup_restores_only_reserve_despite_cached_tariff(restore_probe):
    probe = restore_probe
    reserves = []
    probe.ns["hold_soc_state"].update(active=True, brand="tesla", saved_backup_reserve=15)

    async def pulse(sites, reserve, **kwargs):
        assert kwargs["is_current"]()
        reserves.append(reserve)
        return True

    async def confirmed(*args, **kwargs):
        return True

    probe.ns.update(
        _normalize_tesla_backup_reserve_percent=int,
        _tesla_force_pulse_backup_reserve=pulse,
        _tesla_force_confirm_backup_reserve=confirmed,
        get_tesla_api_base_url=lambda *args: "https://unused.invalid",
        _clear_hold_soc_state=lambda: probe.ns["hold_soc_state"].update(active=False),
        persist_force_mode_state=confirmed,
    )
    probe.run()
    assert reserves == [15]
    assert probe.calls == []
    assert probe.ns["hold_soc_state"]["active"] is False


def test_self_consumption_expiry_still_releases_override(restore_probe):
    probe = restore_probe
    probe.ns["self_consumption_state"]["active"] = True
    probe.ns["_clear_self_consumption_state"] = lambda: probe.ns["self_consumption_state"].update(active=False)
    probe.run()
    assert probe.calls == [probe.custom]
    assert probe.ns["self_consumption_state"]["active"] is False
