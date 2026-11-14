"""Access guidance follows local pairing, admission and recovery independently."""
import ast
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"
spec = importlib.util.spec_from_file_location("_ha_access_presentation", COMPONENT / "licensing/presentation.py")
presentation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(presentation)


def status(**changes):
    return {
        "access_state": "transition", "paired": True, "requires_resume": False,
        "can_optimize": True, "would_allow": True, "recovery_pending": False,
        "enforcement_mode": "shadow", "account_url": "https://powersync.cc/app/ha",
        "legacy_access_until": int(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()),
        **changes,
    }


def test_paired_transition_is_distinct_from_trial_and_needs_no_resume():
    result = presentation.optimizer_access_description(status())
    assert "Free existing-user transition" in result["access_summary"]
    assert "23 October 2026 at 00:00 UTC" in result["access_summary"]
    assert "separate from the seven-day trial" in result["access_summary"]
    assert result["pairing_status"].startswith("Paired")
    assert "No Resume action is needed" in result["next_step"]
    assert "close this window" in result["next_step"]
    assert "Start pairing" not in result["next_step"]
    assert "settings still determine" in result["next_step"]


def test_transition_does_not_imply_server_pairing():
    result = presentation.optimizer_access_description(status(paired=False))
    assert result["pairing_status"].startswith("Not paired")
    assert "Start pairing" in result["next_step"]
    assert "GitHub or Discord" in result["next_step"]
    assert "close this window" not in result["next_step"]


def test_pending_code_requests_approval_and_finish():
    result = presentation.optimizer_access_description(status(paired=False), "approval-code")
    assert "approval-code" in result["next_step"]
    assert "https://powersync.cc/app/ha" in result["next_step"]
    assert "Finish pairing" in result["next_step"]
    assert "Start pairing" not in result["next_step"]


@pytest.mark.parametrize("access_state", ["paid", "sponsor", "trial", "billing_pending", "payment_grace", "support"])
def test_valid_sources_do_not_show_raw_state_or_repairing(access_state):
    result = presentation.optimizer_access_description(status(access_state=access_state))
    assert result["access_summary"] != access_state
    assert "No Resume action is needed" in result["next_step"]
    assert "pairing" not in result["next_step"]


def test_recovery_takes_precedence_over_resume():
    result = presentation.optimizer_access_description(status(recovery_pending=True, requires_resume=True, can_optimize=False))
    assert "Recovery is still pending" in result["next_step"]
    assert "Choose **Resume optimisation**" not in result["next_step"]


def test_valid_access_with_resume_required_does_not_claim_ready():
    result = presentation.optimizer_access_description(status(requires_resume=True, can_optimize=False))
    assert "Choose **Resume optimisation**" in result["next_step"]
    assert "close this window" not in result["next_step"]


def test_settings_explain_saved_on_during_access_pause():
    paused = presentation.optimizer_settings_description(
        status(requires_resume=True, can_optimize=False),
        enabled=True,
        running=False,
    )
    assert "Saved setting is on" in paused
    assert "paused" in paused
    assert "Resume optimisation" in paused
    assert "fresh schedule" in paused
    assert presentation.optimizer_settings_description(
        status(recovery_pending=True, requires_resume=True, can_optimize=False),
        enabled=True,
        running=False,
    ).find("restored") >= 0
    assert presentation.optimizer_settings_description(
        status(), enabled=True, running=True
    ) == "Smart Optimization is running."
    assert presentation.optimizer_settings_description(
        status(requires_resume=True, can_optimize=False),
        enabled=False,
        running=False,
    ) == "Smart Optimization is off in saved settings."


@pytest.mark.parametrize("access_state", ["expired", "subscription_required", "checking", "installation_revoked", "unknown_future_state"])
def test_unavailable_access_is_not_presented_as_ready(access_state):
    result = presentation.optimizer_access_description(status(
        access_state=access_state, requires_resume=True, can_optimize=False,
        would_allow=False, enforcement_mode="enforced",
    ))
    assert "Once access is restored" in result["next_step"]
    assert "No Resume action is needed" not in result["next_step"]


