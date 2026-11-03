"""Regression coverage for persisted static tariffs during entry startup."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


COMPONENT = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


def _load_bootstrap(converter):
    """Extract the startup helper without importing Home Assistant."""
    tree = ast.parse(COMPONENT.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_bootstrap_static_tariff_schedule"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            function,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "Any": object,
        "HomeAssistant": object,
        "ConfigEntry": object,
        "DOMAIN": "power_sync",
        "convert_custom_tariff_to_schedule": converter,
        "currency_for_entry": lambda _entry, _hass: "AUD",
        "_LOGGER": SimpleNamespace(
            debug=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
        ),
    }
    exec(compile(module, str(COMPONENT), "exec"), namespace)
    return namespace["_bootstrap_static_tariff_schedule"]


class _TariffStore:
    def __init__(self, tariff):
        self._tariff = tariff

    def get_custom_tariff(self):
        return self._tariff


def test_persisted_agl_tariff_is_available_before_first_energy_refresh():
    """An existing AGL entry must price its first integrated import interval."""
    expected_schedule = {"tou_periods": {"OFF_PEAK": []}, "buy_price": 45.39}
    bootstrap = _load_bootstrap(lambda tariff, *, currency: expected_schedule)
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="entry-35", data={})
    tariff = {"name": "AGL Rewards", "seasons": {}}

    result = bootstrap(hass, entry, _TariffStore(tariff), "agl")

    assert result is expected_schedule
    assert hass.data["power_sync"]["entry-35"]["tariff_schedule"] is expected_schedule


def test_startup_bootstrap_skips_dynamic_providers_and_missing_tariffs():
    """Dynamic pricing and absent persisted tariffs retain their existing paths."""
    bootstrap = _load_bootstrap(lambda tariff, *, currency: {"unexpected": tariff})
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="entry-35", data={})

    assert bootstrap(hass, entry, _TariffStore({"name": "Ignored"}), "amber") is None
    assert bootstrap(hass, entry, _TariffStore(None), "agl") is None
    assert hass.data == {}


def test_startup_bootstrap_does_not_publish_an_invalid_conversion():
    """A malformed saved tariff still fails closed before energy polling."""
    bootstrap = _load_bootstrap(lambda _tariff, *, currency: {})
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="entry-35", data={})

    assert bootstrap(hass, entry, _TariffStore({"name": "Broken"}), "agl") is None
    assert hass.data == {}


def test_startup_bootstrap_uses_a_new_entry_tariff_when_store_is_empty():
    """A first setup gets the same early protection as a restored entry."""
    expected_schedule = {"tou_periods": {"OFF_PEAK": []}, "buy_price": 45.39}
    bootstrap = _load_bootstrap(lambda tariff, *, currency: expected_schedule)
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(
        entry_id="entry-35",
        data={"initial_custom_tariff": {"name": "AGL Rewards", "seasons": {}}},
    )

    assert bootstrap(hass, entry, _TariffStore(None), "agl") is expected_schedule
    assert hass.data["power_sync"]["entry-35"]["tariff_schedule"] is expected_schedule


def test_setup_bootstraps_and_preserves_tariff_before_refresh():
    """The temporary startup schedule survives the later full entry-data write."""
    source = COMPONENT.read_text()

    loaded = source.index("await automation_store.async_load()")
    bootstrap = source.index("_bootstrap_static_tariff_schedule(", loaded)
    first_refresh = source.index("await amber_coordinator.async_config_entry_first_refresh()")
    full_entry_data = source.index('hass.data[DOMAIN][entry.entry_id] = {')

    assert loaded < bootstrap < first_refresh < full_entry_data
    assert '"tariff_schedule": startup_tariff_schedule,' in source
    assert '"automation_store": automation_store,' in source


def test_automatic_agl_preset_is_repaired_before_startup_prices():
    source = COMPONENT.read_text()
    loaded = source.index("await automation_store.async_load()")
    migration = source.index("await async_migrate_ausgrid_seasonal_import_tariff(", loaded)
    bootstrap = source.index("_bootstrap_static_tariff_schedule(", loaded)
    assert loaded < migration < bootstrap


def test_agl_early_tariff_conversion_prefers_saved_plan_to_initial_snapshot():
    """A later setup stage cannot undo the repair or a user's manual edits."""
    tree = ast.parse(COMPONENT.read_text())
    early_conversion = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and any(
            isinstance(child, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "initial_custom_tariff"
                    for target in child.targets)
            for child in node.body
        )
    )
    module = ast.fix_missing_locations(ast.Module(body=[early_conversion], type_ignores=[]))
    original = {"name": "Old initial preset"}
    saved = {"name": "Saved repaired or edited plan"}
    for persisted in (saved, None):
        converted = []
        hass = SimpleNamespace(data={"power_sync": {"entry": {}}})
        namespace = {
            "electricity_provider": "agl",
            "entry": SimpleNamespace(entry_id="entry", data={"initial_custom_tariff": original}),
            "automation_store": _TariffStore(persisted),
            "hass": hass,
            "DOMAIN": "power_sync",
            "currency_for_entry": lambda *_: "AUD",
            "convert_custom_tariff_to_schedule": lambda tariff, **_: converted.append(tariff) or tariff,
            "_LOGGER": SimpleNamespace(info=lambda *_: None),
        }
        exec(compile(module, str(COMPONENT), "exec"), namespace)
        expected = saved if persisted else original
        assert converted == [expected]
        assert hass.data["power_sync"]["entry"]["tariff_schedule"] is expected


def test_agl_final_initial_tariff_save_keeps_the_persisted_plan():
    tree = ast.parse(COMPONENT.read_text())
    storage = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name) and node.test.id == "initial_custom_tariff"
        and any(isinstance(child, ast.Expr) and isinstance(child.value, ast.Await)
                for child in node.body)
    )
    function = ast.AsyncFunctionDef(
        name="save_initial", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="initial_custom_tariff")], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[storage], decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    initial, saved = {"name": "Original preset"}, {"name": "Persisted repaired or edited tariff"}

    class Store(_TariffStore):
        def set_custom_tariff(self, tariff):
            self._tariff = tariff
        async def async_save(self):
            pass

    for persisted in (saved, None):
        store = Store(persisted)
        updates = []
        namespace = {
            "initial_custom_tariff": initial,
            "electricity_provider": "agl",
            "automation_store": store,
            "entry": SimpleNamespace(data={"initial_custom_tariff": initial, "other": 1}),
            "hass": SimpleNamespace(config_entries=SimpleNamespace(
                async_update_entry=lambda entry, *, data: updates.append(data))),
            "_LOGGER": SimpleNamespace(info=lambda *_: None),
        }
        exec(compile(module, str(COMPONENT), "exec"), namespace)
        asyncio.run(namespace["save_initial"](initial))
        assert store.get_custom_tariff() is (saved if persisted else initial)
        assert updates == [{"other": 1}]
