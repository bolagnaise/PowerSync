"""Backup Reserve must finish before the next automation setting starts."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1] / "custom_components" / "power_sync"


def _entity_method(filename, classname, methodname):
    tree = ast.parse((ROOT / filename).read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == classname)
    method = next(node for node in cls.body
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == methodname)
    namespace = {"DOMAIN": "power_sync"}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT / filename), "exec"), namespace)
    return namespace[methodname]


@pytest.mark.parametrize("reserve_fails", [False, True])
def test_export_reserve_then_grid_charging_waits_for_reserve(reserve_fails):
    """Ticket 140's reordered 75% reserve must not overlap Grid Charging."""
    export = _entity_method("select.py", "TeslaGridExportRuleSelect", "async_select_option")
    reserve = _entity_method("number.py", "BackupReserveNumber", "async_set_native_value")
    grid = _entity_method("switch.py", "GridChargingSwitch", "async_turn_on")

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        completed, queued = [], []

        async def apply_reserve():
            started.set()
            await release.wait()
            if reserve_fails:
                raise RuntimeError("reserve service failed")
            completed.append("reserve 75")

        async def service(domain, name, data, *, blocking):
            assert domain == "power_sync"
            if name == "set_grid_export":
                assert data["rule"] == "never"
                completed.append("export never")
            elif name == "set_backup_reserve":
                assert data == {"percent": 75, "source": "user"}
                if blocking:
                    await apply_reserve()
                else:
                    queued.append(asyncio.create_task(apply_reserve()))
            elif name == "set_grid_charging":
                assert data == {"enabled": True, "source": "user"}
                completed.append("grid charging")
            else:
                raise AssertionError(name)

        entity = SimpleNamespace(
            hass=SimpleNamespace(services=SimpleNamespace(async_call=service)),
            async_write_ha_state=lambda: None,
        )

        async def automation():
            await export(entity, "never")
            await reserve(entity, 75)
            await grid(entity)

        task = asyncio.create_task(automation())
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert completed == ["export never"], "Grid Charging ran before reserve finished"
            assert not task.done(), "Reserve returned while its service was still running"
            release.set()
            if reserve_fails:
                with pytest.raises(RuntimeError, match="reserve service failed"):
                    await task
                assert completed == ["export never"]
            else:
                await task
                assert completed == ["export never", "reserve 75", "grid charging"]
        finally:
            release.set()
            await asyncio.gather(task, *queued, return_exceptions=True)

    asyncio.run(scenario())
