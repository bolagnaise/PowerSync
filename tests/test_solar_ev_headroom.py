"""Discovery permission, restoration, shared budget and load-trial regressions."""
from __future__ import annotations

import asyncio
import ast
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


PATH = Path(__file__).parents[1] / "custom_components/power_sync/solar_ev_headroom.py"
spec = importlib.util.spec_from_file_location("solar_ev_headroom_test", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SolarEVHeadroom = module.SolarEVHeadroom


class Site:
    def __init__(self, saved=None, identity=None):
        self.seconds = 0
        self.base = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.saved = saved
        self.calls = []
        self.timers = []
        self.valid = True
        self.active = True
        self.persist_ok = True
        self.close_ok = True
        self.abort_ok = True
        self.identity = identity or ["sigenergy", "site", 502, 1]
        self.state = {"active": True, "params": {"dynamic_mode": "solar_surplus"}}
        self.candidate = ("EV-A", self.state, module.candidate_token(self.state))
        self.runtime = self.make_runtime()

    def utcnow(self):
        return self.base + timedelta(seconds=self.seconds)

    def status(self, **overrides):
        return {"battery_soc": 100, "solar_power": 1200, "grid_power": 0,
                "battery_power": 0, "native_zero_export": True,
                "site_observed_at": self.utcnow(), **overrides}

    async def persist(self, record):
        self.calls.append("persist-pending" if record["pending"] else "persist-closed")
        if not self.persist_ok:
            raise OSError("disk unavailable")
        self.saved = dict(record)

    async def eligible(self, candidate):
        return self.valid and candidate is self.candidate

    async def reconcile(self, *, opening):
        self.calls.append("release" if opening else "reapply-and-readback")
        if opening:
            assert self.saved["pending"] is True
            assert self.runtime.permits()
            assert self.timers
        return opening or self.close_ok

    async def abort(self, lease):
        self.calls.append("trial-cleanup")
        return self.abort_ok

    def schedule(self, seconds, callback):
        timer = {"seconds": seconds, "callback": callback, "cancelled": False}
        self.timers.append(timer)
        return lambda: timer.update(cancelled=True)

    def make_runtime(self):
        return SolarEVHeadroom(
            identity=self.identity, saved=self.saved, persist=self.persist,
            eligible=self.eligible, reconcile=self.reconcile, abort=self.abort,
            schedule=self.schedule, current=lambda: self.active,
            clock=lambda: self.seconds, utcnow=self.utcnow,
        )


def test_unknown_soc_does_not_itself_grant_a_standing_export_exception():
    site = Site()
    assert not site.runtime.permits()
    assert site.runtime.candidate is None


def test_discovery_is_persisted_armed_and_expires_without_price_polling():
    site = Site()

    async def scenario():
        assert await site.runtime.request(site.candidate, site.status(), sustained_seconds=120)
        deadline = site.runtime.lease["deadline"]
        assert deadline == 270  # Preserve start delay plus command/site sampling.
        site.seconds = 60
        assert not await site.runtime.request(site.candidate, site.status(), sustained_seconds=120)
        assert site.runtime.lease["deadline"] == deadline
        site.seconds = deadline
        assert not site.runtime.permits()
        await site.timers[0]["callback"]()
        assert site.runtime.phase == "cooldown"
        assert site.saved["pending"] is False
        assert not await site.runtime.request(site.candidate, site.status())

    asyncio.run(scenario())
    assert site.calls[:2] == ["persist-pending", "release"]
    assert site.calls[-2:] == ["reapply-and-readback", "persist-closed"]
    assert "trial-cleanup" not in site.calls  # No EV command was submitted.


@pytest.mark.parametrize("bad", [
    {"battery_soc": float("nan")}, {"grid_power": None},
    {"battery_power": 1000}, {"grid_power": -1150},
    {"native_zero_export": False}, {"ac_production_curtailed": True},
    {"site_observed_at": None},
])
def test_discovery_does_not_release_on_invalid_or_incompatible_evidence(bad):
    site = Site()
    assert not asyncio.run(site.runtime.request(site.candidate, site.status(**bad)))
    assert "release" not in site.calls


def test_persistence_failure_cannot_open_export():
    site = Site()
    site.persist_ok = False
    assert not asyncio.run(site.runtime.request(site.candidate, site.status()))
    assert "release" not in site.calls
    assert not site.runtime.permits()


def test_periodic_recovery_preserves_an_active_finite_discovery():
    site = Site()

    async def scenario():
        assert await site.runtime.request(site.candidate, site.status())
        site.seconds = 30
        await site.runtime.recover()
        assert site.runtime.permits()
        assert site.calls == ["persist-pending", "release"]
        await site.runtime.expire()
        assert not site.saved["pending"]

    asyncio.run(scenario())


def test_expiry_cleanup_cannot_invert_the_ev_command_lock():
    site = Site()

    async def scenario():
        command_lock = asyncio.Lock()
        abort_waiting = asyncio.Event()

        async def abort(_lease):
            abort_waiting.set()
            async with command_lock:
                site.calls.append("ordered-stop")
                return True

        site.runtime.abort = abort
        assert await site.runtime.request(site.candidate, site.status())
        assert await site.runtime.command_started(site.candidate)
        async with command_lock:  # The real EV update's outer ownership guard.
            expiry = asyncio.create_task(site.runtime.expire())
            await abort_waiting.wait()
            assert not site.runtime.permits()
            assert site.calls[-1] == "reapply-and-readback"
            assert site.saved["pending"] is True  # EV cleanup still owns work.
            # A new EV tick must be able to unwind and release its command lock
            # while the timer is waiting to submit the compensating stop.
            await asyncio.wait_for(site.runtime.observe(site.candidate, site.status(),
                power_kw=0, power_available=True, safe_amps=0), timeout=1)
            assert not await site.runtime.command_started(site.candidate)
            assert not await site.runtime.request(site.candidate, site.status())
        await asyncio.wait_for(expiry, timeout=1)
        assert site.calls[-3:] == ["reapply-and-readback", "ordered-stop", "persist-closed"]
        assert not site.saved["pending"]

    asyncio.run(scenario())


@pytest.mark.parametrize("sample_age,aware,limit,expected", [
    (0, True, 0, True), (1, True, 0, False),
    (0, False, 0, False), (0, True, 12.6, False),
])
def test_native_closure_requires_fresh_register_readback(sample_age, aware, limit, expected):
    """Exercise the real setup callback, not the generic runtime fake."""
    site = Site()
    source = (PATH.parent / "__init__.py").read_text()
    tree = ast.parse(source)
    callback = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_headroom_reconcile")
    live_spec = importlib.util.spec_from_file_location("headroom_live_status", PATH.parent / "automations/live_status.py")
    live = importlib.util.module_from_spec(live_spec)
    live_spec.loader.exec_module(live)
    observed_at = site.utcnow() - timedelta(seconds=sample_age)
    if not aware:
        observed_at = observed_at.replace(tzinfo=None)
    calls = []

    async def curtail(_):
        calls.append("reconcile-price")

    async def refresh():
        calls.append("fresh-read")

    scope = {
        "_headroom_current": lambda: True,
        "handle_solar_curtailment_check": curtail,
        "is_sigenergy": True,
        "_headroom_entry": {"sigenergy_curtailment_state": "curtailed"},
        "dt_util": SimpleNamespace(utcnow=site.utcnow), "datetime": datetime,
        "_headroom_coord": SimpleNamespace(async_refresh=refresh, last_update_success=True,
            data={"last_update": observed_at, "export_limit_kw": limit}),
        "_native_zero_export": live._native_zero_export,
        "get_current_prices_for_curtailment": lambda *_: (1, 20, "test"),
        "export_earnings_are_uneconomic": lambda *_: True,
        "entry": object(),
        **dict.fromkeys(("amber_coordinator", "localvolts_coordinator", "aemo_sensor_coordinator",
                        "flow_power_kwatch_coordinator", "octopus_coordinator")),
    }
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(PATH.parent / "__init__.py"), "exec"), scope)
    assert asyncio.run(scope["_headroom_reconcile"](opening=False)) is expected
    assert calls == ["reconcile-price", "fresh-read"]


