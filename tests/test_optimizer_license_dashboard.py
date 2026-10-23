"""The plan card distinguishes licensing stops from a saved disabled setting."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"


def test_optimization_api_includes_licensing_without_a_coordinator(monkeypatch):
    package = "_optimizer_license_dashboard_test"
    for name in (package, f"{package}.optimization"):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    ai_summary = ModuleType(f"{package}.optimization.ai_summary")
    for name in (
        "ai_summary_settings", "configured_api_key", "configured_local_endpoint",
        "configured_local_model", "configured_openrouter_model",
        "configured_openai_model",
    ):
        setattr(ai_summary, name, lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, ai_summary.__name__, ai_summary)

    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    view = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationView"
    )
    method = next(
        node for node in view.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get"
    )
    namespace = {
        "__package__": package,
        "web": SimpleNamespace(Request=object, Response=object, json_response=lambda body: body),
        "DOMAIN": "power_sync",
        "_LOGGER": SimpleNamespace(debug=lambda *args: None, info=lambda *args: None),
        "_optimization_ai_settings": lambda *args: {"ai_summary_key_configured": False},
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), "__init__.py", "exec"), namespace)
    access_calls = []
    manager = SimpleNamespace(status=lambda entry_id: access_calls.append(entry_id) or {
        "access_state": "expired", "can_optimize": False,
    })
    entry = SimpleNamespace(entry_id="battery-one")
    hass = SimpleNamespace(
        data={"power_sync": {}, "power_sync_license_manager": manager},
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
    )
    result = asyncio.run(namespace["get"](SimpleNamespace(_hass=hass), object()))

    assert result["enabled"] is False
    assert result["licensing"]["access_state"] == "expired"
    assert result["features"]["licensing_v1"] is True
    assert access_calls == ["battery-one"]

    hass.data.pop("power_sync_license_manager")
    legacy_result = asyncio.run(namespace["get"](SimpleNamespace(_hass=hass), object()))
    assert legacy_result["enabled"] is False
    assert "licensing" not in legacy_result
    assert "licensing_v1" not in legacy_result["features"]

    hass.data["power_sync_license_manager"] = manager
    hass.config_entries.async_entries = lambda domain: [
        entry, SimpleNamespace(entry_id="battery-two")
    ]
    access_calls.clear()
    ambiguous_result = asyncio.run(
        namespace["get"](SimpleNamespace(_hass=hass), object())
    )
    assert "licensing" not in ambiguous_result
    assert access_calls == []


def test_plan_card_displays_exact_stop_reason_and_hides_retained_plan():
    if shutil.which("node") is None:
        return

    script = r'''
const fs = require('fs');
const vm = require('vm');
const registry = {};
class HTMLElement {
  attachShadow() { this.shadowRoot = { innerHTML: '', querySelector() { return null; } }; return this.shadowRoot; }
  getBoundingClientRect() { return { width: 640 }; }
}
const context = {
  HTMLElement,
  customElements: {
    get(name) { return registry[name]; },
    define(name, klass) { registry[name] = klass; },
  },
  window: {}, ResizeObserver: class {}, requestAnimationFrame() {},
};
context.globalThis = context;
vm.runInNewContext(fs.readFileSync('custom_components/power_sync/frontend/power-sync-strategy.js', 'utf8'), context);
const plan = new registry['power-sync-optimization-plan']();
plan._config = { importPriceEntity: 'sensor.import_price' };
plan._hass = { states: {}, config: { currency: 'AUD' } };
const cases = {
  expired: { enabled: false, licensing: { access_state: 'expired', can_optimize: false, requires_resume: true } },
  recovery: { enabled: false, licensing: { access_state: 'support', can_optimize: false, recovery_pending: true, requires_resume: true } },
  resume: { enabled: false, licensing: { access_state: 'support', can_optimize: false, would_allow: true, requires_resume: true } },
  deniedResume: { enabled: false, licensing: { access_state: 'subscription_required', can_optimize: false, would_allow: false, requires_resume: true } },
  disabled: { enabled: false, licensing: { access_state: 'support', can_optimize: true } },
  oldDisabled: { enabled: false },
  active: { enabled: true, licensing: { access_state: 'support', can_optimize: true } },
  shadow: { enabled: true, licensing: { access_state: 'expired', can_optimize: true, enforcement_mode: 'shadow' } },
  monitoring: { enabled: true, monitoring_mode: true, licensing: { access_state: 'support', can_optimize: true } },
};
const results = {};
for (const [name, data] of Object.entries(cases)) {
  plan._data = data;
  plan._error = null;
  results[name] = {
    availability: plan._optimizerAvailability(),
    chips: plan._renderChips({ raw: data }, { displayCurrency: 'AUD' }),
    notice: plan._renderNotice(true),
  };
}
plan._data = cases.active;
plan._error = 'network timeout';
results.error = plan._optimizerAvailability();
plan._data = null;
plan._error = null;
plan._loading = true;
results.loading = plan._optimizerAvailability();
plan._loading = false;
plan._data = { ...cases.expired, schedule: { timestamps: ['2026-10-23T12:00:00+10:00'] } };
plan._error = null;
plan._buildModel = () => ({ raw: plan._data, points: [{timestamp: '2026-10-23T12:00:00+10:00'}], priceLevelWindows: [{start: 0, end: 0}] });
plan._actionRangesFromApi = () => { throw new Error('stale actions rendered'); };
plan._fallbackActionRanges = () => { throw new Error('stale fallback rendered'); };
plan._renderBatteryWindows = () => '';
plan._renderActions = () => '';
plan._render();
results.retainedPlan = plan.shadowRoot.innerHTML;
console.log(JSON.stringify(results));
'''
    result = subprocess.run(
        ["node"], input=script, text=True, capture_output=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered["expired"]["availability"]["label"] == "Licence expired"
    assert "HA optimiser access has expired" in rendered["expired"]["notice"]
    assert "Licence Expired" in rendered["expired"]["chips"]
    assert rendered["recovery"]["availability"]["label"] == "Recovery pending"
    assert rendered["resume"]["availability"]["label"] == "Resume required"
    assert "Resume optimisation" in rendered["resume"]["notice"]
    assert rendered["deniedResume"]["availability"]["label"] == "Access unavailable"
    assert rendered["disabled"]["availability"]["label"] == "Disabled in settings"
    assert rendered["oldDisabled"]["availability"]["label"] == "Disabled in settings"
    assert rendered["active"]["availability"] is None
    assert rendered["shadow"]["availability"] is None
    assert "Mode:</span><span>Monitoring" in rendered["monitoring"]["chips"]
    assert rendered["error"]["label"] == "Status unavailable"
    assert rendered["loading"]["label"] == "Checking status"
    assert "24-Hour Action Plan" in rendered["retainedPlan"]
    assert "SOC and Battery Power" not in rendered["retainedPlan"]
    assert "Projected EV Price-Level Windows" not in rendered["retainedPlan"]
    assert "HA optimiser access has expired" in rendered["retainedPlan"]
