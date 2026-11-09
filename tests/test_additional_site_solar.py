"""Additional AC solar: source proof, canonical power and real accounting."""
from datetime import datetime, timedelta, timezone
import ast
import asyncio
import importlib.util
from pathlib import Path
import sys
import math
from types import SimpleNamespace as NS
from types import MethodType, ModuleType
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


solar = load("additional_solar_test_module", "custom_components/power_sync/site_solar.py")
acc_tests = load("additional_solar_acc_harness", "tests/test_energy_accumulator_midnight_persistence.py")
NOW = datetime(2026, 11, 9, 2, 0, tzinfo=timezone.utc)


def resolve(value="3000", *, age=0, unit="W", **kwargs):
    params = dict(primary_kw=2., grid_kw=-1., battery_kw=0., raw_load_kw=4.,
                  state=NS(state=value, last_reported=NOW-timedelta(seconds=age),
                           attributes={"unit_of_measurement": unit}),
                  identity="array", expected_identity="array", segment="segment",
                  now=NOW)
    params.update(kwargs)
    return solar.resolve_solar(**params)


def test_component_added_once_and_metered_load_preserved():
    result = resolve()
    assert result.solar_kw == 5.
    assert result.raw_load_kw == 4.
    assert result.valid


def test_balance_load_uses_site_total_before_ev_exclusion():
    assert resolve(load_basis="balance").raw_load_kw == 4.
    assert resolve(load_basis="balance", battery_kw=-1.).raw_load_kw == 3.


@pytest.mark.parametrize("value,unit", [("0", "W"), ("0", "kW"), ("0", "MW")])
def test_measured_zero_is_valid(value, unit):
    result = resolve(value, unit=unit)
    assert result.valid and result.solar_kw == 2.


@pytest.mark.parametrize("value,unit", [(None,"W"),("unknown","W"),
    ("unavailable","W"),("nan","W"),("inf","W"),("-1","W"),
    ("1","VA"),("1",""),(True,"W")])
def test_invalid_contribution_is_unknown(value, unit):
    result = resolve(value, unit=unit, load_basis="balance")
    assert not result.valid and result.additional_kw is None
    assert result.raw_load_kw is None


@pytest.mark.parametrize("age", [121, 500, -6])
def test_stale_and_future_observations_fail_closed(age):
    assert not resolve(age=age).valid


def test_source_replacement_and_whole_site_total_cannot_be_added():
    assert not resolve(identity="different").valid
    assert not resolve(blocked_reason="already_included").valid


def test_rename_preserves_registry_identity_but_replacement_does_not():
    old = NS(platform="goodwe", unique_id="pv-serial", config_entry_id="g", device_id="d")
    renamed = NS(**vars(old), entity_id="sensor.renamed")
    replacement = NS(platform="goodwe", unique_id="pv-new", config_entry_id="g", device_id="d")
    assert solar.source_identity(old) == solar.source_identity(renamed)
    assert solar.source_identity(old) != solar.source_identity(replacement)


def accumulator():
    clock = acc_tests._Clock(NOW)
    Accumulator = acc_tests._load_accumulator(clock)
    return clock, Accumulator()


def feed(acc, clock, *, valid=True, segment="one", solar_kw=5.):
    acc._site_solar_sample = resolve(
        segment=segment, now=clock.current,
        state=NS(state="3000", last_reported=clock.current,
                 attributes={"unit_of_measurement":"W"}),
        blocked_reason=None if valid else "stale_observation")
    acc.update(solar_kw, 2., 0., 4., .3, .1)
    clock.current += timedelta(seconds=60)


def test_missing_solar_does_not_integrate_recovery_gap_or_drop_grid_accounting():
    clock, acc = accumulator()
    feed(acc, clock)
    feed(acc, clock)
    before = acc.solar_kwh
    feed(acc, clock, valid=False)
    feed(acc, clock)
    assert acc.solar_kwh == pytest.approx(before)
    assert acc.grid_import_kwh == pytest.approx(2. * 3/60)
    feed(acc, clock)
    assert acc.solar_kwh == pytest.approx(before + 5./60)


