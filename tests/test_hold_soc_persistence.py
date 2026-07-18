"""Regression tests for the Hold-SoC persistence bug cluster.

Covers three co-designed bugs in the force/hold state machine inside
async_setup_entry (custom_components/power_sync/__init__.py):

- OB-5 (MAJOR): Hold SoC was never persisted, so a restart/reload mid-hold
  left the battery frozen in backup/standby with nothing tracking it.
- OB-7 (MEDIUM): async_unload_entry never cancelled the force/hold expiry
  timers, so an orphaned pre-reload timer could fire against the new setup.
- HD-13: hold_soc_state was never registered into hass.data, so the Battery
  Mode sensor could never show Hold SoC.

Follows the AST/source-extraction pattern used by
tests/test_force_mode_controls.py and tests/test_sungrow_curtailment_runtime.py
for structural assertions, plus behavioral exec() round-trips (as in
test_sungrow_curtailment_runtime.py) for the two persistence functions, since
they're small closures with an enumerable set of free variables.
"""

from __future__ import annotations

import ast
import asyncio
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _find_function(tree: ast.AST, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node
    raise AssertionError(f"{function_name} not found")


def _function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    node = _find_function(tree, name)
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


# ---------------------------------------------------------------------------
# (a) persist_force_mode_state serializes a hold_soc branch with expires_at
# ---------------------------------------------------------------------------

def test_persist_force_mode_state_has_hold_soc_branch_with_expires_at():
    source = _function_source("persist_force_mode_state")

    assert 'elif hold_soc_state["active"]:' in source
    assert '"mode": "hold_soc"' in source
    expected_expires_at_line = (
        '"expires_at": hold_soc_state["expires_at"].isoformat() '
        'if hold_soc_state.get("expires_at") else None'
    )
    assert expected_expires_at_line in source
    assert '"locked_soc": hold_soc_state.get("locked_soc")' in source
    assert '"saved_operation_mode": hold_soc_state.get("saved_operation_mode")' in source
    assert '"saved_backup_reserve": hold_soc_state.get("saved_backup_reserve")' in source

    # The hold branch must exist ahead of the write so hold-only activity no
    # longer falls through to state_to_save staying None (which would
    # clobber any persisted state with None).
    hold_branch_index = source.index('elif hold_soc_state["active"]:')
    write_index = source.index('stored_data["force_mode_state"] = state_to_save')
    assert hold_branch_index < write_index


def test_persist_force_mode_state_hold_soc_round_trip_writes_expected_blob():
    """Behavioral: exec the real function body and confirm the actual blob."""
    source = textwrap.dedent(_function_source("persist_force_mode_state"))

    saved: dict = {}

    class _FakeStore:
        async def async_load(self):
            return {"some_other_persisted_key": "unchanged"}

        async def async_save(self, data):
            saved.clear()
            saved.update(data)

    expires_at = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
    namespace = {
        "_LOGGER": SimpleNamespace(debug=lambda *a, **k: None),
        "store": _FakeStore(),
        "force_charge_state": {"active": False},
        "force_discharge_state": {"active": False},
        "hold_soc_state": {
            "active": True,
            "expires_at": expires_at,
            "locked_soc": 61.5,
            "saved_operation_mode": "autonomous",
            "saved_backup_reserve": 15,
        },
    }
    exec(source, namespace)
    asyncio.run(namespace["persist_force_mode_state"]())

    # Untouched keys in the stored blob must survive (no clobbering).
    assert saved["some_other_persisted_key"] == "unchanged"

    blob = saved["force_mode_state"]
    assert blob is not None, "hold-only activity must not persist None"
    assert blob["mode"] == "hold_soc"
    assert blob["expires_at"] == expires_at.isoformat()
    assert blob["locked_soc"] == 61.5
    assert blob["saved_operation_mode"] == "autonomous"
    assert blob["saved_backup_reserve"] == 15


# ---------------------------------------------------------------------------
# (b) restore_force_mode_from_persistence handles mode == "hold_soc" with
# both a future-expiry re-arm and an expired-restore path
# ---------------------------------------------------------------------------

def test_restore_force_mode_from_persistence_handles_hold_soc_mode():
    source = _function_source("restore_force_mode_from_persistence")

    assert 'if mode == "hold_soc":' in source
    hold_section = source.split('if mode == "hold_soc":', 1)[1].split(
        "if _is_monitoring_mode():", 1
    )[0]

    # Future-expiry: re-arm state + timer + dispatcher, no hardware replay
    # (the inverter/gateway stays in standby across an HA restart on its
    # own — see AGENTS.md per-brand notes on Sigenergy STANDBY / FoxESS
    # Backup / Sungrow cap-0 / GoodWe ECO).
    assert 'hold_soc_state["active"] = True' in hold_section
    assert 'hold_soc_state["cancel_expiry_timer"] = async_track_point_in_utc_time(' in hold_section
    assert 'async_dispatcher_send(hass, f"{DOMAIN}_hold_soc_state"' in hold_section

    # Expired: issue the brand restore via restore_normal as forced cleanup,
    # using the existing retry-safe handle_restore_normal contract rather
    # than reimplementing retries here.
    assert "if now >= expires_at:" in hold_section
    assert '{"source": "hold_soc_cleanup", "_force_restore": True}' in hold_section
    assert 'if not hold_soc_state.get("active"):' in hold_section
    assert 'stored_data["force_mode_state"] = None' in hold_section

    # Must return before falling into the charge/discharge-only logic below
    # (which assumes mode is "charge" or "discharge" and would misroute a
    # hold into force_discharge_state).
    assert hold_section.rstrip().endswith("return")


def test_restore_force_mode_from_persistence_hold_soc_future_expiry_rearms_timer_and_dispatch():
    """Behavioral: a still-active persisted hold re-arms state/timer/dispatch."""
    source = textwrap.dedent(_function_source("restore_force_mode_from_persistence"))

    fixed_now = datetime(2026, 7, 8, 10, 0, 0, tzinfo=timezone.utc)
    expires_at = fixed_now + timedelta(minutes=20)

    dispatch_calls: list[tuple[str, dict]] = []
    timer_calls: list[tuple[object, datetime]] = []
    errors: list[str] = []

    def _fake_dispatcher_send(hass_arg, signal, payload):
        dispatch_calls.append((signal, payload))

    def _fake_async_track_point_in_utc_time(hass_arg, callback, when):
        timer_calls.append((callback, when))
        return lambda: timer_calls.append(("cancelled", when))

    namespace = {
        "persisted_force_state": {
            "mode": "hold_soc",
            "expires_at": expires_at.isoformat(),
            "locked_soc": 55.0,
            "saved_operation_mode": "autonomous",
            "saved_backup_reserve": 20,
        },
        "datetime": datetime,
        "dt_util": SimpleNamespace(utcnow=lambda: fixed_now, UTC=timezone.utc),
        "_coerce_force_power_w": lambda v: 0,
        "hold_soc_state": {
            "active": False,
            "saved_operation_mode": None,
            "saved_backup_reserve": None,
            "expires_at": None,
            "cancel_expiry_timer": None,
            "locked_soc": None,
        },
        "_command_generation": [0],
        "async_track_point_in_utc_time": _fake_async_track_point_in_utc_time,
        "async_dispatcher_send": _fake_dispatcher_send,
        "DOMAIN": "power_sync",
        "hass": SimpleNamespace(),
        "_LOGGER": SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda msg, *a, **k: errors.append(msg),
        ),
    }
    exec(source, namespace)
    asyncio.run(namespace["restore_force_mode_from_persistence"]())

    assert not errors, f"restore_force_mode_from_persistence raised: {errors}"
    hold_state = namespace["hold_soc_state"]
    assert hold_state["active"] is True
    assert hold_state["expires_at"] == expires_at
    assert hold_state["locked_soc"] == 55.0
    assert hold_state["saved_operation_mode"] == "autonomous"
    assert hold_state["saved_backup_reserve"] == 20
    assert hold_state["cancel_expiry_timer"] is not None

    assert len(timer_calls) == 1
    _callback, scheduled_for = timer_calls[0]
    assert scheduled_for == expires_at

    assert len(dispatch_calls) == 1
    signal, payload = dispatch_calls[0]
    assert signal == "power_sync_hold_soc_state"
    assert payload["active"] is True
    assert payload["locked_soc"] == 55.0


