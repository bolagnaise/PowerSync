"""GoodWe whole-site solar sensor is configurable from the supported UI.

``custom_solar_power_entity`` must be offered when GoodWe is set up and when its
connection is edited, in both the direct-connection forms and the connection
profile forms, and the chosen sensor must survive a save followed by a reload
(re-opening the form shows it, the coordinator wiring reads it back).

The real config-flow step methods are extracted from ``config_flow.py`` and run
against stand-in Home Assistant objects, in the same style as the other
config-flow tests in this suite.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "power_sync"
CONFIG_FLOW_PATH = COMPONENT / "config_flow.py"
STRINGS_PATH = COMPONENT / "strings.json"
TRANSLATIONS_PATH = COMPONENT / "translations" / "en.json"

SITE_SOLAR = "sensor.total_solar_power"
KEY = "custom_solar_power_entity"
_MISSING = object()


# --------------------------------------------------------------------------- #
# Stand-ins
# --------------------------------------------------------------------------- #

class _Marker:
    """Stand-in for ``vol.Optional`` / ``vol.Required`` schema keys."""

    def __init__(self, schema, default=_MISSING, description=None):
        self.schema = schema
        self.default = default
        self.description = description

    def __hash__(self):
        return hash((type(self).__name__, self.schema))

    def __eq__(self, other):
        return type(self) is type(other) and self.schema == other.schema


class _Optional(_Marker):
    pass


class _Required(_Marker):
    pass


_VOL = SimpleNamespace(Optional=_Optional, Required=_Required, Schema=lambda fields: dict(fields))


def _load_const_and_profiles():
    package = ModuleType("goodwe_flow_test")
    package.__path__ = [str(COMPONENT)]
    backend = ModuleType("goodwe_flow_test.battery_backend")
    backend.__path__ = [str(COMPONENT / "battery_backend")]
    sys.modules[package.__name__] = package
    sys.modules[backend.__name__] = backend
    modules = {}
    for name, path in (
        ("goodwe_flow_test.const", COMPONENT / "const.py"),
        ("goodwe_flow_test.battery_backend.profiles", COMPONENT / "battery_backend" / "profiles.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules[name] = module
    return modules["goodwe_flow_test.const"], modules["goodwe_flow_test.battery_backend.profiles"]


@pytest.fixture()
def flow():
    saved = {n: m for n, m in sys.modules.items() if n.startswith("goodwe_flow_test")}
    const, profiles = _load_const_and_profiles()
    try:
        yield _Flow(const, profiles)
    finally:
        for name in [n for n in sys.modules if n.startswith("goodwe_flow_test")]:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


class _Flow:
    """Builds the real step methods inside a namespace of stand-ins."""

    def __init__(self, const, profiles) -> None:
        self.const = const
        self.profiles = profiles
        self.source = CONFIG_FLOW_PATH.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.namespace = self._namespace()

    # -- extraction --------------------------------------------------------- #
    def _segment(self, node) -> str:
        return textwrap.dedent(ast.get_source_segment(self.source, node, padded=True))

    def _function(self, name: str):
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        raise AssertionError(f"function {name} not found")

    def _method(self, class_name: str, name: str):
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                        return item
        raise AssertionError(f"{class_name}.{name} not found")

    def _class_of(self, name: str) -> str:
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                        return node.name
        raise AssertionError(f"{name} not found on any class")

    def _namespace(self) -> dict:
        ns = {k: v for k, v in vars(self.const).items() if not k.startswith("__")}
        ns.update(
            {
                "vol": _VOL,
                "Any": object,
                "FlowResult": dict,
                "_LOGGER": MagicMock(),
                "PROFILE_REGISTRY": self.profiles.PROFILE_REGISTRY,
                "profiles_for_system": self.profiles.profiles_for_system,
                "resolve_connection_profile": self.profiles.resolve_connection_profile,
                "BATTERY_SENSOR_DISPLAY_RECOMMENDED": "recommended",
                "BATTERY_SENSOR_DISPLAY_MODES": {"recommended": "Recommended"},
                # Unrelated GoodWe validation, covered by its own tests.
                "resolve_goodwe_ems_control_mode_for_protocol": lambda hass, mode, prefix, protocol: mode or "direct",
                "resolve_goodwe_ems_control_mode": lambda mode, prefix: mode or "direct",
                "resolve_goodwe_ems_entity_prefix": lambda hass, prefix: prefix,
                "validate_goodwe_ems_control_mode": lambda hass, mode, prefix: None,
                "goodwe_ems_control_options": lambda: [],
                "resolve_goodwe_port": lambda protocol, port: port or 8899,
            }
        )

        async def _telemetry_prefix(hass, prefix):
            return ""

        async def _connection_ok(hass, host, port):
            return {"success": True, "has_battery": True}

        ns["resolve_goodwe_entity_telemetry_prefix"] = _telemetry_prefix
        ns["test_goodwe_connection"] = _connection_ok
        # Selectors only need to be constructible.
        for name in (
            "EntitySelector", "EntitySelectorConfig", "TextSelector", "TextSelectorConfig",
            "TextSelectorType", "SelectSelector", "SelectSelectorConfig", "SelectSelectorMode",
            "SelectOptionDict", "NumberSelector", "NumberSelectorConfig", "NumberSelectorMode",
        ):
            ns[name] = MagicMock(name=name)
        exec(self._segment(self._function("goodwe_site_solar_schema")), ns)  # noqa: S102
        return ns

    def bind(self, class_name: str, method_names: list[str], extras: dict) -> SimpleNamespace:
        """Return an object whose listed methods are the real, extracted ones."""
        ns = dict(self.namespace)
        ns.update(extras)
        owner = SimpleNamespace(**extras)
        for name in method_names:
            node = self._method(class_name, name)
            exec(self._segment(node), ns)  # noqa: S102
            setattr(owner, name, ns[name].__get__(owner))
        return owner


class _Entry:
    def __init__(self, data: dict, options: dict | None = None) -> None:
        self.data = dict(data)
        self.options = dict(options or {})
        self.entry_id = "entry1"


class _Hass:
    def __init__(self) -> None:
        self.reloads: list[str] = []
        self.config_entries = SimpleNamespace(
            async_update_entry=self._update,
            async_reload=lambda entry_id: entry_id,
            async_entries=lambda domain: [],
            async_get_entry=lambda entry_id: None,
        )

    def _update(self, entry, *, data=None, options=None) -> None:
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)

    def async_create_task(self, task) -> None:
        self.reloads.append(task)


def _options_flow(flow: _Flow, entry: _Entry, method_names: list[str], class_name: str | None = None):
    hass = _Hass()

    def _create_entry(**kw):
        # Home Assistant's options-flow manager stores the created entry's data
        # as the config entry's options.
        entry.options = dict(kw.get("data") or {})
        return {"type": "create_entry", **kw}

    # ``_get_option`` and the entry-saving helpers are the real implementations.
    all_methods = list(dict.fromkeys(method_names + ["_get_option", "_effective_battery_system", "_save_connection_and_reload", "_schedule_entry_reload"]))
    if class_name is None:
        class_name = flow._class_of(method_names[0])
    obj = flow.bind(
        class_name,
        all_methods,
        {
            "hass": hass,
            "config_entry": entry,
            "async_show_form": lambda **kw: {"type": "form", **kw},
            "async_create_entry": _create_entry,
            "async_abort": lambda **kw: {"type": "abort", **kw},
        },
    )

    async def _save_profile(data_updates):
        return obj._save_connection_and_reload(data_updates)

    obj._save_connection_profile_and_reload = _save_profile
    return obj


def _schema_key(result: dict, key: str):
    for marker in result["data_schema"]:
        if marker.schema == key:
            return marker
    return None


def _submit(obj, step: str, user_input: dict | None):
    return asyncio.run(getattr(obj, step)(user_input))


# --------------------------------------------------------------------------- #
# Direct connection: options flow
# --------------------------------------------------------------------------- #

_DIRECT_INPUT = {
    "goodwe_host": "192.0.2.10",
    "goodwe_protocol": "udp",
    "goodwe_port": 8899,
    "goodwe_ems_control_mode": "direct",
    "goodwe_ems_entity_prefix": "",
}


def test_direct_options_form_offers_the_field_and_persists_it(flow):
    entry = _Entry({"battery_system": "goodwe"})
    obj = _options_flow(flow, entry, ["async_step_goodwe_connection_options"])

    form = _submit(obj, "async_step_goodwe_connection_options", None)
    assert _schema_key(form, KEY) is not None
    assert _schema_key(form, KEY).default is _MISSING  # nothing stored: no default

    saved = _submit(obj, "async_step_goodwe_connection_options", {**_DIRECT_INPUT, KEY: SITE_SOLAR})
    assert saved["type"] == "create_entry"
    assert entry.data[KEY] == SITE_SOLAR
    assert entry.options[KEY] == SITE_SOLAR
    assert saved["data"][KEY] == SITE_SOLAR
    assert obj.hass.reloads  # entry is reloaded so the coordinator picks it up

    # Re-open the form as Home Assistant would after the reload: value is retained.
    reopened = _submit(obj, "async_step_goodwe_connection_options", None)
    assert _schema_key(reopened, KEY).default == SITE_SOLAR


def test_direct_options_clearing_the_field_removes_it_everywhere(flow):
    entry = _Entry({"battery_system": "goodwe", KEY: SITE_SOLAR}, {KEY: SITE_SOLAR})
    obj = _options_flow(flow, entry, ["async_step_goodwe_connection_options"])

    _submit(obj, "async_step_goodwe_connection_options", dict(_DIRECT_INPUT))

    assert KEY not in entry.data
    assert KEY not in entry.options
    reopened = _submit(obj, "async_step_goodwe_connection_options", None)
    assert _schema_key(reopened, KEY).default is _MISSING


# --------------------------------------------------------------------------- #
# Direct connection: initial setup flow
# --------------------------------------------------------------------------- #

def _setup_flow(flow: _Flow):
    created = {}
    obj = flow.bind(
        "PowerSyncConfigFlow" if _has_class(flow, "PowerSyncConfigFlow") else flow._class_of("async_step_goodwe_connection"),
        ["async_step_goodwe_connection"],
        {
            "hass": SimpleNamespace(),
            "_goodwe_data": {},
            "async_show_form": lambda **kw: {"type": "form", **kw},
            "_create_final_entry": lambda: created.setdefault("entry", {"type": "create_entry"}),
        },
    )
    return obj, created


def _has_class(flow: _Flow, name: str) -> bool:
    return any(isinstance(n, ast.ClassDef) and n.name == name for n in flow.tree.body)


def test_setup_form_offers_the_field_and_stores_it(flow):
    obj, created = _setup_flow(flow)

    form = _submit(obj, "async_step_goodwe_connection", None)
    assert _schema_key(form, KEY) is not None

    result = _submit(obj, "async_step_goodwe_connection", {**_DIRECT_INPUT, KEY: SITE_SOLAR})
    assert result["type"] == "create_entry"
    assert obj._goodwe_data[KEY] == SITE_SOLAR


def test_setup_without_a_site_sensor_stores_nothing(flow):
    obj, _created = _setup_flow(flow)

    _submit(obj, "async_step_goodwe_connection", dict(_DIRECT_INPUT))

    assert KEY not in obj._goodwe_data


# --------------------------------------------------------------------------- #
# Connection-profile options form (also the only route for Home Assistant
# GoodWe entries, which have no inverter IP)
# --------------------------------------------------------------------------- #

def _profile_entry(**extra) -> _Entry:
    data = {
        "battery_system": "goodwe",
        "battery_connection_profile": "goodwe_direct",
        "goodwe_ems_control_mode": "direct",
        **extra,
    }
    return _Entry(data)


_PROFILE_INPUT = {
    "battery_connection_profile": "goodwe_direct",
    "battery_sensor_display_mode": "recommended",
}


def test_profile_options_form_offers_the_field_and_persists_it(flow):
    entry = _profile_entry()
    obj = _options_flow(flow, entry, ["async_step_battery_connection_profile"])

    form = _submit(obj, "async_step_battery_connection_profile", None)
    assert _schema_key(form, KEY) is not None
    assert _schema_key(form, KEY).default is _MISSING

    saved = _submit(obj, "async_step_battery_connection_profile", {**_PROFILE_INPUT, KEY: SITE_SOLAR})
    assert saved["type"] == "create_entry"
    assert entry.data[KEY] == SITE_SOLAR
    assert entry.options[KEY] == SITE_SOLAR

    reopened = _submit(obj, "async_step_battery_connection_profile", None)
    assert _schema_key(reopened, KEY).default == SITE_SOLAR


def test_profile_options_clearing_the_field_is_saved_as_empty(flow):
    entry = _profile_entry(**{KEY: SITE_SOLAR})
    obj = _options_flow(flow, entry, ["async_step_battery_connection_profile"])

    _submit(obj, "async_step_battery_connection_profile", dict(_PROFILE_INPUT))

    assert entry.options[KEY] == ""
    reopened = _submit(obj, "async_step_battery_connection_profile", None)
    assert _schema_key(reopened, KEY).default is _MISSING  # blank is never a default


def test_non_goodwe_profile_form_does_not_offer_the_field(flow):
    entry = _Entry(
        {"battery_system": "foxess", "battery_connection_profile": "foxess_modbus"}
    )
    obj = _options_flow(flow, entry, ["async_step_battery_connection_profile"])

    form = _submit(obj, "async_step_battery_connection_profile", None)

    assert _schema_key(form, KEY) is None


# --------------------------------------------------------------------------- #
# Profile setup form wiring and strings
# --------------------------------------------------------------------------- #

def test_profile_setup_form_offers_and_stores_the_field():
    source = CONFIG_FLOW_PATH.read_text(encoding="utf-8")
    setup = source.split("async def async_step_battery_connection_profile_setup(", 1)[1]
    setup = setup.split("\n    def _create_final_entry", 1)[0]

    assert "schema_fields.update(goodwe_site_solar_schema())" in setup
    assert "self._battery_profile_data[\n                            CONF_CUSTOM_SOLAR_POWER_ENTITY\n                        ] = site_solar" in setup


def test_goodwe_connection_keys_include_the_site_sensor_so_switching_clears_it():
    source = CONFIG_FLOW_PATH.read_text(encoding="utf-8")
    block = source.split("BATTERY_SYSTEM_GOODWE: (", 1)[1].split("),", 1)[0]

    assert "CONF_CUSTOM_SOLAR_POWER_ENTITY" in block


@pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
def test_every_goodwe_form_labels_the_field_as_whole_site_pv(path):
    strings = json.loads(path.read_text(encoding="utf-8"))
    steps = []
    for section in ("config", "options"):
        for step_id in (
            "goodwe_connection",
            "goodwe_connection_options",
            "battery_connection_profile",
            "battery_connection_profile_setup",
        ):
            step = strings.get(section, {}).get("step", {}).get(step_id)
            if step and "data" in step:
                steps.append((section, step_id, step))

    assert steps, "no GoodWe form strings found"
    for section, step_id, step in steps:
        label = step["data"].get(KEY, "")
        assert "whole-site" in label.lower() and "all pv" in label.lower(), (section, step_id)
        assert "AC-coupled" in step["data_description"][KEY], (section, step_id)
