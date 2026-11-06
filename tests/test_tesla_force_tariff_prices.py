"""Tesla temporary force-discharge tariff rates and config-flow boundaries."""

from __future__ import annotations

import ast
import math
import json
from unittest.mock import AsyncMock, Mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
BUY_KEY = "tesla_force_discharge_buy_price"
SELL_KEY = "tesla_force_discharge_sell_price"


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text())
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _price_helpers():
    path = ROOT / "tesla_force_tariff.py"
    validate = _function(path, "validate_force_discharge_prices")
    configured = _function(path, "configured_force_discharge_prices")
    configured.body = [
        node for node in configured.body if not isinstance(node, ast.ImportFrom)
    ]
    module = ast.fix_missing_locations(ast.Module(body=[validate, configured], type_ignores=[]))
    namespace = {
        "math": math,
        "DEFAULT_TESLA_FORCE_DISCHARGE_BUY_PRICE": 0.55,
        "DEFAULT_TESLA_FORCE_DISCHARGE_SELL_PRICE": 25.0,
        "CONF_TESLA_FORCE_DISCHARGE_BUY_PRICE": BUY_KEY,
        "CONF_TESLA_FORCE_DISCHARGE_SELL_PRICE": SELL_KEY,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["validate_force_discharge_prices"], namespace["configured_force_discharge_prices"]


def test_tesla_force_discharge_prices_default_and_invalid_existing_settings():
    validate, configured = _price_helpers()
    assert configured({}) == (0.55, 25.0)
    assert configured({BUY_KEY: 0.7, SELL_KEY: 30}) == (0.7, 30.0)
    assert configured({BUY_KEY: float("nan"), SELL_KEY: 25}) == (0.55, 25.0)
    for buy, sell in ((99, 99), (25, 0.55), (-1, 25), (0.55, float("inf"))):
        with pytest.raises(ValueError):
            validate(buy, sell)


def test_tesla_discharge_tariff_uses_configured_rates_only_in_force_window():
    _, configured = _price_helpers()
    path = ROOT / "__init__.py"
    function = _function(path, "_create_discharge_tariff")
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    entry = SimpleNamespace(data={BUY_KEY: 0.7, SELL_KEY: 30})
    namespace = {
        "datetime": datetime,
        "timedelta": timedelta,
        "entry": entry,
        "hass": object(),
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 9, 28, 18, 5, tzinfo=timezone.utc)),
        "_LOGGER": SimpleNamespace(info=lambda *args, **kwargs: None),
        "currency_for_entry": lambda *_: "AUD",
        "configured_force_discharge_prices": configured,
    }
    exec(compile(module, str(path), "exec"), namespace)
    tariff, expiry = namespace["_create_discharge_tariff"](30)
    buy = tariff["energy_charges"]["Summer"]["rates"]
    sell = tariff["sell_tariff"]["energy_charges"]["Summer"]["rates"]
    assert (buy["18:00"], sell["18:00"]) == (0.7, 30.0)
    assert (buy["18:30"], sell["18:30"]) == (0.7, 30.0)
    assert (buy["19:00"], sell["19:00"]) == (0.30, 0.08)
    assert expiry.hour == 19 and expiry.minute == 0


@pytest.mark.parametrize("minute", [0, 5, 29, 30, 59])
def test_full_day_discharge_does_not_wrap_to_one_period(minute):
    _, configured = _price_helpers()
    path = ROOT / "__init__.py"
    function = _function(path, "_create_discharge_tariff")
    now = datetime(2026, 10, 24, 23, minute, tzinfo=timezone.utc)
    namespace = {
        "datetime": datetime, "timedelta": timedelta,
        "entry": SimpleNamespace(data={}), "hass": object(),
        "dt_util": SimpleNamespace(now=lambda: now),
        "_LOGGER": SimpleNamespace(info=lambda *args, **kwargs: None),
        "currency_for_entry": lambda *_: "AUD",
        "configured_force_discharge_prices": configured,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(path), "exec"), namespace)
    tariff, expiry = namespace["_create_discharge_tariff"](1440)
    rates = tariff["sell_tariff"]["energy_charges"]["Summer"]["rates"]
    assert len(rates) == 48
    assert set(rates.values()) == {25.0}
    assert now + timedelta(minutes=1440) <= expiry <= now + timedelta(minutes=1470)


