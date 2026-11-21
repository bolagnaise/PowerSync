"""Load the retail plan selector modules without Home Assistant installed.

The test environment has no Home Assistant or voluptuous runtime, so the mixin
module gets light stand-ins that record schema fields and selector options.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import textwrap
import types
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
CONFIG_FLOW_PATH = COMPONENT_ROOT / "config_flow.py"
PACKAGE = "powersync_retail_plan_testpkg"


class Field(str):
    """A schema key that remembers its default and description."""

    def __new__(cls, name, default=None, description=None, required=False):
        field = super().__new__(cls, name)
        field.name = name
        field.default = default
        field.description = description or {}
        field.required = required
        return field


def _vol_module() -> types.ModuleType:
    vol = types.ModuleType("voluptuous")
    vol.UNDEFINED = None
    vol.Required = lambda name, default=None, description=None: Field(name, default, description, True)
    vol.Optional = lambda name, default=None, description=None: Field(name, default, description)
    vol.Schema = lambda fields: dict(fields)
    return vol


def _selector_module() -> types.ModuleType:
    selector = types.ModuleType("homeassistant.helpers.selector")
    selector.SelectSelectorConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    selector.NumberSelectorConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    selector.TextSelectorConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    selector.SelectSelector = lambda config: config
    selector.NumberSelector = lambda config: config
    selector.TextSelector = lambda config: config
    selector.BooleanSelector = lambda: SimpleNamespace(kind="boolean")
    selector.SelectOptionDict = lambda **kwargs: dict(kwargs)
    selector.SelectSelectorMode = SimpleNamespace(DROPDOWN="dropdown", LIST="list")
    selector.NumberSelectorMode = SimpleNamespace(BOX="box")
    selector.TextSelectorType = SimpleNamespace(TEXT="text")
    return selector


def load_modules(session=None) -> SimpleNamespace:
    """Import the pure modules and the mixin under an isolated package."""
    vol = _vol_module()
    selector = _selector_module()
    stubs = {
        "voluptuous": vol,
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.selector": selector,
        "homeassistant.helpers.aiohttp_client": types.ModuleType("homeassistant.helpers.aiohttp_client"),
    }
    stubs["homeassistant.helpers.aiohttp_client"].async_get_clientsession = lambda hass: session
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(COMPONENT_ROOT)]
    sys.modules[PACKAGE] = package
    loaded = {}
    try:
        for name in (
            "tariff_time", "tariff_templates", "aer_plans", "aer_tariff_import",
            "retail_plan_flow", "retail_plan_steps",
        ):
            qualified = f"{PACKAGE}.{name}"
            spec = importlib.util.spec_from_file_location(qualified, COMPONENT_ROOT / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified] = module
            assert spec and spec.loader
            spec.loader.exec_module(module)
            loaded[name] = module
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return SimpleNamespace(vol=vol, selector=selector, **loaded)


def _class_method(class_name: str, method_name: str):
    tree = ast.parse(CONFIG_FLOW_PATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return item
    raise AssertionError(f"{class_name}.{method_name} not found")


def config_flow_namespace(modules: SimpleNamespace) -> dict:
    """Names the extracted config-flow tariff methods use at module level."""
    flow = modules.retail_plan_flow
    selector = modules.selector
    return {
        "Any": object,
        "FlowResult": dict,
        "vol": modules.vol,
        "SelectSelector": selector.SelectSelector,
        "SelectSelectorConfig": selector.SelectSelectorConfig,
        "SelectSelectorMode": selector.SelectSelectorMode,
        "SelectOptionDict": selector.SelectOptionDict,
        "NumberSelector": selector.NumberSelector,
        "NumberSelectorConfig": selector.NumberSelectorConfig,
        "NumberSelectorMode": selector.NumberSelectorMode,
        "TextSelector": selector.TextSelector,
        "TextSelectorConfig": selector.TextSelectorConfig,
        "TextSelectorType": selector.TextSelectorType,
        "BooleanSelector": selector.BooleanSelector,
        "tariff_clock_text": flow.clock_text,
        "tariff_day_selector_options": flow.day_selector_options,
        "tariff_half_hour_options": flow.half_hour_options,
        "normalize_tariff_days": flow.normalize_days,
        "optional_tariff_rate": flow.optional_rate,
        "parse_tariff_clock": flow.parse_clock,
        "render_tariff_period_overview": flow.render_period_overview,
        "tariff_draft_warnings_text": flow.warnings_text,
        "normalize_currency": lambda value, fallback: value or fallback,
        "currency_for_provider": lambda provider, hass: "AUD",
        "DEFAULT_AGL_BATTERY_REWARDS_PEAK_EXPORT_RATE": 28.0,
        "DEFAULT_AGL_BATTERY_REWARDS_OFFPEAK_EXPORT_RATE": 3.0,
    }


def config_flow_class(modules: SimpleNamespace, namespace: dict | None = None) -> type:
    """A stand-in PowerSyncConfigFlow holding the real static tariff helpers."""
    namespace = namespace if namespace is not None else config_flow_namespace(modules)
    attributes = {}
    for name in (
        "_pop_tariff_period", "_tariff_period_remove_options", "_tariff_period_from_input",
        "_tariff_period_schema", "_tariff_season_options", "_custom_tariff_periods",
    ):
        source = ast.get_source_segment(CONFIG_FLOW_PATH.read_text(), _class_method(
            "PowerSyncOptionsFlow" if name == "_custom_tariff_periods" else "PowerSyncConfigFlow", name,
        ))
        exec(textwrap.dedent(source).replace("@staticmethod\n", ""), namespace)
        attributes[name] = staticmethod(namespace[name])
    builder = ast.get_source_segment(
        CONFIG_FLOW_PATH.read_text(), _class_method("PowerSyncConfigFlow", "_build_tariff_from_periods")
    )
    exec(textwrap.dedent(builder), namespace)
    attributes["_build_tariff_from_periods"] = namespace["_build_tariff_from_periods"]
    cls = type("PowerSyncConfigFlow", (), attributes)
    namespace["PowerSyncConfigFlow"] = cls
    return cls


def flow_method(modules: SimpleNamespace, class_name: str, method_name: str, namespace: dict):
    """Exec one config-flow method into ``namespace`` and return it."""
    source = ast.get_source_segment(CONFIG_FLOW_PATH.read_text(), _class_method(class_name, method_name))
    source = textwrap.dedent(source).replace("        from .const import DOMAIN\n", '        DOMAIN = "power_sync"\n')
    exec(source, namespace)
    return namespace[method_name]
