"""Tests for Tesla Powerwall BMS health normalisation."""

from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "powerwall_local"
    / "bms_health.py"
)
SPEC = importlib.util.spec_from_file_location("powerwall_bms_health", MODULE_PATH)
assert SPEC and SPEC.loader
bms_health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bms_health)
reconcile_pack_remaining_with_aggregate = (
    bms_health.reconcile_pack_remaining_with_aggregate
)
assign_pack_roles_from_battery_blocks = (
    bms_health.assign_pack_roles_from_battery_blocks
)
known_expansion_dins_from_gateway_config = (
    bms_health.known_expansion_dins_from_gateway_config
)


def test_reconciles_serial_less_near_empty_expansion_from_aggregate_remaining():
    packs = [
        {
            "role": "leader",
            "serialNumber": "LEADER",
            "isExpansion": False,
            "nominalFullPackEnergyWh": 14290.0,
            "nominalEnergyRemainingWh": 5740.0,
        },
        {
            "role": "follower",
            "serialNumber": "FOLLOWER",
            "isExpansion": False,
            "nominalFullPackEnergyWh": 14290.0,
            "nominalEnergyRemainingWh": 6820.0,
        },
        {
            "role": "expansion",
            "serialNumber": "EXPANSION",
            "isExpansion": True,
            "nominalFullPackEnergyWh": 14420.0,
            "nominalEnergyRemainingWh": 6950.0,
        },
        {
            "role": "expansion",
            "serialNumber": None,
            "isExpansion": True,
            "nominalFullPackEnergyWh": 14470.0,
            "nominalEnergyRemainingWh": 200.0,
        },
    ]

    result = reconcile_pack_remaining_with_aggregate(packs, 25750.0, 57400.0)

    corrected = result[3]
    assert corrected["rawNominalEnergyRemainingWh"] == 200.0
    assert corrected["remainingReconciledFromAggregate"] is True
    assert corrected["nominalEnergyRemainingWh"] == 6240.0
    assert sum(pack["nominalEnergyRemainingWh"] for pack in result) == 25750.0


def test_reconciles_serial_less_tail_follower_with_physical_serial_from_aggregate():
    packs = [
        {
            "role": "leader",
            "serialNumber": "TG124342000VC7",
            "bmsSerialNumber": "TG12434200183R",
            "isExpansion": False,
            "isFollower": False,
            "nominalFullPackEnergyWh": 14290.0,
            "nominalEnergyRemainingWh": 5610.0,
        },
        {
            "role": "expansion",
            "serialNumber": "TG1252140009TS",
            "bmsSerialNumber": "TG1252140009TS",
            "isExpansion": True,
            "isFollower": False,
            "nominalFullPackEnergyWh": 14290.0,
            "nominalEnergyRemainingWh": 6740.0,
        },
        {
            "role": "expansion",
            "serialNumber": "TG1252140014MX",
            "bmsSerialNumber": "TG1252140014MX",
            "isExpansion": True,
            "isFollower": False,
            "nominalFullPackEnergyWh": 14420.0,
            "nominalEnergyRemainingWh": 6870.0,
        },
        {
            "role": "follower",
            "serialNumber": "TG1253090007N5",
            "bmsSerialNumber": None,
            "isExpansion": False,
            "isFollower": True,
            "nominalFullPackEnergyWh": 14470.0,
            "nominalEnergyRemainingWh": 200.0,
        },
    ]

    result = reconcile_pack_remaining_with_aggregate(packs, 25350.0, 57400.0)

    corrected = result[3]
    assert corrected["rawNominalEnergyRemainingWh"] == 200.0
    assert corrected["remainingReconciledFromAggregate"] is True
    assert corrected["nominalEnergyRemainingWh"] == 6130.0
    assert sum(pack["nominalEnergyRemainingWh"] for pack in result) == 25350.0


