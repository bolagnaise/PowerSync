import importlib.util
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "powersync_network_envelope_test",
    ROOT / "custom_components" / "power_sync" / "network_envelope.py",
)
network = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = network
SPEC.loader.exec_module(network)

NOW = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)


def _active(**overrides):
    values = {
        "mode": "active",
        "scope": "aggregate_pcc",
        "current_limit_w": 10_000,
        "fallback_limit_w": 1_500,
        "static_limit_w": None,
        "source_status": "valid",
        "source_updated_at": NOW,
        "received_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "provenance": network.ProvenanceResult(True),
        "fresh_post_subscription": True,
        "attested_all_der_covered": True,
        "site_phase_count": 1,
        "pcc_fresh": True,
        "now": NOW,
    }
    values.update(overrides)
    return network.normalize_envelope(**values)


def test_zero_is_a_valid_live_limit_and_static_cap_takes_precedence() -> None:
    zero = _active(current_limit_w=0)
    assert zero.current_limit_w == 0
    assert zero.effective_limit_w == 0
    assert zero.active_export_permitted is True

    capped = _active(current_limit_w=10_000, static_limit_w=5_000)
    assert capped.effective_limit_w == 5_000


def test_invalid_live_source_uses_site_fallback_and_missing_fallback_fails_closed() -> None:
    stale = _active(received_at=NOW - timedelta(minutes=11))
    assert stale.effective_limit_w == 1_500
    assert stale.active_export_permitted is False

    missing = _active(fallback_limit_w=None)
    assert missing.effective_limit_w == 0
    assert missing.active_export_permitted is False
    assert "fallback" in missing.reason

    out_of_order = _active(source_status="invalid_out_of_order")
    assert out_of_order.effective_limit_w == 1_500
    assert out_of_order.active_export_permitted is False

    stale_schedule = _active(
        source_status="invalid_out_of_order",
        schedule=network.parse_schedule([
            {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "limit_w": 10_000,
            }
        ]),
    )
    assert stale_schedule.schedule == ()
    assert stale_schedule.limit_for_interval(
        NOW, NOW + timedelta(minutes=5)
    ) == 1_500


def test_overlaps_take_minimum_and_schedule_gaps_use_fallback() -> None:
    schedule = network.parse_schedule([
        {"start": NOW.isoformat(), "end": (NOW + timedelta(minutes=10)).isoformat(), "limit_w": 10_000},
        {"start": (NOW + timedelta(minutes=5)).isoformat(), "end": (NOW + timedelta(minutes=15)).isoformat(), "limit_w": 1_500},
        {"start": (NOW + timedelta(minutes=20)).isoformat(), "end": (NOW + timedelta(minutes=25)).isoformat(), "limit_w": 0},
        {"start": "bad", "end": "bad", "limit_w": "bad"},
    ])
    envelope = _active(schedule=schedule, current_limit_w=10_000)
    assert envelope.effective_limit_w == 10_000
    assert envelope.limit_for_interval(NOW, NOW + timedelta(minutes=5)) == 10_000
    assert envelope.limit_for_interval(NOW + timedelta(minutes=5), NOW + timedelta(minutes=10)) == 1_500
    assert envelope.limit_for_interval(NOW + timedelta(minutes=15), NOW + timedelta(minutes=20)) == 1_500
    assert envelope.limit_for_interval(NOW + timedelta(minutes=20), NOW + timedelta(minutes=25)) == 0


def test_next_change_includes_the_end_of_an_active_control() -> None:
    schedule = network.parse_schedule([
        {
            "start": (NOW - timedelta(minutes=5)).isoformat(),
            "end": (NOW + timedelta(minutes=5)).isoformat(),
            "limit_w": 1_500,
        },
        {
            "start": (NOW + timedelta(minutes=20)).isoformat(),
            "end": (NOW + timedelta(minutes=30)).isoformat(),
            "limit_w": 0,
        },
    ])
    envelope = _active(schedule=schedule)

    assert envelope.next_change_at == NOW + timedelta(minutes=5)
    assert envelope.limit_for_interval(
        envelope.next_change_at,
        envelope.next_change_at + timedelta(seconds=1),
    ) == 1_500