def test_segment_switch_preserves_total_and_starts_new_interval():
    clock, acc = accumulator()
    feed(acc, clock)
    feed(acc, clock)
    before = acc.solar_kwh
    feed(acc, clock, segment="replacement")
    assert acc.solar_kwh == pytest.approx(before)
    feed(acc, clock, segment="replacement")
    assert acc.solar_kwh == pytest.approx(before + 5./60)


def test_disabled_path_still_integrates_primary_solar():
    clock, acc = accumulator()
    acc.update(2., 0., 0., 2.)
    clock.current += timedelta(seconds=60)
    acc.update(2., 0., 0., 2.)
    assert acc.solar_kwh == pytest.approx(2./60)


def policy_fixture(clock, *, load_basis="balance"):
    source = NS(platform="goodwe", unique_id="separate-pv", config_entry_id="ac",
                device_id="ac-device", entity_id="sensor.ac_pv")
    identity = solar.source_identity(source)
    entry = NS(entry_id="site", data={"battery_system": "goodwe"}, options={
        solar.CONF_ENABLED: True, solar.CONF_ENTITY: source.entity_id,
        solar.CONF_IDENTITY: identity, solar.CONF_LOAD: load_basis,
    })
    state = NS(state="3000", last_reported=clock.current,
               attributes={"unit_of_measurement": "W"})
    states = {source.entity_id: state}
    registry_rows = {source.entity_id: source}
    registry = NS(async_get=registry_rows.get, entities=registry_rows)
    hass = NS(states=NS(get=states.get), data={"power_sync": {"site": {}}},
              services=NS(async_call=Mock(side_effect=AssertionError("control write"))))
    policy = solar.SiteSolarPolicy(hass, entry, registry=registry)
    policy._now = lambda: clock.current
    return policy, state, registry_rows


def goodwe_update(acc, policy, clock, *, native_daily=12., primary_valid=True):
    """Run the real coordinator, resolver, EV accounting and accumulator."""
    path = ROOT / "custom_components/power_sync/coordinator.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GoodWeEnergyCoordinator")
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_update_data")
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_update_energy_accumulator_with_ev_load")
    ns = {"Any": object, "HomeAssistant": object, "DOMAIN": "power_sync",
          "_LOGGER": Mock(), "dt_util": NS(utcnow=clock.now), "UpdateFailed": RuntimeError,
          "_fresh_site_ev_load": lambda *args: (1., True),
          "_get_current_prices": lambda *args: (.3, .1)}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), helper, method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), ns)
    controller = NS(get_runtime_data=lambda: {
        "solar_power": 2.5, "grid_power": -.6, "battery_power": 0.,
        "load_power": 1.9, "battery_level": 80., "rated_power_w": 10000.,
        "entity_telemetry": True, "telemetry_ready": True,
        "daily_solar_energy_kwh": native_daily,
        "solar_power_valid": primary_valid,
    })
    obj = NS(hass=policy.hass, _entry_id="site", _energy_acc=acc,
             _additional_solar_enabled=True, _using_entity_telemetry=True,
             _telemetry_validated=True, _telemetry_controller=controller,
             _controller=None, _connected=True, _solar_override_entity="",
             _native_integration_enabled=lambda: True,
             _native_stale_data=lambda: None)
    acc._site_solar_policy = policy
    return asyncio.run(ns["_async_update_data"](obj))


