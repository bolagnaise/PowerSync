"""Bounded reset evidence for a stock ESPHome Tesla BLE producer.

This module deliberately knows nothing about Home Assistant stores, vehicle
ownership, or Tesla commands.  The recovery journal owns those concerns and
injects the lifecycle and observation callbacks here.  Keeping the reset
transaction this small makes it difficult to mistake a reconnect or a cached
uptime value for a producer restart.
"""
from __future__ import annotations

import inspect
import math
import time
from typing import Any, Awaitable, Callable, Mapping


class StockResetError(RuntimeError):
    """The reset transaction could not produce qualifying evidence."""


class StockResetUnsupported(StockResetError):
    """The supplied producer profile cannot be qualified safely."""


Callback = Callable[[], Any | Awaitable[Any]]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


async def _call(callback: Callback, *, name: str) -> Any:
    if not callable(callback):
        raise StockResetUnsupported(f"missing {name} callback")
    result = callback()
    if inspect.isawaitable(result):
        result = await result
    return result


def _identity_matches(expected: Mapping[str, Any], actual: Any) -> bool:
    if not isinstance(actual, Mapping):
        return False
    # The caller supplies the exact registry/config/device/VIN binding.  Extra
    # diagnostics from a fresh API session are allowed, but every expected
    # binding field must be present and unchanged.
    fields = tuple(
        field
        for field in (
            "entry_id",
            "config_entry_id",
            "device_id",
            "unique_id",
            "platform",
            "vin",
            "api_generation",
        )
        if field in expected
    )
    required = ("config_entry_id", "device_id", "unique_id", "platform", "vin")
    if any(not isinstance(expected.get(field), str) or not expected[field].strip()
           for field in required):
        return False
    if not fields:
        return False
    return all(actual.get(field) == expected.get(field) for field in fields)


def _uptime_sample(value: Any) -> tuple[float, float | None, str | None, bool]:
    if isinstance(value, Mapping):
        uptime = value.get("uptime", value.get("seconds", value.get("value")))
        stamp = value.get("observed_at", value.get("source_timestamp"))
        boot_id = value.get("boot_id")
        fresh = value.get("fresh") is True
    else:
        uptime, stamp, boot_id, fresh = value, None, None, False
    uptime_number = _number(uptime)
    if uptime_number is None or uptime_number < 0:
        raise StockResetUnsupported("uptime is not a finite non-negative number")
    stamp_number = _number(stamp)
    if stamp is not None and stamp_number is None:
        raise StockResetUnsupported("uptime timestamp is invalid")
    if boot_id is not None and not isinstance(boot_id, (str, int)):
        raise StockResetUnsupported("uptime boot id is invalid")
    return uptime_number, stamp_number, str(boot_id) if boot_id is not None else None, fresh


def _fresh(sample: tuple[float, float | None, str | None, bool], started: float) -> bool:
    # A source timestamp is preferred.  ``fresh=True`` is accepted only from
    # the injected provider observer, which is the component boundary that can
    # distinguish a new API sample from HA's cached entity state.
    return (sample[1] is not None and sample[1] >= started) or sample[3]


