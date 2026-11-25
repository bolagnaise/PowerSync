"""Access guidance follows local pairing, admission and recovery independently."""
import ast
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    # Monitoring mode plans without commands, so it is never "running" control.
    assert presentation.optimizer_settings_description(
        status(), enabled=True, running=True, monitoring=True
    ) == (
        "Smart Optimization is planning only. Monitoring mode is on, "
        "so no battery commands are sent."
    )
    assert presentation.optimizer_settings_description(
        status(), enabled=False, running=False, monitoring=True
    ) == "Smart Optimization is off in saved settings."
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
    from test_ha_optimizer_runtime import runtime

    module = ModuleType(f"{package}.licensing.manager")
    monkeypatch.setitem(sys.modules, f"{package}.licensing.runtime", runtime)
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


def test_reopened_dialog_guides_user_back_to_original_without_disclosing_its_code(monkeypatch):
    manager = SimpleNamespace(
        start_pairing=AsyncMock(return_value={"pairing_code": "original-code"}),
        finish_pairing=AsyncMock(), status=lambda entry: status(paired=False),
    )
    step = flow_method(monkeypatch, manager)
    original = SimpleNamespace(hass=object(), config_entry=SimpleNamespace(entry_id="one"),
                               async_show_form=lambda **kwargs: kwargs)
    reopened = SimpleNamespace(**vars(original))
    asyncio.run(step(original, {"action": "start_pairing"}))
    result = asyncio.run(step(reopened, {"action": "finish_pairing"}))
    assert result["errors"] == {"base": "optimizer_pairing_no_code"}
    assert "original-code" not in str(result)
    assert not result["description_placeholders"]["next_step"]
    manager.finish_pairing.assert_not_awaited()

    assert not asyncio.run(step(original, {"action": "finish_pairing"}))["errors"]
    manager.finish_pairing.assert_awaited_once_with("original-code")


def http_transport(http_status, chunks, *, content_length=None):
    """Use the production HA transport with a local, chunked HTTP response."""
    import test_ha_optimizer_runtime as rt
    from aiohttp import ClientTimeout

    response = SimpleNamespace(status=http_status, content_length=content_length,
                               content=SimpleNamespace(read=AsyncMock(side_effect=[*chunks, b""])))
    session = MagicMock()
    session.request.return_value.__aenter__.return_value = response
    transport = rt.manager_method("_transport")
    transport.__globals__.update(async_get_clientsession=lambda hass: session, ClientTimeout=ClientTimeout,
                                 API="https://example.invalid", PairingConflict=rt.runtime.PairingConflict)
    return transport, response


@pytest.mark.parametrize(("reason", "expected", "guidance"), [
    ("pairing_not_completed", "optimizer_pairing_not_completed", "previously disconnected this HA server"),
    ("installation_slot_in_use", "optimizer_installation_slot_in_use", "connected installation"),
    ("pairing_pending_or_expired", "optimizer_pairing_pending_or_expired", "new one"),
    ("unrecognised private detail", "optimizer_access_unavailable", "Unable to complete"),
])
def test_exchange_conflict_explains_next_step_and_preserves_identity(monkeypatch, reason, expected, guidance):
    import test_ha_optimizer_runtime as rt

    async def run():
        h = rt.Harness()
        manager = h.client
        await manager.initialize()
        original_identity = (manager.installation_id, manager.credential)
        manager.hass = object()
        transport, _ = http_transport(409, [json.dumps({"error": reason}).encode()])
        manager._request = lambda path, body: transport(manager, path, body)
        flow = SimpleNamespace(hass=manager.hass, config_entry=SimpleNamespace(entry_id="one"),
                               _license_pairing_code="pending-code", async_show_form=lambda **kw: kw)
        step = flow_method(monkeypatch, manager)
        result = await step(flow, {"action": "finish_pairing"})
        assert result["errors"] == {"base": expected}
        assert "unrecognised private detail" not in str(result)
        texts = [json.loads((COMPONENT / name).read_text())["options"]["error"][expected]
                 for name in ("strings.json", "translations/en.json")]
        assert texts[0] == texts[1]
        assert guidance in texts[0]
        if reason == "pairing_not_completed":
            assert all(action in texts[0] for action in ("Reset pairing", "Start pairing", "Finish pairing"))
        if reason in ("pairing_not_completed", "installation_slot_in_use"):
            assert not result["description_placeholders"]["next_step"]
        assert (manager.installation_id, manager.credential) == original_identity
        assert not manager.paired and not manager.permits("one")
        assert h.stored["paired"] is False

        if reason == "pairing_not_completed":
            assert not (await step(flow, {"action": "reset_pairing"}))["errors"]
            assert manager.installation_id != original_identity[0]
            assert manager.credential != original_identity[1]
            manager._request = h.request
            assert not (await step(flow, {"action": "start_pairing"}))["errors"]
            assert not (await step(flow, {"action": "finish_pairing"}))["errors"]
            assert manager.paired and h.stored["paired"]

    asyncio.run(run())


@pytest.mark.parametrize(("path", "http_status", "chunks", "length", "reads"), [
    ("/refresh", 409, [b'{"error":"pairing_not_completed"}'], None, 0),
    ("/pairing/exchange", 503, [b'{"error":"pairing_not_completed"}'], None, 0),
    ("/pairing/exchange", 409, [b'{"error":"pairing_not_completed"}'], 16385, 0),
    ("/pairing/exchange", 409, [b" " * 8192, b" " * 8192, b" "], None, 3),
    ("/pairing/exchange", 409, [b"invalid JSON"], None, 2),
    ("/pairing/exchange", 409, [b'{"error":["pairing_not_completed"]}'], None, 2),
    ("/pairing/exchange", 409, [b'{"paired":true}'], None, 2),
])
def test_only_known_bounded_exchange_conflicts_have_specific_errors(path, http_status, chunks, length, reads):
    import test_ha_optimizer_runtime as rt

    transport, response = http_transport(http_status, chunks, content_length=length)
    with pytest.raises(ValueError) as exc:
        asyncio.run(transport(SimpleNamespace(hass=object()), path, {}))
    assert not isinstance(exc.value, rt.runtime.PairingConflict)
    assert str(exc.value) in ("licensing_service_unavailable", "invalid_licensing_response")
    assert response.content.read.await_count == reads


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
        manager.resume_entry = lambda entry_id: rt.manager_method("resume_entry")(manager, entry_id)
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
        assert manager.entries["one"]["generation"] == generation
        assert await stale_work(old_owner) is False
        assert writes == []

    asyncio.run(run())