def accepted_cutover(acc, policy, clock, total=12.):
    """Exercise the real accepted-counter checkpoint and new runtime restore."""
    store = acc_tests._Store()
    acc._store = store
    policy.hass.data["power_sync"]["site"]["primary"] = NS(
        _energy_acc=acc, data={"last_update":clock.current,
                             "energy_summary":{"pv_today_kwh":total}})
    asyncio.run(solar.checkpoint_solar(policy.hass, policy.entry, force=True, now=clock.current))
    restored = acc_tests._load_accumulator(clock)()
    restored._store = store
    asyncio.run(restored.async_restore())
    policy.hass.data["power_sync"]["site"] = {}
    new_policy = solar.SiteSolarPolicy(policy.hass, policy.entry, registry=policy._registry)
    new_policy._now = clock.now
    return restored, new_policy


def test_real_coordinator_seeds_published_baseline_and_ignores_primary_only_overlay():
    clock, acc = accumulator()
    acc.solar_kwh = 8.
    acc.mtd_solar_kwh = 100.
    policy, state, _ = policy_fixture(clock)
    acc, policy = accepted_cutover(acc, policy, clock)
    first = goodwe_update(acc, policy, clock)
    assert first["solar_power"] == 5.5
    assert first["load_power"] == pytest.approx(4.9)
    assert first["energy_summary"]["pv_today_kwh"] == 12.
    assert acc.mtd_solar_kwh == 104.
    clock.current += timedelta(seconds=60)
    state.last_reported = clock.current
    second = goodwe_update(acc, policy, clock, native_daily=12.02)
    assert second["energy_summary"]["pv_today_kwh"] == 12.092
    assert acc.load_kwh == pytest.approx(3.9/60)  # one 1 kW EV, once
    assert second["solar_power_valid"] is True
    policy.hass.services.async_call.assert_not_called()


def test_real_coordinator_recovers_without_backfill_and_keeps_grid_billing():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    goodwe_update(acc, policy, clock)
    clock.current += timedelta(seconds=60)
    state.last_reported = clock.current
    goodwe_update(acc, policy, clock)
    baseline = acc.solar_kwh
    clock.current += timedelta(seconds=60)
    state.state = "unavailable"
    invalid = goodwe_update(acc, policy, clock)
    assert invalid["solar_power"] is None
    assert invalid["load_power"] is None
    assert invalid["solar_power_valid"] is False
    clock.current += timedelta(seconds=60)
    state.state = "3000"
    state.last_reported = clock.current
    goodwe_update(acc, policy, clock)
    assert acc.solar_kwh == baseline
    assert acc.grid_export_kwh == pytest.approx(.6*3/60)
    assert acc.export_earnings_today == pytest.approx(.6*3/60*.1)


def test_real_policy_rejects_same_source_whole_site_and_existing_aggregation():
    clock, acc = accumulator()
    policy, _, _ = policy_fixture(clock)
    policy.prepare(acc, 2., 1., 0., 3., already_included=True)
    assert not acc._site_solar_sample.valid
    policy.entry.options["custom_solar_power_entity"] = "sensor.total_site_pv"
    # New policy represents a restored, conflicting legacy configuration.
    restored = solar.SiteSolarPolicy(policy.hass, policy.entry, registry=policy._registry)
    restored._now = clock.now
    restored.prepare(acc, 2., 1., 0., 3.)
    assert not acc._site_solar_sample.valid


def test_restart_preserves_solar_segment_totals_and_breaks_interval():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    acc, policy = accepted_cutover(acc, policy, clock)
    goodwe_update(acc, policy, clock)
    clock.current += timedelta(seconds=60)
    state.last_reported = clock.current
    goodwe_update(acc, policy, clock)
    store = acc_tests._Store()
    acc._store = store
    asyncio.run(acc.async_flush())
    restored = acc_tests._load_accumulator(clock)()
    restored._store = store
    asyncio.run(restored.async_restore())
    clock.current += timedelta(seconds=60)
    state.last_reported = clock.current
    actual = goodwe_update(restored, policy, clock, native_daily=50.)
    assert actual["energy_summary"]["pv_today_kwh"] == 12.092
    assert restored.mtd_solar_kwh == pytest.approx(acc.mtd_solar_kwh, abs=.0001)


