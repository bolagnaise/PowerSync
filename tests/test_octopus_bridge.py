"""BottlecapDave contracts: meter identity, live corrections and event lifecycle."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

SPEC = importlib.util.spec_from_file_location(
    "octopus_bridge_under_test",
    Path(__file__).parents[1] / "custom_components/power_sync/octopus_bridge.py",
)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)
NOW = datetime(2026, 11, 4, 17, 12, tzinfo=timezone.utc)


def account(mpan="111", value=30, serial="ABC"):
    return {
        "ACCOUNT": NS(account={"electricity_meter_points": [{
            "mpan": mpan, "meters": [{"serial_number": serial, "is_export": False}],
            "agreements": [{"tariff_code": "E-1R-INTELLI-VAR-24-10-29-C"}],
        }]}),
        f"ELECTRICITY_RATES_{mpan}_{serial}": NS(rates=[{
            "start": NOW.replace(minute=0), "end": NOW.replace(minute=30),
            "value_inc_vat": value, "is_intelligent_adjusted": value == 7,
        }]),
    }


def state(value=0.07, mpan="111", serial="ABC"):
    return NS(entity_id=f"sensor.octopus_energy_electricity_{serial}_{mpan}_current_rate",
              state=str(value), last_updated=NOW, attributes={
                  "start": NOW.replace(minute=0), "end": NOW.replace(minute=30),
                  "is_intelligent_adjusted": True,
              })


def hass(accounts=None, states=()):
    return NS(data={"octopus_energy": accounts or {}}, states=NS(
        async_all=lambda domain=None: [s for s in states if domain is None or s.entity_id.startswith(domain + ".")]))


def test_live_public_correction_replaces_internal_price_and_keeps_provenance():
    data = bridge.read_snapshot(hass({"A": account()}, [state()]), now=NOW)
    row = next(r for r in data["current"] if r["channelType"] == "general")
    assert row["perKwh"] == 7
    assert row["is_intelligent_adjusted"] is True
    assert data["octopus"]["binding"]["mpan"] == "111"
    assert data["octopus"]["export_status"] == "unknown"
    assert all(r["perKwh"] == 0 for r in data["forecast"] if r["channelType"] == "feedIn")


def test_multiple_import_meters_require_selection():
    source = hass({"A": account(), "B": account("222", 15)})
    with pytest.raises(bridge.OctopusSourceError, match="Select"):
        bridge.read_snapshot(source, now=NOW)
    data = bridge.read_snapshot(source, {"octopus_import_meter": "A|111|ABC"}, now=NOW)
    assert data["current"][0]["perKwh"] == 30


def test_released_upstream_agreement_shape_and_replaced_meters():
    data = account()
    point = data["ACCOUNT"].account["electricity_meter_points"][0]
    point["meters"].append({"serial_number": "OLD", "is_export": False, "active_to": NOW - timedelta(days=1)})
    point["agreements"] = [
        {"start": NOW - timedelta(days=30), "end": NOW - timedelta(days=1), "tariff_code": "E-1R-OLD-C"},
        {"start": NOW - timedelta(days=1), "end": None, "tariff_code": "E-1R-COSY-VAR-26-10-01-C"},
    ]
    snapshot = bridge.read_snapshot(hass({"A": data}), now=NOW)
    assert snapshot["tariff_code"] == "E-1R-COSY-VAR-26-10-01-C"
    assert snapshot["product_code"] == "COSY-VAR-26-10-01"
    assert snapshot["octopus"]["binding"]["serial"] == "ABC"


@pytest.mark.parametrize("pence", [-5, 0, 1, 2, 7, 250])
def test_public_rate_units_are_explicit(pence):
    data = bridge.read_snapshot(hass(states=[state(pence / 100)]), now=NOW)
    assert data["current"][0]["perKwh"] == pytest.approx(pence)


def test_material_revision_changes_inside_same_half_hour():
    source = hass({"A": account()})
    old = bridge.read_snapshot(source, now=NOW)
    source.data["octopus_energy"]["A"] = account(value=7)
    new = bridge.read_snapshot(source, now=NOW)
    assert old["revision"] != new["revision"]
    assert new["revision"] == bridge.read_snapshot(source, now=NOW + timedelta(seconds=1))["revision"]


def test_new_and_legacy_programs_are_deduplicated_without_claiming_free_retail():
    event = {"code": "event-1", "start": NOW, "end": NOW + timedelta(minutes=20)}
    source = hass({"A": account()}, [
        NS(entity_id="event.octopus_energy_a_octoplus_free_electricity_sessions",
           state="now", attributes={"joined_events": [event]}),
        NS(entity_id="event.octopus_energy_a_octoplus_power_up_events",
           state="now", attributes={"joined_events": [event]}),
        NS(entity_id="event.octopus_energy_b_octoplus_power_up_events",
           state="now", attributes={"joined_events": [{**event, "code": "wrong-account"}]}),
    ])
    data = bridge.read_snapshot(source, now=NOW)
    programs = data["octopus"]["programs"]
    assert len(programs) == 1
    assert programs[0]["code"] == "event-1"
    assert programs[0]["retail_rate_unchanged"] is True
    assert data["current"][0]["perKwh"] == 30


def test_upstream_events_apply_multiple_same_slot_corrections_and_close_drains():
    async def scenario():
        now = datetime.now(timezone.utc)
        source = hass({"A": account()})
        rates = source.data["octopus_energy"]["A"]["ELECTRICITY_RATES_111_ABC"].rates
        rates[0].update(start=now - timedelta(minutes=12), end=now + timedelta(minutes=18))
        listeners = {}

        def listen(name, callback):
            listeners.setdefault(name, []).append(callback)
            return lambda: listeners[name].remove(callback)

        source.bus = NS(async_listen=listen)
        source.async_create_task = lambda task, name: asyncio.create_task(task, name=name)
        source.is_running = True
        coordinator = NS(data=bridge.read_snapshot(source))
        coordinator.async_set_updated_data = lambda data: setattr(coordinator, "data", data)
        changed = []

        async def on_change():
            changed.append(coordinator.data["current"][0]["perKwh"])

        runtime = bridge.OctopusUpdateBridge(source, coordinator, {}, on_change)
        boundaries = []
        runtime._schedule_boundary = lambda data: boundaries.append(data["revision"])
        runtime.start()
        await asyncio.sleep(0.15)
        assert changed == []
        for value in (0.07, 0.08):
            runtime._event(NS(event_type=bridge.RATE_EVENTS[1], data={
                "mpan": "111", "serial_number": "ABC", "rates": [{
                    "start": rates[0]["start"], "end": rates[0]["end"],
                    "value_inc_vat": value, "is_intelligent_adjusted": True,
                }],
            }))
            await asyncio.sleep(0.15)
        assert changed == [7, 8]
        assert len(set(boundaries)) == 3
        source.is_running = False
        runtime._event(NS(event_type=bridge.RATE_EVENTS[1], data={
            "mpan": "111", "serial_number": "ABC", "rates": [{
                "start": rates[0]["start"], "end": rates[0]["end"], "value_inc_vat": 0.09,
            }],
        }))
        await asyncio.sleep(0.15)
        assert changed == [7, 8]
        source.is_running = True
        runtime.request()
        await asyncio.sleep(0.15)
        assert changed == [7, 8, 9]
        runtime.request()
        await runtime.close()
        assert runtime._task.done()
        assert not any(listeners.values())
        runtime.request()
        assert not runtime._pending

    asyncio.run(scenario())


def test_wrong_meter_rate_event_cannot_change_bound_price():
    data = bridge.read_snapshot(hass({"A": account()}), now=NOW, event_rates={
        "wrong": ({"mpan": "222", "serial_number": "ABC", "rates": [{
            "start": NOW.replace(minute=0), "end": NOW.replace(minute=30), "value_inc_vat": 0,
        }]}, NOW)
    })
    assert data["current"][0]["perKwh"] == 30


def test_explicit_rest_source_ignores_other_loaded_accounts():
    assert bridge.read_snapshot(hass({"A": account(), "B": account("222")}),
                                {"octopus_import_meter": "rest"}, now=NOW) is None


def test_explicit_missing_export_does_not_rebind_to_a_discovered_meter():
    data = bridge.read_snapshot(hass({"A": account()}), {"octopus_export_meter": "none"}, now=NOW)
    assert data["octopus"]["export_status"] == "unknown"
