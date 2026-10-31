"""Exercise grid retry routing and late export admission without hardware."""
from __future__ import annotations

import ast
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest


ROOT = Path(__file__).parents[1] / "custom_components/power_sync"


def _load(path, name, namespace):
    tree = ast.parse(path.read_text())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.body = [n for n in node.body if not isinstance(n, ast.ImportFrom)]
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


async def _dispatch_grid_setting(_hass, _entry, *, cloud_call, **kwargs):
    """Minimal dispatch shim: exercise the extracted cloud retry path."""
    return bool(await cloud_call())


async def _record_submission(*args, **kwargs):
    return None


async def _confirmed_export(*args, **kwargs):
    return True


async def _update_cached_export(*args, **kwargs):
    return True


def test_grid_confirmation_budget_is_shared_by_all_cloud_sites():
    clock = SimpleNamespace(now=100.0)
    generation, kick_generation = [1], [1]
    requests = []

    async def confirmed(*args, **kwargs):
        requests.append(kwargs)
        assert kwargs["is_current"]()
        clock.now += 30
        return SimpleNamespace(applied=True)

    namespace = {
        "Any": Any, "Callable": Callable, "time": SimpleNamespace(monotonic=lambda: clock.now),
        "_command_generation": generation, "_tesla_charge_kick_generation": kick_generation,
        "DOMAIN": "power_sync",
        "async_get_clientsession": lambda hass: object(),
        "hass": SimpleNamespace(data={"power_sync": {"entry": {}}}),
        "entry": SimpleNamespace(entry_id="entry", options={"tesla_control_method": "tariff"}, data={}),
        "get_tesla_api_base_url": lambda *args: "https://example.invalid",
        "CONF_FLEET_API_BASE_URL": "fleet_base", "GRID_CONFIRMATION_POLL_OFFSETS": (1, 30, 45, 59),
        "async_set_tesla_grid_charging_confirmed": confirmed,
        "dispatch_powerwall_setting": _dispatch_grid_setting,
        "_local_powerwall_generation_current": lambda: True,
        "_record_tesla_settings_submission": _record_submission,
        "_LOGGER": logging.getLogger(__name__),
    }
    apply = _load(ROOT / "__init__.py", "_tesla_force_apply_grid_charging", namespace)
    sites = [("a", "token", "fleet"), ("b", "token", "fleet")]
    result = asyncio.run(apply(sites, False, reason="force discharge"))
    assert result["confirmed_sites"] == ["a", "b"]
    assert [r["operation_deadline"] for r in requests] == [160, 160]
    assert [r["retry_after"] for r in requests] == [45, 45]
    generation[0] += 1
    assert not requests[-1]["is_current"]()


@pytest.mark.parametrize("reason", ["restore normal", "force charge"])
def test_restore_and_charge_keep_short_confirmation_policy(reason):
    requests = []

    async def confirmed(*args, **kwargs):
        requests.append(kwargs)
        return SimpleNamespace(applied=True)

    namespace = {
        "Any": Any, "Callable": Callable,
        "_command_generation": [1], "_tesla_charge_kick_generation": [1],
        "DOMAIN": "power_sync", "time": SimpleNamespace(monotonic=lambda: 100.0),
        "async_get_clientsession": lambda hass: object(),
        "hass": SimpleNamespace(data={"power_sync": {"entry": {}}}),
        "entry": SimpleNamespace(entry_id="entry", options={"tesla_control_method": "tariff"}, data={}),
        "get_tesla_api_base_url": lambda *args: "https://example.invalid",
        "CONF_FLEET_API_BASE_URL": "fleet_base",
        "async_set_tesla_grid_charging_confirmed": confirmed,
        "dispatch_powerwall_setting": _dispatch_grid_setting,
        "_local_powerwall_generation_current": lambda: True,
        "_record_tesla_settings_submission": _record_submission,
        "_LOGGER": logging.getLogger(__name__),
    }
    apply = _load(ROOT / "__init__.py", "_tesla_force_apply_grid_charging", namespace)
    asyncio.run(apply([("a", "token", "fleet")], True, reason=reason))
    assert set(requests[0]) == {"is_current", "entry_id"}
    assert requests[0]["entry_id"] == "entry"