def test_disable_keeps_accepted_total_and_does_not_resume_lower_native_counter():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    acc, policy = accepted_cutover(acc, policy, clock)
    goodwe_update(acc, policy, clock)
    acc._site_solar_policy = None
    acc._site_solar_sample = None
    clock.current += timedelta(seconds=60)
    acc.update(2., 0., 0., 2.)
    data = acc.publish_site_solar({"solar_power": 2., "energy_summary": {"pv_today_kwh": 8.}})
    assert data["energy_summary"]["pv_today_kwh"] == 12.
    clock.current += timedelta(seconds=60)
    acc.update(2., 0., 0., 2.)
    assert acc.solar_kwh == pytest.approx(12.+2./60)


def test_entity_rename_is_resolved_by_bound_identity():
    clock, acc = accumulator()
    policy, state, rows = policy_fixture(clock)
    row = rows.pop(policy.entity_id)
    row.entity_id = "sensor.renamed_pv"
    rows[row.entity_id] = row
    policy.hass.states = NS(get=lambda entity: state if entity == row.entity_id else None)
    policy.prepare(acc, 2., 0., 0., 2.)
    assert acc._site_solar_sample.valid
    assert policy.entity_id == "sensor.renamed_pv"


def test_late_configuration_result_cannot_publish_as_complete():
    clock, acc = accumulator()
    policy, _, _ = policy_fixture(clock)
    policy.prepare(acc, 2., 0., 0., 2.)
    policy.entry.options[solar.CONF_IDENTITY] = "replacement"
    data = policy.publish(acc, {"solar_power": 5., "load_power": 5.})
    assert data["solar_power"] is None and not data["solar_power_valid"]


def options_flow(monkeypatch, *, source_exists=True):
    clock, _ = accumulator()
    policy, _, rows = policy_fixture(clock)
    if not source_exists:
        rows.clear()
    # Stub only the environment-owned registry and package route. The source
    # binding, checkpoint, validation and options methods below are real code.
    for name in ("homeassistant", "homeassistant.helpers", "power_sync"):
        if name not in sys.modules:
            mod = ModuleType(name)
            mod.__path__ = []
            monkeypatch.setitem(sys.modules, name, mod)
    registry_module = ModuleType("homeassistant.helpers.entity_registry")
    registry_module.async_get = lambda hass: policy._registry
    monkeypatch.setitem(sys.modules, registry_module.__name__, registry_module)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", registry_module, raising=False)
    monkeypatch.setitem(sys.modules, "power_sync.site_solar", solar)

    class Marker:
        def __init__(self, schema, **kwargs):
            self.schema = schema
            self.__dict__.update(kwargs)

    ns = {"__package__": "power_sync", "vol": NS(Optional=Marker, Schema=lambda x:x),
          "BooleanSelector": lambda: None, "EntitySelector": lambda x: None,
          "EntitySelectorConfig": lambda **kw: kw,
          "SelectSelector": lambda x: None, "SelectSelectorConfig": lambda **kw: kw,
          "SelectOptionDict": lambda **kw: kw, "SelectSelectorMode": NS(DROPDOWN="dropdown")}
    ns.update(dict(zip(("CONF_ADDITIONAL_SOLAR_ENABLED", "CONF_ADDITIONAL_SOLAR_POWER_ENTITY",
                       "CONF_ADDITIONAL_SOLAR_SOURCE_IDENTITY", "CONF_ADDITIONAL_SOLAR_LOAD_BASIS"),
                      (solar.CONF_ENABLED, solar.CONF_ENTITY, solar.CONF_IDENTITY, solar.CONF_LOAD))))
    ns.update(CONF_AMBER_API_TOKEN="amber_api_token", CONF_AMBER_SITE_ID="amber_site_id")
    path = ROOT / "custom_components/power_sync/config_flow.py"
    tree = ast.parse(path.read_text())
    names = ("async_step_solar_accounting", "_save_and_finish")
    nodes = [next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name) for name in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), ns)
    policy.entry.options = {"ac_inverter_curtailment_enabled": False, "unrelated": "keep"}
    flow = NS(config_entry=policy.entry, hass=policy.hass,
              _effective_battery_system=lambda: "goodwe",
              _get_option=lambda key, default=None: policy.entry.options.get(key, policy.entry.data.get(key, default)),
              _apply_legacy_data_key_removals=lambda: None,
              async_create_entry=lambda **kw: kw,
              async_show_form=lambda **kw: kw)
    for name in names:
        setattr(flow, name, MethodType(ns[name], flow))
    return flow, policy, rows