def test_restore_force_mode_from_persistence_hold_soc_expired_restores_normal():
    """Behavioral: an expired persisted hold calls restore_normal(source=user)
    and clears the persisted blob, without reimplementing brand-specific
    restore logic (that's handle_restore_normal's job)."""
    source = textwrap.dedent(_function_source("restore_force_mode_from_persistence"))

    fixed_now = datetime(2026, 7, 8, 10, 0, 0, tzinfo=timezone.utc)
    expires_at = fixed_now - timedelta(minutes=5)  # already expired

    service_calls: list[tuple[str, str, dict]] = []
    active_before_call: list[bool] = []
    errors: list[str] = []

    hold_state = {
        "active": False,
        "saved_operation_mode": None,
        "saved_backup_reserve": None,
        "expires_at": None,
        "cancel_expiry_timer": None,
        "locked_soc": None,
    }

    class _FakeServices:
        async def async_call(self, domain, service, data, blocking=True):
            service_calls.append((domain, service, data))
            active_before_call.append(hold_state["active"])
            hold_state["active"] = False

    class _FakeStore:
        def __init__(self):
            self.saved = None

        async def async_load(self):
            return {"force_mode_state": {"mode": "hold_soc"}}

        async def async_save(self, data):
            self.saved = data

    fake_store = _FakeStore()

    namespace = {
        "persisted_force_state": {
            "mode": "hold_soc",
            "expires_at": expires_at.isoformat(),
            "locked_soc": 40.0,
            "saved_operation_mode": "autonomous",
            "saved_backup_reserve": 10,
        },
        "datetime": datetime,
        "dt_util": SimpleNamespace(utcnow=lambda: fixed_now, UTC=timezone.utc),
        "_coerce_force_power_w": lambda v: 0,
        "hold_soc_state": hold_state,
        "hass": SimpleNamespace(services=_FakeServices()),
        "DOMAIN": "power_sync",
        "SERVICE_RESTORE_NORMAL": "restore_normal",
        "store": fake_store,
        "_LOGGER": SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda msg, *a, **k: errors.append(msg),
        ),
    }
    exec(source, namespace)
    asyncio.run(namespace["restore_force_mode_from_persistence"]())

    assert not errors, f"restore_force_mode_from_persistence raised: {errors}"
    # hold_soc_state is populated before the restore call so
    # handle_restore_normal's restore_was_hold_soc branch can do a full
    # cleanup, mirroring the charge/discharge expired path.
    assert active_before_call == [True]
    assert namespace["hold_soc_state"]["active"] is False

    assert len(service_calls) == 1
    domain, service, data = service_calls[0]
    assert domain == "power_sync"
    assert service == "restore_normal"
    assert data == {"source": "hold_soc_cleanup", "_force_restore": True}

    assert fake_store.saved == {"force_mode_state": None}


