"""Regression tests for unfinished Tesla restores surviving a restart.

When a restore_normal's Tesla writes fail, the handler schedules an in-process
retry 60 seconds out. That timer dies with the process, and its callback also
self-skips once force state has been cleared — so on a restart-triggered
restore the retry could never run and the failure was abandoned silently,
leaving the hardware half-restored with nothing tracking it.

Observed live on 2026-08-19: a HACS update restarted HA mid force-charge, the
grid-charging restore failed its readback, "retry 1 scheduled in 60 seconds"
was logged, and nothing ever completed it.

Follows the AST/source-extraction pattern used by tests/test_force_mode_controls.py
and tests/test_hold_soc_persistence.py.
"""

from __future__ import annotations

import ast
import asyncio
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import logging
import pytest


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    node = _find_function(ast.parse(source), name)
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


class _Store:
    """Minimal Store double recording what was written."""

    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.saves = 0

    async def async_load(self):
        return dict(self.data)

    async def async_save(self, data):
        self.data = dict(data)
        self.saves += 1


def _build_persist_helper(store: _Store, entry_data: dict):
    """exec persist_pending_tesla_restore with its free variables injected."""
    namespace: dict = {
        "store": store,
        "hass": SimpleNamespace(data={"power_sync": {"entry-1": entry_data}}),
        "entry": SimpleNamespace(entry_id="entry-1"),
        "DOMAIN": "power_sync",
        "dt_util": SimpleNamespace(
            utcnow=lambda: datetime(2026, 8, 19, 2, 28, tzinfo=timezone.utc)
        ),
        "_LOGGER": SimpleNamespace(
            warning=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        ),
    }
    exec(textwrap.dedent(_function_source("persist_pending_tesla_restore")), namespace)
    return namespace["persist_pending_tesla_restore"]


# ---------------------------------------------------------------------------
# Persisting and clearing the marker
# ---------------------------------------------------------------------------


def test_failed_restore_is_recorded_for_the_next_startup():
    store = _Store()
    entry_data: dict = {}
    persist = _build_persist_helper(store, entry_data)

    asyncio.run(persist("one or more Tesla restore writes failed"))

    marker = store.data["pending_tesla_restore"]
    assert marker["reason"] == "one or more Tesla restore writes failed"
    assert marker["recorded_at"].startswith("2026-08-19")
    # Also mirrored into hass.data so a same-process reload sees it.
    assert entry_data["pending_tesla_restore"] == marker


def test_marker_carries_the_backup_reserve_skip_flag():
    store = _Store()
    persist = _build_persist_helper(store, {})

    asyncio.run(persist("reserve write failed", skip_backup_reserve_restore=True))

    assert store.data["pending_tesla_restore"]["_skip_backup_reserve_restore"] is True


def test_successful_restore_clears_the_marker():
    store = _Store({"pending_tesla_restore": {"reason": "stale"}})
    entry_data = {"pending_tesla_restore": {"reason": "stale"}}
    persist = _build_persist_helper(store, entry_data)

    asyncio.run(persist(None))

    assert store.data["pending_tesla_restore"] is None
    assert entry_data["pending_tesla_restore"] is None


# ---------------------------------------------------------------------------
# Structural guarantees in the handler and the startup path
# ---------------------------------------------------------------------------


def test_restore_failure_persists_before_relying_on_the_retry_timer():
    """The marker must not be conditional on the in-process retry surviving."""
    source = _function_source("handle_restore_normal")

    failure_branch = source[source.index("if tesla_restore_failed:"):]
    persist_at = failure_branch.index("persist_pending_tesla_restore")
    retry_at = failure_branch.index("_schedule_tesla_restore_retry")
    # Persisted first, and outside the `if _schedule...` conditional.
    assert persist_at < retry_at


def test_completed_restore_clears_the_marker():
    source = _function_source("handle_restore_normal")

    # The Tesla success line carries no brand prefix, unlike the per-brand
    # paths above it (Sigenergy, FoxESS, ...) which never set this marker.
    tesla_success = '_LOGGER.info("NORMAL OPERATION RESTORED")'
    assert "await persist_pending_tesla_restore(None)" in source
    assert source.index("await persist_pending_tesla_restore(None)") < source.index(
        tesla_success
    )


