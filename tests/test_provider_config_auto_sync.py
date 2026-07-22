"""Regression coverage for provider-config Auto Sync responses."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import textwrap


INIT_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


def _provider_config_get():
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "ProviderConfigView":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "get":
                    method_source = ast.get_source_segment(source, child)
                    assert method_source is not None
                    break
            else:
                raise AssertionError("ProviderConfigView.get not found")
            break
    else:
        raise AssertionError("ProviderConfigView not found")

    class _Logger:
        def info(self, *_args, **_kwargs):
            pass

        warning = info
        error = info

    def _json_response(payload, status=200):
        return SimpleNamespace(payload=payload, status=status)

    web = SimpleNamespace(Request=object, Response=object, json_response=_json_response)
    namespace = {
        "web": web,
        "_LOGGER": _Logger(),
        "DOMAIN": "power_sync",
        "CONF_BATTERY_SYSTEM": "battery_system",
        "CONF_ELECTRICITY_PROVIDER": "electricity_provider",
        "CONF_AUTO_SYNC_ENABLED": "auto_sync_enabled",
        "CONF_DEMAND_CHARGE_ENABLED": "demand_charge_enabled",
        "CONF_DEMAND_CHARGE_RATE": "demand_charge_rate",
        "CONF_DEMAND_CHARGE_START_TIME": "demand_charge_start_time",
        "CONF_DEMAND_CHARGE_END_TIME": "demand_charge_end_time",
        "CONF_DEMAND_CHARGE_DAYS": "demand_charge_days",
        "CONF_DEMAND_CHARGE_BILLING_DAY": "demand_charge_billing_day",
        "CONF_PRICE_SPIKE_ALERT": "price_spike_alert",
        "CONF_PRICE_SPIKE_IMPORT_THRESHOLD": "price_spike_import_threshold",
        "CONF_PRICE_SPIKE_EXPORT_THRESHOLD": "price_spike_export_threshold",
        "DEFAULT_PRICE_SPIKE_IMPORT_THRESHOLD": 100,
        "DEFAULT_PRICE_SPIKE_EXPORT_THRESHOLD": 50,
        "CONF_MONITORING_MODE": "monitoring_mode",
        "BATTERY_SYSTEM_ANKER_SOLIX": "anker_solix",
    }
    exec(textwrap.dedent(method_source), namespace)
    return namespace["get"]


def test_octopus_provider_config_returns_persisted_auto_sync_false():
    """Every provider exposing Auto Sync must return its persisted false value."""

    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"battery_system": "tesla"},
        options={
            "electricity_provider": "octopus",
            "auto_sync_enabled": False,
        },
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _domain: [entry]),
        data={"power_sync": {"entry-1": {}}},
    )

    response = asyncio.run(_provider_config_get()(SimpleNamespace(_hass=hass), None))

    assert response.status == 200
    assert response.payload["success"] is True
    assert response.payload["electricity_provider"] == "octopus"
    assert response.payload["config"]["auto_sync"] is False