def test_revoked_candidate_during_persistence_cannot_open_export():
    site = Site()
    persist = site.persist

    async def replace(record):
        await persist(record)
        site.valid = False

    site.runtime.persist = replace
    assert not asyncio.run(site.runtime.request(site.candidate, site.status()))
    assert "release" not in site.calls


def test_fresh_accepted_load_closes_and_out_of_order_samples_do_not_count():
    site = Site()

    async def scenario():
        assert await site.runtime.request(site.candidate, site.status())
        assert await site.runtime.command_started(site.candidate, target_power_kw=5)
        site.seconds = 10
        await site.runtime.observe(site.candidate, site.status(), power_kw=3.0, power_available=True, safe_amps=12)
        site.seconds = 20
        await site.runtime.observe(site.candidate, site.status(site_observed_at=site.base + timedelta(seconds=5)),
                                   power_kw=3.0, power_available=True, safe_amps=12)
        assert site.runtime.permits()
        await site.runtime.observe(site.candidate, site.status(), power_kw=3.0, power_available=True, safe_amps=12)
        assert site.runtime.phase == "cooldown"
        assert "trial-cleanup" not in site.calls  # Accepted draw is retained.

    asyncio.run(scenario())


def test_failed_closure_survives_restart_without_resuming_permission():
    site = Site()

    async def scenario():
        assert await site.runtime.request(site.candidate, site.status())
        site.close_ok = False
        await site.runtime.close()
        assert site.saved["pending"]
        restarted = site.make_runtime()
        assert not restarted.permits()
        await restarted.recover()
        assert site.saved["pending"]
        site.close_ok = True
        await restarted.recover()
        assert site.saved["pending"] is False

    asyncio.run(scenario())


