"""Per-cycle solver allowance and cooperative mode supersession."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event

OPTIMIZATION_RUN = ContextVar("power_sync_optimization_run", default=None)
FREE_PREFERENCE_DEADLINE = ContextVar("power_sync_free_preference_deadline", default=None)
COST_NEUTRAL_FREE_PREFERENCE_ATTEMPT_SECONDS = 3.0
COST_NEUTRAL_FREE_PREFERENCE_BUDGET_SECONDS = 12.0


class OptimizerPlanSuperseded(Exception):
    """The requested economic mode changed during a solve."""


@dataclass
class OptimizationRun:
    """Share the optional solver allowance across one coordinator cycle."""

    superseded: Event = field(default_factory=Event)
    preference_elapsed_s: float = 0.0
    preference_attempts: int = 0
    preference_skips: int = 0

    def solve(self, optimize, *args):
        token = OPTIMIZATION_RUN.set(self)
        try:
            self.check_current()
            result = optimize(*args)
            self.check_current()
            return result
        finally:
            OPTIMIZATION_RUN.reset(token)

    def check_current(self):
        if self.superseded.is_set():
            raise OptimizerPlanSuperseded

    @contextmanager
    def free_preference(self, pass_remaining_s):
        self.check_current()
        limit = min(
            COST_NEUTRAL_FREE_PREFERENCE_ATTEMPT_SECONDS,
            COST_NEUTRAL_FREE_PREFERENCE_BUDGET_SECONDS - self.preference_elapsed_s,
            pass_remaining_s,
        )
        if limit <= 0:
            self.preference_skips += 1
            yield 0.0
            return
        started = time.monotonic()
        self.preference_attempts += 1
        token = FREE_PREFERENCE_DEADLINE.set(started + limit)
        try:
            yield limit
        finally:
            self.preference_elapsed_s += time.monotonic() - started
            FREE_PREFERENCE_DEADLINE.reset(token)

    def preference_stats(self):
        return {
            "attempts": self.preference_attempts,
            "skips": self.preference_skips,
            "elapsed_s": round(self.preference_elapsed_s, 4),
            "budget_s": COST_NEUTRAL_FREE_PREFERENCE_BUDGET_SECONDS,
        }