# ---------------------------------------------------------------------------
# (c) async_unload_entry cancels the expiry timers on all three state dicts
# ---------------------------------------------------------------------------

def test_async_unload_entry_cancels_force_and_hold_timers():
    source = _function_source("async_unload_entry")

    assert (
        'for _state_key in ("force_charge_state", "force_discharge_state", "hold_soc_state", "self_consumption_state"):'
        in source
    )
    assert 'for _timer_key in ("cancel_expiry_timer", "cancel_hardware_refresh_timer"):' in source
    assert "if callable(_cancel):" in source

    cancel_index = source.index(
        'for _state_key in ("force_charge_state", "force_discharge_state", "hold_soc_state", "self_consumption_state"):'
    )
    unload_platforms_index = source.index("async_unload_platforms")
    assert cancel_index < unload_platforms_index


# ---------------------------------------------------------------------------
# (d) hold_soc_state is registered into hass.data (HD-13)
# ---------------------------------------------------------------------------

def test_hold_soc_state_registered_into_hass_data():
    source = _function_source("async_setup_entry")

    assert 'hass.data[DOMAIN][entry.entry_id]["hold_soc_state"] = hold_soc_state' in source

    force_charge_index = source.index(
        'hass.data[DOMAIN][entry.entry_id]["force_charge_state"] = force_charge_state'
    )
    force_discharge_index = source.index(
        'hass.data[DOMAIN][entry.entry_id]["force_discharge_state"] = force_discharge_state'
    )
    hold_index = source.index(
        'hass.data[DOMAIN][entry.entry_id]["hold_soc_state"] = hold_soc_state'
    )
    assert force_charge_index < hold_index
    assert force_discharge_index < hold_index
