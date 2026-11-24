"""Generation fences for optimiser work; restoration never requires a paid licence."""
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import asyncio

from .runtime import DATA_KEY

_scope = ContextVar("powersync_optimizer_scope", default=None)
_held_command = ContextVar("powersync_held_command", default=None)
_recovering = ContextVar("powersync_optimizer_recovery", default=False)
_plan_current = ContextVar("powersync_optimizer_plan_current", default=None)


class OptimizerAccessLost(asyncio.CancelledError):
    """Unwind a dispatch before it can publish an action that was not admitted."""


class OptimizerDispatchSuperseded(OptimizerAccessLost):
    """The economic mode changed before the planned command was sent."""


def optimizer_plan_dispatch(function):
    """Keep the mode revision with a dispatched action through awaited checks."""
    @wraps(function)
    async def guarded(self, *args, **kwargs):
        revision = getattr(self, "_economic_mode_revision", 0)
        token = _plan_current.set(
            lambda: getattr(self, "_economic_mode_revision", 0) == revision
        )
        try:
            return await function(self, *args, **kwargs)
        except OptimizerDispatchSuperseded:
            return None
        finally:
            _plan_current.reset(token)
    return guarded


def require_current_optimizer_plan():
    current = _plan_current.get()
    if current is not None and not _recovering.get() and not current():
        raise OptimizerDispatchSuperseded


@contextmanager
def recovery_scope():
    token = _recovering.set(True)
    try:
        yield
    finally:
        _recovering.reset(token)


def manager_for(owner):
    hass = getattr(owner, "hass", None) or getattr(owner, "_hass", None)
    if hass is None:
        coordinator = getattr(owner, "coordinator", None)
        hass = getattr(coordinator, "hass", None) or getattr(coordinator, "_hass", None)
    return getattr(hass, "data", {}).get(DATA_KEY)


def scope_for(owner):
    scope = _scope.get()
    return scope[:2] if scope else (getattr(owner, "entry_id", None), None)


def background_work(function):
    """Long-lived loops start each new decision without inheriting a stale caller scope."""
    @wraps(function)
    async def fresh(self, *args, **kwargs):
        token = _scope.set(None)
        recovery = _recovering.set(False)
        plan = _plan_current.set(None)
        try:
            return await function(self, *args, **kwargs)
        finally:
            _plan_current.reset(plan)
            _recovering.reset(recovery)
            _scope.reset(token)
    return fresh


def optimizer_work(function):
    @wraps(function)
    async def guarded(self, *args, **kwargs):
        manager = manager_for(self)
        if manager is None or _recovering.get():
            return await function(self, *args, **kwargs)
        entry_id, generation = scope_for(self)
        if generation is None:
            generation = manager.admission.generation
        epoch = control_entry_generation()
        if epoch is None:
            epoch = getattr(self, "_licensing_entry_generation", manager.entries.get(entry_id, {}).get("generation", 0))
        if manager.admission.enforced and not entry_id:
            return False
        if not manager.permits(entry_id, generation, epoch):
            return False
        token = _scope.set((entry_id, generation, epoch))
        try:
            result = await function(self, *args, **kwargs)
            if function.__name__ == "_run_optimization" and not manager.permits(entry_id, generation, epoch):
                self._last_optimizer_result = None
                self._current_schedule = None
                raise OptimizerAccessLost("optimizer_plan_superseded")
            return result
        finally:
            _scope.reset(token)
    return guarded


@asynccontextmanager
async def command_scope(owner):
    manager = manager_for(owner)
    entry_id, _ = scope_for(owner)
    marker = (id(manager), entry_id, asyncio.current_task())
    if not manager or _recovering.get() or _held_command.get() == marker:
        require_current_optimizer_plan()
        yield
        return
    async with manager.control_lock(entry_id):
        token = _held_command.set(marker)
        try:
            require_current_optimizer_plan()
            if not manager.permits(entry_id, control_generation(), control_entry_generation()):
                raise OptimizerAccessLost("optimizer_access_required")
            yield
        finally:
            _held_command.reset(token)