@pytest.mark.parametrize("change", [
    None, "disabled", "monitoring", "stale", "missing_timestamp", "future_timestamp",
    "plan_changed", "short_window", "price_blocked", "soc_floor", "unsafe_duration", "nan_soc",
])
def test_waiting_tariff_discharge_rechecks_live_eligibility(change):
    now = datetime(2026, 10, 28, 0, tzinfo=timezone.utc)
    data = {"battery_level": 75, "last_update": now}
    if change == "stale":
        data["last_update"] = now - timedelta(seconds=61)
    if change == "missing_timestamp":
        data.pop("last_update")
    if change == "future_timestamp":
        data["last_update"] = now + timedelta(seconds=1)
    if change == "nan_soc":
        data["battery_level"] = math.nan
    coordinator = SimpleNamespace(
        _enabled=change != "disabled",
        _monitoring_mode_active=lambda: change == "monitoring",
        _energy_telemetry_ready=lambda: True,
        _get_current_action=lambda: SimpleNamespace(action="idle" if change == "plan_changed" else "export"),
        _force_duration_for_action_window=lambda *a, **kw: 4 if change == "short_window" else 20,
        _battery_export_price_status=lambda action: {"price_allows_battery_export": change != "price_blocked"},
        _get_energy_data=lambda: data,
        _force_discharge_reserve_floor=lambda action: .2,
        _force_discharge_reaches_reserve=lambda *args: (change == "soc_floor", None),
        _targetless_export_safe_duration=lambda *args: (4 if change == "unsafe_duration" else 20, None),
    )
    check = _load(ROOT / "optimization/coordinator.py", "tesla_tariff_export_still_allowed", {
        "datetime": datetime, "math": math, "dt_util": SimpleNamespace(utcnow=lambda: now),
    })
    assert check(coordinator, 5) is (change is None)


@pytest.mark.parametrize("change", [None, "generation", "monitoring", "setup", "network", "plan", "restore"])
def test_discharge_cannot_change_mode_after_admission_changes_during_grid_wait(change):
    tree = ast.parse((ROOT / "__init__.py").read_text())
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_force_discharge")
    body = next(n.body for n in handler.body if isinstance(n, ast.Try)
                and any(isinstance(c, ast.FunctionDef) and c.name == "_discharge_request_current"
                        for c in n.body))
    first = next(i for i, n in enumerate(body) if isinstance(n, ast.FunctionDef)
                 and n.name == "_discharge_request_current")
    last = next(i for i, n in enumerate(body[first:], first)
                if isinstance(n, ast.If) and ast.unparse(n.test) == "not mode_success")
    function = ast.AsyncFunctionDef(
        name="prepare", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                           kw_defaults=[], defaults=[]),
        body=body[first:last], decorator_list=[],
    )
    flags = {"after_wait": False}
    generation = [1]
    modes = []
    runtime = {}
    persisted = []
    force_state = {
        "saved_operation_mode": "self_consumption",
        "saved_export_rule": "pv_only",
        "saved_states": {"site": {}},
    }
    guard = SimpleNamespace(manager=SimpleNamespace(snapshot=SimpleNamespace(mode="off")))

    async def grid(*args, **kwargs):
        assert persisted == ["awaiting_grid_confirmation"]
        assert kwargs["is_current"]()
        flags["after_wait"] = True
        if change == "generation":
            generation[0] += 1
        if change == "network":
            guard.manager.snapshot.mode = "active"
        if change == "restore":
            runtime["pending_tesla_restore"] = True
        return {"confirmed_sites": ["site"]}

    async def mode(*args, **kwargs):
        modes.append(args)
        return {"confirmed_sites": ["site"]}

    async def persist():
        persisted.append(force_state.get("command_status"))

    async def capture(state, site_configs, *, is_current, capture_export_rule=True):
        # The extracted preparation path normally obtains these from the
        # preceding Tesla site_info capture.
        state["saved_operation_mode"] = "self_consumption"
        state["saved_export_rule"] = "pv_only"

    def changed(name):
        return flags["after_wait"] and change == name

    coordinator = SimpleNamespace(tesla_tariff_export_still_allowed=lambda minutes: not changed("plan"))
    namespace = {
        "_command_generation": generation, "_restore_gen": 1,
        "_tesla_charge_kick_generation": [1],
        "_tesla_operation_generation": [1],
        "was_already_force_discharging": False,
        "asyncio": asyncio,
        "_monitoring_mode_should_block_control": lambda call: changed("monitoring"),
        "_local_powerwall_generation_current": lambda: not changed("setup"),
        "_local_powerwall_runtime": runtime, "network_guard": guard,
        "call": SimpleNamespace(data={}), "source": "optimizer", "duration": 10,
        "hass": SimpleNamespace(data={"power_sync": {"entry": {"optimization_coordinator": coordinator}}}),
        "DOMAIN": "power_sync", "entry": SimpleNamespace(entry_id="entry"),
        "site_configs": [("site", "token", "fleet")],
        "_tesla_force_apply_grid_charging": grid,
        "_tesla_force_result_all_confirmed": lambda result, sites: True,
        "_tesla_force_apply_export_rule": _confirmed_export,
        "_capture_tesla_setting_baselines": capture,
        "update_cached_export_rule": _update_cached_export,
        "_tesla_force_apply_operation_mode": mode, "HomeAssistantError": RuntimeError,
        "_LOGGER": logging.getLogger(__name__),
        "force_discharge_state": force_state, "persist_force_mode_state": persist,
        "dt_util": SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)),
    }
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(ROOT / "__init__.py"), "exec"), namespace)
    if change is None:
        asyncio.run(namespace["prepare"]())
        assert len(modes) == 1
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(namespace["prepare"]())
        assert not modes


