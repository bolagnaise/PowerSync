"""Exercise custom contracts through HA forms, HTTP, tariffs and optimizer routing."""
from __future__ import annotations

import ast
import asyncio
import sys
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from test_flow_power_plan import PACKAGE, ROOT, _custom_snapshot, _snapshot, flow_power, quota
from test_provider_config_auto_sync import _handler_globals

COMPONENT = ROOT / "custom_components" / "power_sync"


def _extract(path, name, namespace, class_name=None):
    tree = ast.parse(path.read_text())
    body = tree.body if class_name is None else next(
        item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name
    ).body
    node = next(item for item in body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    node.decorator_list = []
    exec("from __future__ import annotations\n" + ast.unparse(node), namespace)
    return namespace[name]


def _form_helpers():
    namespace = {**vars(flow_power)}
    for name in ("_flow_power_form_selection", "_flow_power_custom_export_selection"):
        _extract(COMPONENT / "config_flow.py", name, namespace)
    return namespace


def test_ha_unrelated_save_preserves_custom_and_official_effective_dates():
    parse = _form_helpers()["_flow_power_form_selection"]
    for raw in (_custom_snapshot().selection.to_dict(),
                _snapshot("happy_hour_plus_2026", region="VIC").selection.to_dict(), {
        "schema_version": 1, "plan_id": "happy_hour_2026", "region": "VIC",
        "effective_from": "2026-08-01", "overrides": {},
    }):
        submitted = {"flow_power_plan_id": raw["plan_id"], "flow_power_plan_region": "VIC",
                     "flow_power_base_rate": 39.578}
        result, edit = parse(submitted, raw)
        assert result == raw
        assert not edit
        assert submitted == {"flow_power_base_rate": 39.578}


@pytest.mark.parametrize("class_name,main_step,pending_key", [
    ("PowerSyncConfigFlow", "async_step_flow_power_setup", "_flow_power_data"),
    ("PowerSyncOptionsFlow", "async_step_flow_power_options", "_flow_power_main_options"),
])
def test_ha_two_step_editor_validates_before_continuing(class_name, main_step, pending_key):
    namespace = {**_handler_globals(), **_form_helpers(),
                 "_flow_power_custom_export_schema": lambda *args: {}}
    methods = {name: _extract(COMPONENT / "config_flow.py", name, namespace, class_name)
               for name in (main_step, "async_step_flow_power_custom_export")}

    async def next_step(self):
        return {"next": True}

    methods.update(
        async_show_form=lambda self, **kwargs: kwargs,
        async_step_flow_power_tariff=next_step,
        async_step_flow_power_network_options=next_step,
        _get_option=lambda self, key, default=None: self.config_entry.options.get(key, default),
        _flow_power_options_schema=lambda self: {},
    )
    namespace["_should_collect_flow_power_api_key"] = lambda *args: False
    instance = type("Flow", (), methods)()
    instance.config_entry = SimpleNamespace(data={}, options={})
    first = {"flow_power_plan_id": "account_specific", "flow_power_plan_region": "VIC",
             "flow_power_tiered_export": True, "flow_power_happy_hour_end": "19:30"}
    form = asyncio.run(getattr(instance, main_step)(first))
    assert form["step_id"] == "flow_power_custom_export"
    assert not hasattr(instance, pending_key)
    terms = flow_power.custom_export_defaults("VIC")
    terms.pop("tiered_export_enabled")
    terms["effective_from"] = "2026-08-01"
    invalid = asyncio.run(instance.async_step_flow_power_custom_export({**terms, "export_cap_kwh": 0}))
    assert invalid["errors"] == {"base": "invalid_flow_power_plan"}
    assert not hasattr(instance, pending_key)
    assert asyncio.run(instance.async_step_flow_power_custom_export(terms)) == {"next": True}
    assert getattr(instance, pending_key)["flow_power_plan"] == _custom_snapshot().selection.to_dict()
    assert "flow_power_plan_id" in first  # Caller-owned form data was not consumed.


def _runtime_namespace(now):
    return {
        "__package__": f"{PACKAGE}.optimization",
        "dt_util": SimpleNamespace(now=lambda: now),
    }


@pytest.mark.parametrize("confidence,settled,expected", [
    ("authoritative", 14.5, [False, True, False]),
    ("authoritative", 15, [False, False, False]),
    ("unknown", 0, [False, False, False]),
])
@pytest.mark.parametrize("plan", ["account_specific", "happy_hour_plus_2026"])
def test_export_priority_uses_window_and_remaining_quota(confidence, settled, expected, plan):
    snapshot = _custom_snapshot() if plan == "account_specific" else _snapshot(plan, region="VIC")
    rule_id = flow_power.flow_power_quota_rules(snapshot)[0].rule_id
    ledger = quota.QuotaLedger(flow_power.flow_power_quota_rules(snapshot), quota.QuotaLedgerState(
        tariff_day="2026-09-06", confidence=confidence, settled_kwh={rule_id: settled},
    ))
    now = datetime.fromisoformat("2026-09-06T17:00:00+10:00")
    timestamps = [datetime.fromisoformat(f"2026-09-06T{time}:00+10:00") for time in ("17:00", "17:30", "21:30")]
    method = _extract(COMPONENT / "optimization/coordinator.py", "_flow_power_export_window_slots",
                      _runtime_namespace(now), "OptimizationCoordinator")
    coordinator = SimpleNamespace(
        _provider_key=lambda: "flow_power", _entry=object(),
        _ensure_flow_power_ledger=lambda **kwargs: (snapshot, ledger),
        _price_timestamps=lambda n: timestamps[:n],
    )
    assert method(coordinator, 3) == expected


def test_happy_hour_plus_static_tariff_uses_base_prices_for_full_window():
    snapshot = _snapshot("happy_hour_plus_2026", region="NSW")
    namespace = {
        **vars(sys.modules[f"{PACKAGE}.const"]),
        "__package__": PACKAGE, "date": date,
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 9, 6, tzinfo=timezone.utc)),
        "_LOGGER": SimpleNamespace(info=lambda *args: None),
    }
    method = _extract(COMPONENT / "tariff_converter.py", "apply_flow_power_export", namespace)
    rates = {f"PERIOD_{hour:02d}_{minute:02d}": 0.99 for hour in range(24) for minute in (0, 30)}
    tariff = {"sell_tariff": {"energy_charges": {season: {"rates": dict(rates)}
                                               for season in ("Summer", "Winter")}}}
    method(tariff, "NSW1", export_rate=0.45, happy_hour_end="19:30",
           plan_selection=snapshot.selection.to_dict())
    for season in tariff["sell_tariff"]["energy_charges"].values():
        assert season["rates"] == {
            period: (0.10 if "PERIOD_17_30" <= period < "PERIOD_21_30" else 0.0)
            for period in rates
        }