def _flow_namespace():
    namespace = {}
    # Literal constants let the source-extracted flow run without HA imports.
    for node in ast.parse((ROOT / "const.py").read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    namespace[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
    validate, configured = _price_helpers()
    namespace.update(
        validate_force_discharge_prices=validate,
        configured_force_discharge_prices=configured,
    )
    return namespace


def _compile_flow(function, namespace):
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        function,
    ], type_ignores=[]))
    exec(compile(module, str(ROOT / "config_flow.py"), "exec"), namespace)
    return namespace[function.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("rates", [{}, {BUY_KEY: 0.7, SELL_KEY: 30}])
async def test_reconnecting_tesla_preserves_saved_force_prices(rates):
    namespace = _flow_namespace()
    namespace.update(
        _detect_tesla_ev_integrations=lambda _: {"tesla_fleet": True, "teslemetry": True},
        normalize_powerwall_gateway_host=lambda value: value,
    )
    entry = SimpleNamespace(data={**rates, "tesla_energy_site_id": "123"}, options={})
    flow = SimpleNamespace(
        config_entry=entry,
        hass=SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock())),
        _schedule_entry_reload=Mock(),
        async_create_entry=lambda **kwargs: kwargs,
    )
    function = _compile_flow(_function(ROOT / "config_flow.py", "async_step_tesla_connection"), namespace)
    await function(flow, {"tesla_api_provider": namespace["TESLA_PROVIDER_FLEET_API"]})
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert {key: saved[key] for key in rates} == rates
    for key in {BUY_KEY, SELL_KEY} - rates.keys():
        assert key not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("is_tesla,submitted,expected", [
    (True, {BUY_KEY: 0.8, SELL_KEY: 35}, (0.8, 35.0)),
    (True, {}, (0.7, 30.0)),
    (False, {BUY_KEY: 0.8, SELL_KEY: 35}, (0.7, 30.0)),
    (True, {BUY_KEY: 40, SELL_KEY: 35}, None),
    (True, {BUY_KEY: float("nan"), SELL_KEY: 35}, None),
])
async def test_optimizer_validates_and_saves_force_prices(is_tesla, submitted, expected):
    function = _function(ROOT / "config_flow.py", "_async_step_optimization")
    submission = next(node for node in function.body if isinstance(node, ast.If) and ast.unparse(node.test) == "user_input is not None")
    # Run the production validation and entry-data write, independent of the
    # unrelated optimizer, AI and hardware-reserve settings in this large form.
    nodes = [node for node in submission.body if (
        isinstance(node, ast.If) and ast.unparse(node.test) == "is_tesla"
        or isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {"new_data", "new_options"}
            for target in node.targets
        )
    )]
    function.body = nodes + [ast.Return(value=ast.Name(id="new_data", ctx=ast.Load()))]
    namespace = _flow_namespace()
    namespace["is_tesla"] = is_tesla
    flow = SimpleNamespace(
        config_entry=SimpleNamespace(data={BUY_KEY: 0.7, SELL_KEY: 30}, options={}),
        _async_step_optimization=AsyncMock(return_value="invalid"),
    )
    result = await _compile_flow(function, namespace)(flow, submitted)
    if expected is None:
        assert result == "invalid"
        assert flow._async_step_optimization.call_args.kwargs["errors"] == {
            "base": "tesla_force_discharge_prices_invalid"
        }
    else:
        assert (result[BUY_KEY], result[SELL_KEY]) == expected
        flow._async_step_optimization.assert_not_called()
    assert flow.config_entry.data == {BUY_KEY: 0.7, SELL_KEY: 30}


@pytest.mark.parametrize("is_tesla", [True, False])
def test_optimizer_renders_saved_prices_only_for_tesla(is_tesla):
    function = _function(ROOT / "config_flow.py", "_async_step_optimization")
    branch = next(node for node in function.body if isinstance(node, ast.If)
                  and ast.unparse(node.test) == "is_tesla")
    namespace = _flow_namespace()
    namespace.update(
        is_tesla=is_tesla,
        self=SimpleNamespace(config_entry=SimpleNamespace(data={BUY_KEY: 0.7, SELL_KEY: 30})),
        current_form_values={}, schema_fields={},
        vol=SimpleNamespace(Required=lambda key, **kwargs: key),
        NumberSelector=lambda config: config,
        NumberSelectorConfig=lambda **kwargs: kwargs,
        NumberSelectorMode=SimpleNamespace(BOX="box"),
    )
    exec(compile(ast.Module(body=[branch], type_ignores=[]), "<price-schema>", "exec"), namespace)
    assert namespace["current_form_values"] == ({BUY_KEY: 0.7, SELL_KEY: 30} if is_tesla else {})
    schema = namespace["schema_fields"]
    assert set(schema) == ({BUY_KEY, SELL_KEY} if is_tesla else set())
    if is_tesla:
        assert schema[BUY_KEY]["min"] == 0
        assert schema[SELL_KEY]["min"] == 0.01
        assert schema[SELL_KEY]["max"] == 99


def test_force_price_fields_live_in_optimizer_dispatch_group():
    source = (ROOT / "config_flow.py").read_text()
    for name in ("async_step_tesla_provider", "async_step_tesla_connection", "async_step_site_selection"):
        method = ast.get_source_segment(source, _function(ROOT / "config_flow.py", name))
        assert "CONF_TESLA_FORCE_DISCHARGE" not in method
    optimizer = _function(ROOT / "config_flow.py", "_async_step_optimization")
    sections = next(node.value for node in optimizer.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "section_fields" for t in node.targets))
    namespace = _flow_namespace()
    groups = eval(compile(ast.Expression(body=sections), "<sections>", "eval"), namespace)
    assert {BUY_KEY, SELL_KEY} <= groups["dispatch_behaviour"]
    for name in ("strings.json", "translations/en.json"):
        translations = json.loads((ROOT / name).read_text())
        for step in (translations["config"]["step"]["tesla_provider"], translations["options"]["step"]["tesla_connection"]):
            assert not {BUY_KEY, SELL_KEY} & step["data"].keys()
        dispatch = translations["options"]["step"]["optimization"]["sections"]["dispatch_behaviour"]
        assert {BUY_KEY, SELL_KEY} <= dispatch["data"].keys()
        assert {BUY_KEY, SELL_KEY} <= dispatch["data_description"].keys()