async def async_execute_stock_reset(
    *,
    profile: Mapping[str, Any],
    identity: Mapping[str, Any],
    restart: Callback,
    wait_disconnected: Callback,
    wait_connected: Callback,
    read_identity: Callback,
    read_uptime: Callback,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Perform one qualified stock-producer restart transaction.

    The callbacks are intentionally injected so the recovery journal can
    enforce host quiescence and identity/generation checks around this pure
    producer operation.  ``read_uptime`` must return a mapping containing a
    numeric uptime and either a fresh source timestamp or ``fresh=True``.
    """
    if not isinstance(profile, Mapping):
        raise StockResetUnsupported("missing stock profile")
    if profile.get("supported") is False:
        raise StockResetUnsupported("stock profile is unsupported")
    queue_profile = profile.get("queue_persistence", profile.get("queue"))
    host_profile = profile.get("host_replay", profile.get("host_submission"))
    if queue_profile not in {"ram_only", "volatile"}:
        raise StockResetUnsupported("mutation queue persistence is unqualified")
    if host_profile not in {"none", "not_replayed", "no_replay"}:
        raise StockResetUnsupported("host replay behavior is unqualified")
    qualification = profile.get("qualification")
    runtime_exercise = qualification == "source_and_runtime_exercise"
    if profile.get("exercised") is not True and not runtime_exercise:
        raise StockResetUnsupported("stock behavior has not been exercised")
    if not isinstance(identity, Mapping) or not identity:
        raise StockResetUnsupported("missing exact producer identity")
    if not _identity_matches(identity, identity):
        raise StockResetUnsupported("incomplete exact producer identity")
    if "max_uptime_after_reset" in profile:
        maximum = _number(profile.get("max_uptime_after_reset"))
        if maximum is None or maximum < 0:
            raise StockResetUnsupported("post-reset uptime bound is invalid")
    if "advance_wait" in profile:
        delay = _number(profile.get("advance_wait"))
        if delay is None or delay < 0:
            raise StockResetUnsupported("uptime advance wait is invalid")
    for callback, name in (
        (restart, "restart"),
        (wait_disconnected, "disconnect observation"),
        (wait_connected, "reconnect observation"),
        (read_identity, "identity observation"),
        (read_uptime, "uptime observation"),
    ):
        if not callable(callback):
            raise StockResetUnsupported(f"missing {name} callback")

    sleep = sleep or _sleep
    started = float(now())
    before_identity = await _call(read_identity, name="identity observation")
    if not _identity_matches(identity, before_identity):
        raise StockResetUnsupported("producer identity changed before restart")
    before_raw = await _call(read_uptime, name="uptime observation")
    before = _uptime_sample(before_raw)
    if not _fresh(before, started):
        raise StockResetUnsupported("uptime sample is not fresh before restart")

    result = await _call(restart, name="restart")
    if result is False:
        raise StockResetError("restart request was refused")
    disconnected_at = float(now())
    disconnected = await _call(wait_disconnected, name="disconnect observation")
    if disconnected is False:
        raise StockResetError("producer did not become unavailable")
    connected = await _call(wait_connected, name="reconnect observation")
    if connected is False:
        raise StockResetError("producer did not reconnect")

    after_identity = await _call(read_identity, name="identity observation")
    if not _identity_matches(identity, after_identity):
        raise StockResetUnsupported("reconnected producer identity changed")
    after_raw = await _call(read_uptime, name="uptime observation")
    after = _uptime_sample(after_raw)
    if not _fresh(after, disconnected_at):
        raise StockResetUnsupported("uptime sample is not fresh after reconnect")
    # A restart must move the uptime backwards.  A changed boot id alone is
    # not enough: an old/cached uptime value can carry an arbitrary boot id.
    if after[0] >= before[0]:
        raise StockResetUnsupported("uptime did not reset")
    maximum = _number(profile.get("max_uptime_after_reset"))
    if maximum is not None and after[0] > maximum:
        raise StockResetUnsupported("post-reset uptime is outside profile")

    await sleep(max(0.0, _number(profile.get("advance_wait", 0.0)) or 0.0))
    advancing_raw = await _call(read_uptime, name="uptime observation")
    advancing = _uptime_sample(advancing_raw)
    if not _fresh(advancing, disconnected_at) or advancing[0] <= after[0]:
        raise StockResetUnsupported("uptime did not advance after reset")
    final_identity = await _call(read_identity, name="identity observation")
    if not _identity_matches(identity, final_identity):
        raise StockResetUnsupported("producer identity changed after reset")

    return {
        "profile": dict(profile),
        "qualification": qualification or "prequalified_profile",
        "exercised": True,
        "identity": dict(identity),
        "identity_before": dict(before_identity),
        "identity_after": dict(after_identity),
        "identity_final": dict(final_identity),
        "uptime_before": before[0],
        "uptime_after": after[0],
        "uptime_advancing": advancing[0],
        "uptime_before_observed_at": before[1],
        "uptime_after_observed_at": after[1],
        "uptime_advancing_observed_at": advancing[1],
        "boot_id_before": before[2],
        "boot_id_after": after[2],
        "restarted_at": started,
        "disconnected_at": disconnected_at,
        "qualified_at": float(now()),
    }


async def _sleep(delay: float) -> None:
    import asyncio

    await asyncio.sleep(delay)


__all__ = [
    "StockResetError",
    "StockResetUnsupported",
    "async_execute_stock_reset",
]
