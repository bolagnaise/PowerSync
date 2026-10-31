"""Native Amber command, ownership and failure-path regressions (no network)."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"


class FakeStore:
    def __init__(self, saved=None):
        self.saved = saved

    async def async_load(self):
        return deepcopy(self.saved)

    async def async_save(self, data):
        self.saved = deepcopy(data)


class FakeClient:
    def __init__(self):
        self.override = None
        self.enabled = True
        self.soc = 60
        self.calls = []
        self.config = "battery-a"
        self.fail_add = False
        self.drop_readback = False
        self.fail_cancel = False
        self.seq = 0

    async def stats(self, site):
        return {"live": {"stateOfChargePercentage": self.soc, "batteryPowerW": -2000},
                "override": deepcopy(self.override), "capacity_wh": 13500,
                "strategy": {"configId": self.config, "status": "enabled" if self.enabled else "disabled"}}

    async def earnings(self, site):
        return {"importCostsCents": 3, "exportEarningsCents": None, "totalEarningsCents": -3}

    async def discover(self):
        return "site-a", self.config

    async def add(self, site, config, mode, start, end, *, before_write=None):
        if before_write:
            before_write()
        self.seq += 1
        self.calls.append(("add", site, mode, start, end))
        if self.fail_add:
            raise self.error("Uncertain write")
        result = {"overrideId": f"override-{self.seq}", "value": mode, "validFrom": start, "validTo": end}
        if not self.drop_readback:
            self.override = deepcopy(result)
        return result

    async def cancel(self, site, config, override_id):
        self.calls.append(("cancel", site, override_id))
        if self.fail_cancel:
            raise self.error("Cancellation failed")
        if self.override and self.override["overrideId"] == override_id:
            self.override = None

    async def enable(self, config, enabled, *, before_write=None):
        if before_write:
            before_write()
        self.calls.append(("enable", config, enabled))
        self.enabled = enabled


@pytest.fixture
def modules(monkeypatch):
    def module(name, **attrs):
        item = ModuleType(name)
        item.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, item)
        return item

    class HAError(Exception):
        pass

    class AuthFailed(HAError):
        pass

    class UpdateFailed(Exception):
        pass

    class Coordinator:
        def __init__(self, hass, logger, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_shutdown(self):
            pass

    # A distinct package name avoids importing the integration or leaking stubs.
    package = module("amber_native_test")
    package.__path__ = [str(COMPONENT)]
    module("homeassistant")
    module("homeassistant.exceptions", HomeAssistantError=HAError, ConfigEntryAuthFailed=AuthFailed)
    module("homeassistant.helpers")
    module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
    module("homeassistant.helpers.storage", Store=lambda *args: FakeStore())
    module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=Coordinator, UpdateFailed=UpdateFailed)
    licensing = module("amber_native_test.licensing")
    licensing.__path__ = []
    module("amber_native_test.licensing.guard", service_allowed=lambda *args: True,
           optimizer_command=lambda func: func, optimizer_service_data=lambda owner, data: {**data, "entry_id": owner.entry_id})
    def load(suffix, path):
        name = "amber_native_test." + suffix
        spec = importlib.util.spec_from_file_location(name, COMPONENT / path)
        item = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, item)
        spec.loader.exec_module(item)
        return item
    constants = load("amber_smartshift", "amber_smartshift/__init__.py")
    client = load("amber_smartshift.client", "amber_smartshift/client.py")
    runtime = load("amber_smartshift.runtime", "amber_smartshift/runtime.py")
    controls = load("amber_smartshift.controls", "amber_smartshift/controls.py")
    clock = datetime(2026, 10, 20, 10, 15, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "utcnow", lambda: clock)
    async def no_sleep(delay):
        pass
    monkeypatch.setattr(runtime.asyncio, "sleep", no_sleep)
    return SimpleNamespace(constants=constants, runtime=runtime, client=client, controls=controls, clock=clock, error=HAError)


@pytest.fixture
def native(modules):
    constants = modules.constants
    data = {constants.EMAIL: "account@example.invalid", constants.PASSWORD: "test-password",
            constants.SITE: "site-a", constants.CONFIG: "battery-a", constants.MANAGED: False,
            "amber_site_id": "site-a", "electricity_provider": "amber"}
    entry = SimpleNamespace(entry_id="entry-a", data=data, options={})
    meter = SimpleNamespace(state="1000", attributes={"unit_of_measurement": "W"}, last_reported=modules.clock)
    hass = SimpleNamespace(data={"power_sync": {"entry-a": {}}}, states=SimpleNamespace(get=lambda entity: meter))
    async def executor(func):
        return func()
    hass.async_add_executor_job = executor
    client = FakeClient()
    client.error = modules.client.AmberError
    store = FakeStore()
    coordinator = modules.runtime.AmberBatteryCoordinator(hass, entry, client=client, store=store)
    return SimpleNamespace(coordinator=coordinator, client=client, store=store, entry=entry, hass=hass, modules=modules)


def automatic(native):
    native.entry.options.update({"amber_smartshift_managed": True,
        "custom_grid_power_entity": "sensor.grid", "custom_solar_power_entity": "sensor.solar", "custom_load_power_entity": "sensor.load"})
    native.coordinator.entry_data().update({
        "amber_coordinator": SimpleNamespace(last_update_success=True, data={"last_update": native.modules.clock, "current": [
            {"type": "CurrentInterval", "channelType": channel, "perKwh": value,
             "startTime": (native.modules.clock - timedelta(seconds=30)).isoformat(),
             "endTime": (native.modules.clock + timedelta(minutes=4, seconds=30)).isoformat()}
            for channel, value in (("general", 20), ("feedIn", -15))] }),
        "optimization_coordinator": SimpleNamespace(_last_update_time=native.modules.clock,
            _grid_charge_blackout_active_now=lambda: False, _amber_spike_protected_slots=lambda *args: [False],
            _scheduled_ev_preserve_active=lambda: False, _should_block_export_for_demand=lambda: False),
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["charge", "discharge", "preserve-charge", "self-consume"])
async def test_manual_modes_have_durable_id_and_reported_readback(native, mode):
    result = await native.coordinator.command(mode, 30)
    assert result is True
    assert native.store.saved["owned"]["id"] == "override-1"
    assert native.store.saved["owned"]["phase"] == "active"
    assert native.coordinator.command_state == "reported"
    assert native.coordinator.owner() == "manual"
    assert native.client.calls[0][2] == mode
    assert "test-password" not in json.dumps(native.coordinator.api_data())


@pytest.mark.asyncio
async def test_disabled_smartshift_requires_explicit_enable_permission(native):
    native.client.enabled = False
    with pytest.raises(native.modules.error, match="Enable Smart Shift"):
        await native.coordinator.command("charge")
    assert native.client.calls == []
    native.entry.options["amber_smartshift_allow_enable"] = True
    await native.coordinator.command("charge")
    assert [call[0] for call in native.client.calls] == ["enable", "add"]


@pytest.mark.asyncio
async def test_automatic_requires_owner_prices_meters_and_fresh_plan(native):
    with pytest.raises(native.modules.error, match="disabled"):
        await native.coordinator.command("charge", source="optimizer")
    automatic(native)
    await native.coordinator.command("charge", source="optimizer")
    end = native.modules.runtime.timestamp(native.client.override["validTo"])
    start = native.modules.runtime.timestamp(native.client.override["validFrom"])
    assert end - start == timedelta(minutes=10)
    assert native.coordinator.owner() == "optimizer"


@pytest.mark.asyncio
@pytest.mark.parametrize("change,pattern", [
    ({"monitoring_mode": True}, "disabled"),
    ({"optimization_enabled": False}, "disabled"),
    ({"custom_grid_power_entity": ""}, "meters"),
    ({"amber_site_id": "site-b"}, "sites must match"),
    ({"optimization_allow_grid_charge": False}, "Grid charging is disabled"),
])
async def test_automatic_runtime_guards_reject_legacy_bad_options(native, change, pattern):
    automatic(native)
    native.entry.options.update(change)
    with pytest.raises(native.modules.error, match=pattern):
        await native.coordinator.command("charge", source="optimizer")
    assert native.client.calls == []


@pytest.mark.asyncio
async def test_stale_plan_cannot_extend_a_lease(native):
    automatic(native)
    native.coordinator.entry_data()["optimization_coordinator"]._last_update_time -= timedelta(minutes=7)
    with pytest.raises(native.modules.error, match="fresh optimiser plan"):
        await native.coordinator.command("charge", source="optimizer")
    assert not native.client.calls


@pytest.mark.asyncio
async def test_nonzero_watt_request_is_rejected_and_adapter_uses_modes(native):
    with pytest.raises(native.modules.error, match="target watts"):
        await native.coordinator.command("charge", power_w=1000)
    assert not native.client.calls
    automatic(native)
    adapter = native.modules.controls.AmberOptimizerController(native.coordinator)
    assert await adapter.force_charge(10, power_w=5000)
    assert native.client.calls[0][2] == "charge"
    assert not hasattr(adapter, "set_backup_reserve")


@pytest.mark.asyncio
async def test_external_override_is_never_cancelled_or_adopted(native):
    native.client.override = {"overrideId": "app-owned", "value": "charge", "validTo": (native.modules.clock + timedelta(hours=1)).isoformat()}
    with pytest.raises(native.modules.error, match="external override"):
        await native.coordinator.command("charge")
    assert native.coordinator.owner() == "external"
    assert await native.coordinator.restore_normal()
    assert native.client.calls == []
    assert native.client.override["overrideId"] == "app-owned"


@pytest.mark.asyncio
async def test_manual_override_prevents_automatic_replacement_or_restore(native):
    automatic(native)
    await native.coordinator.command("charge")
    with pytest.raises(native.modules.error, match="owns the battery"):
        await native.coordinator.command("discharge", source="optimizer")
    assert await native.coordinator.restore_normal(automatic=True) is False
    assert [c[0] for c in native.client.calls] == ["add"]


@pytest.mark.asyncio
async def test_poll_cancels_owned_command_at_soc_limit(native):
    await native.coordinator.command("discharge")
    native.client.soc = 20
    result = await native.coordinator._async_update_data()
    assert [c[0] for c in native.client.calls] == ["add", "cancel"]
    assert not native.store.saved["owned"]
    assert result["amber_smartshift"]["override"] == {}


@pytest.mark.asyncio
async def test_failed_cleanup_survives_restart_and_reconciles_exact_id(native):
    await native.coordinator.command("charge")
    native.client.fail_cancel = True
    with pytest.raises(native.modules.error, match="cleanup is pending"):
        await native.coordinator.restore_normal()
    assert native.store.saved["owned"]["phase"] == "cancelling"
    native.client.fail_cancel = False
    restarted = native.modules.runtime.AmberBatteryCoordinator(native.hass, native.entry, client=native.client, store=native.store)
    await restarted._async_update_data()
    assert restarted.owned == {}
    assert native.client.calls[-1] == ("cancel", "site-a", "override-1")


@pytest.mark.asyncio
async def test_lost_add_response_never_replays_mutation(native):
    native.client.fail_add = True
    with pytest.raises(native.modules.error, match="Uncertain write"):
        await native.coordinator.command("charge")
    assert native.store.saved["owned"]["phase"] == "adding"
    native.client.fail_add = False
    with pytest.raises(native.modules.error, match="reconciliation"):
        await native.coordinator.command("charge")
    assert len(native.client.calls) == 1


@pytest.mark.asyncio
async def test_acknowledgement_without_readback_is_not_reported_active(native):
    native.client.drop_readback = True
    with pytest.raises(native.modules.error, match="has not reported"):
        await native.coordinator.command("charge")
    assert native.coordinator.command_state == "unconfirmed"
    assert not native.coordinator.active_override()


@pytest.mark.asyncio
async def test_expired_override_is_not_current(native):
    await native.coordinator.command("charge", 1)
    native.client.override["validTo"] = (native.modules.clock - timedelta(seconds=1)).isoformat()
    await native.coordinator._async_update_data()
    assert native.coordinator.active_override() == {}
    assert native.coordinator.owner() is None
    assert native.coordinator.owned == {}


@pytest.mark.asyncio
async def test_concurrent_identical_requests_do_not_duplicate_writes(native):
    await asyncio.gather(native.coordinator.command("charge"), native.coordinator.command("charge"))
    assert len(native.client.calls) == 1


@pytest.mark.asyncio
async def test_unknown_soc_is_unavailable_and_blocks_commands(native):
    native.client.soc = None
    result = await native.coordinator._async_update_data()
    assert result["battery_level"] is None
    assert result["telemetry_ready"] is False
    with pytest.raises(native.modules.error, match="Fresh Amber battery"):
        await native.coordinator.command("charge")
    assert not native.client.calls


@pytest.mark.asyncio
async def test_units_capacity_and_power_sign_are_normalized_once(native):
    result = await native.coordinator._async_update_data()
    assert result["battery_power"] == -2
    assert result["battery_capacity_kwh"] == 13.5
    assert result["grid_power"] is None


@pytest.mark.asyncio
async def test_changed_identity_blocks_all_writes(native):
    native.client.config = "different-battery"
    with pytest.raises(native.modules.error, match="configuration changed"):
        await native.coordinator.command("charge")
    assert not native.client.calls


@pytest.mark.asyncio
async def test_shutdown_fences_future_writes_and_cleans_own_override(native):
    await native.coordinator.command("charge")
    await native.coordinator.shutdown()
    with pytest.raises(native.modules.error, match="stopping"):
        await native.coordinator.command("discharge")
    assert native.client.calls[-1][0] == "cancel"


class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        return self.body


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(401, {}), (200, {"errors": [{"message": "secret-token"}]}), (429, {}), (200, {})])
async def test_transport_sanitizes_errors_and_never_retries_mutations(modules, status, body):
    calls = []
    session = SimpleNamespace(post=lambda *args, **kwargs: calls.append(kwargs) or Response(status, body))
    async def executor(func):
        return func()
    client = modules.client.AmberClient(session, executor, "account", "password")
    claim = base64.urlsafe_b64encode(json.dumps({"exp": 9999999999}).encode()).decode().rstrip("=")
    client._identity = SimpleNamespace(id_token="header." + claim + ".sig")
    with pytest.raises(modules.client.AmberError) as error:
        await client.request("mutation Example { value }", mutation=True)
    assert "secret-token" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unconfirmed_known_id_is_retained_until_readback_or_expiry(native):
    native.client.drop_readback = True
    with pytest.raises(native.modules.error):
        await native.coordinator.command("charge")
    await native.coordinator._async_update_data()
    assert native.store.saved["owned"]["phase"] == "confirming"
    with pytest.raises(native.modules.error, match="reconciliation"):
        await native.coordinator.command("charge")
    assert len(native.client.calls) == 1
    native.client.override = {"overrideId": "override-1", "value": "charge", "validTo": native.coordinator.owned["end"]}
    await native.coordinator._async_update_data()
    assert native.coordinator.command_state == "reported"
    assert native.coordinator.command_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,value", [("feedIn", 2), ("feedIn", 0), ("feedIn", -5)])
async def test_export_price_sign_and_configured_floor_are_enforced(native, channel, value):
    automatic(native)
    native.entry.options["optimization_min_export_price"] = .1
    prices = native.coordinator.entry_data()["amber_coordinator"].data["current"]
    next(p for p in prices if p["channelType"] == channel)["perKwh"] = value
    with pytest.raises(native.modules.error, match="export price"):
        await native.coordinator.command("discharge", source="automation")
    assert not native.client.calls


@pytest.mark.asyncio
async def test_fresh_fetch_does_not_make_an_expired_price_interval_usable(native):
    automatic(native)
    price = native.coordinator.entry_data()["amber_coordinator"].data["current"][0]
    price["endTime"] = (native.modules.clock - timedelta(seconds=1)).isoformat()
    with pytest.raises(native.modules.error, match="current Amber import interval"):
        await native.coordinator.command("charge", source="automation")
    assert not native.client.calls


@pytest.mark.asyncio
async def test_disabling_management_cancels_automatic_but_preserves_manual(native):
    automatic(native)
    await native.coordinator.command("charge", source="optimizer")
    native.entry.options["amber_smartshift_managed"] = False
    await native.coordinator._async_update_data()
    assert native.client.override is None
    await native.coordinator.command("charge")
    await native.coordinator._async_update_data()
    assert native.coordinator.owner() == "manual"


@pytest.mark.asyncio
async def test_new_external_override_is_preserved_after_restart(native):
    await native.coordinator.command("charge")
    native.client.override = {"overrideId": "new-app-id", "value": "discharge", "validTo": native.coordinator.owned["end"]}
    restarted = native.modules.runtime.AmberBatteryCoordinator(native.hass, native.entry, client=native.client, store=native.store)
    await restarted._async_update_data()
    await restarted.restore_normal()
    assert native.client.override["overrideId"] == "new-app-id"
    assert not any(c[0] == "cancel" for c in native.client.calls)


@pytest.mark.asyncio
async def test_site_locks_and_owned_ids_are_isolated(native):
    second_entry = SimpleNamespace(entry_id="entry-b", data={**native.entry.data}, options={})
    second = native.modules.runtime.AmberBatteryCoordinator(native.hass, second_entry, client=native.client, store=FakeStore())
    assert second.lock is native.coordinator.lock
    await native.coordinator.command("charge")
    with pytest.raises(native.modules.error, match="external override"):
        await second.command("discharge")
    await second.restore_normal()
    assert native.client.override["overrideId"] == "override-1"
    third_entry = SimpleNamespace(entry_id="entry-c", data={**native.entry.data, "amber_smartshift_site_id": "site-c", "amber_site_id": "site-c"}, options={})
    third = native.modules.runtime.AmberBatteryCoordinator(native.hass, third_entry, client=FakeClient(), store=FakeStore())
    assert third.lock is not second.lock


@pytest.mark.asyncio
async def test_credentials_refresh_once_for_concurrent_reads(modules):
    calls = []
    claim = base64.urlsafe_b64encode(json.dumps({"exp": 9999999999}).encode()).decode().rstrip("=")
    async def executor(func):
        calls.append("login")
        func()
    client = modules.client.AmberClient(None, executor, "email", "password")
    client._login = lambda: setattr(client, "_identity", SimpleNamespace(id_token="h." + claim + ".s"))
    results = await asyncio.gather(client._token(), client._token(), client._token())
    assert len(set(results)) == 1
    assert calls == ["login"]


@pytest.fixture
def flow_module(modules, monkeypatch):
    # The repository's lightweight Python harness does not install HA or vol.
    # Only form construction is needed here; API validation below is real code.
    vol = ModuleType("voluptuous")
    vol.UNDEFINED = object()
    class Marker:
        def __init__(self, schema, default=vol.UNDEFINED, **kwargs):
            self.schema, self.default = schema, default
    vol.Optional = vol.Required = Marker
    vol.Schema = lambda fields: SimpleNamespace(schema=fields)
    monkeypatch.setitem(sys.modules, "voluptuous", vol)
    selector = ModuleType("homeassistant.helpers.selector")
    class Selector:
        def __init__(self, *args, **kwargs):
            pass
        def __call__(self, value):
            return value
    for name in ("TextSelector", "BooleanSelector", "EntitySelector", "TextSelectorConfig", "EntitySelectorConfig"):
        setattr(selector, name, Selector)
    selector.TextSelectorType = SimpleNamespace(EMAIL="email", PASSWORD="password")
    monkeypatch.setitem(sys.modules, selector.__name__, selector)
    name = "amber_native_test.amber_smartshift.flow"
    spec = importlib.util.spec_from_file_location(name, COMPONENT / "amber_smartshift/flow.py")
    flow = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, flow)
    spec.loader.exec_module(flow)
    fake = FakeClient()
    monkeypatch.setattr(flow, "AmberClient", lambda *args: fake)
    return flow


@pytest.mark.asyncio
async def test_setup_uses_the_selected_pricing_site_and_defaults_to_manual(native, flow_module):
    class Flow(flow_module.AmberSetupMixin):
        hass = native.hass
        _amber_data = {"amber_api_token": "public-token"}
        _site_data = {"amber_site_id": "site-a"}
        _selected_electricity_provider = "amber"
        def _create_final_entry(self):
            return self._amber_smartshift_data
    result = await Flow().async_step_amber_smartshift({"amber_smartshift_email": "account", "amber_smartshift_password": "password", "amber_smartshift_managed": False})
    assert result["amber_smartshift_site_id"] == "site-a"
    assert result["battery_connection_profile"] == "amber_smartshift_native"
    assert result["amber_smartshift_managed"] is False


@pytest.mark.asyncio
async def test_setup_rejects_mismatched_site_and_automatic_mode_without_meters(native, flow_module):
    credentials = {"amber_smartshift_email": "account", "amber_smartshift_password": "password"}
    with pytest.raises(native.modules.client.AmberError, match="do not match"):
        await flow_module.validate(native.hass, credentials, "other-site", "amber")
    with pytest.raises(native.modules.client.AmberError, match="meters"):
        await flow_module.validate(native.hass, {**credentials, "amber_smartshift_managed": True}, "site-a", "amber")


def test_password_is_never_prefilled_in_setup_form(flow_module):
    form = flow_module.schema({"amber_smartshift_password": "secret"})
    password = next(key for key in form.schema if key.schema == "amber_smartshift_password")
    import voluptuous as vol
    assert password.default is vol.UNDEFINED


@pytest.mark.asyncio
async def test_transport_rechecks_admission_after_authentication(modules):
    calls = []
    session = SimpleNamespace(post=lambda *args, **kwargs: calls.append(kwargs) or Response(200, {"data": {}}))
    client = modules.client.AmberClient(session, None, "email", "password")
    allowed = [True]
    async def token():
        allowed[0] = False
        return "token"
    client._token = token
    def guard():
        if not allowed[0]:
            raise modules.error("Monitoring enabled while authentication was pending")
    with pytest.raises(modules.error, match="Monitoring enabled"):
        await client.request("mutation Example { value }", mutation=True, before_write=guard)
    assert calls == []


@pytest.mark.asyncio
async def test_command_never_crosses_write_boundary_after_permission_withdrawal(native):
    automatic(native)
    async def add(site, config, mode, start, end, *, before_write=None):
        native.entry.options["monitoring_mode"] = True
        before_write()
        pytest.fail("A blocked command reached Amber")
    native.client.add = add
    with pytest.raises(native.modules.error, match="disabled"):
        await native.coordinator.command("charge", source="optimizer")
    assert not native.coordinator.owned
    assert native.coordinator.command_state == "blocked"


@pytest.mark.asyncio
async def test_schema_drift_is_rejected_without_private_data(modules):
    client = modules.client.AmberClient(None, None, "email", "password")
    async def request(*args, **kwargs):
        return {"smartshift": ["private-account-data"]}
    client.request = request
    with pytest.raises(modules.client.AmberError) as error:
        await client.stats("site")
    assert "private-account-data" not in str(error.value)


@pytest.mark.asyncio
async def test_smartshift_off_releases_owned_override_and_pauses_powersync(native):
    automatic(native)
    def update(entry, *, options):
        entry.options = options
    native.hass.config_entries = SimpleNamespace(async_update_entry=update)
    await native.coordinator.command("charge", source="optimizer")
    await native.coordinator.set_smartshift(False)
    assert native.client.override is None
    assert native.client.enabled is False
    assert native.entry.options["amber_smartshift_managed"] is False
    assert [call[0] for call in native.client.calls] == ["add", "cancel", "enable"]


@pytest.mark.asyncio
async def test_status_retains_plan_but_reports_provider_mode_and_measured_power(native):
    await native.coordinator.command("preserve-charge")
    result = native.coordinator.project_optimizer_status({"planned_current_action": "export", "planned_current_power_w": 5000})
    assert result["planned_current_action"] == "export"
    assert result["planned_current_power_w"] == 5000
    assert result["effective_current_action"] == "idle"
    assert result["current_power_w"] == 2000
    assert result["idle_hold_reserve"] is None


@pytest.mark.asyncio
async def test_no_readback_cannot_be_published_as_the_planned_action(native):
    native.client.drop_readback = True
    with pytest.raises(native.modules.error):
        await native.coordinator.command("charge")
    result = native.coordinator.project_optimizer_status({"planned_current_action": "charge"})
    assert result["effective_current_action"] == "amber_managed"
    assert result["amber_smartshift"]["command_state"] == "unconfirmed"
    assert result["planned_current_action"] == "charge"


@pytest.mark.asyncio
async def test_missing_soc_produces_unknown_status_instead_of_planned_export(native):
    native.client.soc = None
    await native.coordinator._async_update_data()
    result = native.coordinator.project_optimizer_status({"planned_current_action": "export"})
    assert result["effective_current_action"] == "unknown"
    assert result["actual_battery_power_w"] is None


@pytest.mark.asyncio
async def test_future_override_is_pending_and_not_current_battery_mode(native):
    await native.coordinator._async_update_data()
    native.coordinator.snapshot["override"] = {"overrideId": "app", "value": "charge", "validFrom": (native.modules.clock + timedelta(minutes=5)).isoformat(), "validTo": (native.modules.clock + timedelta(minutes=15)).isoformat()}
    result = native.coordinator.project_optimizer_status({"planned_current_action": "export"})
    assert result["effective_current_action"] == "pending"
    assert native.coordinator.owner() == "external"


@pytest.mark.asyncio
async def test_missing_identity_cannot_use_ambers_implicit_default_site(native):
    native.coordinator.site = None
    native.coordinator.config = None
    native.client.config = None
    with pytest.raises(native.modules.error, match="validated Amber site"):
        await native.coordinator.command("charge")
    assert not native.client.calls


@pytest.mark.asyncio
async def test_options_can_renew_credentials_without_using_revoked_old_auth(native, flow_module):
    calls = []
    async def old_restore():
        calls.append("old-auth")
        raise native.modules.client.AmberAuthError("Old credentials revoked")
    native.coordinator.restore_normal = old_restore
    native.coordinator.entry_data()["amber_smartshift_coordinator"] = native.coordinator
    class Options(flow_module.AmberOptionsMixin):
        hass = native.hass
        config_entry = native.entry
        def _save_battery_system_selection(self, system):
            calls.append(("select", system))
        def _save_connection_and_reload(self, data, options):
            calls.append("save")
            return {"data": data, "options": options}
    result = await Options().async_step_amber_smartshift({
        "amber_smartshift_email": native.entry.data["amber_smartshift_email"],
        "amber_smartshift_password": "replacement-password", "amber_smartshift_managed": False,
    })
    assert "old-auth" not in calls
    assert calls == [("select", "amber_smartshift"), "save"]
    assert native.coordinator.stopping
    assert result["data"]["amber_smartshift_password"] == "replacement-password"
    assert "amber_smartshift_password" not in result["options"]


@pytest.mark.asyncio
async def test_invalid_new_connection_does_not_replace_the_running_backend(native, flow_module):
    native.entry.data["amber_site_id"] = "wrong-site"
    class Options(flow_module.AmberOptionsMixin):
        hass = native.hass
        config_entry = native.entry
        def _save_battery_system_selection(self, system):
            pytest.fail("Invalid credentials/site must not replace a running backend")
        def async_show_form(self, **kwargs):
            return kwargs
    result = await Options().async_step_amber_smartshift({
        "amber_smartshift_email": "account", "amber_smartshift_password": "password", "amber_smartshift_managed": False,
    })
    assert result["errors"]["base"] == "amber_smartshift_connection"


@pytest.mark.asyncio
async def test_monitoring_handoff_fences_automatic_writes_before_options_change(native):
    automatic(native)
    native.coordinator.entry_data()["_monitoring_handoff_active"] = True
    with pytest.raises(native.modules.error, match="disabled"):
        await native.coordinator.command("charge", source="automation")
    assert not native.client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["charge", "discharge"])
@pytest.mark.parametrize("data", [{"duration": 241}, {"duration": True}, {"duration": 1.5},
                                  {"duration": 7, "until": "18:30"}, {"until": "bad"}])
async def test_manual_timing_rejected_before_amber_ownership_or_command(modules, mode, data):
    from unittest.mock import AsyncMock
    manager = SimpleNamespace(relinquish=AsyncMock())
    runtime = SimpleNamespace(
        hass=SimpleNamespace(data={"power_sync_license_manager": manager}, config=SimpleNamespace(time_zone="Australia/Brisbane")),
        entry=SimpleNamespace(options={}), entry_id="battery", command=AsyncMock(), command_state="idle",
    )
    call = SimpleNamespace(data=data, context=SimpleNamespace(user_id="user"))
    with pytest.raises(modules.error):
        await modules.controls.handlers(runtime)[f"force_{mode}"](call)
    manager.relinquish.assert_not_awaited()
    runtime.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["charge", "discharge"])
async def test_manual_amber_accepts_exact_custom_minutes(modules, mode):
    from unittest.mock import AsyncMock
    runtime = SimpleNamespace(
        hass=SimpleNamespace(data={}, config=SimpleNamespace(time_zone="Australia/Brisbane")),
        entry=SimpleNamespace(options={}), entry_id="battery", command=AsyncMock(return_value=True), command_state="accepted",
    )
    call = SimpleNamespace(data={"duration": "7"}, context=SimpleNamespace(user_id="user"))
    assert (await modules.controls.handlers(runtime)[f"force_{mode}"](call))["success"]
    assert runtime.command.await_args.args == (mode, 7)
