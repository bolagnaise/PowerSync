"""Tests for Tesla Powerwall BMS health normalisation."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from unittest.mock import Mock


INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)
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
trim_excess_pw3_follower_placeholders = (
    bms_health.trim_excess_pw3_follower_placeholders
)
resolve_physical_battery_count = bms_health.resolve_physical_battery_count


def test_battery_health_fetch_uses_site_info_as_authoritative_count():
    tree = ast.parse(INIT_PATH.read_text())
    method = next(
        child
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BatteryHealthView"
        for child in node.body
        if isinstance(child, ast.AsyncFunctionDef)
        and child.name == "_try_fleet_api_bms_fetch"
    )
    assignment = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "batt_count" for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "resolve_physical_battery_count"
    )

    assert [arg.id for arg in assignment.value.args if isinstance(arg, ast.Name)] == [
        "_derived_batt_count",
        "_site_batt_count",
    ]


def test_battery_health_uses_separate_local_and_cloud_v1r_ttls():
    source = INIT_PATH.read_text()
    method_source = source[
        source.index("    async def _try_fleet_api_bms_fetch"):
        source.index("    async def _try_fleet_api_solar_strings_fetch")
    ]

    assert "_sign_in_thread, 12" in method_source
    assert "data=signed_local" in method_source
    assert "_sign_in_thread, 300" in method_source
    assert "_b64.b64encode(signed_cloud)" in method_source


def test_site_info_corrects_intermittent_relay_pack_undercount_after_degradation():
    assert resolve_physical_battery_count(4, 5) == 5


def test_site_info_still_corrects_double_bms_overcount():
    assert resolve_physical_battery_count(10, 5) == 5


def test_missing_or_invalid_site_count_preserves_relay_count():
    assert resolve_physical_battery_count(4, None) == 4
    assert resolve_physical_battery_count(4, 0) == 4
    assert resolve_physical_battery_count(4, "unknown") == 4


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


def test_reconcile_logs_at_debug_level():
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
    logger = Mock()

    reconcile_pack_remaining_with_aggregate(packs, 12000.0, 28760.0, logger=logger)

    logger.debug.assert_called_once()
    logger.warning.assert_not_called()


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


def test_trims_extra_null_bms_followers_to_physical_pw3_followers():
    packs = [
        {
            "serialNumber": "TG125xxxxxxxNY",
            "bmsSerialNumber": "TG125xxxxxxxNY",
            "isFollower": False,
            "nominalFullPackEnergyWh": 14470.0,
            "nominalEnergyRemainingWh": 7000.0,
        },
        {
            "serialNumber": None,
            "bmsSerialNumber": None,
            "isFollower": True,
            "nominalFullPackEnergyWh": 0.0,
            "nominalEnergyRemainingWh": 0.0,
        },
        {
            "serialNumber": None,
            "bmsSerialNumber": None,
            "isFollower": True,
            "nominalFullPackEnergyWh": 0.0,
            "nominalEnergyRemainingWh": 0.0,
        },
        {
            "serialNumber": None,
            "bmsSerialNumber": None,
            "isFollower": True,
            "nominalFullPackEnergyWh": 0.0,
            "nominalEnergyRemainingWh": 0.0,
        },
    ]
    battery_blocks = [
        {"din": "1707000-30-K--TG125xxxxxxxNY"},
        {"din": "1707000-30-L--TG125xxxxxxx2P"},
        {"din": "1707000-30-L--TG125xxxxxxx9Y"},
    ]

    dropped = trim_excess_pw3_follower_placeholders(
        packs,
        battery_blocks,
        "TESLA-GATEWAY-DIN",
    )

    assert dropped == 1
    assert len(packs) == 3

    follower_indices = [
        i for i, p in enumerate(packs)
        if p.get("isFollower") and p.get("nominalFullPackEnergyWh") == 0
    ]
    inferred_full_wh = (
        43250.0
        - sum(p["nominalFullPackEnergyWh"] for p in packs if not p.get("isFollower"))
    ) / len(follower_indices)

    assert round(inferred_full_wh) == 14390


def test_assigns_pw3_roles_when_pairing_din_is_gateway_not_powerwall():
    packs = [
        {
            "serialNumber": "TG125xxxxxxxNY",
            "bmsSerialNumber": "TG125xxxxxxxNY",
            "isFollower": False,
        },
        {"serialNumber": None, "bmsSerialNumber": None, "isFollower": True},
        {"serialNumber": None, "bmsSerialNumber": None, "isFollower": True},
    ]
    battery_blocks = [
        {"din": "1707000-30-K--TG125xxxxxxxNY"},
        {"din": "1707000-30-L--TG125xxxxxxx2P"},
        {"din": "1707000-30-L--TG125xxxxxxx9Y"},
    ]

    is_pw3 = assign_pack_roles_from_battery_blocks(
        packs,
        battery_blocks,
        "TESLA-GATEWAY-DIN",
        [],
    )

    assert is_pw3 is True
    assert [pack["role"] for pack in packs] == ["leader", "follower", "follower"]
    assert packs[0]["physicalDin"] == "1707000-30-K--TG125xxxxxxxNY"
    assert packs[1]["physicalDin"] == "1707000-30-L--TG125xxxxxxx2P"
    assert packs[2]["physicalDin"] == "1707000-30-L--TG125xxxxxxx9Y"


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


# --- Ticket 202: PW3 leader + 2 expansions + 2 Powerwall 2 units ----------------

def _bms_processing_runner():
    """Compile the real post-fetch processing of ``_try_fleet_api_bms_fetch``.

    Executes every statement from the ``status = ...`` aggregate read through
    ``return response`` with the decoded payload injected, so the regression
    covers the shipped pipeline rather than a re-implementation.
    """
    import datetime
    import logging
    import types

    tree = ast.parse(INIT_PATH.read_text())
    method = next(
        child
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BatteryHealthView"
        for child in node.body
        if isinstance(child, ast.AsyncFunctionDef)
        and child.name == "_try_fleet_api_bms_fetch"
    )
    start = next(
        idx for idx, stmt in enumerate(method.body)
        if isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "status" for t in stmt.targets)
    )
    func = ast.FunctionDef(
        name="_process",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg=name) for name in (
                "self", "entry", "data", "din", "source", "fleet_site_id", "gateway_config",
            )],
            kwonlyargs=[], kw_defaults=[], defaults=[],
        ),
        body=method.body[start:],
        decorator_list=[],
        returns=None,
        type_params=[],
    )
    module = ast.Module(body=[func], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        name: getattr(bms_health, name)
        for name in dir(bms_health)
        if not name.startswith("__")
    }
    namespace.update({
        "_LOGGER": logging.getLogger("test_t202"),
        "DOMAIN": "power_sync",
        "CONF_POWERWALL_LOCAL_ENERGY_SITE_ID": "powerwall_local_energy_site_id",
        "dt_util": types.SimpleNamespace(now=lambda: datetime.datetime(2026, 11, 26, 19, 42)),
    })
    exec(compile(module, str(INIT_PATH), "exec"), namespace)
    return namespace["_process"]


def _run_bms_processing(data, din="1707000-30-L--TG126013001MFM"):
    process = _bms_processing_runner()
    view = Mock()
    view._hass.data = {}
    entry = Mock()
    entry.entry_id = "entry"
    entry.data = {}
    return process(view, entry, data, din, "ha_local_tedapi", None, None)


def _msa(part, serial, full=None, remaining=None):
    signals = []
    if full is not None or remaining is not None:
        signals = [
            {"name": "BMS_nominalFullPackEnergy", "value": full},
            {"name": "BMS_nominalEnergyRemaining", "value": remaining},
        ]
    return {"partNumber": part, "serialNumber": serial, "signals": signals}


def _ticket_202_payload(es_can=None):
    """Reporter's 26 Nov 19:42 DeviceController reply (non-BMS rows trimmed)."""
    data = {
        "control": {
            "systemStatus": {
                "nominalFullPackEnergyWh": 71689,
                "nominalEnergyRemainingWh": 59000,
            },
            "batteryBlocks": [
                {"din": "1707000-30-L--TG126013001MFM"},
                {"din": "3012170-05-C--TG122300000TEF"},
                {"din": "3012170-45-E--TG124247002H2L"},
            ],
        },
        "components": {
            "msa": [
                _msa("1707000-30-L", "TG126013001MFM"),
                _msa("3012170-05-C", "TG122300000TEF"),
                _msa("3012170-45-E", "TG124247002H2L"),
                _msa("00000000000000000000", "TG126013002D5B", 14.34, 11.53),
                _msa("00000000000000000000", "TG126116001PNE", 14.42, 12.31),
                _msa("00000000000000000000", "TG126116001PSN", 14.47, 12.61),
                _msa("", "", 14.47, 0.2),
            ]
        },
    }
    if es_can is not None:
        data["esCan"] = es_can
    return data