def test_future_increase_is_not_pinned_by_current_live_limit_but_static_cap_remains() -> None:
    schedule = network.parse_schedule([
        {
            "start": (NOW + timedelta(minutes=5)).isoformat(),
            "end": (NOW + timedelta(minutes=10)).isoformat(),
            "limit_w": 10_000,
        },
    ])
    envelope = _active(current_limit_w=1_500, schedule=schedule)
    assert envelope.effective_limit_w == 1_500
    assert envelope.limit_for_interval(
        NOW + timedelta(minutes=5), NOW + timedelta(minutes=10)
    ) == 10_000

    statically_capped = _active(
        current_limit_w=1_500,
        static_limit_w=5_000,
        schedule=schedule,
    )
    assert statically_capped.limit_for_interval(
        NOW + timedelta(minutes=5), NOW + timedelta(minutes=10)
    ) == 5_000


def test_optimizer_slots_apply_the_same_safety_margin_as_runtime_guard() -> None:
    schedule = network.parse_schedule([
        {
            "start": (NOW + timedelta(minutes=5)).isoformat(),
            "end": (NOW + timedelta(minutes=10)).isoformat(),
            "limit_w": 10_000,
        },
    ])
    envelope = _active(current_limit_w=1_500, schedule=schedule)

    assert network.optimizer_slot_limits(
        envelope,
        [NOW, NOW + timedelta(minutes=5)],
        5,
    ) == [pytest.approx(1_250), pytest.approx(9_500)]


def test_multiphase_per_phase_source_remains_monitoring_only() -> None:
    envelope = _active(scope="per_phase", site_phase_count=3)
    assert envelope.active_export_permitted is False
    assert envelope.reason == "multi-phase per-phase sources are monitoring-only"


class _FakeManager:
    def __init__(self, snapshot, pcc_export_w=0.0):
        self.snapshot = snapshot
        self._pcc_export_w = pcc_export_w
        self.faults = []
        self.swap_to = None

    def pcc_export_w(self):
        if self.swap_to is not None:
            self.snapshot = self.swap_to
            self.swap_to = None
        return self._pcc_export_w, datetime.now(timezone.utc)

    async def async_set_fault(self, reason):
        self.faults.append(reason)


