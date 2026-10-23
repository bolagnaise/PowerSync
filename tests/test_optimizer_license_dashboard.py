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
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"


def settings_post():
    """Run the real settings admission and queue path without HA platform imports."""
    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    view = next(node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == "OptimizationSettingsView")
    method = next(node for node in view.body
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "post")
    namespace = {
        "asyncio": asyncio,
        "DOMAIN": "power_sync",
        "CONF_OPTIMIZATION_ENABLED": "optimization_enabled",
        "_LOGGER": SimpleNamespace(warning=Mock(), error=Mock()),
        "web": SimpleNamespace(Request=object, Response=object,
                               json_response=lambda body, status=200: {**body, "_status": status}),
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), "__init__.py", "exec"), namespace)
    return namespace["post"]


@pytest.mark.parametrize("blocked_by", ["unpaired", "denied", "expired", "storage", "resume", "recovery"])
@pytest.mark.parametrize("with_coordinator", [True, False])
def test_settings_reject_unavailable_access_before_saving_or_queueing(blocked_by, with_coordinator):
    import test_ha_optimizer_runtime as rt

    async def run():
        h = rt.Harness()
        if blocked_by == "unpaired":
            await h.client.initialize()
            await h.client.maintain(force=True)
        else:
            await h.activate()
        if blocked_by == "denied":
            h.denied = True
            h.revision = 2
            await h.client.maintain(force=True)
        elif blocked_by == "expired":
            h.mono = 8 * 86400
        elif blocked_by == "storage":
            h.client.storage_ok = False
        elif blocked_by in {"resume", "recovery"}:
            h.client.entries["one"] = {"resume_required": True,
                                       "recovery_pending": blocked_by == "recovery"}
        assert not h.client.permits("one")
        status = h.client.status("one")
        entry = SimpleNamespace(entry_id="one", data={}, options={"optimization_enabled": False})
        update = Mock()
        queue = Mock(side_effect=asyncio.create_task)
        coordinator = SimpleNamespace(enabled=False, set_settings=AsyncMock(
            return_value={"success": False, "error": "optimizer_access_required"}))
        entry_data = {"optimization_coordinator": coordinator} if with_coordinator else {}
        hass = SimpleNamespace(
            data={"power_sync": {"one": entry_data}, rt.runtime.DATA_KEY: h.client},
            config_entries=SimpleNamespace(async_entries=lambda _: [entry], async_update_entry=update),
            async_create_task=queue,
        )
        request = SimpleNamespace(json=AsyncMock(return_value={"enabled": True}))
        response = await settings_post()(SimpleNamespace(_hass=hass), request)
        # Drain any incorrectly accepted background work so the old behavior is
        # observable without leaving tasks behind in a failed reproduction.
        if task := entry_data.get("_optimization_settings_transition_task"):
            await task
        assert response["_status"] == 402
        assert response["success"] is False
        assert response["code"] == "optimizer_access_required"
        assert "HA optimiser access" in response["error"]
        assert response["licensing"] == status
        assert not entry.options["optimization_enabled"]
        update.assert_not_called()
        queue.assert_not_called()
        coordinator.set_settings.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("access,settings", [
    ("available", {"enabled": True}),
    ("legacy", {"enabled": True}),
    ("denied", {"enabled": False}),
    ("denied", {"cost_function": "self_consumption"}),
])
def test_settings_preserve_allowed_enable_disable_and_ordinary_writes(access, settings):
    import test_ha_optimizer_runtime as rt

    async def run():
        h = rt.Harness()
        await h.activate()
        if access == "denied":
            h.denied = True
            h.revision = 2
            await h.client.maintain(force=True)
        entry = SimpleNamespace(entry_id="one", data={}, options={"optimization_enabled": False})
        coordinator = SimpleNamespace(enabled=settings.get("enabled") is False,
                                      set_settings=AsyncMock(return_value={"success": True, "changes": []}))
        entry_data = {"optimization_coordinator": coordinator}

        def update(target, *, options):
            target.options = dict(options)

        data = {"power_sync": {"one": entry_data}}
        if access != "legacy":
            data[rt.runtime.DATA_KEY] = h.client
        hass = SimpleNamespace(data=data, async_create_task=asyncio.create_task,
                               config_entries=SimpleNamespace(async_entries=lambda _: [entry],
                                                              async_update_entry=update))
        calls_before = list(h.calls)
        response = await settings_post()(SimpleNamespace(_hass=hass),
                                        SimpleNamespace(json=AsyncMock(return_value=settings)))
        assert response["_status"] == 200
        assert response["success"] is True
        if "enabled" in settings:
            assert response["message"] == "Optimizer transition continuing in background"
            await entry_data["_optimization_settings_transition_task"]
            assert entry.options["optimization_enabled"] is settings["enabled"]
        coordinator.set_settings.assert_awaited_once_with(settings)
        assert h.calls == calls_before, "settings preflight must not refresh or contact hardware"

    asyncio.run(run())


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
