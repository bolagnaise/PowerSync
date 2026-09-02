"""Regression coverage for persisted static tariffs during entry startup."""

from __future__ import annotations

import ast
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
