"""Regression coverage for Discord #69: FoxESS negative-price curtailment was
suppressed for a whole optimizer CHARGE window into an already-full battery.

``FoxESSController.force_charge()`` writes ``REMOTE_CONTROL_AC`` (0x0001), which
commands the inverter's AC output and — unlike the ``REMOTE_CONTROL_GRID``
(0x0009) mode used by ``curtail()`` and ``force_discharge()`` — does not hold
grid feed-in.  With the battery at 100% SOC that charge also absorbs nothing,
because the grid-charge SOC cap self-disables at its default 100% setting.  So
``_foxess_force_dispatch_active()`` was handing a live economic protection to a
dispatch that could neither absorb energy nor prevent export, and the reporter
exported 2.0 kW at -7.1 c/kWh feed-in for 2h40m.

The guard itself must stay (commit 32fdf3af: cancelling a *productive* paid
charge would be worse), so these tests pin both directions: no headroom ⇒
curtailment proceeds, real headroom ⇒ the force dispatch still wins.

Uses the runtime source-extraction pattern from
tests/test_sungrow_curtailment_runtime.py and
tests/test_alphaess_curtailment_force_guard.py.
"""

from __future__ import annotations

import ast
import asyncio
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"

DOMAIN = "power_sync"
ENTRY_ID = "test_entry"

# export_earnings = -feedin_price.  The reporter's capture: raw Amber
# feedIn.perKwh = +7.1 ⇒ export earnings -7.1 c/kWh, i.e. paying to export.
TICKET_69_FEEDIN_PRICE = 7.1
TICKET_69_IMPORT_PRICE = -2.6
POSITIVE_EARNINGS_FEEDIN_PRICE = -5.0


def _function_source(name: str) -> str:
    """Extract a function nested inside async_setup_entry, by name."""
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if (
                    isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef))
                    and child.name == name
                ):
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{name} not found")


class FakeFoxESSCoordinator:
    """Records curtail()/restore_curtailment() without touching Modbus."""

    def __init__(self, *, battery_level, last_update_success: bool = True):
        self.curtail_calls = 0
        self.restore_calls = 0
        self.last_update_success = last_update_success
        self.update_interval = timedelta(seconds=30)
        self.data = {
            "grid_power": -2.0,  # 2.0 kW exporting
            "grid_power_valid": True,
            "telemetry_ready": True,
            "last_update": datetime.now(timezone.utc),
        }
        if battery_level is not None:
            self.data["battery_level"] = battery_level

    async def curtail(self) -> bool:
        self.curtail_calls += 1
        return True

    async def restore_curtailment(self) -> bool:
        self.restore_calls += 1
        return True


def _load_handler(
    entry_data: dict,
    *,
    force_charge_active: bool = False,
    force_discharge_active: bool = False,
):
    entry = SimpleNamespace(options={}, data={}, entry_id=ENTRY_ID)
    hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: entry_data}})

    namespace = {
        "DOMAIN": DOMAIN,
        "hass": hass,
        "entry": entry,
        "Mapping": Mapping,
        "math": math,
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        "get_current_prices_for_curtailment": lambda *a, **k: (None, None, None),
        "amber_coordinator": None,
        "localvolts_coordinator": None,
        "aemo_sensor_coordinator": None,
        "flow_power_kwatch_coordinator": None,
        "octopus_coordinator": None,
        "_LOGGER": SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
        ),
        "force_charge_state": {"active": force_charge_active},
        "force_discharge_state": {"active": force_discharge_active},
        "_optimizer_current_force_action_matches": lambda _force_type: False,
        # Default entry threshold 0.0 takes the strict branch.
        "export_earnings_are_uneconomic": lambda value, _active, _entry: value < 0.0,
    }
    exec(_function_source("handle_foxess_curtailment"), namespace)
    return namespace["handle_foxess_curtailment"], hass


def _entry_data(
    *,
    current_state: str = "normal",
    battery_level=100,
    optimizer_force: dict | None = None,
    last_update_success: bool = True,
) -> tuple[dict, FakeFoxESSCoordinator]:
    coordinator = FakeFoxESSCoordinator(
        battery_level=battery_level, last_update_success=last_update_success
    )
    entry_data: dict = {
        "foxess_curtailment_state": current_state,
        "foxess_coordinator": coordinator,
    }
    if optimizer_force is not None:
        entry_data["optimization_coordinator"] = SimpleNamespace(
            get_active_force_state=lambda: optimizer_force
        )
    return entry_data, coordinator


def test_foxess_curtails_when_optimizer_charge_has_no_soc_headroom():
    """Discord #69, the newly observed variant: an optimizer force CHARGE into
    a 100% battery must not suppress negative-price curtailment."""
    entry_data, coordinator = _entry_data(
        battery_level=100, optimizer_force={"active": True, "type": "charge"}
    )
    handler, hass = _load_handler(entry_data)

    asyncio.run(
        handler(
            feedin_price=TICKET_69_FEEDIN_PRICE,
            import_price=TICKET_69_IMPORT_PRICE,
        )
    )

    assert coordinator.curtail_calls == 1
    assert hass.data[DOMAIN][ENTRY_ID]["foxess_curtailment_state"] == "curtailed"