@pytest.mark.parametrize("settled,expected", [(0.0, 0.40), (15.0, 0.10)])
def test_happy_hour_plus_sensor_prefers_remaining_contract_rate(settled, expected):
    snapshot = _snapshot("happy_hour_plus_2026", region="NSW")
    at = datetime.fromisoformat("2026-09-06T20:00:00+10:00")
    ledger = quota.QuotaLedger(flow_power.flow_power_quota_rules(snapshot), quota.QuotaLedgerState(
        tariff_day="2026-09-06", confidence="authoritative",
        settled_kwh={"flow_happy_hour_plus_export": settled},
    ))
    contract = flow_power.flow_power_provider_contract(snapshot, at=at, import_price=0.31, ledger=ledger)
    method = _extract(COMPONENT / "sensor.py", "native_value", {}, "FlowPowerPriceSensor")
    sensor = SimpleNamespace(_is_import_sensor=False,
                             _flow_power_provider_contract=lambda: contract,
                             _get_current_tariff_price=lambda: (0.45, {}))
    assert method(sensor) == pytest.approx(expected)
    sensor._is_import_sensor = True
    assert method(sensor) == pytest.approx(0.31)


def test_custom_static_tariff_preserves_period_structure_and_base_prices():
    snapshot = _custom_snapshot(export_window_start="18:00", outside_window_rate_c_per_kwh=2)
    namespace = {
        "__package__": PACKAGE, "date": date,
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 9, 6, tzinfo=timezone.utc)),
    }
    method = _extract(COMPONENT / "tariff_converter.py", "apply_flow_power_export", namespace)
    rates = {f"PERIOD_{hour:02d}_{minute:02d}": 0.99 for hour in range(24) for minute in (0, 30)}
    tariff = {"sell_tariff": {"energy_charges": {season: {"rates": dict(rates)} for season in ("Summer", "Winter")}}}
    method(tariff, "VIC1", export_rate=0.35, plan_selection=snapshot.selection.to_dict())
    for season in tariff["sell_tariff"]["energy_charges"].values():
        assert season["rates"].keys() == rates.keys()
        assert season["rates"]["PERIOD_17_30"] == 0.02
        assert season["rates"]["PERIOD_18_00"] == 0.1
        assert season["rates"]["PERIOD_21_00"] == 0.1
        assert season["rates"]["PERIOD_21_30"] == 0.02
        assert set(season["rates"].values()) == {0.1, 0.02}