def test_unconfirmed_trial_cleanup_remains_pending_after_native_closure():
    site = Site()

    async def scenario():
        assert await site.runtime.request(site.candidate, site.status())
        assert await site.runtime.command_started(site.candidate)
        site.abort_ok = False
        await site.runtime.close()
        assert "reapply-and-readback" in site.calls
        assert site.saved["pending"] and site.saved["ev_abort_pending"]
        assert site.runtime.phase == "closing"
        site.abort_ok = True
        await site.runtime.recover()
        assert not site.saved["pending"]

    asyncio.run(scenario())


def test_old_timer_cannot_revoke_a_later_lease_and_budget_is_shared_across_evs():
    site = Site()

    async def scenario():
        assert await site.runtime.request(site.candidate, site.status())
        old_timer = site.timers[0]["callback"]
        await site.runtime.close()
        second = ("EV-B", {"active": True}, ())
        site.candidate = second
        assert not await site.runtime.request(second, site.status())
        site.seconds = 1081
        assert await site.runtime.request(second, site.status())
        await old_timer()
        assert site.runtime.permits()

    asyncio.run(scenario())


def test_source_change_keeps_retained_pending_mutation():
    site = Site()

    async def scenario():
        assert await site.runtime.request(site.candidate, site.status())
        site.identity = ["sigenergy", "different-site", 502, 1]
        restarted = site.make_runtime()
        before = list(site.calls)
        await restarted.recover()
        assert site.calls == before
        assert site.saved["pending"]

    asyncio.run(scenario())


def test_expiry_revokes_permission_while_release_is_in_flight_and_closes_afterward():
    site = Site()

    async def scenario():
        entered = asyncio.Event()
        finish = asyncio.Event()
        original = site.reconcile

        async def staged(*, opening):
            if opening:
                entered.set()
                await finish.wait()
                return True
            return await original(opening=False)

        site.runtime.reconcile = staged
        task = asyncio.create_task(site.runtime.request(site.candidate, site.status()))
        await entered.wait()
        expiry = asyncio.create_task(site.timers[0]["callback"]())
        await asyncio.sleep(0)
        assert not site.runtime.permits()
        finish.set()
        assert not await task
        await expiry
        assert not site.saved["pending"]
        assert "reapply-and-readback" in site.calls

    asyncio.run(scenario())


def test_load_probe_is_finite_and_unsafe_load_bypasses_five_minute_delay():
    site = Site()
    entry = {}
    site.state["params"]["sustained_surplus_minutes"] = 0
    site.state.update(current_amps=6, charging_started=True)

    def trial(status):
        return module.idle_load_probe(
            entry, "EV-A", site.state, status, eligible=True, min_amps=6,
            power_kw=0, power_available=True, clock=lambda: site.seconds,
            utcnow=site.utcnow, schedule=lambda record, seconds: lambda: None,
        )

    assert trial(site.status()) == 6
    assert entry["solar_ev_load_probe"]["deadline"] == 90
    site.seconds = 10
    assert trial(site.status(battery_power=1000)) == 0
    assert entry["solar_ev_load_probe"]["phase"] == "expired"