def test_real_options_flow_binds_source_without_enabling_curtailment(monkeypatch):
    flow, policy, rows = options_flow(monkeypatch)
    result = asyncio.run(flow.async_step_solar_accounting({
        solar.CONF_ENABLED: True, solar.CONF_ENTITY: "sensor.ac_pv",
        solar.CONF_LOAD: "measured"}))
    assert result["data"][solar.CONF_IDENTITY] == solar.source_identity(rows["sensor.ac_pv"])
    assert result["data"]["ac_inverter_curtailment_enabled"] is False
    assert result["data"]["unrelated"] == "keep"
    policy.hass.services.async_call.assert_not_called()


def test_real_options_flow_rejects_unbound_source(monkeypatch):
    flow, _, _ = options_flow(monkeypatch, source_exists=False)
    result = asyncio.run(flow.async_step_solar_accounting({
        solar.CONF_ENABLED: True, solar.CONF_ENTITY: "sensor.ac_pv"}))
    assert result["errors"]["base"] == "additional_solar_source_required"


def test_real_options_flow_clears_binding_when_disabled(monkeypatch):
    flow, _, _ = options_flow(monkeypatch)
    flow.config_entry.options[solar.CONF_IDENTITY] = "old"
    result = asyncio.run(flow.async_step_solar_accounting({solar.CONF_ENABLED: False}))
    assert result["data"][solar.CONF_IDENTITY] is None
    assert result["data"][solar.CONF_ENABLED] is False


def test_midnight_and_month_rollover_do_not_carry_old_daily_baseline():
    clock, acc = accumulator()
    clock.current = datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)
    policy, state, _ = policy_fixture(clock)
    goodwe_update(acc, policy, clock)
    clock.current += timedelta(seconds=90)
    state.last_reported = clock.current
    result = goodwe_update(acc, policy, clock, native_daily=.01)
    assert result["energy_summary"]["pv_today_kwh"] == .046
    assert acc.mtd_solar_kwh == pytest.approx(5.5*30/3600)


def extracted_method(path, cls_name, method_name, namespace):
    path = ROOT / path
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    node = next(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method_name)
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[method_name]


def test_tesla_local_primary_is_composed_before_ev_accounting_and_publication():
    clock, acc = accumulator()
    policy, _, _ = policy_fixture(clock)
    local = NS(data=NS(solar_w=2500., grid_w=-600., battery_w=0., load_w=1900.,
                       soc=80., grid_status="Active"), reachable=True,
               last_success_monotonic=100., _observed_ev_power_w=lambda:1000.)
    policy.hass.data["power_sync"]["site"]["powerwall_local"] = {"coordinator": local}
    acc._site_solar_policy = policy
    obj = NS(hass=policy.hass, _entry_id="site", _energy_acc=acc,
             _additional_solar_enabled=True, _firmware=None, _lifetime_totals=None)
    ns = {"DOMAIN": "power_sync", "math": math, "time": NS(monotonic=lambda:100.),
          "TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS":30, "dt_util":NS(utcnow=clock.now),
          "timedelta":timedelta, "_grid_status_is_off_grid":lambda _:False,
          "_stored_battery_health_capacity_kwh":lambda *args:None,
          "_get_current_prices":lambda *args:(.3,.1)}
    fn = extracted_method("custom_components/power_sync/coordinator.py", "TeslaEnergyCoordinator", "_local_powerwall_energy_data", ns)
    result = fn(obj)
    assert result["solar_power"] == 5.5
    assert result["raw_home_load_power"] == pytest.approx(4.9)
    assert result["load_power"] == pytest.approx(3.9)
    assert result["data_source"] == "powerwall_local"


