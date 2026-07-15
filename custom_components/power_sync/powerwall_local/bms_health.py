"""Helpers for normalising Tesla Powerwall BMS health telemetry."""

from __future__ import annotations

import logging
import math
from typing import Any


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_physical_battery_count(
    relay_count: Any,
    site_battery_count: Any,
    aggregate_full_wh: Any,
) -> int:
    """Reconcile Tesla's physical site count with relay BMS telemetry.

    The DeviceController relay can both over-report PW3 BMS sub-modules and
    intermittently omit a physical pack. Site info can also retain registered
    but uninstalled expansion slots, so only let it increase the relay count
    when aggregate full-pack energy makes the relay count physically impossible.
    """
    try:
        derived_count = max(0, int(relay_count))
    except (TypeError, ValueError):
        derived_count = 0

    try:
        authoritative_count = int(site_battery_count)
    except (TypeError, ValueError):
        authoritative_count = 0

    aggregate_capacity = _as_float(aggregate_full_wh)
    if authoritative_count <= 0:
        return derived_count

    # Fresh PW3 packs currently report roughly 14.3-14.5 kWh nominal full
    # energy. A conservative 16 kWh ceiling leaves firmware/measurement
    # headroom while still proving that 71.95 kWh cannot belong to four packs.
    max_nominal_full_pack_wh = 16000.0
    minimum_physical_count = (
        math.ceil(aggregate_capacity / max_nominal_full_pack_wh)
        if aggregate_capacity is not None and aggregate_capacity > 0
        else 0
    )
    if minimum_physical_count and authoritative_count < minimum_physical_count:
        return derived_count

    if authoritative_count < derived_count:
        return authoritative_count
    if (
        authoritative_count > derived_count
        and derived_count < minimum_physical_count <= authoritative_count
    ):
        return authoritative_count
    return derived_count


def serial_from_din(din: Any) -> str | None:
    """Return the trailing serial from a Tesla DIN/VIN string."""
    if not din or not isinstance(din, str):
        return None
    if "--" not in din:
        return din
    return din.rsplit("--", 1)[-1] or None


def _block_din(block: dict[str, Any]) -> str | None:
    din = block.get("din") or block.get("vin")
    return din if isinstance(din, str) and din else None


def _battery_block_dins(battery_blocks: list[dict[str, Any]]) -> list[str]:
    return [
        din
        for din in (_block_din(block) for block in battery_blocks)
        if din
    ]


def _follower_dins_from_battery_blocks(
    battery_blocks: list[dict[str, Any]],
    leader_din: Any = None,
) -> list[str]:
    base_dins = _battery_block_dins(battery_blocks)
    leader_physical_din = leader_din if isinstance(leader_din, str) and leader_din else None
    if leader_physical_din not in base_dins:
        leader_physical_din = None
    leader_physical_din = leader_physical_din or (base_dins[0] if base_dins else None)
    return [din for din in base_dins if din != leader_physical_din]


def known_expansion_dins_from_gateway_config(config: dict[str, Any] | None) -> list[str]:
    """Return configured battery expansion DIN/VIN values from config.json."""
    blocks = (config or {}).get("battery_blocks") or []
    if not isinstance(blocks, list):
        return []

    dins: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        expansions = block.get("battery_expansions") or []
        if not isinstance(expansions, list):
            continue
        for expansion in expansions:
            if not isinstance(expansion, dict):
                continue
            din = expansion.get("din") or expansion.get("vin")
            if isinstance(din, str) and din:
                dins.append(din)
    return dins


def _is_pw3_din(din: Any) -> bool:
    return isinstance(din, str) and din.startswith("1707000-30-")


def _expansion_dins_by_serial(known_expansion_dins: list[str] | None) -> dict[str, str]:
    by_serial: dict[str, str] = {}
    for din in known_expansion_dins or []:
        serial = serial_from_din(din)
        if serial:
            by_serial[serial] = din
    return by_serial


def _pack_bms_serial(pack: dict[str, Any]) -> str | None:
    serial = pack.get("bmsSerialNumber") or pack.get("bms_serial_number") or pack.get("serialNumber")
    return serial if isinstance(serial, str) and serial else None