def optimizer_command(function):
    @wraps(function)
    @optimizer_work
    async def guarded(self, *args, **kwargs):
        require_current_optimizer_plan()
        if _recovering.get():
            name = function.__name__
            if name.startswith("force_") or name == "_start_charging":
                raise OptimizerAccessLost("recovery_cannot_start_control")
            if name in ("_set_charging_amps", "_set_shared_action_amps"):
                amps = kwargs.get("amps", args[1] if len(args) > 1 else None)
                if amps != 0 or name == "_set_charging_amps":
                    raise OptimizerAccessLost("recovery_cannot_start_control")
        manager = manager_for(self)
        if manager and not _recovering.get() and manager.admission.enforced:
            entry_id, generation = scope_for(self)
            if function.__name__ == "_set_shared_action_amps":
                config = kwargs.get("config", args[0] if args else None)
                amps = kwargs.get("amps", args[1] if len(args) > 1 else 0)
                if amps > 0 and hasattr(manager, "journal_ev"):
                    await manager.journal_ev(entry_id, config)
            await manager.journal(entry_id)
            if not manager.permits(entry_id, generation, control_entry_generation()):
                return False
        async with command_scope(self):
            return await function(self, *args, **kwargs)
    return guarded


def control_generation():
    scope = _scope.get()
    return scope[1] if scope else None


def control_entry_id():
    scope = _scope.get()
    return scope[0] if scope else None


def control_entry_generation():
    scope = _scope.get()
    return scope[2] if scope else None


def optimizer_service_data(owner, data):
    result = dict(data)
    if (generation := control_generation()) is not None:
        result["_licensing_generation"] = generation
    if (epoch := control_entry_generation()) is not None:
        result["_licensing_entry_generation"] = epoch
    if entry_id := getattr(owner, "entry_id", None) or control_entry_id():
        result["entry_id"] = entry_id
    return result


def service_allowed(hass, data, operation=None):
    manager = hass.data.get(DATA_KEY)
    if _recovering.get() and operation in ("handle_force_charge", "handle_force_discharge"):
        return False
    if data.get("source") == "optimizer":
        current = _plan_current.get()
        if current is not None and not _recovering.get() and not current():
            return False
    if not manager or _recovering.get() or data.get("source") != "optimizer":
        return True
    generation = data.get("_licensing_generation", control_generation())
    epoch = data.get("_licensing_entry_generation", control_entry_generation())
    if manager.admission.enforced and (not (data.get("entry_id") or control_entry_id()) or type(generation) is not int or type(epoch) is not int):
        return False
    entry_id = data.get("entry_id") or (_scope.get() or (None, None))[0]
    return manager.permits(entry_id, generation, epoch)


async def fenced_call(owner, factory, operation=None):
    require_current_optimizer_plan()
    manager = manager_for(owner)
    if _recovering.get():
        if operation and operation.startswith("force_"):
            raise OptimizerAccessLost("recovery_cannot_force_battery")
        return await factory()
    if manager:
        entry_id, generation = scope_for(owner)
        if not manager.permits(entry_id, generation, control_entry_generation()):
            raise OptimizerAccessLost("optimizer_access_required")
        if manager.admission.enforced:
            await manager.journal(entry_id)
            if not manager.permits(entry_id, generation, control_entry_generation()):
                raise OptimizerAccessLost("optimizer_access_required")
    async with command_scope(owner):
        result = await factory()
        if manager and not manager.permits(entry_id, generation, control_entry_generation()):
            raise OptimizerAccessLost("optimizer_access_changed_during_command")
        return result


def restoration_work(function):
    @wraps(function)
    async def restore(self, *args, **kwargs):
        with recovery_scope():
            return await function(self, *args, **kwargs)
    return restore