def _labels(packs):
    labels = []
    for idx, pack in enumerate(packs):
        role = pack["role"]
        if role == "leader":
            labels.append("Leader Powerwall")
        elif role == "follower":
            n = sum(1 for p in packs[: idx + 1] if p["role"] == "follower")
            labels.append("Follower Powerwall" if n == 1 else f"Follower Powerwall {n}")
        else:
            n = sum(1 for p in packs[: idx + 1] if p["role"] == "expansion")
            labels.append(f"Expansion Pack {n}")
    return labels


def test_mixed_pw3_pw2_site_uses_pw2_pod_energy_and_drops_placeholder_row():
    es_can = {"bus": {
        "THC": [
            {"packageSerialNumber": "TG124247002H2L"},
            {"packageSerialNumber": "TG122300000TEF"},
        ],
        "POD": [
            {"POD_EnergyStatus": {"isMIA": False, "POD_nom_full_pack_energy": 14169,
                                  "POD_nom_energy_remaining": 11300}},
            {"POD_EnergyStatus": {"isMIA": False, "POD_nom_full_pack_energy": 14270,
                                  "POD_nom_energy_remaining": 11250}},
        ],
    }}

    result = _run_bms_processing(_ticket_202_payload(es_can))
    packs = result["individual_batteries"]

    assert _labels(packs) == [
        "Leader Powerwall",
        "Expansion Pack 1",
        "Expansion Pack 2",
        "Follower Powerwall",
        "Follower Powerwall 2",
    ]
    # The serial-less 14.47 kWh / 0.2 kWh placeholder is gone (was "Follower Powerwall 1.4%").
    assert all(p["nominalEnergyRemainingWh"] > 5000 for p in packs)
    followers = packs[3:]
    by_serial = {p["serialNumber"]: p for p in followers}
    assert by_serial["TG124247002H2L"]["nominalFullPackEnergyWh"] == 14169
    assert by_serial["TG122300000TEF"]["nominalFullPackEnergyWh"] == 14270
    assert by_serial["TG122300000TEF"]["physicalDin"] == "3012170-05-C--TG122300000TEF"
    assert all(p["energySource"] == "pw2_pod" for p in followers)
    assert result["current_capacity_wh"] == 71689