def test_http_custom_plan_validates_atomically_and_preserves_on_other_edits():
    namespace = {**_handler_globals(), "__package__": PACKAGE,
                 "web": SimpleNamespace(json_response=lambda payload, status=200: SimpleNamespace(payload=payload, status=status)),
                 "_LOGGER": SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None)}
    method = _extract(COMPONENT / "__init__.py", "post", namespace, "ProviderConfigView")
    entry = SimpleNamespace(entry_id="test", data={}, options={"electricity_provider": "flow_power"})
    writes = []

    def update(target, options):
        writes.append(options)
        target.options = options

    hass = SimpleNamespace(data={"power_sync": {"test": {}}}, config_entries=SimpleNamespace(
        async_entries=lambda domain: [entry], async_update_entry=update,
    ))

    def post(payload):
        async def json():
            return payload
        return asyncio.run(method(SimpleNamespace(_hass=hass), SimpleNamespace(json=json)))

    raw = _custom_snapshot().selection.to_dict()
    invalid = {**raw, "overrides": {**raw["overrides"], "premium_rate_c_per_kwh": float("nan")}}
    assert post({"flow_power_plan": invalid, "auto_sync": False}).status == 400
    assert writes == []
    response = post({"flow_power_plan": raw})
    assert response.status == 200, response.payload
    assert entry.options["flow_power_plan"] == raw
    assert not hass.data["power_sync"]["test"].get("_skip_reload")
    assert post({"auto_sync": False}).status == 200
    assert entry.options["flow_power_plan"] == raw


def test_custom_snapshot_keeps_ledger_for_equivalent_saves_and_resets_on_terms_change():
    snapshot = _custom_snapshot()
    namespace = _runtime_namespace(datetime(2026, 9, 6, tzinfo=timezone.utc))
    method = _extract(COMPONENT / "optimization/coordinator.py", "_flow_power_snapshot",
                      namespace, "OptimizationCoordinator")
    entry = SimpleNamespace(data={}, options={
        "flow_power_plan": snapshot.selection.to_dict(), "flow_power_export_rate": 35,
        "flow_power_happy_hour_end": "19:30", "flow_power_state": "VIC1",
    })
    saved_ledger = object()
    coordinator = SimpleNamespace(
        _provider_key=lambda: "flow_power", _entry=entry,
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Australia/Melbourne")),
        _flow_power_plan_hash=snapshot.plan_hash, _flow_power_ledger=saved_ledger,
        _pending_flow_power_settlement={"import": 0.0, "export": 0.5},
    )
    assert method(coordinator).plan_hash == snapshot.plan_hash
    entry.options["flow_power_export_rate"] = 45  # Inactive compatibility field.
    assert method(coordinator).plan_hash == snapshot.plan_hash
    assert coordinator._flow_power_ledger is saved_ledger
    entry.options["flow_power_plan"]["overrides"]["export_cap_kwh"] = 12
    assert method(coordinator).plan_hash != snapshot.plan_hash
    assert coordinator._flow_power_ledger is None
    assert coordinator._pending_flow_power_settlement == {"import": 0.0, "export": 0.0}


def test_custom_sensor_attributes_describe_active_window_and_contract():
    snapshot = _custom_snapshot(export_window_start="18:00", export_window_end="22:00")
    now = datetime.fromisoformat("2026-09-06T21:45:00+10:00")
    namespace = {**_handler_globals(), "dt_util": SimpleNamespace(now=lambda: now),
                 "_entity_currency_attrs": lambda self, attrs: attrs}
    method = _extract(COMPONENT / "sensor.py", "extra_state_attributes", namespace, "FlowPowerPriceSensor")
    contract = flow_power.flow_power_provider_contract(snapshot, at=now, import_price=0.3)
    sensor = SimpleNamespace(
        _is_import_sensor=False,
        _get_wholesale_price_cents=lambda: 0.3,
        _get_config_value=lambda key, default=None: default,
        _coordinator_source_attributes=lambda: {},
        _flow_power_provider_contract=lambda: contract,
        _get_current_tariff_price=lambda: None,
        _is_happy_hour=lambda: False,
        _get_export_rate=lambda: 0.35,
    )
    result = method(sensor)
    assert result["happy_hour_start"] == "18:00"
    assert result["happy_hour_end"] == "22:00"
    assert result["is_happy_hour"] is True
    assert result["happy_hour_rate"] == 0.3
    assert result["marginal_rate"] == 0.1  # Unknown quota withholds the premium.
    assert result["flow_power_plan"] == snapshot.selection.to_dict()
