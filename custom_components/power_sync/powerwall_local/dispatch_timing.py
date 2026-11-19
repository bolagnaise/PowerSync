"""Opt-in debug timing for a single local Powerwall lease renewal."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar

_LOGGER = logging.getLogger(__name__)
_LOOP_SAMPLE_SECONDS = 0.25
_current_renewal: ContextVar[_RenewalTiming | None] = ContextVar(
    "powerwall_renewal_timing", default=None
)


class _RenewalTiming:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.started = self.loop.time()
        self.measurements = []
        self.max_loop_delay = 0.0
        self._schedule_loop_sample(self.started)

    def _schedule_loop_sample(self, now):
        self._next_sample = now + _LOOP_SAMPLE_SECONDS
        self._loop_sample = self.loop.call_at(self._next_sample, self._sample_loop)

    def _sample_loop(self):
        now = self.loop.time()
        self.max_loop_delay = max(self.max_loop_delay, now - self._next_sample)
        self._schedule_loop_sample(now)

    def finish(self):
        now = self.loop.time()
        self._loop_sample.cancel()
        # A blocked loop may still have an overdue callback when renewal ends.
        self.max_loop_delay = max(self.max_loop_delay, now - self._next_sample)
        _LOGGER.debug(
            "Powerwall renewal timing: total=%.3fs max_loop_delay=%.3fs %s",
            now - self.started,
            self.max_loop_delay,
            " ".join(self.measurements),
        )


@contextmanager
def trace_renewal():
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        yield
        return

    timing = _RenewalTiming()
    token = _current_renewal.set(timing)
    try:
        yield
    finally:
        _current_renewal.reset(token)
        timing.finish()


@contextmanager
def measure_renewal_step(step):
    timing = _current_renewal.get()
    if timing is None:
        yield
        return

    started = timing.loop.time()
    try:
        yield
    finally:
        timing.measurements.append(
            f"{step}={timing.loop.time() - started:.3f}s@{started - timing.started:.3f}s"
        )


async def timed_renewal_read(step, read, *args):
    with measure_renewal_step(step):
        return await read(*args)


def record_telemetry_age(step, age):
    timing = _current_renewal.get()
    if timing is not None:
        timing.measurements.append(f"{step}={age:.3f}s")