def test_canonical_sensor_and_mobile_share_solar_and_never_restore_invalid_total():
    clock, acc = accumulator()
    policy, _, _ = policy_fixture(clock)
    result = goodwe_update(acc, policy, clock)
    ev = load("additional_solar_ev_module", "custom_components/power_sync/ev_load.py")
    normalized = ev.normalize_energy_data(result, battery_system="goodwe",
        ev_load=ev.ObservedEvLoadSnapshot(1., (), NOW, ev.EvLoadQuality.COMPLETE), at=NOW)
    ns = {"SENSOR_TYPE_SOLAR_POWER":"solar_power", "SENSOR_TYPE_HOME_LOAD":"home_load",
          "SENSOR_TYPE_GRID_STATUS":"grid_status", "_LOCAL_OVERRIDABLE":{"solar_power"},
          "_sungrow_ac_inverter_power_kw":lambda *args: (_ for _ in ()).throw(AssertionError("late sum"))}
    getter = extracted_method("custom_components/power_sync/sensor.py", "TeslaEnergySensor", "native_value", ns)
    sensor = NS(_entry=policy.entry, entity_description=NS(key="solar_power", value_fn=lambda d:d["solar_power"]),
                _normalized_energy_data=lambda:normalized,
                _local_coordinator=lambda: (_ for _ in ()).throw(AssertionError("local bypass")),
                _restored_numeric_value=lambda _:99.)
    assert getter(sensor) == 5.5
    view = NS(_config_entry=policy.entry, _hass=NS(data={"power_sync":{"site":{
        "goodwe_coordinator":NS(data=normalized)}}}))
    tree = ast.parse((ROOT/"custom_components/power_sync/__init__.py").read_text())
    cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and any(getattr(x,"name",None)=="_site_snapshot" for x in n.body))
    snapshot = extracted_method("custom_components/power_sync/__init__.py", cls.name, "_site_snapshot", {"DOMAIN":"power_sync","math":math})
    assert snapshot(view)["solar_power_kw"] == getter(sensor)
    assert snapshot(view)["load_power_kw"] == pytest.approx(3.9)
    normalized["solar_power"] = None
    normalized["solar_power_valid"] = False
    assert getter(sensor) is None
    assert snapshot(view)["solar_power_kw"] is None


def test_primary_source_switch_breaks_only_solar_continuity():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    for source in ("tesla_local", "tesla_local", "tesla_cloud"):
        state.last_reported = clock.current
        power, home = policy.prepare(acc,2.,1.,0.,3.,primary_source=source,
                                     primary_observed_at=clock.current)
        acc.update(power,1.,0.,home)
        clock.current += timedelta(seconds=60)
    assert acc.solar_kwh == pytest.approx(5./60)
    assert acc.grid_import_kwh == pytest.approx(2./60)


def test_stale_primary_and_restored_replay_are_not_valid_observations():
    assert not resolve(primary_observed_at=NOW-timedelta(seconds=121)).valid
    restored = NS(state="3000", last_reported=NOW,
                  attributes={"unit_of_measurement":"W","restored":True})
    assert not resolve(state=restored).valid


def test_derived_load_does_not_backfill_invalid_recovery_interval():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    goodwe_update(acc, policy, clock)
    clock.current += timedelta(seconds=60)
    state.last_reported = clock.current
    goodwe_update(acc, policy, clock)
    load_before, mtd_before = acc.load_kwh, acc.mtd_load_kwh
    clock.current += timedelta(seconds=60)
    state.state = "unavailable"
    goodwe_update(acc, policy, clock)
    clock.current += timedelta(seconds=60)
    state.state, state.last_reported = "3000", clock.current
    goodwe_update(acc, policy, clock)
    assert acc.load_kwh == load_before and acc.mtd_load_kwh == mtd_before
    assert acc._load_accounting_partial_today