def test_guard_rechecks_version_and_clamps_downward_before_write() -> None:
    initial = _active(current_limit_w=10_000, snapshot_version=1, now=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    downward = _active(current_limit_w=1_500, snapshot_version=2, now=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    manager = _FakeManager(initial)
    guard = network.ExportGuard(manager)
    assert guard.approve_reoptimized_snapshot(1) is True
    manager.swap_to = downward
    writes = []

    async def writer(value):
        writes.append(value)
        return True

    assert asyncio.run(guard.async_guard_write(8_000, writer)) is True
    assert writes == [1_250]


def test_guard_requires_reoptimization_before_using_an_upward_change() -> None:
    now = datetime.now(timezone.utc)
    low = _active(
        current_limit_w=1_500,
        snapshot_version=1,
        now=now,
        received_at=now,
        expires_at=now + timedelta(hours=1),
    )
    high = _active(
        current_limit_w=10_000,
        snapshot_version=2,
        now=now,
        received_at=now,
        expires_at=now + timedelta(hours=1),
    )
    manager = _FakeManager(low)
    guard = network.ExportGuard(manager)
    assert guard.approve_reoptimized_snapshot(1) is True
    manager.snapshot = high
    assert asyncio.run(guard.clamp_requested_export_w(8_000)) == 1_250
    assert guard.approve_reoptimized_snapshot(2) is True
    assert asyncio.run(guard.clamp_requested_export_w(8_000)) == 8_000


def test_guard_uses_current_schedule_control_not_scalar_live_limit() -> None:
    now = datetime.now(timezone.utc)
    schedule = network.parse_schedule([
        {
            "start": (now - timedelta(minutes=1)).isoformat(),
            "end": (now + timedelta(minutes=1)).isoformat(),
            "limit_w": 1_500,
        },
    ])
    envelope = _active(
        current_limit_w=10_000,
        schedule=schedule,
        snapshot_version=1,
        now=now,
        received_at=now,
        expires_at=now + timedelta(hours=1),
    )
    manager = _FakeManager(envelope)
    guard = network.ExportGuard(manager)

    assert envelope.effective_limit_w == 1_500
    assert guard.approve_reoptimized_snapshot(1) is True
    assert asyncio.run(guard.clamp_requested_export_w(8_000)) == 1_250


def test_guard_faults_and_stops_on_overshoot() -> None:
    now = datetime.now(timezone.utc)
    manager = _FakeManager(
        _active(current_limit_w=1_500, snapshot_version=1, now=now, received_at=now, expires_at=now + timedelta(hours=1)),
        pcc_export_w=1_400,
    )
    stops = []

    async def stop():
        stops.append(True)
        return True

    guard = network.ExportGuard(manager, stop_export=stop)
    assert asyncio.run(guard.clamp_requested_export_w(500)) == 0
    assert stops == [True]
    assert manager.faults == ["PCC export exceeded the guarded network limit"]


def test_latched_fault_disables_active_export_until_cleared() -> None:
    now = datetime.now(timezone.utc)
    manager = _FakeManager(
        _active(
            current_limit_w=5_000,
            snapshot_version=1,
            now=now,
            received_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    manager.snapshot = network.replace(
        manager.snapshot,
        fault="export stop command failed",
        active_export_permitted=False,
    )
    guard = network.ExportGuard(manager)
    assert asyncio.run(guard.clamp_requested_export_w(1_000)) == 0


def test_zero_power_mode_transition_is_denied_after_snapshot_becomes_invalid() -> None:
    first = _active(snapshot_version=21)
    manager = _FakeManager(first, pcc_export_w=0)
    guard = network.ExportGuard(manager)
    assert guard.approve_reoptimized_snapshot(21)
    writes: list[float] = []

    async def writer(value: float) -> bool:
        writes.append(value)
        return True

    manager.swap_to = network.replace(
        first,
        snapshot_version=22,
        active_export_permitted=False,
        reason="network envelope source is stale or invalid",
    )
    assert asyncio.run(guard.async_guard_write(0, writer)) is False
    assert writes == []


def test_no_envelope_off_preserves_legacy_scalar_behavior() -> None:
    off = network.normalize_envelope(
        mode="off",
        scope="aggregate_pcc",
        current_limit_w=None,
        fallback_limit_w=None,
        static_limit_w=5_000,
        source_status=None,
        source_updated_at=None,
        received_at=None,
        expires_at=None,
        now=NOW,
    )
    assert off.effective_limit_w is None
    assert off.limit_for_interval(NOW, NOW + timedelta(minutes=5)) is None


def test_configured_missing_status_or_expiry_source_fails_closed() -> None:
    now = datetime.now(timezone.utc)

    class States:
        values = {
            "sensor.limit": SimpleNamespace(
                state="10000",
                attributes={"unit_of_measurement": "W"},
                last_updated=now,
            ),
            "sensor.pcc": SimpleNamespace(
                state="0",
                attributes={"unit_of_measurement": "W"},
                last_updated=now,
            ),
        }

        def get(self, entity_id):
            return self.values.get(entity_id)

    entry = SimpleNamespace(
        entry_id="powersync",
        data={
            "network_export_mode": "active",
            "network_export_scope": "aggregate_pcc",
            "network_export_limit_entity": "sensor.limit",
            "network_export_status_entity": "sensor.missing_status",
            "network_export_expiry_entity": "sensor.missing_expiry",
            "network_export_pcc_power_entity": "sensor.pcc",
            "network_export_fallback_limit_w": 1500,
            "network_export_all_der_attested": True,
        },
        options={},
    )
    manager = network.HANetworkEnvelopeManager(
        SimpleNamespace(states=States()),
        entry,
        lambda: None,
    )
    manager._fresh_post_subscription = True
    manager._last_received_at = now

    async def trusted(_entity_id):
        return network.ProvenanceResult(True)

    manager._provenance = trusted
    snapshot = asyncio.run(manager._build_snapshot())

    assert snapshot.mode == "monitoring"
    assert snapshot.source_status == "invalid_expiry_source"
    assert snapshot.effective_limit_w == 1500
    assert snapshot.active_export_permitted is False
    assert "release gate" in snapshot.reason


class _StrictStates:
    """State registry that rejects invalid optional entity lookups."""

    def __init__(self, values):
        self.values = values
        self.calls = []

    def get(self, entity_id):
        if entity_id in (None, ""):
            raise AssertionError(f"invalid state lookup: {entity_id!r}")
        self.calls.append(entity_id)
        return self.values.get(entity_id)


def _manager_with_source_schedule(schedule_entity=...):
    now = datetime.now(timezone.utc)
    source_schedule = [
        {
            "start": now.isoformat(),
            "end": (now + timedelta(minutes=30)).isoformat(),
            "limit_w": 1_500,
        }
    ]
    values = {
        "sensor.limit": SimpleNamespace(
            state="10000",
            attributes={
                "unit_of_measurement": "W",
                "status": "valid",
                "valid_until": (now + timedelta(hours=1)).isoformat(),
                "schedule": source_schedule,
            },
            last_updated=now,
        ),
    }
    data = {
        "network_export_mode": "monitoring",
        "network_export_scope": "aggregate_pcc",
        "network_export_limit_entity": "sensor.limit",
        "network_export_fallback_limit_w": 500,
    }
    if schedule_entity is not ...:
        data["network_export_schedule_entity"] = schedule_entity
    states = _StrictStates(values)
    manager = network.HANetworkEnvelopeManager(
        SimpleNamespace(states=states),
        SimpleNamespace(entry_id="powersync", data=data, options={}),
        lambda: None,
    )
    manager._fresh_post_subscription = True
    manager._last_received_at = now

    async def trusted(_entity_id):
        return network.ProvenanceResult(True)

    manager._provenance = trusted
    return manager, states, now


@pytest.mark.parametrize("schedule_entity", [..., None, ""])
def test_unset_schedule_entity_uses_source_attribute_without_lookup(
    schedule_entity,
) -> None:
    manager, states, _now = _manager_with_source_schedule(schedule_entity)

    snapshot = asyncio.run(manager._build_snapshot())

    assert [point.limit_w for point in snapshot.schedule] == [1_500]
    assert states.calls == ["sensor.limit"]


def test_configured_schedule_entity_still_overrides_source_attribute() -> None:
    manager, states, now = _manager_with_source_schedule("sensor.schedule")
    states.values["sensor.schedule"] = SimpleNamespace(
        state="active",
        attributes={
            "schedule": [
                {
                    "start": now.isoformat(),
                    "end": (now + timedelta(minutes=30)).isoformat(),
                    "limit_w": 2_500,
                }
            ]
        },
        last_updated=now,
    )

    snapshot = asyncio.run(manager._build_snapshot())

    assert [point.limit_w for point in snapshot.schedule] == [2_500]
    assert states.calls == ["sensor.limit", "sensor.schedule"]


@pytest.mark.parametrize("pcc_entity", [..., None, ""])
def test_unset_pcc_entity_returns_unavailable_without_lookup(pcc_entity) -> None:
    data = {}
    if pcc_entity is not ...:
        data["network_export_pcc_power_entity"] = pcc_entity
    states = _StrictStates({})
    manager = network.HANetworkEnvelopeManager(
        SimpleNamespace(states=states),
        SimpleNamespace(entry_id="powersync", data=data, options={}),
        lambda: None,
    )

    assert manager.pcc_export_w() == (None, None)
    assert states.calls == []