@pytest.mark.parametrize("superseded", [False, True])
def test_cancelled_discharge_retains_cleanup_unless_a_newer_command_owns_it(superseded):
    tree = ast.parse((ROOT / "__init__.py").read_text())
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_force_discharge")
    cancelled = next(n for n in ast.walk(handler) if isinstance(n, ast.ExceptHandler)
                     and n.type is not None and ast.unparse(n.type) == "asyncio.CancelledError")
    body = ast.parse("raise asyncio.CancelledError()").body
    function = ast.AsyncFunctionDef(
        name="cancel", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                         kw_defaults=[], defaults=[]),
        body=[ast.Try(body=body, handlers=[cancelled], orelse=[], finalbody=[])],
        decorator_list=[],
    )
    cleanups = []

    async def cleanup(reason):
        await asyncio.sleep(0)
        cleanups.append(reason)

    namespace = {
        "asyncio": asyncio, "tesla_force_discharge_mutated": True,
        "_command_generation": [2 if superseded else 1], "_restore_gen": 1,
        "_cleanup_failed_tesla_force_discharge": cleanup,
    }
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(ROOT / "__init__.py"), "exec"), namespace)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(namespace["cancel"]())
    assert bool(cleanups) is not superseded


def test_preparation_journal_preserves_each_sites_original_settings():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    state = {
        "active": True, "expires_at": now, "command_status": "awaiting_grid_confirmation",
        "saved_tariff": {"name": "primary-original"}, "saved_operation_mode": "self_consumption",
        "saved_backup_reserve": 20, "saved_export_rule": "pv_only",
        "saved_states": {
            "a": {"saved_tariff": {"name": "primary-original"}, "saved_grid_charging_enabled": True},
            "b": {"saved_tariff": {"name": "secondary-original"}, "saved_grid_charging_enabled": False},
        },
    }

    class Store:
        value = None

        async def async_load(self):
            return {}

        async def async_save(self, value):
            self.value = value

    store = Store()
    namespace = {
        "store": store, "force_charge_state": {"active": False}, "force_discharge_state": state,
        "_command_generation": [1], "_local_powerwall_generation_current": lambda: True,
        "_coerce_force_power_w": lambda value: value,
        "_select_restorable_tesla_tariff": lambda value: value,
        "datetime": datetime, "hass": SimpleNamespace(data={}), "DOMAIN": "power_sync",
        "entry": SimpleNamespace(entry_id="entry"), "tesla_grid_charging_preferences": {},
        "_LOGGER": logging.getLogger(__name__),
    }
    async def update_store(_store, updates, *, is_current=None):
        await _store.async_save(updates)
        return True
    namespace["_update_powerwall_store"] = update_store
    persist = _load(ROOT / "__init__.py", "persist_force_mode_state", namespace)
    asyncio.run(persist())
    saved = store.value["force_mode_state"]
    assert saved["saved_states"] == state["saved_states"]
    assert saved["command_status"] == "awaiting_grid_confirmation"
    assert datetime.fromisoformat(saved["expires_at"]) == now
