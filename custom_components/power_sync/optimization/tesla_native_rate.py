"""Current-only control inside a Tesla-owned home charging window.

The vehicle owns start/stop and its schedule/charge limit. This controller never
issues those commands. Its journal is a cleanup obligation, never restart
permission to replay a charging target. Hardware access lives behind ports so
planning, acknowledgement and readback can be tested independently.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import math
import time
from typing import Any
from uuid import uuid4

OWNER_MODE = "native_schedule_rate"
COMMAND_INTERVAL = 60
MAX_RESTORE_ATTEMPTS = 3
SETTLEMENT_MAX_GAP = 90  # Individually fresh reports at the normal 30 s tick.


@dataclass(frozen=True)
class RateWriteResult:
    submitted: bool
    confirmed: bool
    reason: str = "waiting_for_site_headroom"

    def __bool__(self):
        return self.confirmed


@dataclass(frozen=True)
class RateObservation:
    vin: str
    entity_id: str
    identity: str
    current_amps: int
    minimum_amps: int
    maximum_amps: int
    watts_per_amp: float
    charging: bool
    at_home: bool
    vehicle_at: float
    meter_at: float
    window_id: str | None
    window_start: float | None
    window_end: float | None
    schedule_at: float | None
    remaining_grid_wh: float
    non_ev_net_w: float
    planned_battery_w: float
    import_limit_w: float | None
    enabled: bool
    borrowable_battery_w: float = 0.0
    measured_ev_w: float = 0.0
    measured_ev_at: float = 0.0
    confirmed_stopped: bool = False
    measured_ev_amps: float | None = None


def decide_rate(observed: RateObservation, baseline_amps: int, now: float) -> tuple[int | None, str]:
    """Choose positive current only; no forecast can authorize vehicle start."""
    if not observed.enabled:
        return None, "disabled"
    if not observed.at_home or not observed.charging:
        return None, "not_charging_at_home"
    if not 0 <= now-observed.vehicle_at <= 90 or not 0 <= now-observed.meter_at <= 15:
        return None, "telemetry_unavailable"
    if (not observed.window_id or observed.window_start is None or observed.window_end is None
            or not observed.window_start <= now < observed.window_end
            or observed.schedule_at is None or not 0 <= now-observed.schedule_at <= 900):
        return None, "native_window_unavailable"
    values = (observed.watts_per_amp, observed.remaining_grid_wh, observed.non_ev_net_w,
              observed.planned_battery_w, observed.borrowable_battery_w)
    if not all(math.isfinite(value) for value in values) or observed.watts_per_amp <= 0:
        return None, "invalid_telemetry"
    if (observed.import_limit_w is None or not math.isfinite(observed.import_limit_w)
            or observed.import_limit_w < 0):
        return None, "import_limit_unavailable"
    minimum = observed.minimum_amps
    maximum = min(baseline_amps, observed.maximum_amps)
    if not 1 <= minimum <= maximum <= 80:
        return None, "current_range_unavailable"
    if observed.remaining_grid_wh <= 0:
        return None, "target_reached"
    hours = (observed.window_end-now)/3600
    needed = math.ceil(observed.remaining_grid_wh/hours/observed.watts_per_amp)
    headroom = (observed.import_limit_w-observed.non_ev_net_w
                - max(0.0,observed.planned_battery_w-max(0.0,observed.borrowable_battery_w)))
    available = math.floor(headroom/observed.watts_per_amp)
    if available < minimum:
        return None, "minimum_current_does_not_fit"
    target = min(maximum,available,max(minimum,needed))
    return target, "limited" if target < needed else "ready"


def restoration_fits(observed: RateObservation, amps: int, now: float) -> bool:
    """Returning a saved current must not introduce an import-limit breach."""
    if amps <= observed.current_amps:
        return True
    values = (observed.watts_per_amp, observed.non_ev_net_w, observed.planned_battery_w)
    if (not all(math.isfinite(value) for value in values) or observed.watts_per_amp <= 0
            or observed.import_limit_w is None or not math.isfinite(observed.import_limit_w)
            or not 0 <= now-observed.meter_at <= 15):
        return False
    headroom = observed.import_limit_w-observed.non_ev_net_w-max(0,observed.planned_battery_w)
    return amps*observed.watts_per_amp <= headroom


def governed_forecast(base, observed, baseline, timestamps, interval_minutes, now):
    """One fixed load for the eligible active window, using the rate decision."""
    amps, _ = decide_rate(observed, baseline, now)
    if amps is None or interval_minutes <= 0:
        return base
    step = interval_minutes*60
    remaining_wh = observed.remaining_grid_wh
    values = []
    for timestamp, original_w in zip(timestamps, base.power_w):
        start = timestamp.timestamp()
        overlap = max(0.0, min(start+step, observed.window_end)-max(start, now, observed.window_start))
        in_active = start < observed.window_end and start+step > max(now, observed.window_start)
        power = amps*observed.watts_per_amp*overlap/step if in_active else original_w
        # The replacement is the same EV load, capped to the same remaining
        # energy. It never creates a second plan or a start/stop command.
        power = max(0.0, min(power, remaining_wh*3600/step))
        values.append(power)
        remaining_wh = max(0.0, remaining_wh-power*step/3600)
    return replace(base, power_w=tuple(values))


class NativeRateController:
    """One entry's serialized current leases, with durable bounded cleanup.

    Ports: read(vin), permitted(vin,session,restore), claim(vin,session),
    release(vin,session), write(observation,amps,authorize), enabled_vins().
    write must recheck authorize at the final provider write and return True
    only after fresh current readback, never merely after command submission.
    """
    def __init__(self, store: Any, ports: Any, *, clock=time.time):
        self.store, self.ports, self.clock = store, ports, clock
        self.sessions: dict[str,dict] = {}
        self.suppressed: dict[str,str] = {}
        self.status: dict[str,dict] = {}
        self.loaded = False
        self.lock = asyncio.Lock()
        self.stopping = False
        self.inflight: dict[str,asyncio.Task] = {}

    async def write(self, vin, observed, amps, authorize, session):
        task = asyncio.create_task(self.ports.write(observed, amps, authorize))
        self.inflight[vin] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The provider may already have accepted the request. Retain that
            # task and journal; no replacement or restore can overtake it.
            session["recovering"] = True
            try:
                await asyncio.shield(self.save())
            except Exception:
                pass
            raise
        finally:
            if task.done():
                self.inflight.pop(vin,None)

    async def load(self):
        if self.loaded:
            return
        data = await self.store.async_load()
        raw = data.get("sessions",{}) if isinstance(data,dict) else {}
        raw = raw if isinstance(raw,dict) else {}
        self.sessions = {vin:{**value,"recovering":True} for vin,value in raw.items()
                         if isinstance(vin,str) and isinstance(value,dict)
                         and isinstance(value.get("session"),str)
                         and isinstance(value.get("entity_id"),str)
                         and isinstance(value.get("identity"),str)
                         and type(value.get("baseline_amps")) is int
                         and 1 <= value["baseline_amps"] <= 80}
        suppressed = data.get("suppressed",{}) if isinstance(data,dict) else {}
        self.suppressed = {vin:window for vin,window in suppressed.items()
                           if isinstance(vin,str) and isinstance(window,str)} if isinstance(suppressed,dict) else {}
        self.loaded = True

    async def save(self):
        await self.store.async_save({"sessions":self.sessions,"suppressed":self.suppressed})
        getattr(self.ports, "changed", lambda: None)()

    def same_setting(self, observation, session):
        return observation.entity_id == session["entity_id"] and observation.identity == session["identity"]

    async def finish(self, vin, session, reason):
        updated=dict(self.sessions);updated.pop(vin,None)
        suppressed=dict(self.suppressed)
        # A manual takeover or cleanup ends authority for this window. The
        # following tick must not immediately acquire a new current lease.
        if isinstance(session.get("window_id"),str):
            suppressed[vin]=session["window_id"]
        await self.store.async_save({"sessions":updated,"suppressed":suppressed})
        self.sessions=updated
        self.suppressed=suppressed
        self.ports.release(vin,session["session"])
        self.status[vin]={"state":reason}
        getattr(self.ports, "changed", lambda: None)()

    async def settle_restoration(self, vin, observed, session):
        """A restored setpoint is not proof that its prospective draw landed.

        Keep the durable baseline reservation through ramp-up and restart. Two
        fresh aligned physical reports must settle before battery renewals can
        reuse this headroom. This state never submits another current command.
        """
        now = self.clock()
        if not session.get("restore_settling"):
            session.update(restore_settling=True, restore_after=now,
                           recovering=True, uncertain=False)
            await self.save()
        self.status[vin]["reason"] = "waiting_for_physical_restoration"
        stamps = (observed.vehicle_at, observed.measured_ev_at, observed.meter_at)
        target_w = session["baseline_amps"] * observed.watts_per_amp
        physical = observed.measured_ev_w
        fresh = (observed.current_amps == session["baseline_amps"]
                 and all(math.isfinite(at) and 0 <= now-at <= 15
                     and at > session["restore_after"] for at in stamps)
                 and max(stamps)-min(stamps) <= 5)
        settled = (math.isfinite(physical) and math.isfinite(target_w)
                   and ((observed.charging and observed.watts_per_amp > 0 and target_w > 0
                         and abs(physical-target_w) <= max(250, target_w*0.05))
                        or (observed.confirmed_stopped and physical <= 250
                            and (observed.watts_per_amp > 0 or observed.measured_ev_amps == 0))))
        if not fresh or not settled:
            if session.pop("restore_sample", None) is not None:
                await self.save()
            return
        first = session.get("restore_sample")
        if (not isinstance(first, list) or len(first) != 3
                or now-min(first) > SETTLEMENT_MAX_GAP):
            session["restore_sample"] = list(stamps)
            await self.save()
            return
        if all(at > before for at, before in zip(stamps, first)) and min(stamps)-min(first) >= 3:
            await self.finish(vin, session, "restored")

    async def restore(self, vin, observed, session):
        self.status[vin]={"state":"restore_pending","baseline_amps":session["baseline_amps"]}
        if observed is None or not self.same_setting(observed,session):
            return
        now=self.clock()
        if not 0 <= now-observed.vehicle_at <= 90:
            return
        if not self.ports.permitted(vin,session["session"],True):
            # A newer owner is authoritative. Do not restore its current.
            if self.ports.superseded(vin,session["session"]):
                await self.finish(vin,session,"superseded")
            return
        attempted=session.get("attempted_amps")
        if observed.current_amps not in (session["baseline_amps"],attempted,session.get("confirmed_amps")):
            await self.finish(vin,session,"superseded")
            return
        if session.get("restore_settling"):
            await self.settle_restoration(vin, observed, session)
            return
        if observed.current_amps == session["baseline_amps"] and not session.get("uncertain"):
            await self.settle_restoration(vin, observed, session)
            return
        if session.get("restore_attempts",0) >= MAX_RESTORE_ATTEMPTS:
            return
        baseline=session["baseline_amps"]
        if not observed.minimum_amps <= baseline <= observed.maximum_amps:
            return
        if (not restoration_fits(observed,baseline,now)
                and not getattr(self.ports, "admits_increases", False)):
            self.status[vin]["reason"]="import_headroom_unavailable"
            return
        session["restore_attempts"]=session.get("restore_attempts",0)+1
        session["recovering"]=True
        await self.save()
        authorize=lambda:self.ports.permitted(vin,session["session"],True)
        if not authorize():
            return
        try:
            confirmed=await self.write(vin,observed,baseline,authorize,session)
        except Exception:
            confirmed=False
        if isinstance(confirmed, RateWriteResult) and not confirmed.submitted:
            session["restore_attempts"] -= 1
            await self.save()
            self.status[vin]["reason"] = confirmed.reason
            return
        confirmed = confirmed.confirmed if isinstance(confirmed, RateWriteResult) else confirmed
        if confirmed is True and authorize():
            await self.settle_restoration(vin, observed, session)

    async def tick(self):
        async with self.lock:
            await self.load()
            vins=set(self.sessions)|set(self.ports.enabled_vins())
            for vin in sorted(vins):
                try:
                    await self.step(vin)
                except asyncio.CancelledError:
                    if session := self.sessions.get(vin):
                        session["recovering"] = True
                        await asyncio.shield(self.save())
                    raise
                except Exception:
                    # Storage/read failures never authorize the next write.
                    self.status[vin]={"state":"unavailable"}
                    if session := self.sessions.get(vin):
                        session["recovering"]=True
                        try:
                            await self.save()
                        except Exception:
                            pass

    async def step(self, vin):
        if task := self.inflight.get(vin):
            if not task.done():
                self.status[vin]={"state":"restore_pending","reason":"command_in_flight"}
                return
            self.inflight.pop(vin,None)
            try:
                task.result()
            except BaseException:
                pass
        waiting = getattr(self.ports, "waiting_reason", lambda: None)()
        if waiting:
            self.status[vin] = {"state": "restore_pending" if vin in self.sessions and
                (self.stopping or self.sessions[vin].get("recovering")) else "waiting_for_site_headroom",
                "reason": waiting}
            return
        observed=await self.ports.read(vin)
        session=self.sessions.get(vin)
        if session is not None:
            if observed is not None and self.same_setting(observed,session):
                confirmed = session.get("confirmed_amps")
                active = not self.stopping and not session.get("recovering")
                if ((active and confirmed is not None and observed.current_amps != confirmed)
                        or observed.current_amps not in (session["baseline_amps"],session.get("attempted_amps"),confirmed)):
                    await self.finish(vin,session,"superseded")
                    return
            if self.stopping or session.get("recovering"):
                await self.restore(vin,observed,session)
                return
        if observed is None:
            self.status[vin]={"state":"telemetry_unavailable"}
            if session:
                session["recovering"]=True
                await self.save()
            return
        if session is None and observed.window_id == self.suppressed.get(vin):
            self.status[vin]={"state":"window_released"}
            return
        baseline=session["baseline_amps"] if session else observed.current_amps
        target,reason=decide_rate(observed,baseline,self.clock())
        if session and (not self.same_setting(observed,session) or observed.window_id != session["window_id"]):
            session["recovering"]=True
            await self.save()
            await self.restore(vin,observed,session)
            return
        if target is None:
            self.status[vin]={"state":reason}
            if session:
                # Do not increase current to the saved baseline merely because
                # available headroom fell. Keep the owned reduction pending.
                if reason == "minimum_current_does_not_fit":
                    return
                session["recovering"]=True
                await self.save()
                await self.restore(vin,observed,session)
            return
        if session is None:
            if self.stopping or target == observed.current_amps:
                self.status[vin]={"state":reason,"target_amps":target}
                return
            identity=uuid4().hex
            if not self.ports.permitted(vin,None,False):
                self.status[vin]={"state":"another_controller"}
                return
            session={"session":identity,"entity_id":observed.entity_id,"identity":observed.identity,
                     "baseline_amps":baseline,"attempted_amps":target,"window_id":observed.window_id,
                     "last_command_at":0,"recovering":False,"uncertain":False}
            context = getattr(self.ports, "session_context", None)
            if context is not None:
                session.update(context(observed))
            self.sessions[vin]=session
            try:
                await self.save()  # durable baseline BEFORE ownership/write
            except asyncio.CancelledError:
                session["recovering"] = True
                await asyncio.shield(self.save())
                raise
            except Exception:
                self.sessions.pop(vin,None)
                raise
            if not self.ports.claim(vin,identity):
                await self.finish(vin,session,"another_controller")
                return
        if not self.ports.permitted(vin,session["session"],False):
            session["recovering"]=True
            await self.save()
            await self.restore(vin,observed,session)
            return
        if target == observed.current_amps:
            self.status[vin]={"state":reason,"target_amps":target,"baseline_amps":baseline}
            return
        if self.clock()-session.get("last_command_at",0) < COMMAND_INTERVAL:
            return
        session.update(attempted_amps=target,last_command_at=self.clock(),uncertain=True)
        await self.save()
        authorize=lambda:self.ports.permitted(vin,session["session"],False)
        if not authorize():
            return
        try:
            confirmed=await self.write(vin,observed,target,authorize,session)
        except Exception:
            confirmed=False
        if isinstance(confirmed, RateWriteResult) and not confirmed.submitted:
            session["uncertain"] = False
            session["attempted_amps"] = session.get("confirmed_amps", baseline)
            await self.save()
            self.status[vin] = {"state": confirmed.reason, "baseline_amps": baseline}
            return
        confirmed = confirmed.confirmed if isinstance(confirmed, RateWriteResult) else confirmed
        if confirmed is not True:
            session["recovering"]=True
            await self.save()
            self.status[vin]={"state":"restore_pending","baseline_amps":baseline}
            return
        session["uncertain"]=False
        session["confirmed_amps"]=target
        await self.save()
        self.status[vin]={"state":reason,"target_amps":target,"baseline_amps":baseline}

    async def stop(self):
        self.stopping=True
        getattr(self.ports, "changed", lambda: None)()
        await self.tick()