def trim_excess_pw3_follower_placeholders(
    packs: list[dict[str, Any]],
    battery_blocks: list[dict[str, Any]],
    leader_din: Any = None,
) -> int:
    """Drop null BMS follower placeholders beyond physical PW3 follower units.

    Some PW3 stacks expose extra ``PW3BMS`` rows where the BMS capacity signal key
    exists but the value is null. Only rows backed by non-leader ``batteryBlocks``
    represent physical follower Powerwalls; extra rows are placeholders and must
    not receive a share of the aggregate capacity.
    """
    if not packs:
        return 0

    base_dins = _battery_block_dins(battery_blocks)
    if not base_dins:
        return 0

    follower_limit = len(_follower_dins_from_battery_blocks(battery_blocks, leader_din))
    placeholder_indices = [
        idx
        for idx, pack in enumerate(packs)
        if pack.get("isFollower")
        and (_as_float(pack.get("nominalFullPackEnergyWh")) or 0) <= 0
    ]
    excess = len(placeholder_indices) - follower_limit
    if excess <= 0:
        return 0

    for idx in reversed(placeholder_indices[follower_limit:]):
        del packs[idx]
    return excess


def _choose_follower_indices(
    packs: list[dict[str, Any]],
    follower_slots_to_assign: int,
    known_expansion_by_serial: dict[str, str],
) -> set[int]:
    if follower_slots_to_assign <= 0:
        return set()

    candidates = [
        idx
        for idx, pack in enumerate(packs)
        if idx > 0
        and not pack.get("isFollower")
        and (
            not (serial := _pack_bms_serial(pack))
            or serial not in known_expansion_by_serial
        )
    ]
    no_serial_candidates = [
        idx for idx in candidates if not _pack_bms_serial(packs[idx])
    ]
    pool = (
        no_serial_candidates
        if len(no_serial_candidates) >= follower_slots_to_assign
        else candidates
    )
    return set(pool[-follower_slots_to_assign:])


def has_pw3_stack(
    battery_blocks: list[dict[str, Any]],
    components: list[dict[str, Any]],
    leader_din: Any = None,
) -> bool:
    """Identify PW3 stacks before applying leader/follower/expansion semantics."""
    if _is_pw3_din(leader_din):
        return True
    if any(_is_pw3_din(_block_din(block)) for block in battery_blocks):
        return True
    return any(
        isinstance(component.get("partNumber"), str)
        and component["partNumber"].startswith("1707000-30-")
        for component in components
    )


def assign_pack_roles_from_battery_blocks(
    packs: list[dict[str, Any]],
    battery_blocks: list[dict[str, Any]],
    leader_din: Any = None,
    components: list[dict[str, Any]] | None = None,
    known_expansion_dins: list[str] | None = None,
) -> bool:
    """Normalise per-pack roles and physical serials from Tesla batteryBlocks.

    PW3 stacks expose base inverter units in ``batteryBlocks`` and BMS modules in
    ``components.msa``; expansions may appear between the leader and follower BMS
    rows. PW2 sites expose separate Powerwalls and must not be labelled as PW3
    followers or expansion packs.

    Returns True when PW3 semantics were applied.
    """
    components = components or []
    base_dins = _battery_block_dins(battery_blocks)
    is_pw3_stack = has_pw3_stack(battery_blocks, components, leader_din)

    if not is_pw3_stack:
        for idx, pack in enumerate(packs):
            physical_din = base_dins[idx] if idx < len(base_dins) else None
            pack["role"] = "powerwall"
            pack["isFollower"] = False
            pack["isExpansion"] = False
            if physical_din:
                pack["physicalDin"] = physical_din
            if not pack.get("serialNumber"):
                serial = serial_from_din(physical_din)
                if serial:
                    pack["serialNumber"] = serial
        return False

    leader_physical_din = leader_din if isinstance(leader_din, str) and leader_din else None
    if leader_physical_din not in base_dins:
        leader_physical_din = None
    leader_physical_din = leader_physical_din or (base_dins[0] if base_dins else None)
    follower_dins = _follower_dins_from_battery_blocks(battery_blocks, leader_physical_din)
    explicit_follower_count = sum(1 for pack in packs if pack.get("isFollower"))
    follower_slots_to_assign = max(0, len(follower_dins) - explicit_follower_count)
    known_expansion_by_serial = _expansion_dins_by_serial(known_expansion_dins)
    follower_indices = _choose_follower_indices(
        packs,
        follower_slots_to_assign,
        known_expansion_by_serial,
    )

    follower_seq = 0
    for idx, pack in enumerate(packs):
        if idx == 0:
            role = "leader"
        elif pack.get("isFollower") or idx in follower_indices:
            role = "follower"
        else:
            role = "expansion"

        pack["role"] = role
        pack["isFollower"] = role == "follower"
        pack["isExpansion"] = role == "expansion"
        if role == "leader":
            pack["physicalDin"] = leader_physical_din
            pack["serialNumber"] = serial_from_din(leader_physical_din) or pack.get("serialNumber")
        elif role == "follower":
            physical_din = follower_dins[follower_seq] if follower_seq < len(follower_dins) else None
            follower_seq += 1
            pack["physicalDin"] = physical_din
            pack["serialNumber"] = serial_from_din(physical_din) or pack.get("serialNumber")
        else:
            expansion_din = known_expansion_by_serial.get(_pack_bms_serial(pack) or "")
            pack["physicalDin"] = expansion_din
            pack["serialNumber"] = serial_from_din(expansion_din) or pack.get("serialNumber")

    return True