def flow_method(monkeypatch, manager):
    """Run the actual options method with only HA form rendering stubbed."""
    package = "_ha_access_flow_tests"
    for name in (package, f"{package}.licensing"):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    module = ModuleType(f"{package}.licensing.manager")
    module.async_get_manager = AsyncMock(return_value=manager)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, f"{package}.licensing.presentation", presentation)
    tree = ast.parse((COMPONENT / "config_flow.py").read_text())
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_optimizer_access")
    namespace = {
        "__package__": package,
        "vol": SimpleNamespace(Schema=lambda schema: schema, Required=lambda name, **kwargs: name),
        "SelectSelector": lambda config: config,
        "SelectSelectorConfig": lambda **kwargs: kwargs,
        "SelectSelectorMode": SimpleNamespace(DROPDOWN="dropdown"),
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), "config_flow.py", "exec"), namespace)
    return namespace[method.name]


@pytest.mark.parametrize("action", ["finish_pairing", "reset_pairing"])
def test_successful_pairing_or_reset_clears_previous_code(monkeypatch, action):
    manager = SimpleNamespace(
        finish_pairing=AsyncMock(), reset_pairing=AsyncMock(),
        status=lambda entry: status(paired=action == "finish_pairing"),
    )
    flow = SimpleNamespace(hass=object(), config_entry=SimpleNamespace(entry_id="one"),
                           _license_pairing_code="old-code", async_show_form=lambda **kwargs: kwargs)
    result = asyncio.run(flow_method(monkeypatch, manager)(flow, {"action": action}))
    assert flow._license_pairing_code is None
    assert "old-code" not in result["description_placeholders"]["next_step"]
    assert not result["errors"]
    if action == "finish_pairing":
        manager.finish_pairing.assert_awaited_once_with("old-code")
        assert "No Resume action is needed" in result["description_placeholders"]["next_step"]
    else:
        manager.reset_pairing.assert_awaited_once()
        assert "Start pairing" in result["description_placeholders"]["next_step"]


def test_failed_pairing_retains_code_for_retry(monkeypatch):
    manager = SimpleNamespace(finish_pairing=AsyncMock(side_effect=ValueError("not_approved")),
                              status=lambda entry: status(paired=False))
    flow = SimpleNamespace(hass=object(), config_entry=SimpleNamespace(entry_id="one"),
                           _license_pairing_code="retry-code", async_show_form=lambda **kwargs: kwargs)
    result = asyncio.run(flow_method(monkeypatch, manager)(flow, {"action": "finish_pairing"}))
    assert flow._license_pairing_code == "retry-code"
    assert "retry-code" in result["description_placeholders"]["next_step"]
    assert result["errors"]["base"] == "optimizer_access_unavailable"


def test_translations_render_the_actual_placeholders():
    steps = [json.loads((COMPONENT / name).read_text())["options"]["step"]["optimizer_access"]
             for name in ("strings.json", "translations/en.json")]
    assert steps[0] == steps[1]
    rendered = steps[0]["description"].format(**presentation.optimizer_access_description(status()))
    assert "Resume required: False" not in rendered
    assert "Not started" not in rendered
    assert "No Resume action is needed" in rendered

    settings = [json.loads((COMPONENT / name).read_text())["options"]["step"]["optimization"]
                for name in ("strings.json", "translations/en.json")]
    assert settings[0] == settings[1]
    settings_description = settings[0]["description"].format(
        optimizer_runtime_status=presentation.optimizer_settings_description(
            status(requires_resume=True, can_optimize=False), enabled=True, running=False
        )
    )
    assert "Saved setting is on, but optimisation is paused" in settings_description
    charge_description = settings[0]["sections"]["battery_forecast_inputs"]["data_description"]["optimization_max_charge_w"]
    assert "solar and grid charging together" in charge_description


def setup_licensing(manager):
    """Run setup's licensing lifecycle hooks, with unrelated HA setup excluded."""
    from copy import deepcopy

    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
    function = deepcopy(function)
    hooks = []
    for node in function.body:
        value = getattr(node, "value", None)
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and (
            isinstance(value.func, ast.Name) and value.func.id == "async_get_manager"
            or isinstance(value.func, ast.Attribute) and value.func.attr == "entry_loaded"
        ):
            hooks.append(node)
    function.body = hooks + [ast.Return(value=ast.Constant(value=True))]
    namespace = dict(HomeAssistant=object, ConfigEntry=object, license_manager=manager,
                     async_get_manager=AsyncMock(return_value=manager))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "__init__.py", "exec"), namespace)
    return namespace[function.name]