def test_assigns_pw3_leader_expansions_and_tail_follower_from_battery_blocks():
    packs = [
        {"serialNumber": "TG12434200183R", "bmsSerialNumber": "TG12434200183R", "isFollower": False},
        {"serialNumber": "TG1252140009TS", "bmsSerialNumber": "TG1252140009TS", "isFollower": False},
        {"serialNumber": "TG1252140014MX", "bmsSerialNumber": "TG1252140014MX", "isFollower": False},
        {"serialNumber": None, "bmsSerialNumber": None, "isFollower": False},
    ]
    battery_blocks = [
        {"din": "1707000-30-K--TG124342000VC7"},
        {"din": "1707000-30-L--TG1253090007N5"},
    ]
    components = [{"partNumber": "1707000-30-K", "serialNumber": "TG124342000VC7"}]

    is_pw3 = assign_pack_roles_from_battery_blocks(
        packs,
        battery_blocks,
        "1707000-30-K--TG124342000VC7",
        components,
    )

    assert is_pw3 is True
    assert [pack["role"] for pack in packs] == [
        "leader",
        "expansion",
        "expansion",
        "follower",
    ]
    assert packs[0]["serialNumber"] == "TG124342000VC7"
    assert packs[0]["bmsSerialNumber"] == "TG12434200183R"
    assert packs[3]["serialNumber"] == "TG1253090007N5"
    assert packs[3]["physicalDin"] == "1707000-30-L--TG1253090007N5"
    assert packs[1]["serialNumber"] == "TG1252140009TS"


def test_assigns_pw3_interleaved_config_expansion_before_followers():
    packs = [
        {"serialNumber": "TG124304001STC", "bmsSerialNumber": "TG124304001STC", "isFollower": False},
        {"serialNumber": "TG125214001188", "bmsSerialNumber": "TG125214001188", "isFollower": False},
        {"serialNumber": None, "bmsSerialNumber": None, "isFollower": False},
        {"serialNumber": None, "bmsSerialNumber": None, "isFollower": False},
    ]
    battery_blocks = [
        {"din": "1707000-30-K--TG1243040015ND"},
        {"din": "1707000-30-L--TG1251520030PH"},
        {"din": "1707000-30-L--TG125152001WC8"},
    ]
    gateway_config = {
        "battery_blocks": [
            {
                "vin": "1707000-30-K--TG1243040015ND",
                "battery_expansions": [
                    {"din": "1807000-20-B--TG125214001188"},
                ],
            },
            {"vin": "1707000-30-L--TG1251520030PH"},
            {"vin": "1707000-30-L--TG125152001WC8"},
        ]
    }
    components = [{"partNumber": "1707000-30-K", "serialNumber": "TG1243040015ND"}]

    is_pw3 = assign_pack_roles_from_battery_blocks(
        packs,
        battery_blocks,
        "1707000-30-K--TG1243040015ND",
        components,
        known_expansion_dins_from_gateway_config(gateway_config),
    )

    assert is_pw3 is True
    assert [pack["role"] for pack in packs] == [
        "leader",
        "expansion",
        "follower",
        "follower",
    ]
    assert packs[1]["physicalDin"] == "1807000-20-B--TG125214001188"
    assert packs[1]["serialNumber"] == "TG125214001188"
    assert packs[2]["physicalDin"] == "1707000-30-L--TG1251520030PH"
    assert packs[3]["physicalDin"] == "1707000-30-L--TG125152001WC8"