def test_only_the_tesla_path_owns_the_marker():
    """Per-brand restores never set the marker, so they must not clear it."""
    source = _function_source("handle_restore_normal")

    assert source.count("persist_pending_tesla_restore") == 2  # one set, one clear


def test_startup_finishes_an_unfinished_restore_without_force_state():
    """The hole: force_mode_state null but a restore still owed."""
    source = _function_source("restore_force_mode_from_persistence")

    guard_start = source.index("if not persisted_force_state:")
    guard = source[guard_start : source.index("return", guard_start)]
    assert "if not persisted_force_state:" in guard
    assert "persisted_pending_restore" in guard
    assert "SERVICE_RESTORE_NORMAL" in source
    assert "startup_pending_restore" in source


def test_startup_marker_is_loaded_from_storage():
    source = INIT_PATH.read_text()

    assert 'stored_data.get("pending_tesla_restore")' in source
    assert '"pending_tesla_restore": pending_tesla_restore,' in source
    assert 'hass.data[DOMAIN][entry.entry_id].get("pending_tesla_restore")' in source


@pytest.mark.parametrize("verified", [False, True])
def test_dynamic_restore_retains_cleanup_until_tariff_upload_verifies(verified):
    """A normally returning sync service can still have failed its 429/readback."""
    tree = ast.parse(INIT_PATH.read_text())
    handler = _find_function(tree, "handle_restore_normal")
    dynamic = next(n for n in ast.walk(handler) if isinstance(n, ast.If)
                   and isinstance(n.test, ast.Compare)
                   and isinstance(n.test.left, ast.Name)
                   and n.test.left.id == "electricity_provider"
                   and ast.unparse(n.test) == "electricity_provider in dynamic_providers")
    failure = next(n for n in ast.walk(handler) if isinstance(n, ast.If)
                   and isinstance(n.test, ast.Name) and n.test.id == "tesla_restore_failed")
    # Keep the actual dynamic branch and terminal cleanup; other settings have
    # successful readbacks in this fixture and cannot hide a failed tariff.
    terminal = next(n for n in ast.walk(handler) if isinstance(n, ast.Try)
                    and failure in n.body)
    end = next(i for i, n in enumerate(terminal.body)
               if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
               and isinstance(n.value.value, ast.Call)
               and isinstance(n.value.value.func, ast.Name)
               and n.value.value.func.id == "persist_pending_tesla_restore")
    body = [dynamic] + terminal.body[terminal.body.index(failure):end + 1]
    node = ast.AsyncFunctionDef(name="run", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=body, decorator_list=[])
    discharge = {"active": True, "saved_tariff": {"name": "original"}}
    data = {}
    store = _Store()
    retries = []
    env = dict(
        electricity_provider="amber", dynamic_providers=("amber", "flow_power", "octopus"),
        allow_monitoring_restore=False, force_mode_cleanup_restore=True,
        hass=SimpleNamespace(data={"power_sync": {"entry-1": data}}),
        entry=SimpleNamespace(entry_id="entry-1"), DOMAIN="power_sync",
        SERVICE_SYNC_TOU="sync_tou", force_discharge_state=discharge,
        force_charge_state={"active": False}, tesla_restore_failed=False,
        skip_backup_reserve_restore=False, confirm_restore=False, restore_was_hold_soc=False,
        _restore_superseded=lambda _: False, _LOGGER=logging.getLogger(__name__),
        persist_pending_tesla_restore=_build_persist_helper(store, data),
        _schedule_tesla_restore_retry=lambda reason: retries.append(reason) or True,
    )
    async def sync(**kwargs):
        return verified
    async def service(*args, **kwargs):
        # Existing HA service does not propagate the upload's Boolean result.
        return None
    async def persist_force():
        store.data["force_mode_state"] = dict(discharge)
    def mark_failed(reason):
        env["tesla_restore_failed"] = True
    env.update(handle_sync_rest_api_check=sync, persist_force_mode_state=persist_force,
               _mark_tesla_restore_failed=mark_failed)
    env["hass"].services = SimpleNamespace(async_call=service)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "<dynamic-restore-completion>", "exec"), env)
    asyncio.run(env["run"]())
    assert discharge["active"] is (not verified)
    assert bool(store.data.get("pending_tesla_restore")) is (not verified)
    assert bool(retries) is (not verified)
    if not verified:
        assert discharge["saved_tariff"] == {"name": "original"}
        assert store.data["force_mode_state"]["active"] is True


