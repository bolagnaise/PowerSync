"""Regression coverage for HD-15/HD-24: boundary-flap hysteresis.

Both bugs are the same class -- a stateless `current >= threshold` (or `<`)
comparison flaps active/inactive on every poll when the live value hovers
right at the decision boundary:

- HD-15: `should_curtail_ac_coupled` (__init__.py) curtails AC-coupled solar
  when export earnings drop below 1c/kWh; a price sitting at ~1c flaps
  curtail/restore every WebSocket tick.
- HD-24: `check_price_spike` (aemo_api.py) flags an AEMO spike at
  `current_price >= threshold`; a dispatch price sitting at the threshold
  flaps enter/exit on every 5-min AEMO poll.

Both are fixed with the same shared helper, `tariff_utils.with_hysteresis`.
This file exercises the real helper plus both call sites (one via AST
source-extraction for the nested `__init__.py` function, one via a direct
module load of `aemo_api.py`).
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
AEMO_API_PATH = ROOT / "custom_components" / "power_sync" / "aemo_api.py"
TARIFF_UTILS_PATH = ROOT / "custom_components" / "power_sync" / "tariff_utils.py"


def _load_tariff_utils():
    spec = importlib.util.spec_from_file_location(
        "power_sync_tariff_utils_hysteresis_test", TARIFF_UTILS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _nested_function_source(name: str) -> str:
    """Extract a (possibly nested) function definition by name from __init__.py."""
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in ast.walk(module):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} not found in {INIT_PATH}")


def _build_should_curtail_ac_coupled(get_live_status, with_hysteresis):
    """Extract and exec should_curtail_ac_coupled with a stub closure."""
    namespace = {
        "CONF_INVERTER_RESTORE_SOC": "inverter_restore_soc",
        "DEFAULT_INVERTER_RESTORE_SOC": 90,
        "_LOGGER": SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
        ),
        "entry": SimpleNamespace(options={}, data={}, entry_id="test_entry"),
        "get_live_status": get_live_status,
        "hass": SimpleNamespace(data={}),
        "DOMAIN": "power_sync",
        "with_hysteresis": with_hysteresis,
    }
    exec(textwrap.dedent(_nested_function_source("should_curtail_ac_coupled")), namespace)
    return namespace["should_curtail_ac_coupled"]


def _load_aemo_api_module():
    """Load aemo_api.py as a real package submodule so its function-local
    `from .tariff_utils import with_hysteresis` resolves."""
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules.setdefault(
        "custom_components.power_sync", types.ModuleType("custom_components.power_sync")
    )

    tariff_spec = importlib.util.spec_from_file_location(
        "custom_components.power_sync.tariff_utils", TARIFF_UTILS_PATH
    )
    tariff_module = importlib.util.module_from_spec(tariff_spec)
    sys.modules[tariff_spec.name] = tariff_module
    assert tariff_spec.loader is not None
    tariff_spec.loader.exec_module(tariff_module)

    aemo_spec = importlib.util.spec_from_file_location(
        "custom_components.power_sync.aemo_api", AEMO_API_PATH
    )
    aemo_module = importlib.util.module_from_spec(aemo_spec)
    sys.modules[aemo_spec.name] = aemo_module
    assert aemo_spec.loader is not None
    aemo_spec.loader.exec_module(aemo_module)
    return aemo_module


# ---------------------------------------------------------------------------
# with_hysteresis (shared helper) unit tests
# ---------------------------------------------------------------------------

def test_with_hysteresis_active_when_high_enter_and_exit():
    with_hysteresis = _load_tariff_utils().with_hysteresis

    # Below enter threshold, inactive -> stays inactive.
    assert with_hysteresis(280.0, False, enter_threshold=300.0, exit_threshold=280.0) is False
    # Crosses at/above enter threshold, inactive -> becomes active.
    assert with_hysteresis(300.0, False, enter_threshold=300.0, exit_threshold=280.0) is True
    # In the dead zone, previously active -> stays active (no flap).
    assert with_hysteresis(290.0, True, enter_threshold=300.0, exit_threshold=280.0) is True
    # In the dead zone, previously inactive -> stays inactive (no flap).
    assert with_hysteresis(290.0, False, enter_threshold=300.0, exit_threshold=280.0) is False
    # Drops below exit threshold, active -> becomes inactive.
    assert with_hysteresis(279.9, True, enter_threshold=300.0, exit_threshold=280.0) is False


def test_with_hysteresis_active_when_low_enter_and_exit():
    with_hysteresis = _load_tariff_utils().with_hysteresis

    # Above enter threshold, inactive -> stays inactive.
    assert with_hysteresis(1.2, False, enter_threshold=1.0, exit_threshold=1.2) is False
    # Drops below enter threshold, inactive -> becomes active.
    assert with_hysteresis(0.99, False, enter_threshold=1.0, exit_threshold=1.2) is True
    # In the dead zone, previously active -> stays active (no flap).
    assert with_hysteresis(1.05, True, enter_threshold=1.0, exit_threshold=1.2) is True
    # In the dead zone, previously inactive -> stays inactive (no flap).
    assert with_hysteresis(1.05, False, enter_threshold=1.0, exit_threshold=1.2) is False
    # Rises to/above exit threshold, active -> becomes inactive.
    assert with_hysteresis(1.2, True, enter_threshold=1.0, exit_threshold=1.2) is False


# ---------------------------------------------------------------------------
# HD-15: should_curtail_ac_coupled
# ---------------------------------------------------------------------------

def test_ac_curtail_no_flap_while_export_earnings_hovers_at_boundary():
    """A price hovering just above/below the 1c/kWh boundary must not flap
    the curtail decision every tick -- only decisive crossings should."""

    async def get_live_status():
        # Exporting, battery not absorbing (not charging, high SOC) -- the
        # curtail decision here is driven purely by export_uneconomic.
        return {
            "solar_power": 3000,
            "battery_power": 100,
            "grid_power": -500,
            "load_power": 2400,
            "battery_soc": 95,
        }

    with_hysteresis = _load_tariff_utils().with_hysteresis
    should_curtail_ac_coupled = _build_should_curtail_ac_coupled(get_live_status, with_hysteresis)

    async def run():
        results = []
        for export_earnings in (0.9, 1.05, 0.95, 1.25, 1.1, 0.99):
            results.append(await should_curtail_ac_coupled(15.0, export_earnings))
        return results

    results = asyncio.run(run())

    # Clean entry at 0.9c (< 1.0c enter threshold).
    assert results[0] is True
    # 1.05c and 0.95c both sit inside the 1.0-1.2c dead zone -- must NOT flap
    # away from the active (curtailing) decision.
    assert results[1] is True, "flapped to restore at 1.05c/kWh while still in the dead zone"
    assert results[2] is True
    # Clean exit at 1.25c (>= 1.2c exit threshold).
    assert results[3] is False
    # 1.1c sits in the dead zone but curtailment already exited -- must not
    # re-enter until decisively below the 1.0c enter threshold.
    assert results[4] is False, "flapped back to curtailing at 1.1c/kWh while still in the dead zone"
    # Clean re-entry at 0.99c.
    assert results[5] is True


# ---------------------------------------------------------------------------
# HD-24: check_price_spike
# ---------------------------------------------------------------------------

def test_aemo_spike_no_flap_while_price_hovers_at_boundary(monkeypatch):
    """A dispatch price hovering just above/below the $/MWh threshold must
    not flap the spike decision every poll -- only decisive crossings should."""

    aemo_module = _load_aemo_api_module()
    client = aemo_module.AEMOAPIClient.__new__(aemo_module.AEMOAPIClient)

    prices = iter([310.0, 290.0, 285.0, 275.0, 295.0, 305.0])

    async def fake_get_region_price(region):
        return {"price": next(prices)}

    monkeypatch.setattr(client, "get_region_price", fake_get_region_price)

    async def run():
        results = []
        was_active = False
        for _ in range(6):
            is_spike, _price, _data = await client.check_price_spike(
                "NSW1", 300.0, was_active=was_active
            )
            results.append(is_spike)
            was_active = is_spike
        return results

    results = asyncio.run(run())

    # Clean entry at $310 (>= $300 enter threshold).
    assert results[0] is True
    # $290 and $285 both sit inside the $280-$300 dead zone -- must NOT flap
    # out of spike mode.
    assert results[1] is True, "flapped out of spike mode at $290/MWh while still in the dead zone"
    assert results[2] is True
    # Clean exit at $275 (< $280 exit threshold).
    assert results[3] is False
    # $295 sits in the dead zone but spike mode already exited -- must not
    # re-enter until decisively at/above the $300 enter threshold.
    assert results[4] is False, "flapped back into spike mode at $295/MWh while still in the dead zone"
    # Clean re-entry at $305.
    assert results[5] is True