def test_mixed_pw3_pw2_site_without_pod_shares_residual_across_both_pw2s():
    result = _run_bms_processing(_ticket_202_payload())
    packs = result["individual_batteries"]

    assert _labels(packs) == [
        "Leader Powerwall",
        "Expansion Pack 1",
        "Expansion Pack 2",
        "Follower Powerwall",
        "Follower Powerwall 2",
    ]
    followers = packs[3:]
    assert [p["serialNumber"] for p in followers] == ["TG122300000TEF", "TG124247002H2L"]
    leader_stack_full = 14340 + 14420 + 14470
    leader_stack_rem = 11530 + 12310 + 12610
    for pack in followers:
        assert round(pack["nominalFullPackEnergyWh"]) == round((71689 - leader_stack_full) / 2)
        assert round(pack["nominalEnergyRemainingWh"]) == round((59000 - leader_stack_rem) / 2)
        soc = pack["nominalEnergyRemainingWh"] / pack["nominalFullPackEnergyWh"]
        assert 0.7 < soc < 0.9


def test_mixed_site_ignores_pod_that_disagrees_with_aggregate():
    es_can = {"bus": {"POD": [
        {"POD_EnergyStatus": {"POD_nom_full_pack_energy": 5000, "POD_nom_energy_remaining": 100}},
        {"POD_EnergyStatus": {"POD_nom_full_pack_energy": 5000, "POD_nom_energy_remaining": 100}},
    ]}}

    packs = _run_bms_processing(_ticket_202_payload(es_can))["individual_batteries"]

    assert [p["energySource"] for p in packs[3:]] == ["aggregate_share", "aggregate_share"]


def test_pw2_units_do_not_take_pw3_follower_slots():
    packs = [
        {"serialNumber": "L", "bmsSerialNumber": "L", "isFollower": False},
        {"serialNumber": "E1", "bmsSerialNumber": "E1", "isFollower": False},
        {"serialNumber": None, "bmsSerialNumber": None, "isFollower": False},
    ]
    battery_blocks = [
        {"din": "1707000-30-L--LEADER"},
        {"din": "3012170-05-C--PW2A"},
    ]

    assign_pack_roles_from_battery_blocks(packs, battery_blocks, "1707000-30-L--LEADER", [])

    assert [p["role"] for p in packs] == ["leader", "expansion", "expansion"]


def test_empty_bms_reply_does_not_publish_zero_health():
    data = {"control": {"batteryBlocks": [{"din": "1707000-30-L--TG126013001MFM"}]},
            "components": {"msa": []}}

    assert _run_bms_processing(data) is None