@pytest.mark.parametrize("pending", [False, True])
def test_tesla_startup_requires_completed_restore_not_service_return(pending):
    data = {"pending_tesla_restore": {"reason": "readback failed"} if pending else None}
    discharge = {"active": pending}
    async def service(*args, **kwargs):
        return None
    async def ready(operation):
        return True
    env = dict(
        active_battery_system="tesla", force_charge_state={"active": False},
        force_discharge_state=discharge, entry=SimpleNamespace(entry_id="entry-1"),
        hass=SimpleNamespace(data={"power_sync": {"entry-1": data}},
                             services=SimpleNamespace(async_call=service)),
        DOMAIN="power_sync", SERVICE_RESTORE_NORMAL="restore_normal",
        _native_battery_control_coordinator=lambda: None,
        _wait_for_native_battery_control_ready=ready, _LOGGER=logging.getLogger(__name__),
    )
    exec(textwrap.dedent(_function_source("_restore_persisted_normal")), env)
    assert asyncio.run(env["_restore_persisted_normal"]({"source": "optimizer"})) is (not pending)


def test_pending_only_restart_blocks_optimizer_and_forces_cleanup():
    data = {"pending_tesla_restore": {"reason": "interrupted restore"}}
    calls = []
    async def service(*args, **kwargs):
        calls.append(args[2])
    env = dict(
        active_battery_system="tesla", BATTERY_SYSTEM_SOLAREDGE="solaredge",
        persisted_force_state={}, persisted_pending_restore=data["pending_tesla_restore"],
        entry=SimpleNamespace(entry_id="entry-1"), DOMAIN="power_sync",
        SERVICE_RESTORE_NORMAL="restore_normal", _LOGGER=logging.getLogger(__name__),
        hass=SimpleNamespace(data={"power_sync": {"entry-1": data}},
                             services=SimpleNamespace(async_call=service)),
    )
    exec("from __future__ import annotations\n" + textwrap.dedent(
        _function_source("restore_force_mode_from_persistence")), env)
    asyncio.run(env["restore_force_mode_from_persistence"]())
    assert data.get("optimizer_force_restart_restore_pending") is True
    assert calls[0]["_force_restore"] is True


@pytest.mark.parametrize("superseded", [False, True])
def test_only_current_verified_completion_releases_restart_gate(superseded):
    data = {"optimizer_force_restart_restore_pending": True}
    handler = _find_function(ast.parse(INIT_PATH.read_text()), "handle_restore_normal")
    terminal = next(n for n in ast.walk(handler) if isinstance(n, ast.Try)
                    and any(isinstance(s, ast.If) and isinstance(s.test, ast.Name)
                            and s.test.id == "tesla_restore_failed" for s in n.body))
    start = next(i for i, n in enumerate(terminal.body)
                 if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
                 and isinstance(n.value.value, ast.Call)
                 and isinstance(n.value.value.func, ast.Name)
                 and n.value.value.func.id == "persist_pending_tesla_restore")
    end = next(i for i, n in enumerate(terminal.body[start:], start)
               if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
               and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "info")
    node = ast.AsyncFunctionDef(name="run", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=terminal.body[start:end], decorator_list=[])
    generation = [1]
    persist = _build_persist_helper(_Store(), data)
    async def persist_during_handoff(marker):
        await persist(marker)
        await asyncio.sleep(0)
        if superseded:
            generation[0] += 1  # New force command while storage yielded.
    env = dict(hass=SimpleNamespace(data={"power_sync": {"entry-1": data}}),
               entry=SimpleNamespace(entry_id="entry-1"), DOMAIN="power_sync",
               _restore_superseded=lambda _: generation[0] != 1,
               persist_pending_tesla_restore=persist_during_handoff)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "<verified-restore-completion>", "exec"), env)
    asyncio.run(env["run"]())
    assert data["optimizer_force_restart_restore_pending"] is superseded


