"""Regression coverage for PW-9/PW-10/PW-11: manual-override / force-toggle
flags must be set BEFORE any await point in the post-write branches of
`handle_set_grid_export` and `handle_set_operation_mode`, not after.

PW-9: `update_cached_export_rule` performs unguarded store I/O; if it raises,
the exception propagates out of `handle_set_grid_export` before the
`manual_export_override` / `manual_export_rule` flags are set, even though
the Tesla write already succeeded.

PW-10: even when the store write succeeds, the flags are set only after two
awaited calls (`update_cached_export_rule`, `refresh_powerwall_local_after_settings_write`).
A concurrently-scheduled curtailment cycle can observe `manual_export_override`
still `False` during that window and revert the user's rule.

PW-11: `handle_set_operation_mode` pops `last_force_toggle_time` only after an
awaited refresh call. A concurrently-scheduled force-mode-toggle check (run
from the periodic TOU sync, a separate asyncio task) can observe the toggle
timestamp still set during that window and force-revert the user's
just-applied self_consumption mode.

These tests exec the extracted post-write branches against fake hass/store
objects (the AGENTS.md-sanctioned pattern from
tests/test_sungrow_curtailment_runtime.py / tests/test_curtailment_restore_manual_override.py)
and assert on actual flag state at the moments that matter -- not just
source-text substrings.
"""

from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)) and child.name == name:
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{name} not found")


def _grid_export_post_write_branch() -> str:
    """Isolate the 'if success:' branch of handle_set_grid_export (everything
    that runs after the Tesla write already succeeded) and wrap it as a
    standalone async function taking its free variables as args."""
    handler = _function_source("handle_set_grid_export")
    start = handler.index("if success:", handler.index("dispatch_powerwall_write("))
    end = handler.index("except Exception as e:")
    branch = handler[start:end]
    wrapped = (
        "async def _post_write(hass, entry, DOMAIN, rule, update_cached_export_rule,\n"
        "                       refresh_powerwall_local_after_settings_write,\n"
        "                       CONF_BATTERY_CURTAILMENT_ENABLED, _LOGGER):\n"
        "    success = True\n"
    ) + textwrap.indent(textwrap.dedent(branch), "    ")
    return wrapped


def _operation_mode_post_write_branch() -> str:
    """Isolate the 'if success:' branch of handle_set_operation_mode and wrap
    it as a standalone async function taking its free variables as args."""
    handler = _function_source("handle_set_operation_mode")
    start = handler.index("if success:")
    end = handler.index("else:\n                raise HomeAssistantError")
    branch = handler[start:end]
    wrapped = (
        "async def _post_write(hass, entry, DOMAIN, mode,\n"
        "                       refresh_powerwall_local_after_settings_write, _LOGGER):\n"
        "    success = True\n"
    ) + textwrap.indent(textwrap.dedent(branch), "    ")
    return wrapped


class _FakeStore:
    """Store whose async_save raises, simulating storage I/O failure."""

    async def async_load(self):
        return {}

    async def async_save(self, data):
        raise RuntimeError("simulated storage failure")


class _FakeLogger:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _make_hass_entry(entry_id="entry1", extra_entry_data=None):
    DOMAIN = "power_sync"
    entry_data = {"store": _FakeStore()}
    if extra_entry_data:
        entry_data.update(extra_entry_data)
    hass = type("Hass", (), {})()
    hass.data = {DOMAIN: {entry_id: entry_data}}
    hass._created_tasks = []
    hass.async_create_task = lambda coro: hass._created_tasks.append(asyncio.ensure_future(coro))

    class _Entry:
        entry_id = "entry1"
        options = {"battery_curtailment_enabled": True}
        data = {}

    return hass, _Entry(), DOMAIN, entry_data


def _exec_grid_export_branch():
    namespace: dict = {}
    exec(compile(_grid_export_post_write_branch(), "<grid_export_branch>", "exec"), namespace)
    return namespace["_post_write"]


def _exec_operation_mode_branch():
    namespace: dict = {}
    exec(compile(_operation_mode_post_write_branch(), "<operation_mode_branch>", "exec"), namespace)
    return namespace["_post_write"]


def test_pw9_override_flags_survive_store_exception():
    """PW-9: even if update_cached_export_rule's store write raises, the
    manual_export_override / manual_export_rule flags must already be set
    (the Tesla write already succeeded and the user's rule must not be lost)."""
    post_write = _exec_grid_export_branch()
    hass, entry, DOMAIN, entry_data = _make_hass_entry()

    async def _raising_update_cached_export_rule(new_rule):
        raise RuntimeError("simulated storage failure")

    async def _refresh(label):
        pass

    async def _run():
        try:
            await post_write(
                hass, entry, DOMAIN, "never",
                _raising_update_cached_export_rule,
                _refresh,
                "battery_curtailment_enabled",
                _FakeLogger(),
            )
        except RuntimeError:
            pass
        if hass._created_tasks:
            await asyncio.gather(*hass._created_tasks)

    asyncio.run(_run())

    assert entry_data.get("manual_export_override") is True
    assert entry_data.get("manual_export_rule") == "never"


def test_pw10_override_flag_visible_before_store_await():
    """PW-10: the manual_export_override flag must be set BEFORE
    update_cached_export_rule is awaited, so a concurrently-scheduled
    curtailment-cycle read can never observe it as False after the Tesla
    write already succeeded."""
    post_write = _exec_grid_export_branch()
    hass, entry, DOMAIN, entry_data = _make_hass_entry()

    observed = {}

    async def _observing_update_cached_export_rule(new_rule):
        # Simulate a concurrent curtailment-cycle read landing here, in the
        # middle of the await chain.
        observed["manual_export_override"] = entry_data.get("manual_export_override", False)
        entry_data["cached_export_rule"] = new_rule

    async def _refresh(label):
        pass

    async def _run():
        await post_write(
            hass, entry, DOMAIN, "never",
            _observing_update_cached_export_rule,
            _refresh,
            "battery_curtailment_enabled",
            _FakeLogger(),
        )
        if hass._created_tasks:
            await asyncio.gather(*hass._created_tasks)

    asyncio.run(_run())

    assert observed["manual_export_override"] is True


def test_pw11_force_toggle_time_popped_before_refresh_await():
    """PW-11: last_force_toggle_time must be popped BEFORE the
    refresh_powerwall_local_after_settings_write await, so a
    concurrently-scheduled force-mode-toggle check can never observe it
    still set after the user's self_consumption write already succeeded."""
    post_write = _exec_operation_mode_branch()
    hass, entry, DOMAIN, entry_data = _make_hass_entry(
        extra_entry_data={"last_force_toggle_time": "2026-07-10T00:00:00Z"}
    )

    observed = {}

    async def _observing_refresh(label):
        # Simulate a concurrent force-mode-toggle check landing here.
        observed["last_force_toggle_time_present"] = "last_force_toggle_time" in entry_data

    async def _run():
        await post_write(hass, entry, DOMAIN, "self_consumption", _observing_refresh, _FakeLogger())
        if hass._created_tasks:
            await asyncio.gather(*hass._created_tasks)

    asyncio.run(_run())

    assert observed["last_force_toggle_time_present"] is False