def test_assigns_pw3_known_expansion_tail_slot_not_as_follower():
    packs = [
        {"serialNumber": "TG124304001STC", "bmsSerialNumber": "TG124304001STC", "isFollower": False},
        {"serialNumber": None, "bmsSerialNumber": None, "isFollower": False},
        {"serialNumber": None, "bmsSerialNumber": None, "isFollower": False},
        {"serialNumber": "TG125214001188", "bmsSerialNumber": "TG125214001188", "isFollower": False},
    ]
    battery_blocks = [
        {"din": "1707000-30-K--TG1243040015ND"},
        {"din": "1707000-30-L--TG1251520030PH"},
        {"din": "1707000-30-L--TG125152001WC8"},
    ]
    components = [{"partNumber": "1707000-30-K", "serialNumber": "TG1243040015ND"}]

    is_pw3 = assign_pack_roles_from_battery_blocks(
        packs,
        battery_blocks,
        "1707000-30-K--TG1243040015ND",
        components,
        ["1807000-20-B--TG125214001188"],
    )

    assert is_pw3 is True
    assert [pack["role"] for pack in packs] == [
        "leader",
        "follower",
        "follower",
        "expansion",
    ]
    assert packs[3]["physicalDin"] == "1807000-20-B--TG125214001188"
    assert packs[1]["physicalDin"] == "1707000-30-L--TG1251520030PH"
    assert packs[2]["physicalDin"] == "1707000-30-L--TG125152001WC8"


def test_keeps_four_pw2_packs_as_plain_powerwalls():
    packs = [
        {"serialNumber": "PW2A", "isFollower": False, "isExpansion": False},
        {"serialNumber": "PW2B", "isFollower": False, "isExpansion": False},
        {"serialNumber": "PW2C", "isFollower": False, "isExpansion": False},
        {"serialNumber": "PW2D", "isFollower": False, "isExpansion": False},
    ]
    battery_blocks = [
        {"din": "1111111-00-A--PW2A"},
        {"din": "1111111-00-A--PW2B"},
        {"din": "1111111-00-A--PW2C"},
        {"din": "1111111-00-A--PW2D"},
    ]

    is_pw3 = assign_pack_roles_from_battery_blocks(packs, battery_blocks, battery_blocks[0]["din"], [])

    assert is_pw3 is False
    assert [pack["role"] for pack in packs] == ["powerwall"] * 4
    assert [pack["serialNumber"] for pack in packs] == ["PW2A", "PW2B", "PW2C", "PW2D"]
    assert [pack["isFollower"] for pack in packs] == [False] * 4
    assert [pack["isExpansion"] for pack in packs] == [False] * 4
    assert [pack["physicalDin"] for pack in packs] == [
        "1111111-00-A--PW2A",
        "1111111-00-A--PW2B",
        "1111111-00-A--PW2C",
        "1111111-00-A--PW2D",
    ]


def test_does_not_reconcile_when_pack_sum_already_matches_aggregate():
    packs = [
        {
            "role": "leader",
            "serialNumber": "LEADER",
            "isExpansion": False,
            "nominalFullPackEnergyWh": 14290.0,
            "nominalEnergyRemainingWh": 5740.0,
        },
        {
            "role": "expansion",
            "serialNumber": None,
            "isExpansion": True,
            "nominalFullPackEnergyWh": 14470.0,
            "nominalEnergyRemainingWh": 200.0,
        },
    ]

    result = reconcile_pack_remaining_with_aggregate(packs, 5940.0, 28760.0)

    assert result[1]["nominalEnergyRemainingWh"] == 200.0
    assert "remainingReconciledFromAggregate" not in result[1]


def test_does_not_reconcile_when_pack_capacities_do_not_match_aggregate():
    packs = [
        {
            "role": "leader",
            "serialNumber": "LEADER",
            "isExpansion": False,
            "nominalFullPackEnergyWh": 14290.0,
            "nominalEnergyRemainingWh": 5740.0,
        },
        {
            "role": "expansion",
            "serialNumber": None,
            "isExpansion": True,
            "nominalFullPackEnergyWh": 14470.0,
            "nominalEnergyRemainingWh": 200.0,
        },
    ]

    result = reconcile_pack_remaining_with_aggregate(packs, 12000.0, 43000.0)

    assert result[1]["nominalEnergyRemainingWh"] == 200.0
    assert "remainingReconciledFromAggregate" not in result[1]
