"""Tests for EV charging session price lookup."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

from power_sync.automations import ev_pricing  # noqa: E402
from power_sync.automations.ev_pricing import (  # noqa: E402
    get_current_ev_prices,
    get_current_export_price,
)


def _hass_with(coordinator_key: str, import_cents: float, export_cents: float):
    return SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    coordinator_key: SimpleNamespace(
                        data={
                            "current": [
                                {
                                    "channelType": "general",
                                    "perKwh": import_cents,
                                },
                                {
                                    "channelType": "feedIn",
                                    "perKwh": -export_cents,
                                },
                            ],
                        },
                    ),
                },
            },
        },
    )


def test_ev_prices_cover_dynamic_provider_coordinators():
    provider_prices = {
        "amber_coordinator": (12.5, 4.0),
        "localvolts_coordinator": (8.1, 2.2),
        "octopus_coordinator": (19.0, 15.0),
        "epex_coordinator": (21.3, 6.7),
        "aemo_sensor_coordinator": (0.0, 3.0),
    }

    for coordinator_key, expected in provider_prices.items():
        assert get_current_ev_prices(
            _hass_with(coordinator_key, *expected),
            "entry-1",
        ) == expected


def test_ev_prices_fall_back_to_stored_current_prices():
    hass = SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    "current_prices": {
                        "import_cents": 27.0,
                        "export_cents": 9.5,
                    },
                },
            },
        },
    )

    assert get_current_ev_prices(hass, "entry-1") == (27.0, 9.5)


def test_strict_export_price_uses_signed_dynamic_provider_earnings():
    assert get_current_export_price(
        _hass_with("amber_coordinator", 30.0, 20.0),
        "entry-1",
    ) == 20.0

    pays_to_export = _hass_with("amber_coordinator", 30.0, -5.0)
    assert get_current_export_price(pays_to_export, "entry-1") == -5.0


def test_strict_export_price_has_no_synthetic_fallback():
    hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})

    assert get_current_export_price(hass, "entry-1") is None


def test_optimizer_retail_price_uses_timestamp_aligned_local_slot(monkeypatch):
    """The current retail slot is selected, not the first/padded optimizer value."""
    monkeypatch.setattr(
        ev_pricing.dt_util,
        "now",
        lambda: datetime(2026, 8, 5, 12, 7, tzinfo=timezone.utc),
    )
    hass = SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    "optimization_coordinator": SimpleNamespace(
                        data={
                            "schedule": {
                                "timestamps": [
                                    "2026-08-05T11:30:00+00:00",
                                    "2026-08-05T12:00:00+00:00",
                                    "2026-08-05T12:30:00+00:00",
                                ],
                                "import_price": [0.1111, 0.2432, 0.3555],
                            }
                        }
                    )
                }
            }
        }
    )

    assert ev_pricing.get_current_retail_price(hass, "entry-1") == 24.32


def test_optimizer_export_price_uses_timestamp_aligned_local_slot(monkeypatch):
    monkeypatch.setattr(
        ev_pricing.dt_util,
        "now",
        lambda: datetime(2026, 8, 5, 12, 7, tzinfo=timezone.utc),
    )
    hass = SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    "optimization_coordinator": SimpleNamespace(
                        data={
                            "schedule": {
                                "timestamps": [
                                    "2026-08-05T11:30:00+00:00",
                                    "2026-08-05T12:00:00+00:00",
                                    "2026-08-05T12:30:00+00:00",
                                ],
                                "export_price": [0.05, 0.20, 0.08],
                            }
                        }
                    )
                }
            }
        }
    )

    assert get_current_export_price(hass, "entry-1") == 20.0