def test_foxess_preserves_an_optimizer_charge_with_real_headroom():
    """Commit 32fdf3af must survive: a productive paid charge still wins."""
    entry_data, coordinator = _entry_data(
        battery_level=60, optimizer_force={"active": True, "type": "charge"}
    )
    handler, hass = _load_handler(entry_data)

    asyncio.run(
        handler(
            feedin_price=TICKET_69_FEEDIN_PRICE,
            import_price=TICKET_69_IMPORT_PRICE,
        )
    )

    assert coordinator.curtail_calls == 0
    assert hass.data[DOMAIN][ENTRY_ID]["foxess_curtailment_state"] == "normal"


def test_foxess_preserves_an_optimizer_discharge_regardless_of_soc():
    """The discharge/export branch is unchanged: it does hold grid feed-in."""
    entry_data, coordinator = _entry_data(
        battery_level=100, optimizer_force={"active": True, "type": "discharge"}
    )
    handler, hass = _load_handler(entry_data)

    asyncio.run(
        handler(
            feedin_price=TICKET_69_FEEDIN_PRICE,
            import_price=TICKET_69_IMPORT_PRICE,
        )
    )

    assert coordinator.curtail_calls == 0
    assert hass.data[DOMAIN][ENTRY_ID]["foxess_curtailment_state"] == "normal"


def test_foxess_preserves_a_manual_force_charge_with_headroom():
    """The manual force_charge_state branch keeps the same headroom rule."""
    entry_data, coordinator = _entry_data(battery_level=55)
    handler, hass = _load_handler(entry_data, force_charge_active=True)

    asyncio.run(
        handler(
            feedin_price=TICKET_69_FEEDIN_PRICE,
            import_price=TICKET_69_IMPORT_PRICE,
        )
    )

    assert coordinator.curtail_calls == 0


def test_foxess_curtails_when_a_manual_force_charge_is_inert():
    """A manual force charge at 100% SOC is equally unable to hold feed-in."""
    entry_data, coordinator = _entry_data(battery_level=100)
    handler, hass = _load_handler(entry_data, force_charge_active=True)

    asyncio.run(
        handler(
            feedin_price=TICKET_69_FEEDIN_PRICE,
            import_price=TICKET_69_IMPORT_PRICE,
        )
    )

    assert coordinator.curtail_calls == 1


def test_foxess_soc_unreadable_keeps_the_force_guard_active():
    """Fail closed: no SOC reading must not silently cancel a force charge."""
    entry_data, coordinator = _entry_data(
        battery_level=None, optimizer_force={"active": True, "type": "charge"}
    )
    handler, hass = _load_handler(entry_data)

    asyncio.run(
        handler(
            feedin_price=TICKET_69_FEEDIN_PRICE,
            import_price=TICKET_69_IMPORT_PRICE,
        )
    )

    assert coordinator.curtail_calls == 0


def test_foxess_stale_coordinator_keeps_the_force_guard_active():
    """A failed telemetry update is not evidence the battery is full."""
    entry_data, coordinator = _entry_data(
        battery_level=100,
        optimizer_force={"active": True, "type": "charge"},
        last_update_success=False,
    )
    handler, hass = _load_handler(entry_data)

    asyncio.run(
        handler(
            feedin_price=TICKET_69_FEEDIN_PRICE,
            import_price=TICKET_69_IMPORT_PRICE,
        )
    )

    assert coordinator.curtail_calls == 0


def test_foxess_curtails_normally_with_no_force_dispatch():
    """Baseline behaviour is preserved when nothing owns remote control."""
    entry_data, coordinator = _entry_data(battery_level=100)
    handler, hass = _load_handler(entry_data)

    asyncio.run(
        handler(
            feedin_price=TICKET_69_FEEDIN_PRICE,
            import_price=TICKET_69_IMPORT_PRICE,
        )
    )

    assert coordinator.curtail_calls == 1
    assert hass.data[DOMAIN][ENTRY_ID]["foxess_curtailment_state"] == "curtailed"


def test_foxess_restores_when_export_earnings_recover():
    """The restore path stays untouched when no force dispatch is active."""
    entry_data, coordinator = _entry_data(
        current_state="curtailed", battery_level=100
    )
    handler, hass = _load_handler(entry_data)

    asyncio.run(
        handler(
            feedin_price=POSITIVE_EARNINGS_FEEDIN_PRICE, import_price=30.0
        )
    )

    assert coordinator.restore_calls == 1
    assert hass.data[DOMAIN][ENTRY_ID]["foxess_curtailment_state"] == "normal"