def test_fresh_endpoints_cannot_bridge_five_minute_unobserved_gap():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    for _ in range(2):
        state.last_reported = clock.current
        power, home = policy.prepare(acc,2.,1.,0.,3.)
        acc.update(power,1.,0.,home)
        clock.current += timedelta(seconds=60)
    before = (acc.solar_kwh, acc.load_kwh)
    clock.current += timedelta(seconds=240)
    state.last_reported = clock.current
    power, home = policy.prepare(acc,2.,1.,0.,3.)
    acc.update(power,1.,0.,home)
    assert (acc.solar_kwh, acc.load_kwh) == before
    assert acc.grid_import_kwh == pytest.approx(6./60)
    assert acc._solar_partial_today


def test_invalid_primary_is_not_replaced_by_valid_extra_quality():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    goodwe_update(acc, policy, clock)
    clock.current += timedelta(seconds=60)
    state.last_reported = clock.current
    before = acc.solar_kwh
    result = goodwe_update(acc, policy, clock, primary_valid=False)
    assert result["solar_power"] is None and not result["solar_power_valid"]
    assert acc.solar_kwh == before
    assert acc.grid_export_kwh > 0


def test_primary_expiry_is_rechecked_at_accumulation_and_publication():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    state.last_reported = clock.current-timedelta(seconds=10)
    power, home = policy.prepare(acc,2.,1.,0.,3.,
        primary_observed_at=clock.current-timedelta(seconds=110))
    assert acc._site_solar_sample.valid
    clock.current += timedelta(seconds=20)
    result = policy.publish(acc, {"solar_power":power,"load_power":home})
    assert not result["solar_power_valid"]
    acc.update(power,1.,0.,home)
    assert acc.solar_kwh == 0.


def test_renamed_additional_source_cannot_become_primary_input():
    clock, acc = accumulator()
    policy, state, rows = policy_fixture(clock)
    policy.entry.data["battery_system"] = "custom"
    policy.entry.options["custom_solar_power_entity"] = "sensor.primary_pv"
    policy = solar.SiteSolarPolicy(policy.hass, policy.entry, registry=policy._registry)
    policy._now = clock.now
    row = rows.pop("sensor.ac_pv")
    row.entity_id = "sensor.primary_pv"
    rows[row.entity_id] = row
    policy.hass.states = NS(get=lambda _:state)
    policy.prepare(acc,2.,1.,0.,3.)
    assert not acc._site_solar_sample.valid


@pytest.mark.parametrize("stamp", [None, "bad", 7, NOW.replace(tzinfo=None)])
def test_bad_observation_time_stays_unknown_without_publish_exception(stamp):
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    state.last_reported = stamp
    policy.prepare(acc,2.,1.,0.,3.)
    result = policy.publish(acc, {"solar_power":2.,"load_power":3.})
    assert result["solar_power"] is None


@pytest.mark.parametrize("kwargs", [dict(value="1e308",unit="MW"),
    dict(primary_kw=math.nan), dict(primary_kw=math.inf),
    dict(primary_kw=1e308,value="1e308",unit="kW")])
def test_nonfinite_inputs_and_conversion_cannot_poison_totals(kwargs):
    assert not resolve(**kwargs).valid


def test_later_native_counter_cannot_reseed_an_accepted_segment():
    clock, acc = accumulator()
    policy, state, _ = policy_fixture(clock)
    acc, policy = accepted_cutover(acc, policy, clock)
    goodwe_update(acc, policy, clock)
    clock.current += timedelta(seconds=60)
    state.last_reported = clock.current
    result = goodwe_update(acc, policy, clock, native_daily=999.)
    assert result["energy_summary"]["pv_today_kwh"] == 12.092


