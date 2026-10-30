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


def test_energy_flow_fences_away_canonical_vehicle_presence():
    source = ENERGY_FLOW_PATH.read_text()

    assert "canonicalSitePresence === 'away'" in source
    assert "const rawSignedPower = toOptionalWatt(powerState)" in source
    assert "const signedPower = canonicalSitePresence === 'away'" in source
    assert ": (powerKnown ? rawSignedPower : 0);" in source
    assert "present: canonicalSitePresence === 'away'" in source


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
        // A basis-normalized Home Load must not lose the EV draw a second time.
        [576, 6923, false, 576],
        [7499, 6923, true, 576],
        [null, 7000, true, null],
      ];
      for (const [raw, ev, included, expected] of cases) {
        const actual = displayedHomeLoadPower(raw, ev, included);
        if (actual !== expected) throw new Error(`${raw}/${ev}/${included}: ${actual}`);
      }
    """
    subprocess.run(["node", "-e", f"{helper.group(0)}\n{checks}"], check=True)


def test_energy_flow_keeps_unavailable_home_load_unknown():
    """Missing Home telemetry must not be rendered as a measured zero."""
    source = ENERGY_FLOW_PATH.read_text()
    helper = re.search(
        r"function toOptionalWatt\([^)]*\) \{.*?\n  \}",
        source,
        re.DOTALL,
    )
    assert helper is not None
    dynamic = source[source.index("_renderDynamic()") :]
    assert "toOptionalWatt(this._entityState(cfg.entities.load_power))" in dynamic
    assert "loadPowerKnown ? this._formatKW(loadPower) : '--'" in dynamic

    checks = """
      const cases = [
        [null, null],
        [{ state: 'unknown', attributes: {} }, null],
        [{ state: 'unavailable', attributes: {} }, null],
        [{ state: 'not-a-number', attributes: {} }, null],
        [{ state: '0', attributes: { unit_of_measurement: 'W' } }, 0],
        [{ state: '2.5', attributes: { unit_of_measurement: 'kW' } }, 2500],
      ];
      for (const [entity, expected] of cases) {
        const actual = toOptionalWatt(entity);
        if (actual !== expected) throw new Error(`${entity?.state}: ${actual}`);
      }
    """
    subprocess.run(["node", "-e", f"{helper.group(0)}\n{checks}"], check=True)


def test_energy_flow_keeps_unavailable_core_telemetry_distinct_from_zero():
    """Ticket-39: a visible card must not render unavailable site values as zero."""
    source = ENERGY_FLOW_PATH.read_text()
    watt_helper = re.search(
        r"function toOptionalWatt\([^)]*\) \{.*?\n  \}", source, re.DOTALL
    )
    safe_num_helper = re.search(
        r"function safeNum\([^)]*\) \{.*?\n  \}", source, re.DOTALL
    )
    clamp_helper = re.search(
        r"function clamp\([^)]*\) \{.*?\n  \}", source, re.DOTALL
    )
    pct_helper = re.search(
        r"function toOptionalPct\([^)]*\) \{.*?\n  \}", source, re.DOTALL
    )
    assert watt_helper is not None
    assert safe_num_helper is not None
    assert clamp_helper is not None
    assert pct_helper is not None
    dynamic = source[source.index("_renderDynamic()") :]
    assert "&& (!batteryConfigured || Number.isFinite(batteryPower));" in dynamic
    assert "if (!coreTelemetryKnown)" in dynamic
    assert "this._setText('#flow-solar-power', Number.isFinite(solarPower) ? this._formatKW(solarPower) : '--');" in dynamic
    assert "this._setText('#flow-grid-power', Number.isFinite(gridPower) ? this._formatKW(gridPower) : '--');" in dynamic

    checks = """
      const unavailable = { state: 'unavailable', attributes: {} };
      const unknown = { state: 'unknown', attributes: {} };
      const zeroW = { state: '0', attributes: { unit_of_measurement: 'W' } };
      const zeroPct = { state: '0', attributes: {} };
      for (const entity of [unavailable, unknown]) {
        if (toOptionalWatt(entity) !== null) throw new Error(`power ${entity.state}`);
        if (toOptionalPct(entity) !== null) throw new Error(`pct ${entity.state}`);
      }
      if (toOptionalWatt(zeroW) !== 0) throw new Error('measured 0 W lost');
      if (toOptionalPct(zeroPct) !== 0) throw new Error('measured 0% lost');
    """
    subprocess.run(
        [
            "node",
            "-e",
            f"{safe_num_helper.group(0)}\n{clamp_helper.group(0)}\n{watt_helper.group(0)}\n{pct_helper.group(0)}\n{checks}",
        ],
        check=True,
    )


def test_energy_flow_grid_status_requires_known_state():
    source = ENERGY_FLOW_PATH.read_text()
    assert (
        "const gridState = gridStatusState ? gridConnectionState(gridStatusState.state) : null;"
        in source
    )
    assert "if (!hasGridStatusPosition || !gridState)" in source
    helper = re.search(
        r"function gridConnectionState\([^)]*\) \{.*?\n  \}",
        source,
        re.DOTALL,
    )
    assert helper is not None

    checks = """
      const cases = [
        ['Active', 'connected'],
        ['SystemGridConnected', 'connected'],
        ['Inactive', 'off_grid'],
        ['Islanded', 'off_grid'],
        ['Off-Grid', 'off_grid'],
        ['SystemIslandedActive', 'off_grid'],
        ['SystemIslandedReady', null],
        ['SystemTransitionToGrid', null],
        ['SystemTransitionToIsland', null],
        ['SystemMicroGridFaulted', null],
        ['SystemWaitForUser', null],
        ['connected', null],
        ['on-grid', null],
        ['off grid', null],
        ['unknown', null],
        ['unavailable', null],
        ['', null],
        [null, null],
        ['unexpected-status', null],
      ];
      for (const [raw, expected] of cases) {
        const actual = gridConnectionState(raw);
        if (actual !== expected) throw new Error(`${raw}: ${actual}`);
      }
    """
    subprocess.run(["node", "-e", f"{helper.group(0)}\n{checks}"], check=True)


def test_energy_flow_glows_without_filters():
    """The refreshed overlay glows with static strokes and gradients, never
    SVG filters (which caused repaint stutter on wall tablets)."""
    source = ENERGY_FLOW_PATH.read_text()

    for line_id in (
        "line-solar-load", "line-grid-load", "line-battery-load",
        "line-junction-home-load", "line-wallbox-ev", "line-wallbox-ev2",
        "line-solar-grid", "line-solar-battery", "line-grid-battery",
    ):
        assert f'<path id="{line_id}-halo" class="flow-halo"' in source, line_id
    assert source.count('<ellipse class="flow-node-halo"') == 6
    assert 'id="flow-node-halo-grad"' in source
    # Halos are re-measured only when a label or the scene changes.
    assert "this._nodeHaloSignature === textSignature" in source
    assert "filter:" not in source


def test_energy_flow_colours_streaks_by_source():
    source = ENERGY_FLOW_PATH.read_text()

    # Solar exported to the grid is solar energy, so it takes solar's colour.
    assert "{ id: 'line-solar-grid', cls: 'flow-solar', watt: solarExport" in source
    # The grid reading turns red while exporting.
    assert "classList.toggle('exporting', Number.isFinite(gridPower) && gridPower < -gridMin)" in source
    assert ".flow-node.exporting #flow-grid-power" in source
    # Streak speed and width follow power, quantised to avoid restarts.
    assert "_syncFlowLineRate(el, classes.length ? watt : 0)" in source