def reconcile_pack_remaining_with_aggregate(
    packs: list[dict[str, Any]],
    aggregate_remaining_wh: Any,
    aggregate_full_wh: Any = None,
    *,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Fix stale near-empty expansion-pack readings using Tesla's aggregate BMS total.

    Tesla's MSA component surface can report a real expansion pack with a plausible
    full capacity but a stale near-zero remaining-energy value and no serial number.
    The control.systemStatus aggregate remains authoritative for the whole site, so
    use it to fill those suspect modules when all pack capacities are accounted for.
    """
    total_remaining = _as_float(aggregate_remaining_wh)
    if not packs or total_remaining is None or total_remaining <= 0:
        return packs

    prepared: list[tuple[dict[str, Any], float, float]] = []
    for pack in packs:
        full = _as_float(pack.get("nominalFullPackEnergyWh"))
        remaining = _as_float(pack.get("nominalEnergyRemainingWh"))
        if full is None or full <= 0 or remaining is None:
            return packs
        prepared.append((pack, full, remaining))

    pack_full_total = sum(full for _, full, _ in prepared)
    total_full = _as_float(aggregate_full_wh)
    if total_full and total_full > 0:
        full_tolerance = max(1000.0, total_full * 0.05)
        if abs(pack_full_total - total_full) > full_tolerance:
            return packs

    pack_remaining_total = sum(remaining for _, _, remaining in prepared)
    delta = total_remaining - pack_remaining_total
    remaining_tolerance = max(500.0, total_remaining * 0.05)
    if delta <= remaining_tolerance:
        return packs

    candidates: list[tuple[dict[str, Any], float, float]] = []
    for pack, full, remaining in prepared:
        is_expansion = bool(pack.get("isExpansion") or pack.get("role") == "expansion")
        is_follower = bool(pack.get("isFollower") or pack.get("role") == "follower")
        has_bms_serial = bool(
            pack.get("bmsSerialNumber")
            or pack.get("bms_serial_number")
            or (pack.get("serialNumber") if is_expansion else None)
            or (pack.get("serial_number") if is_expansion else None)
        )
        near_empty = remaining < 500.0 or remaining / full < 0.05
        if (is_expansion or is_follower) and not has_bms_serial and near_empty:
            candidates.append((pack, full, remaining))

    if not candidates:
        return packs

    candidate_ids = {id(pack) for pack, _, _ in candidates}
    trusted_remaining = sum(
        remaining
        for pack, _, remaining in prepared
        if id(pack) not in candidate_ids
    )
    replacement_remaining = total_remaining - trusted_remaining
    candidate_full_total = sum(full for _, full, _ in candidates)
    if replacement_remaining < 0:
        return packs
    if replacement_remaining > candidate_full_total * 1.05:
        return packs

    if logger:
        logger.debug(
            "fleet_api_bms: reconciling %d serial-less near-empty expansion pack(s) "
            "from aggregate remaining energy (pack sum %.0f Wh, aggregate %.0f Wh)",
            len(candidates),
            pack_remaining_total,
            total_remaining,
        )

    for pack, full, raw_remaining in candidates:
        share = replacement_remaining * (full / candidate_full_total)
        pack["rawNominalEnergyRemainingWh"] = raw_remaining
        pack["nominalEnergyRemainingWh"] = max(0.0, min(full, share))
        pack["remainingReconciledFromAggregate"] = True

    return packs