def test_checkpoint_fences_old_publication_before_awaiting_storage():
    clock, acc = accumulator()
    policy, _, _ = policy_fixture(clock)
    acc.solar_kwh = 8.
    publications = []

    class RacingStore(acc_tests._Store):
        async def async_save(self, data):
            clock.current += timedelta(seconds=60)
            acc.update(60.,1.,0.,1.)
            publications.append(acc.publish_site_solar({
                "solar_power":60., "energy_summary":{"pv_today_kwh":13.}}))
            await super().async_save(data)

    acc.update(2.,1.,0.,1.)
    acc._store = RacingStore()
    policy.hass.data["power_sync"]["site"]["primary"] = NS(
        _energy_acc=acc, data={"last_update":clock.current,
                             "energy_summary":{"pv_today_kwh":12.}})
    asyncio.run(solar.checkpoint_solar(policy.hass, policy.entry, force=True, now=clock.current))
    assert publications[0]["energy_summary"]["pv_today_kwh"] == 12.
    assert publications[0]["solar_power"] is None
    assert acc._store.data["solar_kwh"] == 12.


def test_missing_runtime_bucket_is_bound_before_accepting_a_sample():
    clock, acc = accumulator()
    policy, _, _ = policy_fixture(clock)
    policy.hass.data = {}
    policy = solar.SiteSolarPolicy(policy.hass, policy.entry, registry=policy._registry)
    policy._now = clock.now
    policy.hass.data["power_sync"]["site"] = {}
    policy.prepare(acc,2.,1.,0.,3.)
    assert not acc._site_solar_sample.valid


def test_routine_token_refresh_does_not_change_solar_coverage():
    clock, acc = accumulator()
    policy, _, _ = policy_fixture(clock)
    policy.entry.options["teslemetry_api_token"] = "replacement_test_token"
    policy.prepare(acc,2.,1.,0.,3.)
    assert acc._site_solar_sample.valid
    assert policy.publish(acc,{"solar_power":5.,"load_power":6.})["solar_power_valid"]


def test_registry_replacement_after_prepare_invalidates_late_publication():
    clock, acc = accumulator()
    policy, _, rows = policy_fixture(clock)
    policy.prepare(acc,2.,1.,0.,3.)
    rows[policy.entity_id].unique_id = "replacement"
    result = policy.publish(acc,{"solar_power":5.,"load_power":6.})
    assert result["solar_power"] is None
    assert result["solar_accounting_reason"] == "source_changed"


def test_failed_checkpoint_releases_old_runtime_fence():
    clock, acc = accumulator()
    policy, _, _ = policy_fixture(clock)
    class FailingStore(acc_tests._Store):
        async def async_save(self, data):
            raise OSError("test disk failure")
    acc._store = FailingStore()
    acc.solar_kwh = 8.
    policy.hass.data["power_sync"]["site"]["primary"] = NS(
        _energy_acc=acc, data={"last_update":clock.current,
                             "energy_summary":{"pv_today_kwh":12.}})
    with pytest.raises(OSError):
        asyncio.run(solar.checkpoint_solar(policy.hass,policy.entry,force=True,now=clock.current))
    assert not acc._solar_cutover_pending
    assert acc.solar_kwh == 8.


def test_dual_sungrow_preserves_unknown_derived_load_from_composed_primary():
    ns = {"_LOGGER":Mock(),"dt_util":NS(utcnow=lambda:NOW)}
    fn = extracted_method("custom_components/power_sync/coordinator.py", "DualSungrowCoordinator", "_async_update_data",ns)
    obj = NS(_coord1=NS(data={"solar_power":None,"solar_power_valid":False,"load_power":None}),
             _coord2=NS(data={"solar_power":1.,"load_power":1.}),
             _cap1=10.,_cap2=10.,_soc_cap=100,
             _children_control_ready=lambda:True,_combined_power_limit_w=lambda _:None)
    result = asyncio.run(fn(obj))
    assert result["solar_power"] is None and result["load_power"] is None
