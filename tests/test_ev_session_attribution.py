"""Per-vehicle attribution of charger-metered (OCPP) charging sessions."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import pinned_sys_modules


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
NOW = datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
_dt = types.ModuleType("homeassistant.util.dt")
_dt.DEFAULT_TIME_ZONE = timezone.utc
_dt.now = lambda: NOW
_dt.utcnow = lambda: NOW
_util = types.ModuleType("homeassistant.util")
_util.dt = _dt
_ha = types.ModuleType("homeassistant")
_ha.util = _util

_MODULES = {
    "power_sync": _ps,
    "power_sync.automations": _automations,
    "homeassistant": _ha,
    "homeassistant.util": _util,
    "homeassistant.util.dt": _dt,
}
_NAMES = (
    "power_sync.automations.ev_loadpoint_profiles",
    "power_sync.automations.ev_session_attribution",
    "power_sync.automations.ev_charging_session",
)


@pytest.fixture(autouse=True)
def modules():
    with pinned_sys_modules(_MODULES):
        for name in _NAMES:
            sys.modules.pop(name, None)
        loaded = {name.rsplit(".", 1)[1]: importlib.import_module(name) for name in _NAMES}
        yield SimpleNamespace(**loaded)
        for name in _NAMES:
            sys.modules.pop(name, None)


CARD_A = "04A1B2C3D4E5F6"
CARD_B = "0499887766AABB"


def _profiles(*ids, charger="wallbox"):
    return [
        {"vehicle_id": vid, "display_name": f"Car {vid[-1]}", "charger_type": "ocpp",
         "ocpp_charger_id": charger}
        for vid in ids
    ]


def _data(*ids, **extra):
    return {"vehicle_charging_configs": _profiles(*ids), **extra}


class _Store:
    def __init__(self, data):
        self._data = data
        self.saved = 0

    async def async_save(self):
        self.saved += 1


class _Manager:
    """Minimal live manager around real ChargingSession objects."""

    def __init__(self, session_cls):
        self._cls = session_cls
        self.active_sessions = {}
        self.marked = []

    def start(self, key, **kwargs):
        session = self._cls(id=f"id-{key}", vehicle_id=key, start_time=NOW.isoformat(), **kwargs)
        self.active_sessions[key] = session
        return session

    async def mark_metered_by(self, key, metering_id):
        self.marked.append((key, metering_id))
        self.active_sessions[key].metered_by_session_id = metering_id
        return True


def _tag_state(tag, reported=NOW):
    return SimpleNamespace(state=tag, last_reported=reported, last_updated=reported)


def _hass(data, remote_tags=()):
    charge_points = {f"cp{i}": SimpleNamespace(_remote_id_tag=t) for i, t in enumerate(remote_tags)}
    return SimpleNamespace(data={
        "power_sync": {"entry-1": {"automation_store": _Store(data)}},
        "ocpp": {"entry-x": SimpleNamespace(charge_points=charge_points), "config": {}},
    })


def _attribute(m, hass, manager, tag_state):
    asyncio.run(m.ev_session_attribution.async_attribute_charger_session(
        hass, "entry-1", manager, "ocpp_wallbox", "wallbox", tag_state))


def test_resolver_precedence(modules):
    attr = modules.ev_session_attribution
    data = _data("VIN_A", "VIN_B")
    ref = attr.tag_ref(CARD_A)
    data["rfid_tags"] = {ref: {"vehicle_id": "VIN_A", "last4": "E5F6"}}
    data["connected_ev_by_loadpoint"] = {"ocpp_wallbox": "VIN_B"}

    assert attr.resolve_session_vehicle(data, "ocpp_wallbox", CARD_A) == ("VIN_A", "rfid")
    assert attr.resolve_session_vehicle(data, "ocpp_wallbox", CARD_B) == ("VIN_B", "selected")
    data.pop("connected_ev_by_loadpoint")
    assert attr.resolve_session_vehicle(data, "ocpp_wallbox", None) == (None, "unassigned")
    assert attr.resolve_session_vehicle(_data("VIN_A"), "ocpp_wallbox", None) == ("VIN_A", "only_profile")


def test_mapping_to_deleted_vehicle_is_ignored(modules):
    attr = modules.ev_session_attribution
    data = _data("VIN_B")
    data["rfid_tags"] = {attr.tag_ref(CARD_A): {"vehicle_id": "VIN_GONE"}}
    assert attr.resolve_session_vehicle(data, "ocpp_wallbox", CARD_A) == ("VIN_B", "only_profile")


def test_rfid_session_is_attributed_and_final(modules):
    attr = modules.ev_session_attribution
    data = _data("VIN_A", "VIN_B")
    data["rfid_tags"] = {attr.tag_ref(CARD_A): {"vehicle_id": "VIN_A", "last4": "E5F6"}}
    data["connected_ev_by_loadpoint"] = {"ocpp_wallbox": "VIN_B"}
    hass = _hass(data)
    manager = _Manager(modules.ev_charging_session.ChargingSession)
    session = manager.start("ocpp_wallbox", mode="ocpp", attribution_source="unassigned")

    _attribute(modules, hass, manager, _tag_state(CARD_A))
    assert (session.attributed_vehicle_id, session.attribution_source) == ("VIN_A", "rfid")
    assert session.id_tag_last4 == "E5F6"
    assert session.charger_id == "wallbox"

    # A later card on the sensor cannot steal a session already tagged.
    _attribute(modules, hass, manager, _tag_state(CARD_B))
    assert session.attributed_vehicle_id == "VIN_A"
    assert session.effective_vehicle_id == "VIN_A"


def test_late_tag_upgrades_provisional_attribution(modules):
    attr = modules.ev_session_attribution
    data = _data("VIN_A", "VIN_B")
    data["connected_ev_by_loadpoint"] = {"ocpp_wallbox": "VIN_B"}
    hass = _hass(data)
    manager = _Manager(modules.ev_charging_session.ChargingSession)
    session = manager.start("ocpp_wallbox", mode="ocpp", attribution_source="unassigned")

    _attribute(modules, hass, manager, None)
    assert (session.attributed_vehicle_id, session.attribution_source) == ("VIN_B", "selected")

    data["rfid_tags"] = {attr.tag_ref(CARD_A): {"vehicle_id": "VIN_A"}}
    _attribute(modules, hass, manager, _tag_state(CARD_A))
    assert (session.attributed_vehicle_id, session.attribution_source) == ("VIN_A", "rfid")


def test_unmapped_card_is_recorded_once_without_raw_tag(modules):
    attr = modules.ev_session_attribution
    data = _data("VIN_A", "VIN_B")
    hass = _hass(data)
    store = hass.data["power_sync"]["entry-1"]["automation_store"]
    manager = _Manager(modules.ev_charging_session.ChargingSession)
    session = manager.start("ocpp_wallbox", mode="ocpp", attribution_source="unassigned")

    for _ in range(3):
        _attribute(modules, hass, manager, _tag_state(CARD_B))

    assert session.effective_vehicle_id == "unassigned"
    assert store.saved == 1
    listing = attr.public_tag_listing(data)
    assert listing["tags"] == []
    assert [item["last4"] for item in listing["recent_unmapped"]] == ["AABB"]
    assert CARD_B not in repr(data) and CARD_B not in repr(listing)

    ref = listing["recent_unmapped"][0]["tag_ref"]
    attr.assign_tag(data, ref, "VIN_B", "Sam's card")
    listing = attr.public_tag_listing(data)
    assert listing["recent_unmapped"] == []
    assert listing["tags"][0]["vehicle_id"] == "VIN_B"
    assert listing["tags"][0]["vehicle_name"] == "Car B"
    assert attr.resolve_session_vehicle(data, "ocpp_wallbox", CARD_B) == ("VIN_B", "rfid")


def test_assign_rejects_unknown_ref_and_vehicle(modules):
    attr = modules.ev_session_attribution
    data = _data("VIN_A")
    attr.record_unmapped_tag(data, CARD_A, "wallbox", now=NOW)
    ref = attr.tag_ref(CARD_A)
    with pytest.raises(ValueError):
        attr.assign_tag(data, ref, "VIN_NOPE")
    with pytest.raises(ValueError):
        attr.assign_tag(data, "0" * 64, "VIN_A")
    attr.assign_tag(data, ref, "VIN_A")
    assert attr.remove_vehicle_tags(data, "VIN_A") is True
    assert "rfid_tags" not in data


def test_stale_and_remote_start_tags_are_ignored(modules):
    data = _data("VIN_A", "VIN_B")
    attr = modules.ev_session_attribution
    data["rfid_tags"] = {attr.tag_ref(CARD_A): {"vehicle_id": "VIN_A"}}
    manager = _Manager(modules.ev_charging_session.ChargingSession)

    # Card left on the sensor from a session hours ago (free-vend start now).
    session = manager.start("ocpp_wallbox", mode="ocpp", attribution_source="unassigned")
    _attribute(modules, _hass(data), manager, _tag_state(CARD_A, NOW - timedelta(hours=3)))
    assert session.attribution_source == "unassigned"

    # HACS's own remote-start tag says nothing about the car.
    remote = "RANDOMREMOTETAG1"
    hass = _hass(data, remote_tags=[remote])
    _attribute(modules, hass, manager, _tag_state(remote))
    assert session.attribution_source == "unassigned"
    assert "rfid_recent_unmapped" not in data


def test_powersync_owned_session_on_charger_is_metered_once(modules):
    """A Tesla profile started by PowerSync on an OCPP charger is not doubled."""
    data = _data("VIN_A", "VIN_B")
    manager = _Manager(modules.ev_charging_session.ChargingSession)
    owner = manager.start("VIN_A", mode="solar_surplus")
    session = manager.start("ocpp_wallbox", mode="ocpp", attribution_source="unassigned")

    _attribute(modules, _hass(data), manager, None)

    assert (session.attributed_vehicle_id, session.attribution_source) == ("VIN_A", "selected")
    assert owner.metered_by_session_id == session.id


def test_statistics_split_cost_by_vehicle(modules):
    cs = modules.ev_charging_session
    manager = object.__new__(cs.ChargingSessionManager)
    manager.hass = SimpleNamespace(data={"power_sync": {"entry-1": {
        "automation_store": _Store(_data("VIN_A", "VIN_B"))}}})
    manager.active_sessions = {}

    def make(i, vid, kwh, cents, **kwargs):
        return cs.ChargingSession(
            id=f"s{i}", vehicle_id=vid, start_time=(NOW - timedelta(hours=i)).isoformat(),
            end_time=NOW.isoformat(), total_energy_kwh=kwh, grid_energy_kwh=kwh,
            total_cost_cents=cents, completed=True, mode="ocpp", **kwargs)

    manager._sessions_cache = [
        make(1, "ocpp_wallbox", 10, 300, charger_id="wallbox",
             attributed_vehicle_id="VIN_A", attribution_source="rfid"),
        make(2, "ocpp_wallbox", 20, 200, charger_id="wallbox",
             attributed_vehicle_id="VIN_B", attribution_source="rfid"),
        make(3, "ocpp_wallbox", 5, 150, charger_id="wallbox", attribution_source="unassigned"),
        # Empty owner session metered by the OCPP session: hidden from history.
        make(4, "VIN_A", 0, 0, metered_by_session_id="s1"),
        # Legacy record without attribution fields keeps its vehicle id.
        cs.ChargingSession.from_dict({"id": "old", "vehicle_id": "VIN_A",
                                      "start_time": (NOW - timedelta(days=2)).isoformat(),
                                      "completed": True, "total_energy_kwh": 4,
                                      "total_cost_cents": 100}),
    ]
    manager._cache_loaded = True

    stats = asyncio.run(manager.get_statistics(days=30))
    by_vehicle = stats["by_vehicle"]
    assert stats["total_sessions"] == 4
    assert set(by_vehicle) == {"VIN_A", "VIN_B", "unassigned"}
    assert by_vehicle["VIN_A"]["sessions"] == 2
    assert by_vehicle["VIN_A"]["cost_dollars"] == 4.0
    assert by_vehicle["VIN_A"]["display_name"] == "Car A"
    assert by_vehicle["VIN_B"]["avg_cost_per_kwh_cents"] == 10.0
    assert by_vehicle["unassigned"]["display_name"] == "Unassigned"
    assert stats["by_charger"]["wallbox"]["sessions"] == 3
    assert stats["by_charger"]["wallbox"]["cost_dollars"] == 6.5

    only_b = asyncio.run(manager.get_session_history(vehicle_id="VIN_B"))
    assert [s.id for s in only_b] == ["s2"]


def test_session_roundtrip_and_manual_reassignment(modules):
    cs = modules.ev_charging_session
    session = cs.ChargingSession(id="s1", vehicle_id="ocpp_wallbox", start_time=NOW.isoformat(),
                                 charger_id="wallbox", attributed_vehicle_id="VIN_A",
                                 attribution_source="rfid", id_tag_last4="E5F6")
    restored = cs.ChargingSession.from_dict(session.to_dict())
    assert restored.to_dict() == session.to_dict()
    assert session.to_dict()["effective_vehicle_id"] == "VIN_A"

    assert restored.set_attribution("VIN_B", "selected") is False
    assert restored.set_attribution("VIN_B", "manual") is True
    assert restored.effective_vehicle_id == "VIN_B"