@pytest.mark.parametrize("periodic,expected_cap", [(True, 60.0), (False, None)])
@pytest.mark.parametrize("site_results", [[True, True], [True, False], [False, False]])
def test_sync_reports_all_site_upload_results_to_restore(site_results, periodic, expected_cap):
    """Periodic syncs cap Retry-After; restore and manual syncs do not."""
    tree = ast.parse(INIT_PATH.read_text())
    sync = _find_function(tree, "_handle_sync_tou_internal")
    begin = next(i for i, n in enumerate(sync.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "success" for t in n.targets))
    loop = next(n for n in sync.body[begin:] if isinstance(n, ast.For))
    node = ast.AsyncFunctionDef(name="run", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=sync.body[begin:sync.body.index(loop) + 1] + [sync.body[-1]], decorator_list=[])
    async def upload(hass, site, tariff, token, provider, **kwargs):
        assert kwargs["is_current"]()
        assert kwargs["max_retry_after_seconds"] == expected_cap
        return site_results[int(site)]
    env = dict(site_configs=[("0", "fake", "teslemetry"), ("1", "fake", "teslemetry")],
               hass=None, tariff={}, entry=SimpleNamespace(data={}), CONF_FLEET_API_BASE_URL="base",
               send_tariff_to_tesla=upload, _command_generation=[1], sync_command_generation=1,
               TESLA_TOU_READBACK_ZERO_NORMALIZED="normalized", _LOGGER=logging.getLogger(__name__),
               periodic=periodic, TOU_PERIODIC_SYNC_MAX_RETRY_AFTER_SECONDS=60.0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "<sync-upload-result>", "exec"), env)
    assert asyncio.run(env["run"]()) is all(site_results)


@pytest.mark.parametrize("verified", [False, True])
def test_restore_sync_check_propagates_result(verified):
    async def sync(*args, **kwargs):
        return verified
    env = dict(amber_coordinator=True, _handle_sync_tou_internal=sync,
               _LOGGER=logging.getLogger(__name__))
    exec(textwrap.dedent(_function_source("handle_sync_rest_api_check")), env)
    assert asyncio.run(env["handle_sync_rest_api_check"]()) is verified


def test_pending_only_restore_retry_runs_without_force_state():
    scheduled = []
    calls = []
    async def service(domain, service, data, **kwargs):
        calls.append(data)
    def track(hass, callback, when):
        scheduled.append(callback)
    env = dict(
        restore_retry_count=0, restore_was_force_charging=False,
        force_charge_state={"active": False}, force_discharge_state={"active": False},
        hass=SimpleNamespace(data={"power_sync": {"entry-1": {
            "pending_tesla_restore": {"reason": "interrupted restore"},
        }}}, services=SimpleNamespace(async_call=service)),
        entry=SimpleNamespace(entry_id="entry-1"), DOMAIN="power_sync",
        _command_generation=[2], skip_backup_reserve_restore=False,
        SERVICE_RESTORE_NORMAL="restore_normal", call=None,
        dt_util=SimpleNamespace(utcnow=lambda: datetime(2026, 10, 21, tzinfo=timezone.utc)),
        timedelta=timedelta, async_track_point_in_utc_time=track,
        _LOGGER=logging.getLogger(__name__),
    )
    exec(textwrap.dedent(_function_source("_schedule_tesla_restore_retry")), env)
    assert env["_schedule_tesla_restore_retry"]("readback failed") is True
    asyncio.run(scheduled[0](None))
    assert len(calls) == 1
    assert calls[0]["_force_restore"] is True
    assert calls[0]["_tesla_restore_generation"] == 2
