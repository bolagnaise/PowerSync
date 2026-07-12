"""Regression tests for the built-in energy flow dashboard card."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


ENERGY_FLOW_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "frontend"
    / "power-sync-energy-flow.js"
)


def test_energy_flow_keeps_scene_bitmap_outside_animated_svg_layer():
    source = ENERGY_FLOW_PATH.read_text()

    assert 'id="flow-scene-frame"' in source
    assert "--scene-background" in source
    assert "cssBackgroundUrl(url)" in source
    assert "new Image()" in source
    assert '<image id="flow-scene-image"' not in source
    assert "xlink:href" not in source
    assert "function scaledSceneViewBox(scale)" in source
    assert 'viewBox="${sceneViewBox}"' in source
    assert "transform: scale" not in source


def test_energy_flow_avoids_svg_filter_repaint_hotspots():
    source = ENERGY_FLOW_PATH.read_text()

    assert "drop-shadow" not in source
    assert "paint-order: stroke fill" in source
    assert "el.textContent !== value" in source


def test_energy_flow_caps_dash_gap_for_short_active_paths():
    source = ENERGY_FLOW_PATH.read_text()

    assert "_syncFlowLineMetrics" in source
    assert "getTotalLength()" in source
    assert "Short SVG paths can fall entirely inside a long dash gap" in source
    assert "el.style.setProperty('--flow-seg'" in source
    assert "el.style.setProperty('--flow-gap'" in source


def test_energy_flow_models_ev_discharge_as_site_supply():
    source = ENERGY_FLOW_PATH.read_text()

    assert "supplyPower: Math.max(0, -signedPower)" in source
    assert "const evToLoad = Math.min(remainingLoad, evSupplyRemaining)" in source
    assert "const evToGrid = Math.min(remainingGridExportAfterBattery, evSupplyRemaining)" in source
    assert "this._activatePath('line-wallbox-ev', 'flow-green', evSupplyTotal * ev1SupplyShare, 1, true)" in source


def test_energy_flow_treats_power_sync_ev_attributes_as_presence():
    source = ENERGY_FLOW_PATH.read_text()

    assert "const attrs = entityState.attributes || {};" in source
    assert "attrs.is_connected === true || attrs.is_charging === true" in source
    assert "String(attrs.is_connected || '').toLowerCase() === 'true'" in source


def test_energy_flow_does_not_treat_ev_power_as_battery_percent():
    source = ENERGY_FLOW_PATH.read_text()

    assert "const batteryPct = toPct(batteryState, Number.NaN);" in source
    assert "toPct(powerState, Number.NaN)" not in source
    assert "toPct(presenceState, Number.NaN)" not in source
    assert "toPct(switchState, Number.NaN)" not in source
    assert "hasBatteryEntity: Number.isFinite(batteryPct)" in source


def test_energy_flow_can_remove_generic_ev_draw_from_reported_home_load():
    """Generic charger draw must not appear in both Home and EV branches."""
    source = ENERGY_FLOW_PATH.read_text()
    helper = re.search(
        r"function displayedHomeLoadPower\([^)]*\) \{.*?\n  \}",
        source,
        re.DOTALL,
    )
    assert helper is not None
    assert "displayedHomeLoadPower(" in source[source.index("_renderDynamic()") :]

    checks = """
      const cases = [
        [8000, 7000, true, 1000],
        [8000, 7000, false, 8000],
        [8000, -7000, true, 8000],
        [8000, 0, true, 8000],
        [5000, 7000, true, 0],
        [12000, 9000, true, 3000],
      ];
      for (const [raw, ev, included, expected] of cases) {
        const actual = displayedHomeLoadPower(raw, ev, included);
        if (actual !== expected) throw new Error(`${raw}/${ev}/${included}: ${actual}`);
      }
    """
    subprocess.run(["node", "-e", f"{helper.group(0)}\n{checks}"], check=True)