def switch_turn_on(monkeypatch, runtime):
    """Execute the real switch guard and option writes without loading all HA platforms."""
    import logging

    package = "_ha_access_flow_tests"
    monkeypatch.setitem(sys.modules, package + ".licensing.runtime", runtime)
    exceptions = ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    monkeypatch.setitem(sys.modules, exceptions.__name__, exceptions)
    tree = ast.parse((COMPONENT / "switch.py").read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OptimizationEnabledSwitch")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_turn_on")
    namespace = dict(__package__=package, Any=object, _LOGGER=logging.getLogger(__name__),
                     CONF_OPTIMIZATION_PROVIDER="optimization_provider", OPT_PROVIDER_POWERSYNC="powersync",
                     CONF_OPTIMIZATION_ENABLED="optimization_enabled")
    exec(compile(ast.Module(body=[method], type_ignores=[]), "switch.py", "exec"), namespace)
    return namespace[method.name]


@pytest.mark.parametrize("source", ["trial", "billing_pending", "paid"])
@pytest.mark.parametrize("disabled_options", [
    {"optimization_enabled": False, "optimization_provider": "powersync"},
    {"optimization_enabled": True, "optimization_provider": "native"},
])
def test_refresh_resume_and_switch_after_reload_without_optimizer(monkeypatch, source, disabled_options):
    from unittest.mock import Mock
    import test_ha_optimizer_runtime as rt

    async def run():
        h = rt.Harness()
        await h.activate()
        manager = h.client
        entry = SimpleNamespace(entry_id="one", domain="power_sync", data={}, options=disabled_options.copy())
        update = Mock()
        hass = SimpleNamespace(data={rt.runtime.DATA_KEY: manager},
                               config_entries=SimpleNamespace(async_get_entry=lambda _: entry, async_update_entry=update))
        manager.hass = hass
        manager.entry_loaded = lambda entry_id: rt.manager_method("entry_loaded")(manager, entry_id)
        manager.coordinators = {}
        rt.attach_enable_manager(manager, hass)
        manager.entries["one"] = {"generation": 1}
        old_owner = SimpleNamespace(hass=hass, entry_id="one", _licensing_entry_generation=1)
        writes = []

        @rt.guard.optimizer_work
        async def stale_work(owner):
            writes.append("old control")

        # Unload fences the retired optimizer; disabled/native setup has no new
        # optimizer to attach. Both states must still allow the HA switch later.
        rt.manager_method("detach")(manager, "one")
        generation = manager.entries["one"]["generation"]
        assert manager.entries["one"]["unloaded"]
        assert await stale_work(old_owner) is False
        assert await setup_licensing(manager)(hass, entry)

        async def request(path, body):
            if path == "/policy":
                return {"policy": rt.sign(h.policy, "PSHP1")}
            assert path == "/refresh"
            return {"permission": rt.sign({**rt.fixture["claims"],
                "installation_id": manager.installation_id, "entitlement_revision": 2, "source_state": source})}

        manager._request = request
        manager.reconcile_entries = lambda: rt.manager_method("reconcile_entries")(manager)
        manager.resume_entry = lambda entry_id, **kwargs: rt.manager_method("resume_entry")(manager, entry_id, **kwargs)
        flow = SimpleNamespace(hass=hass, config_entry=entry, async_show_form=lambda **kwargs: kwargs)
        step = flow_method(monkeypatch, manager)
        assert not (await step(flow, {"action": "refresh"}))["errors"]
        assert not (await step(flow, {"action": "resume"}))["errors"]
        switch = SimpleNamespace(hass=hass, _entry=entry, _attr_is_on=False, async_write_ha_state=Mock())
        await switch_turn_on(monkeypatch, rt.runtime)(switch)
        assert switch._attr_is_on
        assert update.call_args.kwargs["options"]["optimization_enabled"] is True
        assert update.call_args.kwargs["options"]["optimization_provider"] == "powersync"
        assert manager.status("one")["can_optimize"]
        assert manager.entries["one"]["generation"] > generation
        assert await stale_work(old_owner) is False
        assert writes == []

    asyncio.run(run())


