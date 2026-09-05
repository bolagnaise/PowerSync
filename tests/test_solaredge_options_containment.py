"""Execute options cleanup against a blocked SolarEdge coordinator."""

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/power_sync/config_flow.py"


def load_cleanup():
    tree = ast.parse(SOURCE.read_text())
    cls = next(node for node in tree.body if getattr(node, "name", None) == "PowerSyncOptionsFlow")
    method = next(node for node in cls.body if getattr(node, "name", None) == "_restore_owned_curtailment_limits")
    namespace = {"DOMAIN": "power_sync", "_LOGGER": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), "exec"), namespace)  # noqa: S102 - Execute only this repository's method.
    return namespace[method.name]


@pytest.mark.parametrize("blocked", [True, False])
@pytest.mark.parametrize("write_ok", [True, False])
def test_options_cleanup_uses_shared_containment(blocked, write_ok):
    events = []

    async def restore():
        events.append("write")
        return write_ok

    async def disconnect():
        events.append("disconnect")

    async def guarded(operation, *, automatic):
        assert automatic is True
        events.append("lock")
        try:
            if blocked:
                return False
            return await operation()
        finally:
            events.append("unlock")

    direct = SimpleNamespace(restore=AsyncMock(side_effect=restore), disconnect=AsyncMock(side_effect=disconnect))
    coordinator = SimpleNamespace(run_external_mutation=AsyncMock(side_effect=guarded))
    entry_data = {"solaredge_controller": direct, "solaredge_coordinator": coordinator, "solaredge_curtailment_state": "curtailed"}
    flow = SimpleNamespace(hass=SimpleNamespace(data={"power_sync": {"entry": entry_data}}), config_entry=SimpleNamespace(entry_id="entry"))
    asyncio.run(load_cleanup()(flow))
    assert events == (["lock", "unlock"] if blocked else ["lock", "write", "disconnect", "unlock"])
    assert entry_data["solaredge_curtailment_state"] == ("normal" if not blocked and write_ok else "curtailed")
