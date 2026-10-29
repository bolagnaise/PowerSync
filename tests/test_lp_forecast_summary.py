"""Forecast summary labels follow each displayed metric's actual window."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"


def _extract_function(path, name):
    node = next(
        node for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace = {"Any": Any}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def _render_metrics(attributes):
    result = subprocess.run(
        ["node", "-e", r'''
const fs = require('fs');
const vm = require('vm');
const registry = {};
const context = {
  HTMLElement: class { attachShadow() { return {}; } },
  customElements: {
    get(name) { return registry[name]; },
    define(name, klass) { registry[name] = klass; },
  },
  window: {},
  ResizeObserver: class {},
  requestAnimationFrame() {},
};
vm.runInNewContext(fs.readFileSync(
  'custom_components/power_sync/frontend/power-sync-strategy.js', 'utf8'), context);
const summary = new registry['power-sync-forecast-summary']();
summary._hass = { states: {} };
const metrics = JSON.parse(fs.readFileSync(0, 'utf8')).map((attributes) => {
  summary._hass.states['sensor.forecast'] = { state: '65.2', attributes };
  return summary._renderMetric({ entity: 'sensor.forecast', name: 'Solar Forecast' });
});
console.log(JSON.stringify(metrics));
'''],
        input=json.dumps(attributes), text=True, capture_output=True, cwd=ROOT,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("interval,solar_steps,price_steps", [(5, 576, 288), (30, 48, 24)])
def test_summary_windows_follow_actual_solar_load_and_price_data(interval, solar_steps, price_steps):
    get_forecasts = _extract_function(COMPONENT / "optimization/coordinator.py", "get_forecast_data")
    coordinator = SimpleNamespace(
        _config=SimpleNamespace(interval_minutes=interval),
        _last_solar_forecast=[1.0] * solar_steps,
        _last_load_forecast=[2.0] * solar_steps,
        _solar_nowcast_derate=1.0,
        _last_solar_nowcast_ratio=None,
        _summarise_load_forecast=lambda: {},
        _last_planned_ev_load_forecast_w=None,
        _last_display_import_prices=[0.25] * price_steps,
        _last_display_export_prices=[0.10] * price_steps,
        _last_import_prices=[0.30] * solar_steps,
        _last_export_prices=[0.15] * solar_steps,
        _current_schedule=None,
    )
    data = get_forecasts(coordinator)
    sensor_path = COMPONENT / "sensor.py"
    solar_attrs = _extract_function(sensor_path, "_lp_solar_forecast_attributes")
    attributes = [solar_attrs(data)]
    tree = ast.parse(sensor_path.read_text())
    for key in ("SENSOR_TYPE_LP_LOAD_FORECAST", "SENSOR_TYPE_LP_IMPORT_PRICE_FORECAST", "SENSOR_TYPE_LP_EXPORT_PRICE_FORECAST"):
        description = next(
            node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and any(kw.arg == "key" and isinstance(kw.value, ast.Name) and kw.value.id == key for kw in node.keywords)
        )
        attr_fn = next(kw.value for kw in description.keywords if kw.arg == "attr_fn")
        attributes.append(eval(compile(ast.Expression(attr_fn), str(sensor_path), "eval"))(data))

    hours = solar_steps * interval / 60
    price_hours = price_steps * interval / 60
    assert data["solar_forecast_kwh"] == hours
    assert data["load_forecast_kwh"] == hours * 2
    assert [attrs["forecast_hours"] for attrs in attributes] == [hours, hours, price_hours, price_hours]
    for html, expected_hours in zip(_render_metrics(attributes), [hours, hours, price_hours, price_hours]):
        assert f"Next {expected_hours:g} hours" in html
        assert "65.2" in html


def test_summary_handles_fractional_single_hour_and_missing_window():
    windows = [1, 1.5, None, 0, -1, "48"]
    rendered = _render_metrics([{"forecast_hours": hours} for hours in windows] + [{}])
    assert "Next 1 hour<" in rendered[0]
    assert "Next 1.5 hours<" in rendered[1]
    assert all("Optimizer forecast window" in html for html in rendered[2:])


@pytest.mark.parametrize("enabled,factor", [(False, 1.0), (True, 0.7)])
def test_solar_forecast_sensor_exposes_live_correction_setting_and_factor(enabled, factor):
    attributes = _extract_function(COMPONENT / "sensor.py", "_lp_solar_forecast_attributes")({
        "solar_nowcast_enabled": enabled, "solar_nowcast_derate": factor,
    })
    assert attributes["solar_nowcast_enabled"] is enabled
    assert attributes["solar_nowcast_derate"] == factor
