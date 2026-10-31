"""Site serialization, final fences and cancellation receipts without HA/I/O."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"
PACKAGE = "powerwall_site_control_test"
package = ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.powerwall_control", COMPONENT / "powerwall_control.py")
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def entry(site_id, entry_id="entry-a", provider="fleet_api"):
    return SimpleNamespace(entry_id=entry_id,
                           data={"tesla_energy_site_id": site_id, "tesla_api_provider": provider}, options={})


async def settle():
    """Let supervised workers and their done callbacks reach their barriers."""
    for _ in range(3):
        await asyncio.sleep(0)


def test_identity_shared_across_entries_providers_but_not_sites():
    hass = SimpleNamespace(data={"power_sync": {"entry-a": {"unrelated": True}}})
    a = m.coordinator_for(hass, entry(123))
    b = m.coordinator_for(hass, entry(" 00123 ", "entry-b", "teslemetry"))
    c = m.coordinator_for(hass, site_id="456")
    assert a is b and c is not a
    assert a.route_lock is b.route_lock and c.route_lock is not a.route_lock
    assert hass.data["power_sync"] == {"entry-a": {"unrelated": True}}
    with pytest.raises(ValueError):
        m.coordinator_for(hass, entry(None))


def test_request_and_each_physical_target_are_immutable_and_distinct():
    site = m.PowerwallSiteControl("123")
    targets = [m.NativeMaxTarget("charge"), m.BatteryPowerTarget(-5000),
               m.GridExportTarget(5000), m.SiteImportTarget(5000)]
    assert len({target.kind for target in targets}) == 4
    request = site.request("original", targets[0], deadline=150)
    with pytest.raises(FrozenInstanceError):
        request.deadline = 200
    with pytest.raises(FrozenInstanceError):
        targets[1].watts = 6000
    for invalid in (float("nan"), float("inf"), True, "5000"):
        with pytest.raises(ValueError):
            m.BatteryPowerTarget(invalid)
    with pytest.raises(ValueError):
        m.GridExportTarget(-1)
    with pytest.raises(TypeError):
        site.request("ambiguous", target={"watts": 5000})


@pytest.mark.asyncio
async def test_same_site_serializes_two_entries_while_other_site_progresses():
    hass = SimpleNamespace(data={})
    first = m.coordinator_for(hass, entry(123))
    second = m.coordinator_for(hass, entry("123", "entry-b", "teslemetry"))
    other = m.coordinator_for(hass, entry(456, "entry-c"))
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def blocking():
        writes.append("first")
        entered.set()
        await release.wait()

    async def write(label):
        writes.append(label)

    running = asyncio.create_task(first.run_write("first", blocking))
    await entered.wait()
    queued = asyncio.create_task(second.run_write("second", lambda: write("second")))
    await other.run_write("other", lambda: write("other"))
    assert writes == ["first", "other"]
    release.set()
    await asyncio.gather(running, queued)
    assert writes == ["first", "other", "second"]


@pytest.mark.asyncio
async def test_same_task_nested_cleanup_keeps_original_request_and_deadline():
    site = m.PowerwallSiteControl(123, clock=lambda: 100)
    writes = []

    async def cleanup():
        async with site.operation(label="cleanup", deadline=1000) as nested:
            writes.append(("cleanup", nested))
            async with site.write_context("restore"):
                writes.append(("restore", nested))

    async with site.operation(label="force", target=m.NativeMaxTarget("charge"), deadline=200) as original:
        await site.run_write("apply and cleanup", cleanup)
    assert [label for label, _ in writes] == ["cleanup", "restore"]
    assert all(request is original and request.deadline == 200 for _, request in writes)
    effects = site.snapshot()["effects"]
    assert {effect["request_id"] for effect in effects} == {original.id}
    assert all(effect["status"] == "returned" for effect in effects)


@pytest.mark.asyncio
async def test_child_task_never_inherits_operation_or_write_authority():
    site = m.PowerwallSiteControl(123)
    writes = []
    child_started = asyncio.Event()

    async def child():
        child_started.set()
        async with site.write_context("child"):
            writes.append("child")

    async with site.operation(label="parent") as original:
        async with site.write_context("parent"):
            pending = asyncio.create_task(child())
            await child_started.wait()
            await settle()
            assert writes == []
            with pytest.raises(m.ControlRequestStale):
                async def steal():
                    async with site.operation(original):
                        pytest.fail("A child inherited its parent's authority")
                await asyncio.create_task(steal())
        await pending
    snapshot = site.snapshot()
    parent_effect, child_effect = snapshot["effects"]
    assert parent_effect["request_id"] != child_effect["request_id"]
    assert writes == ["child"]


@pytest.mark.asyncio
async def test_cancelled_queued_writer_never_submits_or_mutates():
    site = m.PowerwallSiteControl(123)
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def block():
        entered.set()
        await release.wait()

    async def forbidden():
        writes.append("cancelled")

    running = asyncio.create_task(site.run_write("blocking", block))
    await entered.wait()
    queued = asyncio.create_task(site.run_write("queued", forbidden))
    await settle()
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    release.set()
    await running
    await settle()
    assert writes == []
    assert [effect["label"] for effect in site.snapshot()["effects"]] == ["blocking"]
    assert not site.snapshot()["submitted_tasks"]


@pytest.mark.asyncio
async def test_cancelled_submitted_worker_is_tracked_and_late_result_is_its_own():
    site = m.PowerwallSiteControl(123)
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def block():
        entered.set()
        await release.wait()
        writes.append("old completed")
        return "old result"

    caller = asyncio.create_task(site.run_write("old", block))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    snapshot = site.snapshot()
    old_id = snapshot["operation"]["id"]
    assert snapshot["operation"]["status"] == "cancelled"
    assert snapshot["uncertain_effects"][0]["request_id"] == old_id
    assert snapshot["submitted_tasks"][0]["status"] == "submitted"
    # Status/stop decisions can advance while the old I/O remains blocked.
    async with site.operation(label="new stop decision") as newer:
        assert site.snapshot()["operation"]["id"] == newer.id
    release.set()
    await settle()
    snapshot = site.snapshot()
    assert writes == ["old completed"]
    assert snapshot["operation"]["id"] == newer.id
    assert snapshot["operation"]["status"] == "completed"
    assert snapshot["effects"][0]["request_id"] == old_id
    assert snapshot["effects"][0]["status"] == "returned"
    assert not snapshot["uncertain_effects"] and not snapshot["submitted_tasks"]


@pytest.mark.asyncio
async def test_direct_context_cancellation_retains_unknown_and_original_journal():
    site = m.PowerwallSiteControl(123)
    journal = {"id": "existing-journal", "phase": "applying", "baseline": {"reserve": 15}}
    site.set_obligations("native_max", journal)
    entered = asyncio.Event()

    async def direct():
        async with site.write_context("signed local post"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(direct())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Exiting a later ordinary context must never clear a prior unknown.
    async with site.write_context("another journal-authorized action"):
        pass
    snapshot = site.snapshot()
    assert len(snapshot["uncertain_effects"]) == 1
    assert snapshot["uncertain_effects"][0]["label"] == "signed local post"
    assert snapshot["obligations"]["native_max"] == journal
    snapshot["obligations"]["native_max"]["baseline"]["reserve"] = 99
    assert site.snapshot()["obligations"]["native_max"]["baseline"]["reserve"] == 15
    assert journal["baseline"]["reserve"] == 15


@pytest.mark.asyncio
async def test_original_deadline_expiring_while_queued_never_reaches_callback():
    clock = SimpleNamespace(now=100)
    site = m.PowerwallSiteControl(123, clock=lambda: clock.now)
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def block():
        entered.set()
        await release.wait()

    async def forbidden():
        writes.append("expired")

    running = asyncio.create_task(site.run_write("blocking", block))
    await entered.wait()
    original = site.request("original", m.NativeMaxTarget("charge"), deadline=110)
    queued = asyncio.create_task(site.run_write("expired", forbidden, request=original, deadline=200))
    await settle()
    clock.now = 111
    release.set()
    await running
    with pytest.raises(m.ControlDeadlineExpired):
        await queued
    assert writes == []
    assert [effect["label"] for effect in site.snapshot()["effects"]] == ["blocking"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "current"])
async def test_final_boundary_rechecks_identity_and_current_after_queue(change):
    site = m.PowerwallSiteControl(123)
    config = SimpleNamespace(site_id="123", current=True)
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def block():
        entered.set()
        await release.wait()

    async def forbidden():
        writes.append("stale")

    running = asyncio.create_task(site.run_write("blocking", block))
    await entered.wait()
    queued = asyncio.create_task(site.run_write("fenced", forbidden,
                                               identity=lambda: config.site_id,
                                               is_current=lambda: config.current))
    await settle()
    if change == "identity":
        config.site_id = "456"
    else:
        config.current = False
    release.set()
    await running
    expected = m.ControlIdentityChanged if change == "identity" else m.ControlRequestStale
    with pytest.raises(expected):
        await queued
    assert writes == []


@pytest.mark.asyncio
async def test_callback_exception_is_preserved_as_unknown_without_blocking_status():
    site = m.PowerwallSiteControl(123)

    async def broken():
        raise OSError("lost response")

    with pytest.raises(OSError, match="lost response"):
        await site.run_write("lost response", broken)
    snapshot = site.snapshot()
    assert snapshot["operation"]["status"] == "failed"
    assert snapshot["uncertain_effects"][0]["label"] == "lost response"
    async with site.operation(label="recovery decision"):
        assert site.snapshot()["operation"]["status"] == "running"
    assert len(site.snapshot()["uncertain_effects"]) == 1


@pytest.mark.asyncio
async def test_nested_mutation_inherits_outer_write_guard_and_tighter_deadline():
    clock = SimpleNamespace(now=100)
    site = m.PowerwallSiteControl(123, clock=lambda: clock.now)
    current = SimpleNamespace(value=True)
    async with site.operation(label="parent", deadline=200):
        async with site.write_context("outer", deadline=110, is_current=lambda: current.value):
            current.value = False
            with pytest.raises(m.ControlRequestStale):
                async with site.write_context("stale nested"):
                    pytest.fail("Nested cleanup lost the outer write's current fence")
            current.value = True
            clock.now = 111
            with pytest.raises(m.ControlDeadlineExpired):
                async with site.write_context("expired nested"):
                    pytest.fail("Nested cleanup extended the outer write deadline")
    assert [effect["label"] for effect in site.snapshot()["effects"]] == ["outer"]


@pytest.mark.asyncio
async def test_async_or_truthy_admission_never_grants_mutation_authority():
    site = m.PowerwallSiteControl(123)
    async def async_guard():
        return True
    async def forbidden():
        pytest.fail("Admission did not return exact synchronous True")
    for guard in (lambda: "yes", async_guard):
        with pytest.raises(m.ControlRequestStale):
            await site.run_write("invalid admission", forbidden, is_current=guard)
    assert site.snapshot()["effects"] == []


@pytest.mark.asyncio
async def test_explicit_unknown_and_origin_survive_successful_context_and_retention():
    site = m.PowerwallSiteControl(123)
    async with site.operation(label="original", target=m.SiteImportTarget(5000)) as original:
        async with site.write_context("unresolved signed post") as receipt:
            site.mark_effect_uncertain(receipt, "existing_journal_unresolved")
    for index in range(70):
        async with site.write_context(f"ordinary-{index}"):
            pass
    snapshot = site.snapshot()
    assert snapshot["uncertain_effects"][0]["id"] == receipt.id
    origin = next(operation for operation in snapshot["operations"] if operation["id"] == original.id)
    assert origin["target"]["kind"] == m.ControlTargetKind.SITE_IMPORT_WATTS
    assert origin["target"]["watts"] == 5000


@pytest.mark.asyncio
async def test_expired_apply_can_restore_without_renewing_forcing_authority():
    clock = SimpleNamespace(now=100)
    site = m.PowerwallSiteControl(123, clock=lambda: clock.now)
    async with site.operation(label="apply", target=m.NativeMaxTarget("charge"), deadline=110) as apply:
        clock.now = 111
        with pytest.raises(m.ControlDeadlineExpired):
            async with site.write_context("expired apply"):
                pytest.fail("Expired forcing was submitted")
        async with site.operation(label="restore", restoring=True) as restore:
            assert restore.id != apply.id and restore.target is None and restore.deadline is None
            async with site.write_context("restore baseline"):
                pass
            with pytest.raises(m.ControlRequestStale):
                async with site.operation(target=m.NativeMaxTarget("charge")):
                    pytest.fail("Restoration renewed forcing")
            with pytest.raises(m.ControlRequestStale):
                async with site.write_context("reuse apply", request=apply):
                    pytest.fail("Restoration reused an apply request")
        with pytest.raises(m.ControlDeadlineExpired):
            async with site.write_context("outer apply remains expired"):
                pytest.fail("Restoration extended its parent's deadline")
        assert apply.deadline == 110
    assert [effect["label"] for effect in site.snapshot()["effects"]] == ["restore baseline"]


@pytest.mark.asyncio
async def test_restore_keeps_parent_identity_and_prepare_precedes_submission():
    site = m.PowerwallSiteControl(123)
    current = SimpleNamespace(site_id="123")
    sequence = []
    async def prepare():
        assert site.snapshot()["effects"] == []
        sequence.append("durable intent")
    async def send():
        assert site.snapshot()["effects"][0]["status"] == "submitted"
        sequence.append("send")
    async with site.operation(identity=lambda: current.site_id):
        async with site.operation(restoring=True):
            await site.run_write("restore", send, prepare=prepare)
        current.site_id = "456"
        with pytest.raises(m.ControlIdentityChanged):
            async with site.operation(restoring=True):
                await site.run_write("wrong restore", send)
    assert sequence == ["durable intent", "send"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["monitoring", "stopping"])
async def test_restore_replaces_apply_policy_but_keeps_runtime_and_identity_fences(reason):
    site = m.PowerwallSiteControl(123)
    state = SimpleNamespace(runtime_current=True, site_id="123", monitoring=False, stopping=False)
    def may_apply():
        return not state.monitoring and not state.stopping
    def may_restore():
        # Explicit supervised cleanup is allowed under either condition.
        return True
    async with site.operation(label="force", target=m.NativeMaxTarget("charge"),
                              is_current=lambda: state.runtime_current,
                              identity=lambda: state.site_id, apply_guard=may_apply):
        setattr(state, reason, True)
        with pytest.raises(m.ControlRequestStale):
            async with site.write_context("continued forcing"):
                pytest.fail("Revoked apply policy was ignored")
        async with site.operation(label="cleanup", restoring=True, apply_guard=may_restore):
            async with site.write_context("restore despite policy transition"):
                pass
            state.runtime_current = False
            with pytest.raises(m.ControlRequestStale):
                async with site.write_context("stale cleanup"):
                    pytest.fail("Restoration lost the parent's runtime fence")
            state.runtime_current = True
            state.site_id = "456"
            with pytest.raises(m.ControlIdentityChanged):
                async with site.write_context("wrong site cleanup"):
                    pytest.fail("Restoration lost the parent's identity fence")
            state.site_id = "123"
        with pytest.raises(m.ControlRequestStale):
            async with site.write_context("outer forcing still denied"):
                pytest.fail("Cleanup renewed parent forcing permission")
        with pytest.raises(m.ControlRequestStale):
            async with site.operation(restoring=True, apply_guard=lambda: False):
                async with site.write_context("denied restore"):
                    pytest.fail("Restoration's own policy was ignored")
    assert [effect["label"] for effect in site.snapshot()["effects"]] == ["restore despite policy transition"]