def test_ha_switch_enable_uses_the_shared_refresh_and_resume(monkeypatch):
    from unittest.mock import Mock
    import test_ha_optimizer_runtime as rt

    async def run():
        h = rt.Harness()
        await h.activate()
        h.client.entries["one"] = {"resume_required": True}
        entry = SimpleNamespace(entry_id="one", domain="power_sync", data={}, options={"optimization_enabled": False})
        def update(target, *, data, options):
            assert not h.client.permits("one")
            target.data, target.options = data, options
        hass = SimpleNamespace(data={rt.runtime.DATA_KEY: h.client}, config_entries=SimpleNamespace(
            async_get_entry=lambda _: entry, async_update_entry=update))
        rt.attach_enable_manager(h.client, hass)
        switch = SimpleNamespace(hass=hass, _entry=entry, _attr_is_on=False, async_write_ha_state=Mock())
        before = len(h.calls)
        await switch_turn_on(monkeypatch, rt.runtime)(switch)
        assert switch._attr_is_on
        assert entry.options["optimization_enabled"]
        assert h.client.permits("one")
        assert [path for path, _ in h.calls[before:]] == ["/policy", "/refresh"]
    asyncio.run(run())


@pytest.mark.parametrize("target_enabled,previous_enabled,denied", [
    (True, False, False), (True, False, True), (False, True, True), (True, True, False),
])
def test_ha_options_enable_checks_access_before_persisting(target_enabled, previous_enabled, denied):
    """Exercise the real final options persistence block with normalized form inputs."""
    from copy import deepcopy
    from unittest.mock import Mock
    import logging
    import test_ha_optimizer_runtime as rt

    async def run():
        h = rt.Harness()
        await h.activate()
        h.denied = denied
        h.revision = 2
        entry = SimpleNamespace(entry_id="one", domain="power_sync", data={}, options={
            "optimization_enabled": previous_enabled, "optimization_provider": "powersync",
        })
        def update(target, *, data, options):
            if target_enabled and not previous_enabled:
                assert not h.client.permits("one")
            target.data, target.options = data, options
        write = Mock(side_effect=update)
        hass = SimpleNamespace(data={rt.runtime.DATA_KEY: h.client}, config_entries=SimpleNamespace(
            async_get_entry=lambda _: entry, async_update_entry=write))
        rt.attach_enable_manager(h.client, hass)
        flow = SimpleNamespace(hass=hass, config_entry=entry,
            _get_option=lambda key, default: entry.options.get(key, default),
            _async_step_optimization=AsyncMock(return_value={"access_error": True}))
        tree = ast.parse((COMPONENT / "config_flow.py").read_text())
        method = deepcopy(next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_step_optimization"))
        save = next(node for node in ast.walk(method) if isinstance(node, ast.Try) and node.finalbody
                    and any(isinstance(n, ast.Name) and n.id == "finish_monitoring_handoff" for n in ast.walk(node)))
        method.body = [save, ast.Return(value=ast.Constant(True))]
        namespace = dict(Any=object, FlowResult=object, entry_data={}, persisted_changed=True,
            monitoring_handoff=False, optimization_enabled=target_enabled, new_data={},
            new_options={"optimization_enabled": target_enabled, "optimization_provider": "powersync"},
            CONF_OPTIMIZATION_PROVIDER="optimization_provider", CONF_OPTIMIZATION_ENABLED="optimization_enabled",
            OPT_PROVIDER_NATIVE="native", OPT_PROVIDER_POWERSYNC="powersync", _LOGGER=logging.getLogger(__name__))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "config_flow.py", "exec"), namespace)
        before = len(h.calls)
        result = await namespace[method.name](flow)
        if target_enabled and denied:
            assert result == {"access_error": True}
            write.assert_not_called()
            assert not entry.options["optimization_enabled"]
            assert "_skip_reload" not in namespace["entry_data"]
        else:
            assert result is True
            write.assert_called_once()
            assert entry.options["optimization_enabled"] is target_enabled
        expected = ["/policy", "/refresh"] if target_enabled and not previous_enabled else []
        assert [path for path, _ in h.calls[before:]] == expected
    asyncio.run(run())
